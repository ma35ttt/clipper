"""Chat sources: Twitch (anonymous IRC-over-TLS), JSONL replay, and a simulator."""

from __future__ import annotations

import json
import random
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6697

BOT_NAMES = {
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot",
    "sery_bot", "wizebot", "commanderroot", "soundalerts", "creatisbot",
}


@dataclass(frozen=True)
class ChatEvent:
    t: float          # stream clock time the message is *about*
    text: str
    user: str


def parse_target(spec: str) -> tuple[str, str]:
    """'twitch:xqc' -> ('twitch', 'xqc'). Bare names default to twitch."""
    spec = (spec or "").strip()
    if spec.lower() in ("sim", "sim:"):
        return "sim", ""
    if ":" in spec:
        kind, value = spec.split(":", 1)
        kind = kind.strip().lower()
        if kind in ("twitch", "file", "sim"):
            return kind, value.strip()
    return "twitch", spec


class ChatSource(threading.Thread):
    def __init__(self, clock: Callable[[], float], on_event, on_status=None):
        super().__init__(daemon=True, name="chat")
        self.clock = clock
        self.on_event = on_event
        self.on_status = on_status or (lambda _msg: None)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, t: float, text: str, user: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.on_event(ChatEvent(t=float(t), text=text, user=user))


def normalize_channel(value: str) -> str:
    """Accept xqc, #xqc, twitch.tv/xqc, https://www.twitch.tv/xqc/."""
    v = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    if v.startswith("www."):
        v = v[4:]
    for host in ("m.twitch.tv/", "twitch.tv/"):
        if v.startswith(host):
            v = v[len(host):]
    v = v.split("?")[0]
    if "/" in v:
        v = v.rstrip("/").split("/")[-1]
    return v.lstrip("#").strip()


class TwitchChat(ChatSource):
    """Read-only anonymous Twitch chat. No token, no password, no writes."""

    def __init__(self, channel: str, clock, on_event, on_status=None, lag: float = 3.0,
                 bot_filter: bool = True):
        super().__init__(clock, on_event, on_status)
        self.channel = normalize_channel(channel)
        self.lag = max(0.0, float(lag))
        self.bot_filter = bot_filter

    def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            sock = None
            try:
                self.on_status(f"connecting to twitch/#{self.channel}")
                sock = self._connect()
                self.on_status(f"chat live on #{self.channel}")
                backoff = 2.0
                self._read_loop(sock)
            except Exception as exc:  # noqa: BLE001 - network is unreliable by nature
                self.on_status(f"chat disconnected ({exc}); retrying in {backoff:.0f}s")
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _connect(self) -> ssl.SSLSocket:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((TWITCH_HOST, TWITCH_PORT), timeout=20)
        sock = ctx.wrap_socket(raw, server_hostname=TWITCH_HOST)
        sock.settimeout(180)
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._send(sock, "CAP REQ :twitch.tv/tags twitch.tv/commands")
        self._send(sock, f"NICK {nick}")
        self._send(sock, f"JOIN #{self.channel}")
        return sock

    @staticmethod
    def _send(sock: ssl.SSLSocket, line: str) -> None:
        sock.sendall((line + "\r\n").encode("utf-8"))

    def _read_loop(self, sock: ssl.SSLSocket) -> None:
        buffer = ""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                # Keep the connection warm; Twitch pings well inside the timeout.
                self._send(sock, "PING :tmi.twitch.tv")
                continue
            if not chunk:
                raise ConnectionError("socket closed by remote")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\r\n" in buffer:
                line, buffer = buffer.split("\r\n", 1)
                self._handle(sock, line)

    def _handle(self, sock: ssl.SSLSocket, line: str) -> None:
        if line.startswith("PING"):
            self._send(sock, "PONG :tmi.twitch.tv")
            return
        if "PRIVMSG" not in line:
            return
        match = re.search(r"display-name=([^;]*);", line)
        user = (match.group(1) if match else "") or line.split("!", 1)[0].lstrip(":").lstrip("@")
        split = line.split("PRIVMSG", 1)
        if len(split) < 2 or " :" not in split[1]:
            return
        text = split[1].split(" :", 1)[1]
        if self.bot_filter:
            bare = user.lstrip("@").lower()
            if bare in BOT_NAMES or text.startswith("!"):
                return
        # The message describes what viewers saw `lag` seconds ago.
        self._emit(self.clock() - self.lag, text, user)


class FileChat(ChatSource):
    """Replay a recorded chat as JSONL: {"t": seconds, "text": "...", "user": "..."}"""

    def __init__(self, path: str, clock, on_event, on_status=None):
        super().__init__(clock, on_event, on_status)
        self.path = Path(path)
        self._events: list[tuple[float, str, str]] = []
        self._loaded = False

    def _load(self) -> bool:
        if self._loaded:
            return True
        if not self.path.exists():
            self.on_status(f"chat file not found: {self.path}")
            return False
        rows: list[tuple[float, str, str]] = []
        with open(self.path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append((float(obj["t"]), str(obj.get("text", "")), str(obj.get("user", ""))))
                except (ValueError, KeyError, TypeError):
                    continue
        rows.sort(key=lambda r: r[0])
        self._events = rows
        self._loaded = True
        self.on_status(f"replaying {len(rows)} chat messages from {self.path.name}")
        return True

    def run(self) -> None:
        while not self._stop.is_set() and not self._load():
            if self._stop.wait(1.0):
                return
        start = self.clock()
        index = 0
        while not self._stop.is_set():
            now = self.clock()
            while index < len(self._events) and start + self._events[index][0] <= now:
                offset, text, user = self._events[index]
                self._emit(start + offset, text, user)
                index += 1
            if index >= len(self._events):
                return
            time.sleep(0.05)


class SimChat(ChatSource):
    """Synthetic chat: quiet baseline plus periodic hype bursts. For demos and tuning."""

    KEYWORDS = [
        "NO WAY", "WHAT", "LETS GO", "OMG", "W", "KEKW", "LMAO",
        "CLIP IT", "INSANE", "HOW", "COOKED", "BRO",
    ]

    def __init__(self, clock, on_event, on_status=None, burst_every: float = 35.0):
        super().__init__(clock, on_event, on_status)
        self.burst_every = max(10.0, float(burst_every))

    def run(self) -> None:
        rng = random.Random(7)
        self.on_status("simulated chat active")
        start = self.clock()
        next_burst = start + rng.uniform(12.0, 20.0)
        next_idle = start
        while not self._stop.is_set():
            now = self.clock()
            if now >= next_burst:
                for i in range(26):
                    self._emit(now + i * 0.22, rng.choice(self.KEYWORDS), f"viewer{rng.randint(1, 999)}")
                next_burst = now + self.burst_every
            if now >= next_idle:
                self._emit(now, rng.choice(["nice", "lol", "ok", "haha", "true"]), "lurker")
                next_idle = now + rng.uniform(2.0, 6.0)
            time.sleep(0.2)


def make_chat(spec: str, clock, on_event, on_status=None, lag: float = 3.0,
              bot_filter: bool = True) -> ChatSource:
    kind, value = parse_target(spec)
    if kind == "twitch":
        if not value:
            raise SystemExit("error: --chat twitch:<channel> needs a channel name")
        return TwitchChat(value, clock, on_event, on_status, lag=lag, bot_filter=bot_filter)
    if kind == "file":
        return FileChat(value, clock, on_event, on_status)
    return SimChat(clock, on_event, on_status)
