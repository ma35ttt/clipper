"""Run several streams at once: one worker process per channel, one console.

Each channel is a completely separate `clipper run` process (its own ingest,
buffer, chat connection and clip queue), supervised by this master. The master
serves a single console at http://127.0.0.1:<port> that tiles every worker.

    clipper multi --channels xqc,kaicenat,caseoh
    clipper multi --channel "xqc" --channel "kai|twitch.tv/kaicenat"
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .chat import normalize_channel
from .util import fmt_clock, safe_name

ROOT = Path(__file__).resolve().parent.parent

GRID = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clipper — multi</title>
<style>
  :root { color-scheme: dark; --bg:#0b0d12; --panel:#141822; --line:#232a38;
          --text:#e8ecf5; --dim:#8b95a8; --hot:#ff4d6d; --cool:#4dd4ac; --warn:#ffd166; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:14px 20px;
           border-bottom:1px solid var(--line); position:sticky; top:0;
           background:rgba(11,13,18,.9); backdrop-filter:blur(10px); z-index:5; }
  h1 { font-size:14px; margin:0; letter-spacing:.14em; text-transform:uppercase; font-weight:700; }
  .strip { display:flex; gap:10px; flex-wrap:wrap; margin-left:auto; }
  .chip { display:flex; align-items:center; gap:8px; background:var(--panel);
          border:1px solid var(--line); border-radius:99px; padding:5px 12px; font-size:12px; }
  .dot { width:7px;height:7px;border-radius:50%;background:var(--cool); }
  .dot.off { background:var(--hot); }
  .dot.live { box-shadow:0 0 8px var(--cool); }
  .bar { width:54px; height:5px; background:#1b2130; border-radius:99px; overflow:hidden; }
  .bar > i { display:block; height:100%; width:0%; background:var(--warn); transition:width .2s; }
  .grid { display:grid; gap:14px; padding:16px 20px 32px;
          grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
  .tile { background:var(--panel); border:1px solid var(--line); border-radius:14px;
          overflow:hidden; display:flex; flex-direction:column; min-height:0; }
  .tile header { position:static; background:transparent; border:0; padding:10px 14px;
                 border-bottom:1px solid var(--line); backdrop-filter:none; }
  .tile h2 { margin:0; font-size:13px; letter-spacing:.08em; text-transform:uppercase; }
  .tile .meta { color:var(--dim); font-size:12px; display:flex; gap:12px; align-items:center; }
  .tile iframe { border:0; width:100%; flex:1; height:640px; background:var(--bg); }
  .tile a { color:var(--cool); text-decoration:none; font-size:12px; }
  .single iframe { height:calc(100vh - 190px); }
  .single { grid-template-columns:1fr; }
  footer { color:var(--dim); font-size:12px; padding:0 20px 24px; }
</style>
</head>
<body>
<header>
  <h1>clipper multi</h1>
  <span class="strip" id="strip"></span>
  <button class="chip" onclick="document.body.classList.toggle('single')">grid / single</button>
</header>
<div class="grid" id="grid"></div>
<footer id="foot"></footer>
<script>
const CH = __CHANNELS__;
const pct = v => (Math.max(0, Math.min(1, v)) * 100).toFixed(1) + '%';
const grid = document.getElementById('grid');

for (const c of CH) {
  const tile = document.createElement('div');
  tile.className = 'tile';
  tile.innerHTML = `
    <header>
      <h2>${c.name}</h2>
      <span class="meta">
        <span class="bar"><i id="bar-${c.name}"></i></span>
        <span id="score-${c.name}">0.00</span>
        <span id="clips-${c.name}">0 clips</span>
        <span id="state-${c.name}"></span>
        <a href="http://127.0.0.1:${c.port}/" target="_blank">open ↗</a>
      </span>
    </header>
    <iframe src="http://127.0.0.1:${c.port}/" loading="lazy"></iframe>`;
  grid.appendChild(tile);
}

async function tick() {
  try {
    const data = await (await fetch('/api/overview')).json();
    let live = 0, clips = 0;
    const strip = document.getElementById('strip');
    strip.innerHTML = '';
    for (const c of data.channels) {
      const on = c.connected;
      if (on) live++;
      clips += c.clips;
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `<span class="dot ${on ? 'live' : 'off'}"></span>
        <b>${c.name}</b> ${c.score.toFixed(2)}
        <span class="bar"><i style="width:${pct(c.score)}"></i></span>`;
      strip.appendChild(chip);

      const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
      set(`score-${c.name}`, c.score.toFixed(2));
      set(`clips-${c.name}`, `${c.clips} clip${c.clips === 1 ? '' : 's'}`);
      set(`state-${c.name}`, c.capturing ? 'CLIPPING' : (c.cooldown > 0 ? 'cooldown' : 'armed'));
      const bar = document.getElementById(`bar-${c.name}`);
      if (bar) bar.style.width = pct(c.score);
    }
    document.getElementById('foot').textContent =
      `${live}/${data.channels.length} streams live · ${clips} clips captured · ${data.uptime}`;
  } catch (e) {
    document.getElementById('foot').textContent = 'master unreachable';
  }
}
setInterval(tick, 1500); tick();
</script>
</body>
</html>
"""


@dataclass
class Channel:
    name: str
    source: str
    chat: str
    port: int
    out_dir: Path
    work_dir: Path
    proc: subprocess.Popen | None = None
    restarts: int = 0
    state: dict = field(default_factory=dict)


def parse_channels(specs: list[str], out_root: Path, work_root: Path,
                   port_base: int) -> list[Channel]:
    """'xqc' | 'xqc|twitch.tv/xqc' | 'alice|demo.mp4|sim' (name|input|chat)."""
    entries: list[tuple[str, str, str]] = []
    for spec in specs:
        for raw in spec.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split("|")]
            name = parts[0]
            source = parts[1] if len(parts) > 1 and parts[1] else f"https://twitch.tv/{name}"
            chat = parts[2] if len(parts) > 2 and parts[2] else f"twitch:{name}"
            entries.append((safe_name(normalize_channel(name) or name, 24), source, chat))
    if not entries:
        raise SystemExit("error: no channels given")

    seen: set[str] = set()
    channels: list[Channel] = []
    for index, (name, source, chat) in enumerate(entries):
        if name in seen:
            raise SystemExit(f"error: duplicate channel name '{name}'")
        seen.add(name)
        channels.append(Channel(
            name=name,
            source=source,
            chat=chat,
            port=port_base + 1 + index,
            out_dir=out_root / name,
            work_dir=work_root / name,
        ))
    return channels


def free_port(start: int, count: int) -> int:
    """Find `count` consecutive free ports at or above `start`."""
    candidate = start
    while candidate < start + 200:
        if all(_is_free(p) for p in range(candidate, candidate + count)):
            return candidate
        candidate += 1
    raise SystemExit(f"error: no consecutive free ports near {start}")


def _is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Multi:
    def __init__(self, channels: list[Channel], master_port: int, forward: list[str],
                 run_for: float = 0.0, max_restarts: int = 5):
        self.channels = channels
        self.master_port = master_port
        self.forward = forward
        self.run_for = run_for
        self.max_restarts = max_restarts
        self.started_at = time.monotonic()
        self._stop = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None

    # -- lifecycle -----------------------------------------------------
    def log(self, message: str) -> None:
        print(f"[clipper multi {time.strftime('%H:%M:%S')}] {message}", flush=True)

    def run(self) -> int:
        self.log(f"master console → http://127.0.0.1:{self.master_port}")
        self._serve()
        for channel in self.channels:
            self._spawn(channel)
        try:
            self._supervise()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.log("stopping workers…")
        for channel in self.channels:
            self._terminate(channel)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        counts = sum(len(self._clips(channel)) for channel in self.channels)
        self.log(f"stopped — {counts} clip(s) this session")

    def _spawn(self, channel: Channel) -> None:
        channel.out_dir.mkdir(parents=True, exist_ok=True)
        channel.work_dir.mkdir(parents=True, exist_ok=True)
        log_path = channel.work_dir / "worker.log"
        handle = open(log_path, "ab")
        cmd = [
            sys.executable, "-m", "clipper", "run",
            "-i", channel.source,
            "-c", channel.chat,
            "--out", str(channel.out_dir),
            "--work", str(channel.work_dir),
            "--port", str(channel.port),
            *self.forward,
        ]
        channel.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        handle.close()
        self.log(f"started {channel.name:<14} console :{channel.port}  → {channel.out_dir}")

    def _terminate(self, channel: Channel) -> None:
        proc = channel.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- supervision ---------------------------------------------------
    def _supervise(self) -> None:
        last_status = 0.0
        while not self._stop.is_set():
            if self.run_for and time.monotonic() - self.started_at >= self.run_for:
                self.log("run duration reached")
                return
            for channel in self.channels:
                proc = channel.proc
                if proc is None:
                    continue
                if proc.poll() is None:
                    continue
                if self._stop.is_set():
                    return
                channel.restarts += 1
                if channel.restarts > self.max_restarts:
                    self.log(f"{channel.name}: gave up after {self.max_restarts} restarts "
                             f"(see {channel.work_dir / 'worker.log'})")
                    channel.proc = None
                    continue
                self.log(f"{channel.name}: worker exited ({proc.returncode}) — "
                         f"restarting ({channel.restarts}/{self.max_restarts})")
                time.sleep(min(2.0 * channel.restarts, 10.0))
                if not self._stop.is_set():
                    self._spawn(channel)

            if time.monotonic() - last_status > 5.0:
                last_status = time.monotonic()
                self._poll_states()
                self._status_line()
            time.sleep(0.5)

    def _status_line(self) -> None:
        parts = []
        for channel in self.channels:
            state = channel.state
            score = float(state.get("score", 0.0))
            filled = int(round(score * 6))
            bar = "▮" * filled + "▯" * (6 - filled)
            state_txt = "CLIP" if state.get("capturing") else "arm "
            parts.append(f"{channel.name[:10]:<10} {bar} {score:.2f} {state_txt}")
        self.log(" | ".join(parts))

    # -- worker telemetry ----------------------------------------------
    def _poll_states(self) -> None:
        for channel in self.channels:
            url = f"http://127.0.0.1:{channel.port}/api/state"
            try:
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    channel.state = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                if channel.proc is None or channel.proc.poll() is not None:
                    channel.state = {}
        self._last_poll = time.monotonic()

    @staticmethod
    def _clips(channel: Channel) -> list[Path]:
        try:
            return sorted(channel.out_dir.glob("*.mp4"))
        except OSError:
            return []

    def overview(self) -> dict:
        self._poll_states()
        channels = []
        for channel in self.channels:
            state = channel.state or {}
            channels.append({
                "name": channel.name,
                "port": channel.port,
                "connected": bool(state.get("connected")),
                "capturing": bool(state.get("capturing")),
                "cooldown": float(state.get("cooldown") or 0.0),
                "score": round(float(state.get("score") or 0.0), 3),
                "clips": len(self._clips(channel)),
                "chat_status": state.get("chat_status", "off"),
            })
        uptime = time.monotonic() - self.started_at
        return {
            "channels": channels,
            "master_port": self.master_port,
            "uptime": fmt_clock(uptime),
        }

    # -- console -------------------------------------------------------
    def _serve(self) -> None:
        master = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003
                return

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/overview"):
                    body = json.dumps(master.overview()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path in ("/", "/index.html"):
                    tiles = [{"name": c.name, "port": c.port} for c in master.channels]
                    body = GRID.replace("__CHANNELS__", json.dumps(tiles)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.master_port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                         daemon=True, name="master-console").start()
