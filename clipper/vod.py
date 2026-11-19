"""Clip a stream that has already ended.

The live engine clips off a rolling buffer as the stream plays: the buffer is
short, the clock is "now", and a trigger cuts whatever just happened. A past
stream is different in three ways, and this module handles each one:

* **There is no clock.** A VOD has a fixed length, so the whole thing is
  analysed once, up front, and the best moments are chosen by score.
* **There is no realtime pressure.** The audio is measured in one linear pass
  and the chat is fetched in parallel, then the clips are rendered one at a
  time. Quality wins over throughput.
* **The chat is not live.** Twitch keeps a VOD's chat on a timeline, so the
  same chat-rate novelty the live engine uses can be rebuilt offline.

Nothing here re-implements the renderer: a chosen moment is cut out of the VOD
as its own little file, wrapped in the same :class:`~clipper.cutter.Prepared`
object the live path produces, and handed to :meth:`Engine._render`. So the
frame, the captions, the hook, the cold open and the voiceover all behave
exactly as they do live.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import camera
from .config import Config
from .resolve import TWITCH_CLIENT_ID, TWITCH_GQL, _post_json
from .signals import (AGREE_BAR, AGREE_CAP, AUDIO_DELTA_SPAN, AUDIO_Z_FLOOR,
                      AUDIO_Z_SPAN, AUDIO_Z_WEIGHT, BURST_WINDOW, CHAT_Z_FLOOR,
                      CHAT_Z_SPAN, MIN_BASE, _keyword_hits, _median, _z_to_score)
from .util import FFMPEG, probe, run, safe_name

STEP = 1.0                     # seconds per analysis bucket
AUDIO_WINDOW = 3.0             # seconds of audio averaged into "now"
CHAT_CHUNK = 600.0             # seconds of VOD chat fetched by one worker
CHAT_WORKERS = 6
CHAT_PAGE = 100                # Twitch returns at most this many per request
CHAT_PAGE_CAP = 4000           # per-chunk safety valve
CHAT_MSG_CAP = 500_000         # stop before memory becomes the problem
CHAT_BUDGET = 900.0            # seconds of wall clock for the whole chat fetch

TWITCH_VOD = re.compile(r"twitch\.tv/videos/(\d+)", re.I)
YT_HOST = re.compile(r"(?:youtube\.com|youtu\.be)/", re.I)
MEDIA = re.compile(r"\.(?:m3u8|mp4|flv|ts|mkv|webm)(?:\?|$)", re.I)

TWITCH_TOKEN_Q = (
    'query($id:ID!){videoPlaybackAccessToken(id:$id, params:{'
    'platform:"web", playerBackend:"mediaplayer", playerType:"site"})'
    '{value signature}}'
)
TWITCH_VIDEO_Q = (
    'query($id:ID!){video(id:$id){title lengthSeconds owner{login} '
    'creator{login}}}'
)
CHAT_FIRST_Q = (
    'query($id:ID!,$off:Int){video(id:$id){comments(first:%d, '
    'contentOffsetSeconds:$off){edges{cursor node{contentOffsetSeconds '
    'commenter{displayName} message{fragments{text}}}} pageInfo{hasNextPage}}}}}'
) % CHAT_PAGE
CHAT_NEXT_Q = (
    'query($id:ID!,$cur:Cursor){video(id:$id){comments(first:%d, '
    'after:$cur){edges{cursor node{contentOffsetSeconds '
    'commenter{displayName} message{fragments{text}}}} pageInfo{hasNextPage}}}}}'
) % CHAT_PAGE


@dataclass
class VodSource:
    """A resolved past stream: something ffmpeg can seek into, plus a timeline."""

    url: str
    duration: float
    kind: str                 # twitch | youtube | file | direct
    channel: str
    title: str = ""
    video_id: str | None = None
    note: str = ""


@dataclass
class Moment:
    t: float                  # the trigger second, in VOD time
    score: float
    end: float | None = None  # end of the sustained burst, when it matters


# --------------------------------------------------------------------------- #
# resolving a link into a playable, seekable source
# --------------------------------------------------------------------------- #

def _twitch_gql(query: str, variables: dict) -> dict:
    try:
        return _post_json(TWITCH_GQL, {"query": query, "variables": variables},
                          {"Client-ID": TWITCH_CLIENT_ID})
    except Exception as exc:  # noqa: BLE001 - surfaced as one clear sentence
        raise RuntimeError(f"Twitch did not answer ({type(exc).__name__}: {exc})") from exc


def _twitch_vod_source(video_id: str, log=lambda m: None) -> VodSource:
    token = _twitch_gql(TWITCH_TOKEN_Q, {"id": video_id})
    access = (token.get("data") or {}).get("videoPlaybackAccessToken")
    if not access:
        raise RuntimeError("Twitch would not hand out a playback token for that VOD")
    value, signature = access["value"], access["signature"]
    from urllib.parse import quote

    url = (
        f"https://usher.ttvnw.net/vod/{video_id}.m3u8"
        f"?client_id={TWITCH_CLIENT_ID}"
        f"&token={quote(value, safe='')}&sig={quote(signature, safe='')}"
        "&allow_source=true&player=twitchweb"
    )
    title, duration, channel = "", 0.0, ""
    try:
        meta = _twitch_gql(TWITCH_VIDEO_Q, {"id": video_id})
        node = (meta.get("data") or {}).get("video") or {}
        title = str(node.get("title") or "")
        duration = float(node.get("lengthSeconds") or 0.0)
        owner = node.get("owner") or node.get("creator") or {}
        channel = str(owner.get("login") or "")
    except Exception:  # noqa: BLE001 - metadata is a nicety, playback is the point
        pass
    if not channel:
        try:
            channel = str(json.loads(value).get("channel_name") or "")
        except (ValueError, TypeError):
            channel = ""
    if duration <= 0:
        duration = float(probe(url).get("duration") or 0.0)
    return VodSource(url=url, duration=duration, kind="twitch",
                     channel=channel or "vod", title=title, video_id=video_id,
                     note=f"twitch VOD {video_id}")


def _ytdlp_source(link: str, log=lambda m: None) -> VodSource:
    from .resolve import has_ytdlp

    if not has_ytdlp():
        raise RuntimeError(
            "that link needs yt-dlp — install it (pip install yt-dlp or "
            "winget install yt-dlp), or pass a local file / .m3u8")
    meta = run(["yt-dlp", "--no-warnings", "--no-playlist",
                "--print", "%(duration)s", "--print", "%(title)s",
                "--print", "%(channel)s", link], timeout=120)
    lines = (meta.stdout or "").splitlines()
    duration = 0.0
    title = lines[1] if len(lines) > 1 else ""
    channel = lines[2] if len(lines) > 2 else ""
    if len(lines) > 0:
        try:
            duration = float(lines[0])
        except ValueError:
            duration = 0.0
    resolved = run(["yt-dlp", "--no-warnings", "--no-playlist", "-g",
                    "-f", "best[protocol^=m3u8]/best", link], timeout=180)
    if resolved.returncode != 0 or not (resolved.stdout or "").strip():
        raise RuntimeError("yt-dlp could not resolve that link: "
                           + (resolved.stderr or "").strip()[-200:])
    url = resolved.stdout.strip().splitlines()[0]
    if duration <= 0:
        duration = float(probe(url).get("duration") or 0.0)
    channel = "" if channel in ("", "NA") else channel
    title = "" if title == "NA" else title
    return VodSource(url=url, duration=duration,
                     kind="youtube" if YT_HOST.search(link) else "direct",
                     channel=channel or "vod", title=title, note="yt-dlp")


def resolve_vod(link: str, work_dir: Path, log=lambda m: None) -> VodSource:
    """Turn a link (or a local recording) into a seekable source with a length."""
    link = (link or "").strip().strip("'\"")
    if not link:
        raise ValueError("paste a link to a past stream first")

    if Path(link).exists() and not link.lower().startswith(("http://", "https://")):
        info = probe(link)
        duration = float(info.get("duration") or 0.0)
        if duration <= 0:
            raise RuntimeError(f"could not read a duration from {link}")
        return VodSource(url=str(Path(link)), duration=duration, kind="file",
                         channel=Path(link).stem, title=Path(link).name,
                         note="local file")

    vod = TWITCH_VOD.search(link)
    if vod:
        return _twitch_vod_source(vod.group(1), log=log)

    if link.lower().startswith(("http://", "https://")):
        if MEDIA.search(link):
            info = probe(link)
            duration = float(info.get("duration") or 0.0)
            if duration <= 0:
                raise RuntimeError(
                    "that media link has no known length — it may be a live "
                    "playlist; use `clipper run` for live streams")
            return VodSource(url=link, duration=duration, kind="direct",
                             channel="stream", note="direct media")
        return _ytdlp_source(link, log=log)

    raise ValueError(
        "paste a twitch.tv/videos/... or youtube.com link, a local recording, "
        "or a .m3u8 URL")


# --------------------------------------------------------------------------- #
# Twitch VOD chat
# --------------------------------------------------------------------------- #

def _chat_page(video_id: str, offset: int | None = None,
               cursor: str | None = None):
    """One page of VOD chat. Returns (rows, next_cursor, has_next)."""
    try:
        if cursor:
            data = _twitch_gql(CHAT_NEXT_Q, {"id": video_id, "cur": cursor})
        else:
            data = _twitch_gql(CHAT_FIRST_Q, {"id": video_id, "off": offset or 0})
    except Exception:  # noqa: BLE001 - a flaky page just ends that chunk
        return [], None, False
    comments = ((data.get("data") or {}).get("video") or {}).get("comments")
    if not comments:
        return [], None, False
    rows = []
    for edge in comments.get("edges") or []:
        node = edge.get("node") or {}
        try:
            t = int(node.get("contentOffsetSeconds") or 0)
        except (TypeError, ValueError):
            continue
        user = ((node.get("commenter") or {}).get("displayName")
                or (node.get("commenter") or {}).get("name") or "")
        text = "".join(frag.get("text", "")
                       for frag in (node.get("message") or {}).get("fragments") or [])
        rows.append((t, str(user), str(text)))
    last = (comments.get("edges") or [{}])[-1].get("cursor") if comments.get("edges") else None
    has_next = bool((comments.get("pageInfo") or {}).get("hasNextPage"))
    return rows, last, has_next


def _chat_chunk(video_id: str, start: float, end: float, stop: threading.Event,
                budget: float):
    """Every message in ``[start, end)``, following the cursor forward."""
    rows: list[tuple[int, str, str]] = []
    cursor = None
    pages = 0
    deadline = time.monotonic() + budget
    while not stop.is_set():
        if time.monotonic() > deadline:
            break
        page, cursor, has_next = _chat_page(video_id, offset=int(start), cursor=cursor)
        pages += 1
        if not page:
            break
        for row in page:
            if row[0] >= end:
                return rows
            rows.append(row)
        if not has_next or cursor is None or pages >= CHAT_PAGE_CAP:
            break
    return rows


def fetch_twitch_chat(video_id: str, duration: float, work_dir: Path,
                      log=lambda m: None, cache: Path | None = None
                      ) -> list[tuple[float, str, str]]:
    """The VOD's whole chat, in parallel chunks, cached to disk.

    A VOD that ends has its chat stored on the same timeline, so the fastest
    honest read is to split the timeline into chunks and page each chunk
    forward independently. The result is sorted, de-duplicated and cached, so
    re-running an analysis is instant.
    """
    cache = cache or (Path(work_dir) / "vod" / f"{video_id}.chat.jsonl.gz")
    if cache.exists():
        cached = _load_chat_cache(cache)
        if cached:
            log(f"vod chat: {len(cached)} message(s) restored from cache")
            return cached

    span = max(1.0, float(duration))
    bounds = [(i * CHAT_CHUNK, min(span, (i + 1) * CHAT_CHUNK))
              for i in range(int(span // CHAT_CHUNK) + 1)]
    stop = threading.Event()
    collected: list[tuple[int, str, str]] = []
    lock = threading.Lock()
    done = 0

    def work(chunk):
        return _chat_chunk(video_id, chunk[0], chunk[1], stop, CHAT_BUDGET)

    log(f"vod chat: reading {len(bounds)} chunk(s) from Twitch…")
    with ThreadPoolExecutor(max_workers=CHAT_WORKERS) as pool:
        futures = {pool.submit(work, chunk): chunk for chunk in bounds}
        try:
            for future in as_completed(futures, timeout=CHAT_BUDGET + 120):
                rows = future.result()
                with lock:
                    collected.extend(rows)
                    done += 1
                    if done % 12 == 0:
                        log(f"vod chat: {done}/{len(bounds)} chunk(s), "
                            f"{len(collected)} message(s)")
                    if len(collected) >= CHAT_MSG_CAP:
                        stop.set()
        except Exception as exc:  # noqa: BLE001 - partial chat is still useful
            log(f"vod chat: stopped early ({exc})")

    collected.sort(key=lambda row: row[0])
    # Chunks overlap at their edges; one message should count once.
    seen: set[tuple[int, str, str]] = set()
    unique: list[tuple[int, str, str]] = []
    for row in collected:
        key = row
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= CHAT_MSG_CAP:
            break
    if len(unique) >= CHAT_MSG_CAP:
        log(f"vod chat: capped at {CHAT_MSG_CAP} message(s)")
    _save_chat_cache(cache, unique)
    log(f"vod chat: {len(unique)} message(s)")
    return unique


def _load_chat_cache(path: Path) -> list[tuple[float, str, str]]:
    rows: list[tuple[float, str, str]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    t, user, text = json.loads(line)
                except (ValueError, TypeError):
                    continue
                rows.append((float(t), str(user), str(text)))
    except (OSError, EOFError):
        return []
    return rows


def _save_chat_cache(path: Path, rows) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for t, user, text in rows:
                fh.write(json.dumps([t, user, text]) + "\n")
        tmp.replace(path)
    except OSError:
        pass


def chat_from_file(path: str) -> list[tuple[float, str, str]]:
    """Read a recorded chat (``--chat file:...``) for a VOD analysis."""
    rows: list[tuple[float, str, str]] = []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append((float(obj["t"]), str(obj.get("user", "")),
                                 str(obj.get("text", ""))))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: row[0])
    return rows


# --------------------------------------------------------------------------- #
# audio loudness over the whole VOD
# --------------------------------------------------------------------------- #

_ASTATS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|inf|-inf)")


def audio_levels(source: str, work_dir: Path, duration: float,
                 log=lambda m: None, cache: Path | None = None) -> list[float]:
    """One RMS value per second of the VOD, in the speech band.

    ffmpeg does the decoding and the maths in C: resample to 8 kHz, band-limit
    to speech, then let ``astats`` report the RMS of each exactly-one-second
    frame. Streaming the numbers back avoids a second of Python per second of
    video.
    """
    cache = cache or (Path(work_dir) / "vod"
                      / (safe_name(Path(str(source)).name or "vod", 40)
                         + f"_{int(duration)}.levels.json"))
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                log(f"vod audio: {len(data)}s restored from cache")
                return [float(v) for v in data]
        except (OSError, ValueError):
            pass

    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn",
        "-af", ("aresample=8000,highpass=f=200,lowpass=f=3400,"
                "asetnsamples=n=8000,astats=metadata=1:reset=1,"
                "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"),
        "-f", "null", "-",
    ]
    levels: list[float] = []
    log(f"vod audio: measuring loudness over {duration/3600:.1f}h…")
    proc = None
    try:
        import subprocess

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        assert proc.stdout is not None
        for raw in proc.stdout:
            match = _ASTATS.search(raw.decode("utf-8", "replace"))
            if not match:
                continue
            token = match.group(1)
            value = -90.0 if token in ("inf", "-inf") else float(token)
            levels.append(value)
            if len(levels) % 1800 == 0:
                log(f"vod audio: {len(levels)/3600:.1f}h measured")
        proc.wait(timeout=60)
    except Exception as exc:  # noqa: BLE001 - a partial curve still ranks moments
        log(f"vod audio: measurement stopped ({exc})")
        if proc is not None and proc.poll() is None:
            proc.terminate()
    if not levels:
        return []
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([round(v, 2) for v in levels]), encoding="utf-8")
    except OSError:
        pass
    log(f"vod audio: {len(levels)}s measured")
    return levels


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def build_curve(levels: list[float], chat, cfg: Config,
                step: float = STEP) -> list[float]:
    """A per-second 0..1 hype score across the whole VOD.

    The same novelty idea the live engine uses, applied to a finished timeline:
    robust (median/MAD) z-scores of the speech-band loudness, of the chat's
    unique-user count and of chat consensus, fused with the configured weights
    and gated so one modality alone cannot run away with it.
    """
    n = len(levels)
    if n == 0:
        return []

    chat = chat or []
    users = [0] * n
    consensus = [0] * n
    hits = [0] * n
    per_user: list[set] = [set() for _ in range(n)]
    per_token: list[dict] = [defaultdict(set) for _ in range(n)]
    for t, user, text in chat:
        index = int(t // step)
        if not 0 <= index < n:
            continue
        per_user[index].add(user)
        low = text.lower()
        for token in set(re.findall(r"[a-z']+", low)):
            per_token[index][token].add(user)
        hits[index] += _keyword_hits(text)
    for i in range(n):
        users[i] = len(per_user[i])
        consensus[i] = max((len(who) for who in per_token[i].values()), default=0)
    del per_user, per_token

    window = max(1, int(round(cfg.chat_window / step)))
    audio_win = max(1, int(round(AUDIO_WINDOW / step)))

    users_win = _rolling_sum(users, window)
    hits_win = _rolling_sum(hits, window)
    cons_win = _rolling_max(consensus, window)
    audio_win_levels = _rolling_mean(levels, audio_win)

    users_z = _robust_z_series(users_win, 1.0)
    cons_z = _robust_z_series(cons_win, 1.0)
    audio_z = _robust_z_series(audio_win_levels, 1.0)
    audio_med = _median(audio_win_levels)

    chat_score = [
        max(_z_to_score(users_z[i], CHAT_Z_FLOOR, CHAT_Z_SPAN),
            _z_to_score(cons_z[i], CHAT_Z_FLOOR, CHAT_Z_SPAN))
        for i in range(n)
    ]
    text_score = [
        min(1.0, hits_win[i] / 6.0) * min(1.0, users_win[i] / 3.0)
        for i in range(n)
    ]
    audio_score = []
    for i in range(n):
        z_part = _z_to_score(audio_z[i], AUDIO_Z_FLOOR, AUDIO_Z_SPAN)
        delta = audio_win_levels[i] - audio_med
        audio_score.append(min(1.0, max(0.0, AUDIO_Z_WEIGHT * z_part
                                        + (1.0 - AUDIO_Z_WEIGHT)
                                        * min(1.0, delta / AUDIO_DELTA_SPAN))))

    has_chat = bool(chat)
    has_audio = True
    raw: list[float] = []
    for i in range(n):
        parts: list[tuple[float, float]] = []      # (weight, score)
        groups: list[float] = []
        if has_chat:
            parts.append((cfg.w_chat, chat_score[i]))
            parts.append((cfg.w_text, text_score[i]))
            groups.append(max(chat_score[i], text_score[i]))
        if has_audio:
            parts.append((cfg.w_audio, audio_score[i]))
            groups.append(audio_score[i])
        total_w = sum(w for w, _ in parts) or 1.0
        mean = sum(w * s for w, s in parts) / total_w
        peak = max((s for _, s in parts), default=0.0)
        value = 0.6 * peak + 0.4 * mean
        required = cfg.agree or (2 if (has_chat and has_audio) else 1)
        agreeing = sum(1 for s in groups if s >= AGREE_BAR)
        if agreeing < required:
            value = min(value, AGREE_CAP)
        raw.append(value)

    sustained = _rolling_mean(raw, max(1, int(round(BURST_WINDOW / step))))
    return [round(v, 4) for v in sustained]


def _rolling_sum(values: list[float], window: int) -> list[float]:
    n = len(values)
    out = [0.0] * n
    acc = 0.0
    for i in range(n):
        acc += values[i]
        if i >= window:
            acc -= values[i - window]
        out[i] = acc
    return out


def _rolling_max(values: list[float], window: int) -> list[float]:
    n = len(values)
    out = [0.0] * n
    from collections import deque

    dq: deque[int] = deque()
    for i in range(n):
        while dq and values[dq[-1]] <= values[i]:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - window:
            dq.popleft()
        out[i] = values[dq[0]]
    return out


def _rolling_mean(values: list[float], window: int) -> list[float]:
    n = len(values)
    out = [0.0] * n
    acc = 0.0
    for i in range(n):
        acc += values[i]
        if i >= window:
            acc -= values[i - window]
        out[i] = acc / min(i + 1, window)
    return out


def _robust_z_series(values: list[float], floor_scale: float) -> list[float]:
    if len(values) < MIN_BASE:
        return [0.0] * len(values)
    med = _median(values)
    mad = _median([abs(v - med) for v in values])
    scale = max(1.4826 * mad, floor_scale)
    return [(v - med) / scale for v in values]


def pick_moments(curve: list[float], cfg: Config, step: float = STEP,
                 max_clips: int = 0) -> list[Moment]:
    """Where the sustained score clears the trigger, one clip per burst.

    Mirrors the live trigger: the score must stay above the threshold for the
    hold time, and clips keep the cooldown apart. Each burst contributes one
    clip, taken at its strongest second.
    """
    n = len(curve)
    if n == 0:
        return []
    hold = max(1, int(round(cfg.confirm / step)))
    cooldown = max(0, int(round(cfg.cooldown / step)))
    moments: list[Moment] = []
    i = 0
    last_index = -cooldown - 1
    while i < n:
        if curve[i] < cfg.threshold:
            i += 1
            continue
        start = i
        while i < n and curve[i] >= cfg.threshold:
            i += 1
        end = i  # first index back under threshold
        if end - start < hold:
            continue
        peak = max(range(start, end), key=lambda k: curve[k])
        if peak < last_index + cooldown:
            continue
        moments.append(Moment(t=peak * step, score=curve[peak], end=end * step))
        last_index = peak
    if max_clips and len(moments) > max_clips:
        ranked = sorted(moments, key=lambda m: m.score, reverse=True)[:max_clips]
        moments = sorted(ranked, key=lambda m: m.t)
    return moments


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

def run(cfg: Config, link: str, *, max_clips: int = 15, chat_mode: str = "auto",
        log=None, should_stop=None, state: dict | None = None) -> int:
    """Analyse a past stream and render its best moments. Returns an exit code.

    ``state`` is an optional dict the caller can read for progress (the console
    uses it); ``should_stop`` is polled between steps so a job can be stopped
    without killing the process.
    """
    from .engine import Engine

    engine = Engine(cfg)
    log = log or engine.log
    state = state if state is not None else {}
    stop = should_stop or (lambda: False)

    def progress(**fields) -> None:
        state.update(fields)

    progress(phase="resolving", index=0, total=0, rendered=0, found=0, clips=0)
    try:
        source = resolve_vod(link, cfg.work_dir, log=log)
    except (RuntimeError, ValueError) as exc:
        progress(phase="error", error=str(exc))
        log(f"vod: {exc}")
        return 1
    if source.duration < 60:
        message = (f"that source is only {source.duration:.0f}s — "
                   "nothing worth clipping")
        progress(phase="error", error=message)
        log(f"vod: {message}")
        return 1

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    engine.channel = safe_name(cfg.name or source.channel or "vod", 24)
    engine._maybe_load_profile()
    engine._load_cam()
    log(f"vod: {source.note or source.kind} · {engine.channel} · "
        f"{source.duration/3600:.1f}h")
    if source.title:
        log(f"vod: \"{source.title[:80]}\"")

    # --- chat ---------------------------------------------------------
    chat: list[tuple[float, str, str]] = []
    if chat_mode != "off":
        progress(phase="chat")
        if cfg.chat and cfg.chat.lower().startswith("file:"):
            path = cfg.chat.split(":", 1)[1]
            chat = chat_from_file(path)
            log(f"vod chat: {len(chat)} message(s) from {Path(path).name}")
        elif source.kind == "twitch" and source.video_id:
            chat = fetch_twitch_chat(source.video_id, source.duration,
                                     cfg.work_dir, log=log)
        elif chat_mode == "on":
            log("vod chat: this source has no VOD chat — analysing audio only")
    if not chat:
        log("vod: no chat — the triggers are audio-led")

    # --- score --------------------------------------------------------
    progress(phase="analysing")
    levels = audio_levels(source.url, cfg.work_dir, source.duration, log=log)
    if not levels:
        progress(phase="error", error="could not measure the audio")
        log("vod: could not measure the audio — is the source playable?")
        return 1
    curve = build_curve(levels, chat, cfg)
    moments = pick_moments(curve, cfg, max_clips=max_clips)
    progress(found=len(moments), total=len(moments))
    log(f"vod: analysing {len(levels)}s → {len(moments)} moment(s) "
        f"at threshold {cfg.threshold:.2f}")

    # --- camera -------------------------------------------------------
    if (not stop() and cfg.layout == "split" and getattr(engine, "_cam_setting", "auto")
            == "auto" and camera.available()):
        seek = moments[0].t if moments else min(source.duration * 0.25, 1800.0)
        box = camera.detect(source.url, cfg.work_dir, seek=seek, log=log)
        if box:
            engine.adopt_cam(box)
        else:
            log("vod: no webcam found — split clips will use the blurred frame")

    # --- render -------------------------------------------------------
    if not moments:
        progress(phase="done", clips=0)
        log("vod: no clip-worthy moments — try a lower --threshold")
        return 0

    subtitles = engine.cutter.subtitle_enabled()
    progress(phase="rendering", clips=0)
    log(f"vod: rendering {len(moments)} clip(s)"
        + (" with captions" if subtitles else ""))
    rendered = 0
    for index, moment in enumerate(moments, 1):
        if stop():
            log("vod: stopped — the clips made so far are kept")
            break
        start = max(0.0, moment.t - cfg.pre)
        end = min(source.duration, moment.t + cfg.post)
        if moment.end:
            end = min(source.duration, max(end, moment.end),
                      moment.t + cfg.max_post)
        progress(index=index, clips=rendered)
        log(f"vod: clip {index}/{len(moments)} — cutting "
            f"{_clock(start)}–{_clock(end)} (score {moment.score:.2f})")
        if _render_moment(engine, source, start, end, moment, chat, subtitles):
            rendered += 1
    progress(phase="done", clips=rendered, index=min(rendered, len(moments)),
             total=len(moments))
    log(f"vod: finished — {rendered} clip(s) → {cfg.out_dir}")
    return 0


def _render_moment(engine, source: VodSource, start: float, end: float,
                   moment: Moment, chat, subtitles: bool) -> bool:
    """Cut one moment out of the VOD and run it through the normal renderer."""
    job_id = f"vod_{int(time.time() * 1000)}"
    prepared = engine.cutter.prepare_window(source.url, start, end, job_id,
                                            log=engine.log)
    if prepared is None:
        engine.log("vod: the moment could not be cut — skipping")
        return False
    window = [{"t": round(t, 1), "user": user, "text": text}
              for t, user, text in chat if start <= t <= end]
    meta = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stamp": time.strftime("%Y%m%d-%H%M%S"),
        "source": source.url if source.kind != "file" else str(source.url),
        "vod": source.note or source.kind,
        "vod_title": source.title,
        "channel": engine.channel,
        "layout": engine.cfg.layout,
        "stream_start": round(start, 2),
        "stream_end": round(end, 2),
        "duration_s": round(end - start, 2),
        "score": round(moment.score, 3),
        "forced": False,
        "signals": {"t": round(moment.t, 3), "score": round(moment.score, 3)},
        "title": "",
        "chat_excerpt": [],
        "clipper_version": __import__("clipper").__version__,
    }
    meta["chat_excerpt"] = window[-14:]
    engine._render(prepared, meta, subtitles, window)
    return bool(meta.get("file"))


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"
