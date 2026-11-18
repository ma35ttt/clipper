"""Add another streamer to a running console: one worker process per link.

Paste a Twitch or YouTube link into the console and it starts a second, fully
separate ``clipper run`` process for that streamer — its own ingest, rolling
buffer, chat, captions and clip queue. Clips land in ``<clips root>/<name>/``
and the worker serves its own console on its own port, so it can be tuned
independently. The console process keeps clipping its own (primary) stream.

The manager only *starts* workers; it never restarts them behind your back.
If a worker dies (offline channel, bad link) its row says so and can be
retried from the console.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import profile
from .chat import normalize_channel
from .multi import free_port
from .util import safe_name

ROOT = Path(__file__).resolve().parent.parent

TWITCH = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)", re.I)
YOUTUBE_HOST = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/", re.I)
YT_HANDLE = re.compile(r"youtube\.com/@([A-Za-z0-9_.-]+)", re.I)
YT_NAMED = re.compile(r"youtube\.com/(?:c|user)/([A-Za-z0-9_.-]+)", re.I)
YT_CHANNEL = re.compile(r"youtube\.com/channel/([A-Za-z0-9_-]+)", re.I)
YT_LIVE = re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{6,})", re.I)
YT_VIDEO = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{6,})", re.I)
MEDIA = re.compile(r"\.(?:m3u8|mp4|flv|ts)(?:\?|$)", re.I)

# Pages under twitch.tv that are not a channel login.
TWITCH_RESERVED = {
    "videos", "directory", "search", "settings", "subscriptions", "inventory",
    "wallet", "drops", "turbo", "friends", "messages", "downloads", "jobs",
    "store", "clips", "p", "download", "prime", "following",
}

ASK = "ask"
BOTH = "both"
CANCEL = "cancel"


@dataclass(frozen=True)
class Target:
    """A streamer link, resolved just enough to name it and start a worker."""

    input: str
    name: str
    kind: str                 # twitch | youtube | direct
    chat: str | None = None

    def as_dict(self) -> dict:
        return {"input": self.input, "name": self.name, "kind": self.kind,
                "chat": self.chat}


def _clean_name(text: str, fallback: str = "") -> str:
    name = safe_name(text, 24) if (text or "").strip() else ""
    return name or fallback


def derive_target(link: str, name: str = "") -> Target:
    """Turn a pasted link into a worker target. Raises ValueError with a hint."""
    raw = (link or "").strip().strip("'\"")
    if not raw:
        raise ValueError("paste a Twitch or YouTube link first")

    # A local file or a direct HLS URL needs no host parsing.
    if Path(raw).exists() or MEDIA.search(raw):
        fallback = Path(raw.split("?")[0]).stem or "stream"
        if Path(raw).exists() and not raw.lower().startswith(("http://", "https://")):
            fallback = Path(raw).stem
        return Target(input=raw, name=_clean_name(name, safe_name(fallback, 24)),
                      kind="direct")

    twitch = TWITCH.match(raw)
    if twitch:
        login = normalize_channel(twitch.group(1))
        if login in TWITCH_RESERVED:
            raise ValueError(f"twitch.tv/{login} is not a streamer channel")
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
            raise ValueError(f"could not read a channel name from {raw}")
        return Target(input=raw, name=_clean_name(name, login), kind="twitch",
                      chat=f"twitch:{login}")

    if YOUTUBE_HOST.match(raw):
        base = "youtube"
        handle = YT_HANDLE.search(raw)
        named = YT_NAMED.search(raw)
        channel = YT_CHANNEL.search(raw)
        live = YT_LIVE.search(raw)
        video = YT_VIDEO.search(raw)
        if handle:
            base = handle.group(1)
        elif named:
            base = named.group(1)
        elif channel:
            base = "yt_" + channel.group(1)
        elif live:
            base = "yt_" + live.group(1)
        elif video:
            base = "yt_" + video.group(1)
        return Target(input=raw, name=_clean_name(name, safe_name(base, 24)),
                      kind="youtube", chat=None)

    raise ValueError(
        "paste a twitch.tv or youtube.com link, or a direct .m3u8 URL"
    )


@dataclass
class Stream:
    target: Target
    out_dir: Path
    work_dir: Path
    port: int
    proc: object | None = None
    state: dict = field(default_factory=dict)
    status: str = "starting"
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @property
    def name(self) -> str:
        return self.target.name


class StreamManager:
    """Spawns and reports the extra `clipper run` workers."""

    def __init__(self, engine, spawn=None):
        self.engine = engine
        self.streams: dict[str, Stream] = {}
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._last_poll = 0.0
        self._spawn = spawn or self._real_spawn

    # -- layout --------------------------------------------------------
    @property
    def root(self) -> Path:
        """Where per-streamer folders live (``clips/`` for ``--out clips/xqc``)."""
        out = self.engine.cfg.out_dir
        if out.name.lower() == self.engine.channel.lower():
            return out.parent
        return out

    # -- adding --------------------------------------------------------
    def add(self, target: Target, mode: str = ASK) -> dict:
        mode = (mode or ASK).strip().lower()
        if mode == CANCEL:
            return {"ok": True, "cancelled": True}
        with self._lock:
            problem = self._problem(target)
            if problem:
                return {"ok": False, "error": problem}
            busy = self._active_names()
            if busy and mode != BOTH:
                return {"ok": True, "needs_choice": True, "busy": busy,
                        "target": target.as_dict()}
            try:
                stream = self._start(target)
            except SystemExit as exc:
                return {"ok": False, "error": str(exc) or "no free port for the worker"}
            except OSError as exc:
                return {"ok": False, "error": f"could not start the worker: {exc}"}
            except Exception as exc:  # noqa: BLE001 - a bad launch must not kill the console
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "stream": self._entry(stream)}

    def retry(self, name: str) -> dict:
        """Restart a worker that has stopped, without the simultaneous prompt."""
        with self._lock:
            stream = self._find(name)
            if stream is None:
                return {"ok": False, "error": "no such stream"}
            if stream.proc is not None and stream.proc.poll() is None:
                return {"ok": False, "error": f"{stream.name} is still running"}
            problem = self._problem(stream.target, ignore=stream.name)
            if problem:
                return {"ok": False, "error": problem}
            try:
                fresh = self._start(stream.target)
            except SystemExit as exc:
                return {"ok": False, "error": str(exc) or "no free port for the worker"}
            except OSError as exc:
                return {"ok": False, "error": f"could not start the worker: {exc}"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "stream": self._entry(fresh)}

    def stop(self, name: str) -> dict:
        with self._lock:
            stream = self._find(name)
            if stream is None:
                return {"ok": False, "error": "no such stream"}
            self._terminate(stream)
            stream.status = "stopped"
            stream.state = {}
            return {"ok": True, "stream": self._entry(stream)}

    def stop_all(self) -> None:
        with self._lock:
            for stream in self.streams.values():
                self._terminate(stream)

    # -- internals -----------------------------------------------------
    def _find(self, name: str) -> Stream | None:
        wanted = str(name or "").strip().lower()
        for key, stream in self.streams.items():
            if key.lower() == wanted:
                return stream
        return None

    def _problem(self, target: Target, ignore: str = "") -> str:
        existing = self._find(target.name)
        if existing is not None and existing.name.lower() != ignore.lower():
            alive = existing.proc is not None and existing.proc.poll() is None
            if alive:
                return f"{existing.name} is already being clipped"
        if (target.name.lower() == self.engine.channel.lower()
                and self._main_live()):
            return f"{target.name} is the stream this console is clipping"
        out = (self.root / target.name).resolve()
        if out == self.engine.cfg.out_dir.resolve():
            return f"{target.name} would write into this stream's clip folder"
        return ""

    def _main_live(self) -> bool:
        proc = getattr(self.engine, "_ingest", None)
        return proc is not None and proc.poll() is None

    def _active_names(self) -> list[str]:
        names: list[str] = []
        if self._main_live():
            names.append(self.engine.channel)
        for stream in self.streams.values():
            if stream.proc is not None and stream.proc.poll() is None:
                names.append(stream.name)
        return names

    def _start(self, target: Target) -> Stream:
        out_dir = self.root / target.name
        work_dir = Path(self.engine.cfg.work_dir) / "streams" / target.name
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Give the worker the console's tuning through its own profile. It is a
        # fresh process, so everything still sits at the Config default and the
        # saved profile applies cleanly.
        values = dict(self.engine.settings_values())
        values["cam"] = "auto"          # detect *this* streamer's webcam
        if values.get("title_mode") == "fixed":
            values["title_mode"] = "auto"   # a fixed hook belongs to the other stream
        # A link pasted into an untouched console has no decoder vocabulary yet.
        # Fill the streamer's proper nouns so the first clips already spell them.
        if not values.get("subs_prompt"):
            preset, _ = profile.autoconfigure(target.name, target.input)
            if preset.get("subs_prompt"):
                values["subs_prompt"] = preset["subs_prompt"]
        profile.save(profile.path_for(work_dir, target.name), values, target.name)

        port = free_port(self.engine.cfg.port + 1, 1)
        stream = Stream(target=target, out_dir=out_dir, work_dir=work_dir, port=port)
        cmd = [
            sys.executable, "-m", "clipper", "run",
            "-i", target.input,
            "--name", target.name,
            "--out", str(out_dir),
            "--work", str(work_dir),
            "--port", str(port),
        ]
        if target.chat:
            cmd += ["-c", target.chat]
        stream.proc = self._spawn(cmd, work_dir / "worker.log")
        for key in [k for k in self.streams if k.lower() == target.name.lower()]:
            self.streams.pop(key, None)
        self.streams[target.name] = stream
        return stream

    def _real_spawn(self, cmd: list[str], log_path: Path):
        handle = open(log_path, "ab")
        try:
            return subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        finally:
            handle.close()

    def _terminate(self, stream: Stream) -> None:
        proc = stream.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- telemetry -----------------------------------------------------
    def _poll(self) -> None:
        if not self.streams:
            return
        if time.monotonic() - self._last_poll < 1.2:
            return
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            self._last_poll = time.monotonic()
            for stream in list(self.streams.values()):
                if stream.status == "stopped":
                    continue
                proc = stream.proc
                if proc is None:
                    if stream.status not in ("failed", "stopped"):
                        stream.status = "stopped"
                    continue
                code = proc.poll()
                if code is not None:
                    stream.status = "failed" if code else "stopped"
                    stream.error = _tail_line(stream.work_dir / "worker.log")
                    stream.state = {}
                    continue
                try:
                    url = f"http://127.0.0.1:{stream.port}/api/state"
                    with urllib.request.urlopen(url, timeout=0.5) as response:
                        stream.state = json.loads(response.read().decode("utf-8"))
                except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                    pass
                stream.status = "live" if stream.state.get("connected") else "starting"
        finally:
            self._poll_lock.release()

    def snapshot(self) -> list[dict]:
        self._poll()
        with self._lock:
            return [self._entry(stream)
                    for stream in sorted(self.streams.values(), key=lambda s: s.name.lower())]

    def _entry(self, stream: Stream) -> dict:
        state = stream.state or {}
        proc = stream.proc
        alive = proc is not None and proc.poll() is None
        try:
            clips = len(list(stream.out_dir.glob("*.mp4")))
        except OSError:
            clips = 0
        status = stream.status if proc is not None else "stopped"
        if proc is not None and not alive and status not in ("failed", "stopped"):
            status = "stopped"
        return {
            "name": stream.name,
            "kind": stream.target.kind,
            "input": stream.target.input,
            "chat": stream.target.chat,
            "out_dir": str(stream.out_dir),
            "port": stream.port,
            "pid": getattr(proc, "pid", None) if alive else None,
            "status": status,
            "connected": bool(state.get("connected")),
            "capturing": bool(state.get("capturing")),
            "recording": bool(state.get("recording")),
            "score": round(float(state.get("score") or 0.0), 3),
            "clips": clips,
            "uptime_s": round(max(0.0, time.monotonic() - stream.started_at), 1),
            "error": stream.error,
        }


def _tail_line(path: Path, limit: int = 4000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:220] if lines else ""