"""Cut a clip out of the rolling buffer and render it for posting."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, DEFAULT_CAM, parse_cam
from .hls import Segment
from .util import FFMPEG, find_font, pick_encoder, probe, run, safe_name
from . import caption, transcribe

# Phrases that actually signal a moment. The hook is pulled from the clip's own
# transcript, so it says something about *this* clip. A generic "NO WAY" fires on
# every streamer alive; naming what he said is what makes a viewer stop.
HOOK_PHRASES = (
    "oh my god", "what the", "what is he doing", "are you serious",
    "you're trolling", "you are trolling", "im done", "i'm done", "i am done",
    "lets go", "let's go", "no shot", "you're kidding", "shut up", "bro what",
    "no way", "no no no", "wait what", "what happened", "get out of here",
    "what is happening", "he's so bad", "he is so bad",
    "i can't", "i cant", "look at this", "watch this",
)


@dataclass
class Prepared:
    work: Path
    list_path: Path
    offset: float
    duration: float
    width: int
    height: int
    title_file: Path | None


def hook_from_cues(cues) -> str:
    """Pull the most quotable short phrase out of the clip's own transcript.

    Returns "" when nothing quotable was said — an honest empty hook beats a
    caption that says nothing about the clip.
    """
    text = " ".join(c.text for c in cues).lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    best = ""
    for phrase in HOOK_PHRASES:
        if phrase in text and len(phrase) > len(best):
            best = phrase
    return best.upper() if best else ""


# Chat repeats the thing the clip is actually about ("LOOK AT THE MAP" three
# times from three people). Those repeats make a far better hook than a generic
# "NO WAY", and they are still grounded in this clip. Very short messages are
# noise: emoji, "W", "LOL", one-word reactions.
CHAT_STOPWORDS = {
    "A", "AN", "THE", "U", "UR", "YOU", "IS", "ARE", "WAS", "WERE", "AM", "BE",
    "AT", "ON", "IN", "OF", "TO", "IT", "AND", "OR", "FOR", "THIS", "THAT",
    "MY", "ME", "HE", "SHE", "HIS", "HER", "SO", "JUST", "DO", "DONT", "NOT",
    "WITH", "AS", "IF", "BUT", "NOW", "WHY", "HOW", "WHAT", "WHO",
}
CHAT_NOISE = {
    "LOL", "LMAO", "LMFAO", "KEKW", "KEK", "POG", "POGGERS", "W", "L", "F",
    "OMG", "WTF", "WTH", "GG", "EZ", "CLIP", "CLIP IT", "OMEGALUL", "LUL",
    "TRUE", "REAL", "FAX", "BASED", "SUS", "COPE", "MALD", "RATIO",
}
CHAT_PROFANITY = {
    "FUCK", "FUCKING", "FUCKED", "SHIT", "BITCH", "CUNT", "NIGGA", "NIGGER",
    "RETARD", "RETARDED", "SPED", "DICK", "PUSSY", "ASS", "ASSHOLE",
}


def _chat_text(message) -> tuple[str, str]:
    if isinstance(message, dict):
        return str(message.get("text") or ""), str(message.get("user") or "")
    return str(message or ""), ""


def _bad_phrase(phrase: str) -> bool:
    words = phrase.split()
    if len(phrase) < 6 or len(words) < 2:
        return True
    if any(word in CHAT_PROFANITY for word in words):
        return True
    if phrase in CHAT_NOISE:
        return True
    return all(word in CHAT_STOPWORDS for word in words)


def hook_from_chat(chat) -> str:
    """The phrase chat keeps repeating in this clip, or "" when there is none.

    Counts 2-4 word n-grams across messages and needs at least two messages from
    two different users, so one loud person cannot invent a hook. Returns an
    uppercase phrase without the "chat says" framing.
    """
    from collections import Counter, defaultdict

    counts: Counter[str] = Counter()
    users: defaultdict[str, set[str]] = defaultdict(set)
    for index, message in enumerate(chat or ()):
        text, user = _chat_text(message)
        text = re.sub(r"[^A-Za-z0-9' ]+", " ", text.upper())
        words = re.sub(r"\s+", " ", text).strip().split()
        if not 2 <= len(words) <= 7:
            continue
        who = user or f"#{index}"
        for size in (4, 3, 2):
            for start in range(len(words) - size + 1):
                phrase = " ".join(words[start:start + size])
                if _bad_phrase(phrase):
                    continue
                counts[phrase] += 1
                users[phrase].add(who)

    best, best_score = "", 0.0
    for phrase, count in counts.items():
        people = len(users[phrase])
        if count < 2 or people < 2:
            continue
        # Favour the phrase with the most agreement, then the longer phrasing:
        # "LOOK AT THE MAP" beats the "LOOK AT" it contains.
        score = count * people + len(phrase) / 1000.0
        if score > best_score or (score == best_score and len(phrase) > len(best)):
            best, best_score = phrase, score
    return best


# Hooks can borrow one emoji from the audience, never invent their own: only an
# emoji that at least two different people typed in this clip's chat, and only
# from a small set that reads as a reaction rather than decoration.
REACTION_EMOJI = ("🔥", "💀", "😂", "🤯", "😱", "🥶", "🗿", "💯", "👀")


def chat_reaction(chat) -> str:
    """The reaction emoji chat actually used in this clip, or ""."""
    from collections import defaultdict

    users: defaultdict[str, set[str]] = defaultdict(set)
    for index, message in enumerate(chat or ()):
        text, user = _chat_text(message)
        who = user or f"#{index}"
        for ch in text:
            if ch in REACTION_EMOJI:
                users[ch].add(who)
    best = ""
    for emoji, people in users.items():
        if len(people) >= 2 and len(people) > len(users.get(best, ())):
            best = emoji
    return best


def hook_keyword(chat) -> str:
    """One word chat keeps shouting on its own ("BACK", "MAP", "COOKED").

    A single word is only trusted when several different people typed it as a
    message of its own — that is consensus, not one person spamming. It is the
    last resort after a repeated phrase, a question and a quotable line, but it
    is still grounded in this clip.
    """
    from collections import Counter, defaultdict

    counts: Counter[str] = Counter()
    users: defaultdict[str, set[str]] = defaultdict(set)
    for index, message in enumerate(chat or ()):
        text, user = _chat_text(message)
        text = re.sub(r"[^A-Za-z0-9' ]+", " ", text.upper())
        words = re.sub(r"\s+", " ", text).strip().split()
        if len(words) != 1:
            continue
        word = words[0]
        if (len(word) < 4 or word in CHAT_NOISE or word in CHAT_STOPWORDS
                or word in CHAT_PROFANITY):
            continue
        counts[word] += 1
        users[word].add(user or f"#{index}")

    best, best_score = "", 0
    for word, count in counts.items():
        people = len(users[word])
        if count < 3 or people < 3:
            continue
        score = count * people
        if score > best_score or (score == best_score and len(word) > len(best)):
            best, best_score = word, score
    return best


QUESTION_STARTS = {
    "IS", "ARE", "WAS", "WERE", "AM", "DO", "DOES", "DID", "CAN", "COULD",
    "WOULD", "WILL", "SHOULD", "WHAT", "WHERE", "WHY", "HOW", "WHO", "WHEN",
    "WHOSE", "WHICH", "HAVE", "HAS", "HAD",
}


def hook_question(cues, max_words: int = 8, max_chars: int = 52) -> str:
    """The shortest genuine question in the clip, or "".

    A question is what a viewer actually clicks on ("is this the bridge I
    jumped off?"), so it beats a generic reaction like "HOLY". Whisper often
    packs several sentences into one cue, so clauses are split on punctuation
    before looking for the question; when the recogniser dropped the question
    mark, an interrogative opening word counts too. Long questions are cut to
    the first ``max_words`` words — the extra words are usually a mangled name
    or a trailing aside, and the question still reads.
    """
    for cue in cues or ():
        text = re.sub(r"\s+", " ", str(cue.text)).strip()
        for clause in re.split(r"(?<=[.!?])\s+", text):
            clause = clause.strip()
            if len(clause) < 6:
                continue
            words = clause.split()
            if not 3 <= len(words) <= max_words * 2:
                continue
            opening = re.sub(r"[^A-Za-z]", "", words[0]).upper()
            if "?" not in clause and opening not in QUESTION_STARTS:
                continue
            if len(words) > max_words:
                words = words[:max_words]
                clause = " ".join(words) + "?"
            clause = " ".join(clause.split())
            clause = re.sub(r"[^A-Za-z0-9'!? ]+$", "", clause).strip()
            if not clause.endswith("?"):
                clause += "?"
            if len(clause) > max_chars:
                continue
            return clause.upper()
    return ""


def pick_title(cfg: Config, signals: dict, cues=(), chat=()) -> str:
    if cfg.title_mode == "none":
        return ""
    if cfg.title_mode == "fixed":
        return cfg.title or ""
    hook = ""
    repeated = hook_from_chat(chat)
    if repeated:
        hook = f"CHAT SAYS {repeated}"
    if not hook:
        hook = hook_question(cues)
    if not hook:
        hook = hook_from_cues(cues)
    if not hook:
        keyword = hook_keyword(chat)
        if keyword:
            hook = f"CHAT SAYS {keyword}"
    if not hook:
        return ""
    reaction = chat_reaction(chat)
    return f"{hook} {reaction}" if reaction else hook


def _wrap(text: str, width: int = 20) -> str:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines[:3])


# -- `--layout split` geometry, on a 1080x1920 frame -------------------------
SPLIT_CAM_H = 760     # camera band across the top (when the game cannot be cropped)
SPLIT_GAME_H = 608    # 16:9 gameplay at 1080 wide
SPLIT_BOT_MIN = 440   # space kept below the game for the hook and subtitles

# Candidate verification runs while the loop is live, so it gets fewer threads
# than subtitle transcription — it just needs to be quick, not perfect.
VERIFY_THREADS = 2


# Only one ffmpeg render at a time across every worker. Two 1080x1920 encodes
# at once is enough to trip macOS jetsam on an 8 GB laptop, and the retry then
# drops captions from a clip that would have rendered fine on its own. The lock
# is per machine (TMPDIR), released when the handle closes.
def _render_lock(timeout: float = 900.0):
    try:
        import fcntl
    except ImportError:  # Windows: no advisory locks, just render
        return None
    path = Path(os.environ.get("TMPDIR") or "/tmp") / "clipper-render.lock"
    try:
        handle = open(path, "a+")
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.5)


def _even(value: float) -> int:
    return max(2, int(round(value)) - int(round(value)) % 2)


@dataclass(frozen=True)
class SplitGeometry:
    cam_h: int
    game_h: int
    bot_h: int
    # (x, y, w, h) of the gameplay crop in source pixels, or None to keep the
    # whole frame and blur the webcam inset instead.
    crop: tuple[int, int, int, int] | None = None


def split_geometry(cam: str = DEFAULT_CAM, src_w: int = 1920,
                   src_h: int = 1080) -> SplitGeometry:
    """Work out the three bands, cropping the game away from the webcam side.

    Streamers park the webcam inset in a corner of the 16:9 frame. When that
    corner is anchored to the left or right edge, the gameplay is cropped to the
    rest of the width: his face is already across the top, so the inset is dead
    pixels, and the narrower crop makes the game fill more of the vertical
    frame. If the inset floats in the middle, the frame is kept whole and the
    inset is blurred out (the old behaviour).
    """
    x, y, w, h = parse_cam(cam)
    src_w = int(src_w or 1920)
    src_h = int(src_h or 1080)
    pad = 10
    crop: tuple[int, int, int, int] | None = None

    if x >= src_w * 0.6:
        # Webcam on the right: keep everything to the left of it.
        width = _even(max(src_w * 0.55, x - pad))
        if width >= src_w * 0.55:
            crop = (0, 0, width, src_h)
    elif x + w <= src_w * 0.4:
        # Webcam on the left: keep everything to the right of it.
        left = _even(min(x + w + pad, src_w * 0.45))
        if src_w - left >= src_w * 0.55:
            crop = (left, 0, _even(src_w - left), src_h)

    if crop:
        cw, ch = crop[2], crop[3]
        game_h = max(540, min(960, _even(1080 * ch / cw)))
        cam_h = max(480, min(720, 1920 - game_h - SPLIT_BOT_MIN))
        bot_h = 1920 - cam_h - game_h
        return SplitGeometry(cam_h, game_h, bot_h, crop)

    return SplitGeometry(SPLIT_CAM_H, SPLIT_GAME_H,
                         1920 - SPLIT_CAM_H - SPLIT_GAME_H, None)


def split_graph(cam: str = DEFAULT_CAM, src_w: int = 1920,
                src_h: int = 1080) -> list[str]:
    """Filter parts for cam-on-top / gameplay-middle / captions-below.

    The webcam inset is cropped out of the source and shown across the top over
    a blurred fill of itself. The gameplay comes next, cropped away from the
    webcam side when the inset sits on an edge, so the game fills the frame and
    the duplicate face is not shown. The band left over carries the burned
    subtitles and the hook caption.
    """
    geo = split_geometry(cam, src_w, src_h)
    x, y, w, h = parse_cam(cam)
    crop = f"crop={w}:{h}:{x}:{y}"
    # A webcam inset is small. Filling a 1080-wide band with it is a big
    # upscale, and soft footage reads as "broken" next to crisp gameplay, so
    # give the face band a little edge back when it is stretched hard.
    sharpen = ""
    if h > 0 and geo.cam_h / h >= 2.2:
        sharpen = ",unsharp=5:5:0.8:5:5:0.0"

    parts = [
        "[0:v]split=4[c1][c2][c3][c4]" if geo.crop
        else "[0:v]split=5[c1][c2][c3][c4][c5]",
        f"[c1]{crop},scale=1080:{geo.cam_h}:force_original_aspect_ratio=increase,"
        f"crop=1080:{geo.cam_h},gblur=sigma=18,eq=brightness=-0.2[cambg]",
        f"[c2]{crop},scale=-2:{geo.cam_h}{sharpen}[camfg]",
        "[cambg][camfg]overlay=(W-w)/2:(H-h)/2,format=yuv420p,setsar=1[top]",
    ]

    bot_input = "[c4]"
    if geo.crop:
        cx, cy, cw, ch = geo.crop
        parts.append(
            f"[c3]crop={cw}:{ch}:{cx}:{cy},"
            f"scale=1080:{geo.game_h}:force_original_aspect_ratio=increase,"
            f"crop=1080:{geo.game_h},format=yuv420p,setsar=1[game]"
        )
    else:
        # No clean side to crop: scale the whole frame and blur the duplicate
        # webcam inset that is baked into it.
        scale = min(1080 / (src_w or 1920), geo.game_h / (src_h or 1080))
        sx, sy = _even(max(0, x - 6)), _even(max(0, y - 6))
        sw, sh = _even(w + 12), _even(h + 12)
        ox, oy = _even(sx * scale), _even(sy * scale)
        ow, oh = _even(sw * scale), _even(sh * scale)
        bot_input = "[c5]"
        parts.append(
            f"[c3]scale=1080:{geo.game_h}:force_original_aspect_ratio=decrease[game0]"
        )
        parts.append(
            f"[c4]crop={sw}:{sh}:{sx}:{sy},scale={ow}:{oh},gblur=sigma=8[patchblur]"
        )
        parts.append(
            f"[game0][patchblur]overlay={ox}:{oy},format=yuv420p,setsar=1[game]"
        )

    parts.append(
        f"{bot_input}scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"gblur=sigma=8,scale=1080:{geo.bot_h},eq=brightness=-0.3,"
        "format=yuv420p,setsar=1[bot]"
    )
    parts.append("[top][game]vstack=inputs=2[tg]")
    parts.append("[tg][bot]vstack=inputs=2[vbase]")
    return parts


class Cutter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.encoder = pick_encoder(cfg.encoder)
        self.tmp = cfg.work_dir / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    # -- step 1: snapshot segments off the live buffer (fast, no encoding) --
    def prepare(self, segments: list[Segment], a: float, b: float, job_id: str) -> Prepared | None:
        usable = [s for s in segments if s.path.exists()]
        if not usable:
            return None
        usable.sort(key=lambda s: s.seq)
        start = usable[0].start
        end = usable[-1].end
        begin = max(a, start)
        finish = min(b, end)
        if finish - begin < 1.0:
            return None

        work = self.tmp / job_id
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        lines = []
        for index, seg in enumerate(usable):
            dest = work / f"{index:04d}.ts"
            try:
                shutil.copy2(seg.path, dest)
            except OSError:
                continue
            lines.append(f"file '{dest.resolve().as_posix()}'")
        if not lines:
            return None
        list_path = work / "concat.txt"
        list_path.write_text("\n".join(lines) + "\n")

        info = probe(usable[0].path)
        return Prepared(
            work=work,
            list_path=list_path,
            offset=begin - start,
            duration=finish - begin,
            width=info.get("width") or 0,
            height=info.get("height") or 0,
            title_file=work / "title.txt",
        )

    # -- step 2: render -----------------------------------------------------
    def render(self, prepared: Prepared, out_path: Path, title: str,
               cues: list | None = None) -> tuple[bool, str, str]:
        """Render the clip. Returns ``(ok, error, warning)``.

        Captions are nice to have, a clip is not — but subtitles are the whole
        point here, so degrade in priority order instead of dropping everything
        on the first failure: keep subtitles and drop the hook caption, then
        drop subtitles, then render bare. Each retry is a fresh ffmpeg process,
        which also rides out the transient jetsam kills this 8 GB machine does.
        """
        # Keep the object as-is: it may be a ``Cues`` carrying word timings that
        # the popping captions need, which ``list(...)`` would throw away.
        cues = cues if cues is not None else []
        tmp_out = prepared.work / "render.mp4"

        # Try the full-quality render twice before degrading: a jetsam kill is
        # transient, and dropping the hook or subtitles on the first failure is
        # what made good clips come out bare.
        plan: list[tuple[str, list, str]] = [(title, cues, ""), (title, cues, "")]
        seen: set[tuple[bool, bool]] = {(bool(title), bool(cues))}

        def add(attempt_title: str, attempt_cues: list, note: str) -> None:
            key = (bool(attempt_title), bool(attempt_cues))
            if key not in seen:
                seen.add(key)
                plan.append((attempt_title, attempt_cues, note))

        add("", cues, "hook caption dropped")
        add(title, [], "subtitles dropped")
        add("", [], "all captions dropped" if (title or cues) else "")

        warning = ""
        last_error = ""
        lock = _render_lock()
        try:
            for attempt_title, attempt_cues, note in plan:
                timeout = 900 if not warning else 600
                result = run(self._args(prepared, tmp_out, attempt_title, attempt_cues),
                             timeout=timeout)
                if result.returncode == 0:
                    warning = note
                    break
                last_error = (result.stderr or "").strip()[-600:]
            else:
                return False, last_error, warning
        finally:
            if lock is not None:
                lock.close()

        if warning and last_error:
            # Say why a caption was dropped: a bare "subtitles dropped" leaves
            # nobody able to tell a memory kill from a graph error.
            reason = last_error.strip().splitlines()[-1][:160]
            warning = f"{warning} — {reason}"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # The scratch render lives under --work, which is usually on the boot
        # disk; --out may be an external SSD. os.replace/rename cannot cross a
        # filesystem boundary (EXDEV), so move, which falls back to copy.
        try:
            shutil.move(str(tmp_out), str(out_path))
        except OSError as exc:
            return False, f"could not write the clip to {out_path.parent}: {exc}", warning
        return True, "", warning

    # -- step 2b: subtitles -------------------------------------------------
    def subtitle_enabled(self) -> bool:
        mode = getattr(self.cfg, "subs", "off")
        if mode == "off" or not mode:
            return False
        if mode == "on":
            return True
        return transcribe.has_subtitles()

    def caption_style(self) -> str:
        style = getattr(self.cfg, "subs_style", "pop")
        return style if style in ("pop", "block") else "pop"

    def cam_spec(self) -> str:
        """The webcam box to use, or "" while detection has not found one.

        ``auto`` is not a box: when the watcher has not found a face (a stream
        with no webcam, or a machine without Vision) the split layout falls back
        to the blurred 9:16 frame rather than cropping a guessed region of the
        gameplay.
        """
        spec = (getattr(self.cfg, "cam", "") or "").strip()
        return "" if spec.lower() == "auto" else spec

    def speech_wav(self, prepared: Prepared) -> Path | None:
        """Extract a cleaned 16 kHz mono wav over the clip's exact audio window."""
        wav = prepared.work / "speech.wav"
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(prepared.list_path),
            "-ss", f"{prepared.offset:.3f}", "-t", f"{prepared.duration:.3f}",
            "-vn",
            # Stream audio is loud, clipped and bass-heavy. Clean it up and
            # level it before whisper sees it — this measurably helps accuracy.
            "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
        result = run(args, timeout=180)
        if result.returncode != 0 or not wav.exists():
            return None
        return wav

    def transcribe(self, prepared: Prepared, model: str | None = None,
                   threads: int | None = None, streamer: bool = True,
                   focus: float | None = None,
                   log=lambda message: None) -> tuple[list, str]:
        """Speech-to-text over the exact audio window the clip will use.

        ``streamer`` gates the result down to the streamer's own voice (see
        :mod:`clipper.speaker`); the verifier turns it off because it only needs
        to know whether *anything* was said.
        """
        wav = self.speech_wav(prepared)
        if wav is None:
            return [], "could not extract clip audio"
        try:
            return self.transcribe_wav(wav, prepared, model=model,
                                       threads=threads, streamer=streamer,
                                       focus=focus, log=log)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    def transcribe_wav(self, wav: Path, prepared: Prepared, model: str | None = None,
                       threads: int | None = None, streamer: bool = True,
                       focus: float | None = None,
                       log=lambda message: None) -> tuple[list, str]:
        """Decode a prepared wav, then gate it to the streamer."""
        cues, note = transcribe.transcribe(
            wav,
            model=model if model is not None else getattr(self.cfg, "subs_model", None),
            language=getattr(self.cfg, "subs_language", "en") or "en",
            prompt=getattr(self.cfg, "subs_prompt", None),
            # Cue grouping is only a fallback now: popping captions use the
            # token timings directly, and block captions want readable lines.
            max_chars=44,
            threads=threads if threads is not None
            else getattr(self.cfg, "subs_threads", 0),
        )
        if streamer and cues:
            from . import speaker

            cues, gate_note = speaker.streamer_only(
                cues, prepared, self.cam_spec(), wav, self.cfg.work_dir,
                focus=focus, log=log)
            if gate_note:
                note += " · " + gate_note
        return cues, note

    def transcribe_rendered(self, media: Path, prepared: Prepared,
                            focus: float | None = None,
                            log=lambda message: None) -> tuple[list, str]:
        """Transcribe the finished clip's own audio track.

        The live buffer is copied while ffmpeg is still writing it; rarely, that
        copy comes out silent or short and whisper then "hears" a few words in
        nothing. The rendered file is the audio the viewer will actually get, so
        it is the safer second opinion.
        """
        wav = prepared.work / "retry.wav"
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(media), "-vn",
            "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
        result = run(args, timeout=180)
        if result.returncode != 0 or not wav.exists():
            return [], "could not read the clip audio"
        try:
            return self.transcribe_wav(wav, prepared, focus=focus, log=log)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    def verify(self, prepared: Prepared) -> str:
        """A cheap transcript of a candidate window, for the verifier signal.

        Uses the smallest installed model on few threads, because it only has to
        answer "did he actually say something worth clipping". Returns "" when
        verification is not possible.
        """
        fast = transcribe.find_fast_model()
        if fast is None:
            return ""
        wav = self.speech_wav(prepared)
        if wav is None:
            return ""
        try:
            cues, _ = transcribe.transcribe(wav, model=str(fast), threads=VERIFY_THREADS)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass
        return " ".join(cue.text for cue in cues)

    def _args(self, prepared: Prepared, out_path: Path, title: str,
              cues: list) -> list[str]:
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-f", "concat", "-safe", "0",
            "-i", str(prepared.list_path),
        ]

        graph, needs_filter, extra = self._graph(prepared, title, cues)
        for path in extra:
            # 1 fps stills: far less decoder memory than the default rate.
            args += ["-loop", "1", "-framerate", "1", "-i", str(path)]

        args += ["-ss", f"{prepared.offset:.3f}", "-t", f"{prepared.duration:.3f}"]

        if needs_filter:
            args += ["-filter_complex", graph, "-map", "[vout]", "-map", "0:a?"]
        else:
            args += ["-map", "0:v:0", "-map", "0:a?"]

        args += self._encoder_args()
        args += ["-movflags", "+faststart", "-metadata", "comment=clipped live", str(out_path)]
        return args

    def _encoder_args(self) -> list[str]:
        cfg = self.cfg
        args = ["-c:v", self.encoder]
        if self.encoder == "h264_videotoolbox":
            args += ["-b:v", "8M", "-allow_sw", "1"]
        elif self.encoder == "libx264":
            args += ["-preset", cfg.preset, "-crf", str(cfg.crf), "-profile:v", "high"]
        else:
            args += ["-b:v", "12M"]
        if cfg.fps:
            args += ["-r", str(cfg.fps)]
        args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
        return args

    def _graph(self, prepared: Prepared, title: str,
               cues: list) -> tuple[str, bool, list[Path]]:
        cfg = self.cfg
        cam = self.cam_spec()
        # No detected webcam means there is nothing to put across the top: show
        # the blurred 9:16 frame instead of cropping a guessed region.
        layout = "blur" if cfg.layout == "split" and not cam else cfg.layout
        vertical = layout != "horizontal"
        style = self.caption_style()
        if layout == "horizontal" and not title and not cues:
            return "", False, []

        parts: list[str] = []
        if layout == "vertical":
            parts.append(
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1[vbase]"
            )
        elif layout == "blur":
            # Blur a quarter-size frame and upscale: identical look, ~16x cheaper.
            parts.append("[0:v]split=2[bg][fg]")
            parts.append(
                "[bg]scale=270:480:force_original_aspect_ratio=increase,"
                "crop=270:480,gblur=sigma=7,scale=1080:1920,"
                "eq=brightness=-0.06[b]"
            )
            parts.append("[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f]")
            parts.append("[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[vbase]")
        elif layout == "split":
            parts.extend(split_graph(cam, prepared.width, prepared.height))
        else:
            parts.append("[0:v]setsar=1[vbase]")

        extra: list[Path] = []
        current = "vbase"
        index = 1
        geo = (split_geometry(cam, prepared.width, prepared.height)
               if layout == "split" else None)
        sub_margin = 64 if vertical else 40
        sub_ratio = 0.095 if style == "pop" else 0.062
        sub_estimate = (int(1080 * sub_ratio * 1.7) if vertical else 160)
        game_bottom = (geo.cam_h + geo.game_h) if geo else 1920

        def draw_png(text: str, path: Path, font_px: int, max_width: int,
                     pop: bool = False, font: str | None = None,
                     fill: str = "white",
                     char_width: float = 0.56,
                     emoji: bool = False) -> tuple[int, int]:
            """Rasterise one overlay and register it as the next ffmpeg input."""
            drawn = None
            # Real outlined font when ImageMagick is around, bitmap otherwise.
            if caption.has_styled():
                if pop:
                    drawn = caption.render_pop(text, path, max_width,
                                               cap_px=font_px, fill=fill)
                else:
                    drawn = caption.render_styled(text, font_px, path, max_width,
                                                  font=font, fill=fill,
                                                  char_width=char_width,
                                                  emoji=emoji)
            if drawn is None:
                if emoji:
                    text = caption.strip_emoji(text)
                drawn = caption.render(text, font_px, path, max_width)
            extra.append(path)
            return drawn or (0, 0)

        def overlay_png(x: str, y: str,
                        window: tuple[float, float] | None = None) -> None:
            nonlocal current, index
            label = f"vov{index}"
            enable = (f":enable='between(t,{window[0]:.3f},{window[1]:.3f})'"
                      if window else "")
            parts.append(f"[{current}][{index}:v]overlay=x={x}:y={y}{enable}:format=auto[{label}]")
            current = label
            index += 1

        if title:
            hook_font = caption.hook_font()
            font = hook_font or find_font()
            # drawtext has no emoji fallback, and Arial Black has no emoji
            # glyphs: keep emoji on the PNG path, and drop them only when no
            # emoji face exists at all (a "?" in a hook is worse than no emoji).
            emoji = caption.has_emoji(title) and caption.can_render_emoji()
            if not emoji:
                title = caption.strip_emoji(title)
            if caption.has_drawtext() and font and not emoji:
                prepared.title_file.write_text(_wrap(title.upper()), encoding="utf-8")
                size = int(1080 * 0.062) if vertical else int(max(320, prepared.height or 720) * 0.072)
                if geo:
                    y = f"h-{sub_margin + sub_estimate + 24}-text_h"
                elif vertical:
                    y = "h*0.06" if style == "pop" else "h*0.70"
                else:
                    y = "h-text_h-90"
                parts.append(
                    f"[{current}]drawtext="
                    f"fontfile='{font}':textfile='{prepared.title_file.resolve().as_posix()}':"
                    f"fontcolor=0xFFD23F:fontsize={size}:line_spacing=8:"
                    "box=1:boxcolor=black@0.55:boxborderw=26:"
                    f"x=(w-text_w)/2:y={y}:"
                    "shadowcolor=black@0.65:shadowx=2:shadowy=2[vdraw]"
                )
                current = "vdraw"
            else:
                font_px = int(1080 * 0.068) if vertical else int(max(320, prepared.height or 720) * 0.10)
                max_width = int((1080 if vertical else (prepared.width or 1280)) * 0.9)
                if vertical:
                    # Keep the hook on one line: the band under the game is
                    # shallow, and a two-line hook would run into the subtitles.
                    fitting = int(max_width / (max(1, len(title)) * 0.72))
                    font_px = max(28, min(font_px, fitting))
                _, height = draw_png(
                    title, prepared.work / "title.png", font_px, max_width,
                    font=hook_font, fill=caption.HOOK_FILL,
                    char_width=0.72 if hook_font else 0.56,
                    emoji=emoji,
                )
                if geo:
                    # Just under the gameplay, above the subtitle band; if the
                    # hook is tall it may sit slightly higher, never on the subs.
                    limit = 1920 - sub_margin - sub_estimate - height - 20
                    y = str(max(game_bottom + 18, limit))
                    x = "(main_w-overlay_w)/2"
                else:
                    x, y = caption.overlay_expr(layout, style)
                overlay_png(x, y)

        if cues:
            shown = transcribe.pop_chunks(cues) if style == "pop" else cues
            base = 1080 if vertical else max(320, prepared.width or 1280)
            max_width = int((1080 if vertical else (prepared.width or 1280)) * 0.92)
            font_px = int(base * sub_ratio)
            if geo:
                x, y = "(main_w-overlay_w)/2", f"main_h-{sub_margin}-overlay_h"
            else:
                x, y = caption.subtitle_expr(layout, style)
            for position, cue in enumerate(shown):
                draw_png(
                    cue.text, prepared.work / f"sub_{position:03d}.png",
                    font_px, max_width, pop=(style == "pop"),
                )
                overlay_png(x, y, window=(cue.start, cue.end))

        parts.append(f"[{current}]null[vout]")
        return ";".join(parts), True, extra


def clip_filename(channel: str, score: float, stamp: str | None = None) -> str:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{safe_name(channel, 24)}_{int(round(score * 100)):03d}.mp4"
