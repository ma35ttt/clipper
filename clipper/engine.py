"""The engine: ingest -> buffer -> signals -> trigger -> clip."""

from __future__ import annotations

import copy
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from . import edit as edit_mod
from . import profile
from . import styles as styles_mod
from . import voice as voice_mod
from .audio import LoudnessAnalyzer
from .chat import ChatEvent, make_chat, normalize_channel, parse_target
from .config import Config, parse_cam
from .cutter import Cutter, clip_filename, pick_title
from .dashboard import Dashboard
from .hls import HLSBuffer
from .resolve import resolve
from .signals import AudioPoint, Signals
from .streams import StreamManager, derive_target
from .util import FFMPEG, fmt_clock, probe, safe_name, tail_file

REMOTE = re.compile(r"^(https?|rtmp|rtmps|rtsp|srt|udp|tcp)://", re.I)


def word_count(cues) -> int:
    """Words in a transcript, preferring whisper's token timings when present."""
    words = getattr(cues, "words", None) or []
    if words:
        return len(words)
    return sum(len(getattr(cue, "text", "").split()) for cue in cues or [])


@dataclass
class Capture:
    trig: float
    start: float
    end: float
    peak: float
    peak_signals: dict = field(default_factory=dict)
    forced: bool = False


class _Verifier(threading.Thread):
    """Transcribes a candidate window with the fast model, off the hot loop.

    The cheap signals decide *when* to look; this decides whether he actually
    said something. It is deliberately small and throttled: on an 8 GB laptop
    it must not compete with the ingest, the browser or the render.
    """

    def __init__(self, cutter, jobs: "queue.Queue", out: "queue.Queue"):
        super().__init__(daemon=True, name="verify")
        self.cutter = cutter
        self.jobs = jobs
        self.out = out
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if job is None:
                return
            segments, a, b, t = job
            text = ""
            try:
                prepared = self.cutter.prepare(segments, a, b, f"verify_{int(t)}")
                if prepared is not None:
                    text = self.cutter.verify(prepared)
                    shutil.rmtree(prepared.work, ignore_errors=True)
            except Exception:  # noqa: BLE001 - verification must never crash the loop
                text = ""
            self.out.put((t, text))


class _CamWatcher(threading.Thread):
    """Keeps the split layout's webcam box in sync with the live stream.

    Streamers move their camera. Sampling frames on the hot loop would stall the
    ingest, so detection runs here instead, in the background, and only updates
    the box the cutter reads. Failures are ignored: the last good box (or the
    fallback) stays in use.
    """

    def __init__(self, engine, interval: float):
        super().__init__(daemon=True, name="cam")
        self.engine = engine
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from . import camera

        # Until the first box is found, retry quickly: a clip rendered in the
        # meantime falls back to the blurred frame, which is not what the split
        # layout promises. Once a box exists, follow slower — it rarely moves.
        found = False
        while not self._stop.is_set():
            url = self.engine.cam_source()
            if url:
                box = camera.detect(url, self.engine.cfg.work_dir,
                                    log=self.engine.log)
                if box:
                    found = True
                    self.engine.adopt_cam(box)
            wait = self.interval if found else min(10.0, self.interval)
            if self._stop.wait(wait):
                return


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hls_dir = cfg.work_dir / "hls"
        self.log_path = cfg.work_dir / "ingest.log"
        self.buf = HLSBuffer(self.hls_dir)
        self.signals = Signals(cfg)
        self.cutter = Cutter(cfg)
        self.capture: Capture | None = None
        self._stop = threading.Event()
        self._force = threading.Event()
        self._closed = False
        self._above_since: float | None = None
        self._cooldown_until = 0.0
        self._chat_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue(maxsize=128)
        self._audio_out: queue.Queue = queue.Queue()
        self._audio_queued: set[int] = set()
        self._audio_map: dict[int, tuple[float, float]] = {}
        self._verify_q: queue.Queue = queue.Queue(maxsize=2)
        self._verify_out: queue.Queue = queue.Queue()
        self._verify_worker: _Verifier | None = None
        self._cam_watcher: _CamWatcher | None = None
        self._verify_on = False
        self._warned_no_cam = False
        self._verify_pending = False
        self._last_verify = 0.0
        self._excerpt: list[tuple[float, str, str]] = []
        self._ingest: subprocess.Popen | None = None
        self._log_fh = None
        self._last_log = 0.0
        self._last_status = 0.0
        self._started_at = time.monotonic()
        self._stream_url = cfg.input
        self._expires_at: float | None = None
        self._restart_count = 0
        self._restart_base = 0.0
        self._offline = False
        self._source_note = ""
        self._score = {"score": 0.0, "chat": 0.0, "audio": 0.0, "text": 0.0,
                       "chat_rate": 0.0, "keywords": 0, "audio_db": None}
        self._chat_status = "off"
        self._clip_count = 0
        self._restarts_seen = 0
        self.channel = self._channel_name()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        self.chat = None
        self.analyzer = LoudnessAnalyzer(self._audio_q, self._audio_out)
        self.dashboard: Dashboard | None = None
        # Extra streamers added from the console, each a separate worker process.
        self.streams = StreamManager(self)
        # Past-stream (VOD) job: one at a time, run on a background thread so the
        # console keeps its live score while an old stream is analysed.
        self._vod_job: dict | None = None
        self._vod_lock = threading.Lock()
        self._vod_stop = threading.Event()
        # Post-session styling job: separate from VOD, one at a time.
        self._style_job: dict | None = None
        self._style_lock = threading.Lock()
        self._style_stop = threading.Event()
        # -- console state ---------------------------------------------
        # A small ring of recent log lines so the website can show activity
        # without tailing a file.
        self._log_ring: deque[str] = deque(maxlen=250)
        self._auto = False
        self._manual_before_auto: dict | None = None
        # The cam box the *user* asked for; ``cfg.cam`` is what detection ended
        # up using. Keeping them apart means saving a profile never persists a
        # detected box in place of "auto".
        self._cam_setting = self.cfg.cam
        # Manual recording: "Clip Now" opens a capture, pressing again cuts it.
        self._rec_cap: Capture | None = None
        self._rec_peak = 0.0

    # -- public --------------------------------------------------------
    def log(self, message: str) -> None:
        line = f"[clipper {time.strftime('%H:%M:%S')}] {message}"
        self._log_ring.append(line)
        print(line, flush=True)

    def stop(self) -> None:
        self._stop.set()

    def force_clip(self) -> None:
        self._force.set()

    def run(self) -> int:
        if getattr(self.cfg, "console", False):
            return self._run_console()
        self._prepare_dirs()
        self._maybe_load_profile()
        self._load_cam()
        self._start_ingest()
        try:
            self._wait_for_buffer()
            self.analyzer.start()
            self._start_verify()
            self._start_cam_watch()
            self._start_chat()
            if self.cfg.dashboard:
                self.dashboard = Dashboard(self, self.cfg.port, self.cfg.out_dir)
                self.dashboard.start()
            self._loop()
        finally:
            self._shutdown()
        return 0

    def _run_console(self) -> int:
        """Serve the console without an ingest; links start workers from the page.

        The console still owns the tuning: edits are saved to its own profile and
        copied into every worker it starts, so you can line up the settings first
        and paste a link afterwards.
        """
        self._prepare_dirs()
        self._maybe_load_profile()
        self._chat_status = "off"
        try:
            self.dashboard = Dashboard(self, self.cfg.port, self.cfg.out_dir)
            self.dashboard.start()
            self.log("console ready — paste a Twitch or YouTube link to start clipping")
            while not self._stop.is_set():
                if self.cfg.run_for and time.monotonic() - self._started_at >= self.cfg.run_for:
                    self.log("run duration reached")
                    break
                time.sleep(0.25)
        finally:
            self._shutdown()
        return 0

    # -- operator console ----------------------------------------------
    def _profile_path(self) -> Path:
        return profile.path_for(self.cfg.work_dir, self.channel)

    def _maybe_load_profile(self) -> None:
        """Fill in settings the CLI did not touch, from the saved profile.

        Only fields still sitting at their :class:`Config` default are filled,
        so an explicit command-line flag always beats the saved file.
        """
        if not getattr(self.cfg, "profiles", True):
            return
        saved = profile.load(self._profile_path())
        if not saved:
            return
        base = Config()
        applied = 0
        for field in profile.FIELDS:
            if field.key not in saved:
                continue
            if getattr(self.cfg, field.key, None) != getattr(base, field.key, None):
                continue
            try:
                setattr(self.cfg, field.key, profile.coerce(field, saved[field.key]))
            except ValueError:
                continue
            applied += 1
        if applied:
            self._cam_setting = self.cfg.cam
            self.log(f"profile '{self.channel}' loaded — {applied} setting(s)")

    def settings_values(self) -> dict:
        """Current settings as the console sees them (cam = the user's choice)."""
        values = profile.values(self.cfg)
        values["cam"] = self._cam_setting
        return values

    def settings_snapshot(self) -> dict:
        return {
            "spec": profile.spec(),
            "values": self.settings_values(),
            "groups": list(profile.GROUP_ORDER),
            "auto": self._auto,
            "profiles": bool(getattr(self.cfg, "profiles", True)),
            "profile_saved": profile.saved(self._profile_path()),
            "channel": self.channel,
            "encoder": self.cutter.encoder,
        }

    def _save_profile(self) -> bool:
        if not getattr(self.cfg, "profiles", True):
            return False
        return profile.save(self._profile_path(), self.settings_values(), self.channel)

    def apply_settings(self, values: dict) -> dict:
        """Apply a batch of console edits and return the fresh settings."""
        try:
            changed = profile.apply(self.cfg, values)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **self.settings_snapshot()}
        self._after_change(changed)
        self._save_profile()
        if changed:
            self.log(f"settings updated — {', '.join(changed)}")
        return {"ok": True, "changed": changed, **self.settings_snapshot()}

    def apply_auto(self, on: bool) -> dict:
        """The do-everything toggle: apply AUTO, or hand the old settings back."""
        if on == self._auto:
            return {"ok": True, **self.settings_snapshot()}
        if on:
            self._manual_before_auto = self.settings_values()
            try:
                profile.apply(self.cfg, profile.AUTO)
            except ValueError:
                pass
            self._auto = True
            self.log("auto on — split frame, webcam follow, capture-only; "
                     "style chosen clips when the session ends")
        else:
            if self._manual_before_auto:
                try:
                    profile.apply(self.cfg, self._manual_before_auto)
                except ValueError:
                    pass
            self._auto = False
            self.log("auto off — your settings are back")
        # Re-evaluate the settings AUTO touched, whichever direction we went.
        self._after_change(list(profile.AUTO))
        self._save_profile()
        return {"ok": True, **self.settings_snapshot()}

    def auto_configure(self) -> dict:
        """One-shot preset tuned for this streamer (not the Auto toggle).

        Unlike AUTO this is not a mode to switch off: it simply applies the best
        known values for this channel and leaves them editable. Named streamers
        get their own decoder vocabulary; a YouTube link gets audio-led trigger
        weights because there is no chat to read.
        """
        from . import transcribe

        values, note = profile.autoconfigure(
            self.channel,
            getattr(self.cfg, "input", "") or getattr(self.cfg, "chat", "") or "")
        if values.get("subs") == "on" and not transcribe.has_subtitles():
            values["subs"] = "off"
            note += " · captions off (no whisper model)"
        if values.get("verify") == "on" and transcribe.find_fast_model() is None:
            values["verify"] = "off"
        try:
            changed = profile.apply(self.cfg, values)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **self.settings_snapshot()}
        # It is a preset, not the AUTO mode, so make sure the toggle reflects
        # that the user's settings are now their own again.
        self._auto = False
        self._manual_before_auto = None
        self._after_change(changed)
        self._save_profile()
        self.log(f"auto-configured for {self.channel} — {note}")
        return {"ok": True, "changed": changed, "note": note,
                **self.settings_snapshot()}

    def _after_change(self, changed: list[str]) -> None:
        """React to live edits that spin machinery up or down."""
        keys = set(changed)
        if "cam" in keys:
            self._cam_setting = self.cfg.cam
        if "encoder" in changed:
            from .util import pick_encoder

            self.cutter.encoder = pick_encoder(self.cfg.encoder)
        if getattr(self.cfg, "console", False):
            # The console has no ingest of its own; its values become the
            # template for the next worker, so there is nothing to restart.
            return
        if keys & {"verify", "verify_interval", "verify_window"}:
            self._restart_verify()
        if keys & {"cam", "cam_interval", "layout"}:
            self._restart_cam_watch()

    def save_profile(self) -> dict:
        ok = self._save_profile()
        self.log(f"profile saved → {self._profile_path().name}" if ok
                 else "profile could not be saved")
        return {"ok": ok, **self.settings_snapshot()}

    def load_profile(self) -> dict:
        if not getattr(self.cfg, "profiles", True):
            return {"ok": False, "error": "profiles are disabled",
                    **self.settings_snapshot()}
        saved = profile.load(self._profile_path())
        if not saved:
            return {"ok": False, "error": "no saved profile for this stream",
                    **self.settings_snapshot()}
        try:
            changed = profile.apply(self.cfg, saved)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **self.settings_snapshot()}
        self._auto = False
        self._after_change(changed)
        self.log(f"profile loaded — {len(changed)} setting(s)")
        return {"ok": True, **self.settings_snapshot()}

    def reset_profile(self) -> dict:
        profile.clear(self._profile_path())
        base = Config()
        values = {f.key: getattr(base, f.key, None) for f in profile.FIELDS}
        try:
            changed = profile.apply(self.cfg, values)
        except ValueError:
            changed = []
        self._auto = False
        self._after_change(changed)
        self.log("settings reset to defaults")
        return {"ok": True, **self.settings_snapshot()}

    def redetect_cam(self) -> dict:
        from . import camera

        if not camera.available():
            return {"ok": False, "error": "camera detection needs macOS + Swift"}
        url = self.cam_source()
        if not url:
            return {"ok": False, "error": "no stream yet"}

        def work():
            box = camera.detect(url, self.cfg.work_dir, log=self.log)
            if box:
                self.adopt_cam(box)
            else:
                self.log("camera detection found no face")

        threading.Thread(target=work, daemon=True, name="cam-now").start()
        return {"ok": True, "note": "detecting…"}

    def delete_clip(self, name: str) -> dict:
        target = self.cfg.out_dir / Path(str(name)).name
        if target.suffix != ".mp4" or not target.exists():
            return {"ok": False, "error": "no such clip"}
        try:
            target.unlink()
            side = target.with_suffix(".json")
            if side.exists():
                side.unlink()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self.log(f"clip deleted → {target.name}")
        return {"ok": True}

    # -- updating (the header button) ----------------------------------
    def check_update(self) -> dict:
        from . import update as update_mod

        return update_mod.check(log=self.log)

    def run_update(self) -> dict:
        from . import update as update_mod

        result = update_mod.run(log=self.log)
        if result.get("updated"):
            self.log("update applied — restart the console to load it")
        return result

    # -- past streams (the VOD section) --------------------------------
    def start_vod(self, link: str, name: str = "", max_clips: int = 12,
                  chat_mode: str = "auto", reset: bool = False) -> dict:
        """Analyse a finished stream on a background thread and render its best
        moments. Tuning is inherited from the running console, so this is the
        same frame, captions, hook and cold open the live path makes."""
        link = (link or "").strip()
        if not link:
            return {"ok": False, "error": "paste a link to a past stream first"}
        if self._style_job is not None and self._style_job.get("running"):
            return {"ok": False,
                    "error": "clips are being styled — wait for that to finish"}
        with self._vod_lock:
            if self._vod_job is not None and self._vod_job.get("running"):
                return {"ok": False,
                        "error": "a past stream is already being clipped"}
            self._vod_job = {
                "running": True, "link": link, "name": name or "",
                "error": "", "state": {},
            }
        self._vod_stop = threading.Event()
        self.log(f"past stream — analysing {link}")
        threading.Thread(target=self._vod_worker,
                         args=(link, name, max_clips, chat_mode, reset),
                         daemon=True, name="vod").start()
        return {"ok": True, "vod": self.vod_snapshot()}

    def _vod_worker(self, link: str, name: str, max_clips: int,
                    chat_mode: str, reset: bool = False) -> None:
        from . import vod as vod_mod

        cfg = copy.copy(self.cfg)          # shallow: same tuning, own engine
        cfg.name = name or self.cfg.name
        cfg.dashboard = False
        cfg.console = False
        cfg.profiles = getattr(self.cfg, "profiles", True)
        job = self._vod_job or {}
        try:
            code = vod_mod.run(cfg, link, max_clips=max_clips,
                               chat_mode=chat_mode, reset=reset, log=self.log,
                               should_stop=self._vod_stop.is_set,
                               state=job.setdefault("state", {}))
            job["ok"] = code == 0
        except Exception as exc:  # noqa: BLE001 - a bad VOD must not kill the console
            job["error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"past stream crashed — {job['error']}")
            job["ok"] = False
        finally:
            job["running"] = False
            state = job.get("state") or {}
            if state.get("phase") not in ("done", "error"):
                state["phase"] = "done"

    def stop_vod(self) -> dict:
        job = self._vod_job
        if job is None or not job.get("running"):
            return {"ok": False, "error": "no past stream is being clipped"}
        self._vod_stop.set()
        self.log("past stream — stopping after the current clip")
        return {"ok": True, "note": "stopping after the current clip"}

    def vod_snapshot(self) -> dict:
        job = self._vod_job
        if not job:
            return {}
        try:
            state = dict(job.get("state") or {})
        except RuntimeError:          # the worker mutated it mid-copy
            state = {}
        total = int(state.get("total") or 0)
        index = int(state.get("index") or 0)
        return {
            "running": bool(job.get("running")),
            "link": job.get("link", ""),
            "name": job.get("name", ""),
            "phase": state.get("phase", "starting"),
            "index": index,
            "total": total,
            "rendered": int(state.get("clips") or state.get("rendered") or 0),
            "found": int(state.get("found") or total),
            "fresh": int(state.get("fresh") or total),
            "note": state.get("note", ""),
            "error": job.get("error", "") or state.get("error", ""),
        }

    # -- post-session styling ------------------------------------------
    def start_style(self, files: list[str], style: str = "clean") -> dict:
        """Style selected clips from the session on a background thread."""
        files = [str(name) for name in (files or []) if str(name).strip()]
        if not files:
            return {"ok": False, "error": "select at least one clip"}
        if self._vod_job is not None and self._vod_job.get("running"):
            return {"ok": False,
                    "error": "a past stream is still rendering — wait for it to finish"}
        with self._style_lock:
            if self._style_job is not None and self._style_job.get("running"):
                return {"ok": False, "error": "clips are already being styled"}
            self._style_job = {"running": True, "style": style or "clean",
                               "error": "", "state": {}}
        self._style_stop = threading.Event()
        self.log(f"style — applying {style or 'clean'} to {len(files)} clip(s)")
        threading.Thread(target=self._style_worker, args=(files, style or "clean"),
                         daemon=True, name="style").start()
        return {"ok": True, "style": self.style_snapshot()}

    def _style_worker(self, files: list[str], style: str) -> None:
        from . import restyle

        cfg = copy.copy(self.cfg)
        cfg.dashboard = False
        cfg.console = False
        job = self._style_job or {}
        try:
            code = restyle.run(cfg, files, style, log=self.log,
                               should_stop=self._style_stop.is_set,
                               state=job.setdefault("state", {}))
            job["ok"] = code == 0
        except Exception as exc:  # noqa: BLE001 - styling must not kill the console
            job["error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"style crashed — {job['error']}")
            job["ok"] = False
        finally:
            job["running"] = False
            job["done_at"] = time.strftime("%H:%M:%S")
            state = job.get("state") or {}
            if state.get("phase") not in ("done", "error"):
                state["phase"] = "done"

    def stop_style(self) -> dict:
        job = self._style_job
        if job is None or not job.get("running"):
            return {"ok": False, "error": "no clips are being styled"}
        self._style_stop.set()
        self.log("style — stopping after the current clip")
        return {"ok": True, "note": "stopping after the current clip"}

    def style_snapshot(self) -> dict:
        job = self._style_job
        if not job:
            return {}
        try:
            state = dict(job.get("state") or {})
        except RuntimeError:
            state = {}
        total = int(state.get("total") or 0)
        return {
            "running": bool(job.get("running")),
            "style": job.get("style", ""),
            "phase": state.get("phase", "starting"),
            "index": int(state.get("index") or 0),
            "total": total,
            "rendered": int(state.get("clips") or 0),
            "file": state.get("file", ""),
            "done_at": job.get("done_at", ""),
            "error": job.get("error", "") or state.get("error", ""),
        }

    # -- extra streams (the link section) ------------------------------
    def add_stream(self, link: str, name: str = "", mode: str = "ask") -> dict:
        """Start clipping another streamer from a pasted link.

        Returns ``needs_choice`` instead of starting when this console is
        already clipping something, so the website can ask about running both.
        """
        try:
            target = derive_target(link, name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        result = self.streams.add(target, mode)
        stream = result.get("stream")
        if stream:
            self.log(f"stream added — {stream['name']} ({stream['kind']}) "
                     f"→ {stream['out_dir']}")
        return result

    def stop_stream(self, name: str) -> dict:
        result = self.streams.stop(name)
        if result.get("ok"):
            self.log(f"stream stopped — {result['stream']['name']}")
        return result

    def retry_stream(self, name: str) -> dict:
        result = self.streams.retry(name)
        if result.get("ok"):
            self.log(f"stream retried — {result['stream']['name']}")
        return result

    def _restart_verify(self) -> None:
        if self._verify_worker:
            self._verify_worker.stop()
            self._verify_worker = None
        self._verify_on = False
        self._verify_pending = False
        self._start_verify()

    def _restart_cam_watch(self) -> None:
        if self._cam_watcher:
            self._cam_watcher.stop()
            self._cam_watcher = None
        self._start_cam_watch()

    # -- manual recording ("Clip Now") ---------------------------------
    def _buffer_start(self) -> float:
        segments = self.buf.segments
        return segments[0].start if segments else 0.0

    def start_recording(self) -> dict:
        """Open a capture at the current moment; the lead-in comes from the buffer."""
        if self._rec_cap is not None:
            return {"ok": True, "recording": True}
        now = self.buf.stream_now
        if now is None:
            return {"ok": False, "error": "no stream yet", "recording": False}
        start = max(self._buffer_start(), now - max(0.0, self.cfg.pre))
        self._rec_cap = Capture(trig=now, start=start, end=now, peak=0.0,
                                peak_signals=dict(self._score), forced=True)
        self._rec_peak = float(self._score.get("score", 0.0))
        self.log(f"recording from {fmt_clock(start)} — press stop to cut the clip")
        return {"ok": True, "recording": True}

    def stop_recording(self) -> dict:
        """Close the open capture and cut everything up to now."""
        cap = self._rec_cap
        if cap is None:
            return {"ok": True, "recording": False}
        now = self.buf.stream_now or cap.end
        cap.end = max(cap.end, now)
        cap.peak = max(cap.peak, self._rec_peak)
        self._rec_cap = None
        if cap.end - cap.start < 1.0:
            self.log("recording too short — discarded")
            return {"ok": True, "recording": False, "note": "too short"}
        self.log(f"recording stopped — {cap.end - cap.start:.1f}s, cutting now")
        self._submit(cap, now)
        return {"ok": True, "recording": False}

    def toggle_recording(self) -> dict:
        return (self.stop_recording() if self._rec_cap is not None
                else self.start_recording())

    # -- setup ---------------------------------------------------------
    def _channel_name(self) -> str:
        explicit = str(getattr(self.cfg, "name", "") or "").strip()
        if explicit:
            return safe_name(explicit, 24)
        if getattr(self.cfg, "console", False):
            return "console"
        if self.cfg.chat:
            kind, value = parse_target(self.cfg.chat)
            if kind == "twitch" and value:
                return safe_name(normalize_channel(value), 24)
        if REMOTE.match(self.cfg.input):
            return "stream"
        return safe_name(Path(self.cfg.input).stem, 24)

    def _prepare_dirs(self) -> None:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self._clean_hls()
        self._log_fh = open(self.cfg.work_dir / "scores.jsonl", "w", encoding="utf-8")

    def _ingest_cmd(self) -> list[str]:
        cfg = self.cfg
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "warning", "-y"]
        remote = bool(REMOTE.match(self._stream_url))
        if not remote:
            cmd += ["-stream_loop", "-1"] if cfg.loop else []
            cmd += ["-readrate", str(cfg.speed)]
        elif self._stream_url.lower().startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        cmd += ["-i", self._stream_url]

        if cfg.transcode:
            cmd += [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-force_key_frames", f"expr:gte(t,n_forced*{cfg.segment_time})",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            ]
        else:
            cmd += ["-c", "copy"]

        cmd += [
            "-f", "hls",
            "-hls_time", str(cfg.segment_time),
            "-hls_list_size", str(cfg.hls_list_size),
            "-hls_flags", "delete_segments+omit_endlist+independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(self.hls_dir / "seg_%06d.ts"),
            str(self.hls_dir / "index.m3u8"),
        ]
        return cmd

    def _clean_hls(self) -> None:
        for stale in self.hls_dir.glob("*.ts"):
            try:
                stale.unlink()
            except OSError:
                pass
        try:
            (self.hls_dir / "index.m3u8").unlink()
        except OSError:
            pass

    def _start_ingest(self) -> None:
        cfg = self.cfg
        if not REMOTE.match(cfg.input) and not Path(cfg.input).exists():
            raise SystemExit(f"error: input not found: {cfg.input}")
        if not cfg.transcode and not REMOTE.match(cfg.input):
            info = probe(cfg.input)
            if info.get("vcodec") and info["vcodec"] not in ("h264",):
                self.log(f"warning: {info['vcodec']} source — HLS copy may be unreliable; "
                         "add --transcode")
        try:
            source = resolve(cfg.input)
        except RuntimeError as exc:
            raise SystemExit(f"error: {exc}") from exc
        self._stream_url = source.url
        self._expires_at = source.expires_at
        self._source_note = source.note
        self._clean_hls()

        label = source.note or ("live" if REMOTE.match(cfg.input) else "file")
        self.log(f"ingest → {label}  (segment {cfg.segment_time:g}s, "
                 f"window {cfg.hls_list_size * cfg.segment_time:.0f}s)")
        stderr = open(self.log_path, "ab")
        self._ingest = subprocess.Popen(
            self._ingest_cmd(), stdout=subprocess.DEVNULL, stderr=stderr,
            stdin=subprocess.DEVNULL,
        )
        stderr.close()

    def _wait_for_buffer(self) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._ingest and self._ingest.poll() is not None:
                raise SystemExit(
                    "error: ffmpeg ingest exited\n" + tail_file(self.log_path, 14)
                )
            self.buf.poll()
            if self.buf.stream_now and self.buf.stream_now >= self.cfg.segment_time:
                self.log(f"buffer live — {len(self.buf.segments)} segments ready")
                return
            time.sleep(0.15)
        raise SystemExit("error: timed out waiting for the stream buffer\n"
                         + tail_file(self.log_path, 14))

    def _start_chat(self) -> None:
        if not self.cfg.chat:
            self._chat_status = "off"
            return
        self.chat = make_chat(
            self.cfg.chat,
            clock=lambda: self.buf.stream_now or 0.0,
            on_event=self._chat_q.put,
            on_status=self._on_chat_status,
            lag=self.cfg.chat_lag,
            bot_filter=self.cfg.bot_filter,
        )
        self.chat.start()

    def _on_chat_status(self, status: str) -> None:
        self._chat_status = status
        self.log(f"chat: {status}")

    def _start_cam_watch(self) -> None:
        """Start following the streamer's webcam when --cam auto is in force."""
        if self._cam_setting != "auto" or self.cfg.layout != "split":
            return
        from . import camera

        if not camera.available():
            self.log("camera auto-detect unavailable (needs macOS + Swift) — "
                     "using the --cam fallback")
            return
        interval = max(30.0, float(getattr(self.cfg, "cam_interval", 90.0) or 90.0))
        self._cam_watcher = _CamWatcher(self, interval)
        self._cam_watcher.start()
        self.log("camera detection on — following the streamer's webcam")

    def cam_source(self) -> str | None:
        """What detection should read: the local buffer, not the live URL.

        Reading the network stream a second time fights the ingest for
        bandwidth, and on some CDNs it returns an empty body — which is exactly
        how a split clip silently became a blurred frame. The local HLS buffer
        holds the same frames, already downloaded.
        """
        playlist = self.hls_dir / "index.m3u8"
        try:
            if playlist.exists() and len(self.buf.segments) >= 2:
                return str(playlist)
        except OSError:
            pass
        return self._stream_url or None

    def adopt_cam(self, box: tuple[int, int, int, int]) -> None:
        from . import camera

        spec = camera.format_box(box)
        if spec != self.cfg.cam:
            self.cfg.cam = spec
            self.log(f"camera box → {spec}")
        self._save_cam(spec)

    def _cam_cache_path(self) -> Path:
        return self.cfg.work_dir / "cam.json"

    def _save_cam(self, spec: str) -> None:
        """Remember the box so a restart does not go back to the blurred frame."""
        try:
            self._cam_cache_path().write_text(
                json.dumps({"cam": spec, "at": time.time()}) + "\n",
                encoding="utf-8")
        except OSError:
            pass

    def _load_cam(self) -> None:
        if self._cam_setting != "auto" or getattr(self.cfg, "cam", "auto") != "auto":
            return
        try:
            data = json.loads(self._cam_cache_path().read_text())
            spec = str(data.get("cam") or "")
        except (OSError, ValueError, AttributeError):
            return
        try:
            parse_cam(spec)
        except ValueError:
            return
        self.cfg.cam = spec
        self.log(f"camera box restored from the last run → {spec}")

    def _start_verify(self) -> None:
        from . import transcribe

        mode = getattr(self.cfg, "verify", "auto")
        if mode == "off":
            return
        model = transcribe.find_fast_model()
        if model is None:
            if mode == "on":
                self.log("transcript verification requested but no fast model — "
                         "run: clipper models --get tiny.en")
            return
        self._verify_on = True
        self._verify_worker = _Verifier(self.cutter, self._verify_q, self._verify_out)
        self._verify_worker.start()
        self.log(f"transcript verification on ({model.name}, "
                 f"every {self.cfg.verify_interval:g}s)")

    # -- main loop -----------------------------------------------------
    def _loop(self) -> None:
        cfg = self.cfg
        tick = 0.1
        self.log(f"armed — threshold {cfg.threshold:.2f}, pre {cfg.pre:g}s, "
                 f"post {cfg.post:g}s, layout {cfg.layout}, encoder {self.cutter.encoder}")
        if self.cutter.subtitle_enabled():
            self.log("subtitles on (whisper)")
        elif getattr(cfg, "subs", "off") == "on":
            from . import transcribe

            self.log(f"subtitles requested but unavailable — {transcribe.status()[1]}")
        else:
            self.log("captions off — the hook comes from chat"
                     + (" and the fixed title" if cfg.title_mode == "fixed" else ""))
        while not self._stop.is_set():
            started = time.monotonic()
            if cfg.run_for and started - self._started_at >= cfg.run_for:
                self.log("run duration reached")
                break
            if self._ingest is None:
                self._recover_ingest("ingest not running")
            elif self._ingest.poll() is not None:
                self._recover_ingest(f"ingest exited ({self._ingest.returncode})")
            elif self._expires_at and time.time() > self._expires_at - 120:
                self._recover_ingest("stream URL expiring")

            new_segments = self.buf.poll()
            if new_segments:
                if self._offline:
                    self._offline = False
                    self.log("stream back — media is flowing again")
                self._queue_audio()
            self._drain_audio()
            self._drain_chat()
            self._drain_transcript()

            if self.buf.restarts != self._restarts_seen:
                self._restarts_seen = self.buf.restarts
                self.signals.reset()
                self._audio_map.clear()
                self._audio_queued.clear()
                self.log("ingest restarted — signals reset")

            now = self.buf.stream_now
            if now is None:
                time.sleep(tick)
                continue

            self._score = self.signals.score(now)
            # The counter is only forgiven after the new ingest has actually
            # survived a while; otherwise a dead URL loops forever.
            if self._restart_count and time.monotonic() - self._restart_base > 45:
                self._restart_count = 0
            self._write_score_log(now)
            self._maybe_verify(now)
            self._maybe_force(now)
            self._advance(now)
            self._status_line(now)

            elapsed = time.monotonic() - started
            time.sleep(max(0.0, tick - elapsed))

    def _recover_ingest(self, reason: str) -> None:
        if self._stop.is_set():
            return
        self._restart_count += 1
        n = self._restart_count
        # A burst of failures is a bad URL: give up quickly and let the
        # console say why. A live stream that has gone offline is different:
        # keep retrying, slowly, so an overnight session survives the gap.
        if n <= 6:
            self.log(f"{reason} — restarting ingest ({n}/6)")
            wait = min(2.0 * n, 10.0)
        else:
            self._offline = True
            if (n - 6) % 5 == 1:
                self.log(f"{reason} — stream offline; retrying every 60s")
            wait = 60.0
        if self._ingest is not None and self._ingest.poll() is None:
            self._ingest.terminate()
            try:
                self._ingest.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ingest.kill()
        self._ingest = None
        time.sleep(wait)
        try:
            self._start_ingest()
        except SystemExit as exc:
            self.log(str(exc))
            self._ingest = None
            return
        self._restart_base = time.monotonic()

    # -- queues --------------------------------------------------------
    def _queue_audio(self) -> None:
        for seg in self.buf.segments:
            if seg.seq in self._audio_queued:
                continue
            try:
                self._audio_q.put_nowait((seg.seq, str(seg.path)))
            except queue.Full:
                break
            self._audio_queued.add(seg.seq)
            self._audio_map[seg.seq] = (seg.start, seg.end)
        if len(self._audio_map) > 400:
            for seq in sorted(self._audio_map)[:200]:
                self._audio_map.pop(seq, None)
                self._audio_queued.discard(seq)

    def _drain_audio(self) -> None:
        while True:
            try:
                seq, mean, peak = self._audio_out.get_nowait()
            except queue.Empty:
                return
            span = self._audio_map.get(seq)
            if span:
                self.signals.add_audio(AudioPoint(span[0], span[1], mean, peak))

    def _drain_chat(self) -> None:
        while True:
            try:
                event: ChatEvent = self._chat_q.get_nowait()
            except queue.Empty:
                return
            self.signals.add_chat(event)
            self._excerpt.append((event.t, event.user, event.text))
        if len(self._excerpt) > 4000:
            del self._excerpt[:-2000]

    def _drain_transcript(self) -> None:
        while True:
            try:
                t, text = self._verify_out.get_nowait()
            except queue.Empty:
                return
            self._verify_pending = False
            if text:
                self.log(f"verify — \"{text[:80]}\"")
            self.signals.add_transcript(text, t)

    def _maybe_verify(self, now: float) -> None:
        """Look at a candidate early, so the transcript is ready when it counts."""
        if not self._verify_on or self._verify_pending or self.capture is not None:
            return
        if self._verify_worker is None or not self._verify_worker.is_alive():
            return
        if float(self._score.get("score", 0.0)) < self.cfg.threshold * 0.85:
            return
        if now - self._last_verify < self.cfg.verify_interval:
            return
        start = max(0.0, now - self.cfg.verify_window)
        segments = self.buf.between(start, now)
        if not segments:
            return
        try:
            self._verify_q.put_nowait((segments, start, now, now))
        except queue.Full:
            return
        self._last_verify = now
        self._verify_pending = True

    # -- triggering ----------------------------------------------------
    def _maybe_force(self, now: float) -> None:
        if not self._force.is_set():
            return
        self._force.clear()
        if self._rec_cap is not None:
            self.log("clip now ignored — a recording is already open")
            return
        if self.capture is not None:
            self.capture.end = max(self.capture.end, now + self.cfg.post)
            self.log("manual clip — extending current capture")
            return
        peak = dict(self._score)
        peak["score"] = 1.0
        self.capture = Capture(
            trig=now, start=max(0.0, now - self.cfg.pre), end=now + self.cfg.post,
            peak=1.0, peak_signals=peak, forced=True,
        )
        self.log("manual clip — started")

    def _advance(self, now: float) -> None:
        cfg = self.cfg
        score = float(self._score.get("score", 0.0))

        # While a manual recording is open it owns the cut; auto-triggers hold
        # off so a hot moment does not chop the recording into two clips.
        if self._rec_cap is not None:
            self._rec_peak = max(self._rec_peak, score)
            self._rec_cap.end = now
            return

        if self.capture is None:
            if score >= cfg.threshold:
                if self._above_since is None:
                    self._above_since = now
                if (now - self._above_since >= cfg.confirm) and now >= self._cooldown_until:
                    self.capture = Capture(
                        trig=now,
                        start=max(0.0, now - cfg.pre),
                        end=now + cfg.post,
                        peak=score,
                        peak_signals=dict(self._score),
                    )
                    self._above_since = None
                    self.log(f"trigger — score {score:.2f} at {fmt_clock(now)}")
            else:
                self._above_since = None
            return

        cap = self.capture
        if score > cap.peak:
            cap.peak = score
            cap.peak_signals = dict(self._score)
        if score >= cfg.threshold * 0.9:
            cap.end = max(cap.end, min(now + cfg.post, cap.trig + cfg.max_post))
        if now >= cap.end:
            self._finalize(now)

    def _finalize(self, now: float) -> None:
        cap = self.capture
        self.capture = None
        if cap is None:
            return
        self._cooldown_until = now + self.cfg.cooldown
        self._above_since = None
        self._submit(cap, now)

    def _submit(self, cap: Capture, now: float) -> None:
        """Snapshot a capture off the buffer and hand it to the render pool."""
        segments = self.buf.between(cap.start, cap.end)
        if not segments:
            self.log("capture produced no segments — skipping")
            return
        job_id = f"clip_{int(time.time() * 1000)}"
        prepared = self.cutter.prepare(segments, cap.start, cap.end, job_id)
        if prepared is None:
            self.log("capture too short or segments already rotated out — skipping")
            return

        meta = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stamp": time.strftime("%Y%m%d-%H%M%S"),
            "source": self.cfg.input,
            "channel": self.channel,
            "layout": self.cfg.layout,
            "stream_start": round(cap.start, 2),
            "stream_end": round(cap.end, 2),
            "duration_s": round(prepared.duration, 2),
            "score": round(cap.peak, 3),
            "forced": cap.forced,
            "signals": cap.peak_signals,
            # Filled in by _render, once the transcript exists to quote from.
            "title": "",
            "chat_excerpt": [],
            "clipper_version": __version__,
        }
        # The hook sees the clip's *whole* chat window, not the trimmed sidecar
        # copy: a busy IRL chat sends hundreds of messages in twenty seconds,
        # and a repeated phrase is exactly what a hook is made of.
        window_chat = [
            {"t": round(t, 1), "user": u, "text": x}
            for t, u, x in self._excerpt
            if cap.start <= t <= cap.end
        ]
        meta["chat_excerpt"] = window_chat[-14:]
        self.pool.submit(self._render, prepared, meta,
                         self.cutter.subtitle_enabled(), window_chat)

    def _render(self, prepared, meta: dict, subtitles: bool = False,
                chat_window: list | None = None) -> None:
        cues: list = []
        focus: float | None = None
        chat_for_hooks = (chat_window if chat_window is not None
                          else meta.get("chat_excerpt", []))
        try:
            if self.cfg.layout == "split" and not self.cutter.cam_spec() \
                    and not self._warned_no_cam:
                self._warned_no_cam = True
                self.log("no webcam box — this clip uses the blurred frame instead "
                         "of the split layout (camera detection has not found a face)")
            # The cold open and the voiceover need words even when the clip
            # is not burning captions, so transcribe for them too, then keep
            # the transcript out of the render when subtitles are off.
            wants_words = (getattr(self.cfg, "edit_mode", "off") == "coldopen"
                           or getattr(self.cfg, "voice", "off") == "on")
            cues, focus = [], None
            if subtitles or wants_words:
                try:
                    focus = (float(meta["signals"]["t"])
                             - float(meta["stream_start"]))
                except (KeyError, TypeError, ValueError):
                    focus = None
                cues, note = self.cutter.transcribe(prepared, focus=focus,
                                                    log=self.log)
                if cues:
                    meta["transcript"] = " ".join(cue.text for cue in cues)
                    if subtitles:
                        self.log(f"subtitles — {word_count(cues)} word(s) "
                                 f"via {note}")
                        meta["subtitles"] = {"engine": note, "cues": len(cues)}
                elif subtitles:
                    self.log(f"no subtitles for this clip: {note}")
                    meta["subtitles"] = {"engine": note, "cues": 0}
                elif wants_words:
                    self.log(f"no transcript to edit or narrate: {note}")
            # Rung 1: tease the payoff, then rewind and play the clip. The
            # transcript is rebuilt on the new timeline so the captions still
            # land on the words. Fails soft: the original order is kept.
            if getattr(self.cfg, "edit_mode", "off") == "coldopen":
                plan = edit_mod.plan(cues, focus=focus,
                                     duration=prepared.duration)
                if plan is None:
                    self.log("edit: no cold open for this clip "
                             "(too short, or no clear payoff)")
                    meta["edit"] = {"mode": "coldopen", "applied": False}
                else:
                    edited = edit_mod.assemble(prepared, plan,
                                               encoder=self.cutter.encoder,
                                               log=self.log)
                    if edited is None:
                        meta["edit"] = {"mode": "coldopen", "applied": False}
                    else:
                        cues = edit_mod.retime(cues, plan)
                        prepared = edited
                        if focus is not None:
                            focus = plan.cold_len + focus   # the trigger moved
                        meta["duration_s"] = round(edited.duration, 2)
                        meta["edit"] = {"mode": "coldopen", "applied": True,
                                        "payoff": list(plan.payoff),
                                        "added_s": round(plan.cold_len, 2)}
            # The hook only knows what the clip is about once the transcript and
            # chat excerpt exist: a repeated chat phrase wins, then a quotable line.
            cam = self.cutter.cam_spec()
            meta["cam"] = cam or "none"
            meta["layout_used"] = ("split" if (self.cfg.layout == "split" and cam)
                                   else "blur" if self.cfg.layout == "split"
                                   else self.cfg.layout)
            meta["title"] = pick_title(self.cfg, meta.get("signals", {}), cues,
                                       chat_for_hooks)
            meta["capture_only"] = bool(
                not meta["title"] and not (subtitles and cues)
                and getattr(self.cfg, "edit_mode", "off") != "coldopen"
                and getattr(self.cfg, "voice", "off") != "on")
            name = clip_filename(self.channel, meta["score"], meta.get("stamp"))
            out = self.cfg.out_dir / name
            index = 2
            while out.exists():
                out = self.cfg.out_dir / f"{Path(name).stem}-{index}{Path(name).suffix}"
                index += 1
            ok, error, warning = self.cutter.render(
                prepared, out, meta.get("title", ""),
                cues if subtitles else [])
            if not ok:
                self.log(f"render failed: {error[-300:]}")
                return
            if warning:
                self.log(f"warning: {warning}")
                meta["render_warning"] = warning

            # Second opinion. The live copy of the buffer is occasionally silent
            # or truncated, and whisper then invents a few words. The finished
            # file has the audio the viewer will hear, so if the transcript is
            # suspiciously thin, listen to that instead and re-render once.
            if subtitles and not warning and self._thin_transcript(cues, meta) \
                    and "caption gate" not in str(meta.get("subtitles", {}).get("engine", "")):
                better, note2 = self.cutter.transcribe_rendered(
                    out, prepared, focus=focus, log=self.log)
                if word_count(better) > word_count(cues):
                    self.log(f"re-transcribed from the saved clip — "
                             f"{word_count(better)} word(s)")
                    cues = better
                    meta["subtitles"] = {"engine": note2 + " · from the saved clip",
                                         "cues": len(better)}
                    meta["transcript"] = " ".join(cue.text for cue in cues)
                    meta["title"] = pick_title(self.cfg, meta.get("signals", {}),
                                               cues, chat_for_hooks)
                    again, err2, _ = self.cutter.render(
                        prepared, out, meta.get("title", ""),
                        cues if subtitles else [])
                    if not again:
                        self.log(f"re-render with the corrected transcript failed: "
                                 f"{err2[-200:]}")

            # Rung 2: a short factual intro in a synthetic voice, mixed over a
            # ducked copy of the clip. The video is stream-copied, so this is
            # one audio encode. Any failure keeps the un-narrated render.
            if getattr(self.cfg, "voice", "off") == "on":
                self._add_voiceover(out, cues, meta)

            info = probe(out)
            meta.update({
                "file": out.name,
                "duration_s": round(info.get("duration") or meta["duration_s"], 2),
                "width": info.get("width"),
                "height": info.get("height"),
            })
            try:
                out.with_suffix(".json").write_text(json.dumps(meta, indent=2),
                                                    encoding="utf-8")
            except OSError:
                pass
            self._clip_count += 1
            self.log(f"clip saved → {out}  "
                     f"({meta['duration_s']}s, score {meta['score']:.2f})")
        except Exception as exc:  # a dead render thread loses the clip silently
            self.log(f"render crashed: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(prepared.work, ignore_errors=True)

    def _add_voiceover(self, out: Path, cues, meta: dict) -> None:
        """Narrate a short, factual intro over the clip, in place."""
        longest = float(getattr(self.cfg, "voice_max", 10.0) or 10.0)
        script = voice_mod.script_for(
            self.channel, cues, meta.get("chat_excerpt", []),
            max_words=max(6, int(longest * 2.6)))
        if not script:
            self.log("voice: nothing factual to say about this clip — skipped")
            meta["voiceover"] = {"engine": "", "script": ""}
            return
        wav = (self.cfg.work_dir / "tmp"
               / f"voice_{int(time.time() * 1000)}.wav")
        try:
            path, engine = voice_mod.speak(
                script, wav, getattr(self.cfg, "voice_name", "") or None,
                log=self.log)
            if path is None:
                self.log(f"voice: {engine}")
                meta["voiceover"] = {"engine": "", "script": script}
                return
            mixed = voice_mod.mix(out, path, out, log=self.log)
            meta["voiceover"] = {"engine": engine if mixed else "",
                                 "script": script, "mixed": mixed}
            if mixed:
                self.log(f"voice: {engine} · {script}")
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    def _thin_transcript(self, cues, meta: dict) -> bool:
        """True when a clip is long enough to expect more words than it got."""
        duration = float(meta.get("duration_s") or 0.0)
        if duration < 6.0:
            return False
        return word_count(cues) < max(2.0, duration / 6.0)

    # -- telemetry -----------------------------------------------------
    def _write_score_log(self, now: float) -> None:
        wall = time.monotonic()
        if wall - self._last_log < 0.5 or self._log_fh is None:
            return
        self._last_log = wall
        try:
            self._log_fh.write(json.dumps({"wall": time.time(), "t": round(now, 2),
                                           **self._score}) + "\n")
            self._log_fh.flush()
        except OSError:
            pass

    def _status_line(self, now: float) -> None:
        wall = time.monotonic()
        if wall - self._last_status < 5.0:
            return
        self._last_status = wall
        s = self._score
        filled = int(round(float(s.get("score", 0)) * 10))
        bar = "█" * filled + "·" * (10 - filled)
        state = ("CLIPPING" if self.capture
                 else "offline" if self._offline
                 else "armed")
        audio = s.get("audio_db")
        audio_txt = f"{audio:6.1f}dB" if isinstance(audio, (int, float)) else "   —   "
        sig = f"{s.get('agreeing', 0)}/{s.get('required', 1)}"
        self.log(f"{bar} {float(s.get('score', 0)):.2f}  "
                 f"chat {s.get('chat_users', 0)}u/{s.get('consensus', 0)}c  "
                 f"{audio_txt}  sig {sig}  {state}  clips {self._clip_count}")

    def snapshot(self) -> dict:
        now = self.buf.stream_now or 0.0
        clips = []
        try:
            files = sorted(self.cfg.out_dir.glob("*.mp4"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:40]
        except OSError:
            files = []
        for path in files:
            entry = {"file": path.name, "title": "", "score": 0,
                     "styled": False, "style": ""}
            side = path.with_suffix(".json")
            if side.exists():
                try:
                    data = json.loads(side.read_text())
                    entry["title"] = data.get("title") or ""
                    entry["score"] = int(round(float(data.get("score", 0)) * 100))
                    entry["styled"] = bool(data.get("styled_from"))
                    entry["style"] = data.get("style") or ""
                except (OSError, ValueError):
                    pass
            clips.append(entry)

        uptime = time.monotonic() - self._started_at
        cooldown = max(0.0, self._cooldown_until - now) if now else 0.0
        return {
            "version": __version__,
            "channel": self.channel,
            "console": bool(getattr(self.cfg, "console", False)),
            "layout": self.cfg.layout,
            "cam": self.cfg.cam,
            "caption_style": getattr(self.cfg, "subs_style", "pop"),
            "capture_only": bool(
                getattr(self.cfg, "subs", "off") == "off"
                and getattr(self.cfg, "title_mode", "none") == "none"
                and getattr(self.cfg, "edit_mode", "off") == "off"
                and getattr(self.cfg, "voice", "off") == "off"),
            "connected": self._ingest is not None and self._ingest.poll() is None,
            "uptime_h": f"{int(uptime // 3600)}h {int(uptime % 3600 // 60):02d}m",
            "buffer_h": f"{fmt_clock(now)} / {len(self.buf.segments)} segs",
            "score": round(float(self._score.get("score", 0.0)), 3),
            "threshold": self.cfg.threshold,
            "capturing": self.capture is not None,
            "cooldown": cooldown,
            "signals": {
                "chat": self._score.get("chat", 0.0),
                "audio": self._score.get("audio", 0.0),
                "text": self._score.get("text", 0.0),
                "transcript": self._score.get("transcript", 0.0),
                "chat_rate": self._score.get("chat_rate", 0.0),
                "chat_users": self._score.get("chat_users", 0),
                "consensus": self._score.get("consensus", 0),
                "audio_db": self._score.get("audio_db"),
                "audio_z": self._score.get("audio_z", 0.0),
                "keywords": self._score.get("keywords", 0),
                "agreeing": self._score.get("agreeing", 0),
                "required": self._score.get("required", 1),
                "gated": self._score.get("gated", False),
            },
            "chat_status": self._chat_status,
            "auto": self._auto,
            "recording": self._rec_cap is not None,
            "recording_s": (round(max(0.0, now - self._rec_cap.start), 1)
                            if self._rec_cap is not None and now else 0.0),
            "record_max_s": round(self.cfg.hls_list_size * self.cfg.segment_time, 1),
            "encoder": self.cutter.encoder,
            "profiles": bool(getattr(self.cfg, "profiles", True)),
            "profile_saved": profile.saved(self._profile_path()),
            "out_dir": str(self.cfg.out_dir),
            "streams_root": str(self.streams.root),
            "streams": self.streams.snapshot(),
            "vod": self.vod_snapshot(),
            "style": self.style_snapshot(),
            "styles": styles_mod.catalogue(self.cfg.work_dir),
            "log": list(self._log_ring)[-60:],
            "clips": clips,
        }

    # -- teardown ------------------------------------------------------
    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self.chat:
            self.chat.stop()
        self.analyzer.stop()
        if self._verify_worker:
            self._verify_worker.stop()
        if self._cam_watcher:
            self._cam_watcher.stop()
        if self.dashboard:
            self.dashboard.stop()
        self.streams.stop_all()
        if self._ingest and self._ingest.poll() is None:
            self._ingest.terminate()
            try:
                self._ingest.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ingest.kill()
        self.log("finishing renders…")
        if self._rec_cap is not None:
            self.log("cutting the open recording")
            self.stop_recording()
        if self.capture is not None and self.buf.stream_now is not None:
            self.log("flushing the in-flight capture")
            self._finalize(self.buf.stream_now)
        self.pool.shutdown(wait=True)
        if self._log_fh:
            self._log_fh.close()
        self.log(f"stopped — {self._clip_count} clip(s) this session")
