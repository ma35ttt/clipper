"""Speech-to-text for clip subtitles.

Backends, tried in order:

* ``whisper-cli`` — whisper.cpp (``brew install whisper-cpp``). No Python deps.
* ``mlx_whisper`` — Apple-Silicon Whisper, if the module is importable.
* ``whisper``     — openai-whisper, if the module is importable.

Everything degrades to "no subtitles" with a one-line hint, never an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .util import have, run

# Preferred model filenames, most accurate first. Bigger models are noticeably
# better at names and slang, which is most of what streamer speech is.
MODEL_NAMES = (
    "ggml-medium.en.bin",
    "ggml-small.en.bin",
    "ggml-base.en.bin",
    "ggml-tiny.en.bin",
    "ggml-medium.bin",
    "ggml-small.bin",
    "ggml-base.bin",
    "ggml-tiny.bin",
)

# The smallest installed model, used to *verify* candidates that the cheap
# signals already flagged. It only needs to read "did he actually say something
# worth clipping", not produce good subtitles, so favour speed.
FAST_MODEL_NAMES = (
    "ggml-tiny.en.bin",
    "ggml-tiny.bin",
    "ggml-base.en.bin",
    "ggml-base.bin",
)

MODEL_DIRS = (
    Path("models"),
    Path(".clipper") / "models",
    Path.home() / ".cache" / "whisper.cpp" / "models",
    Path.home() / ".local" / "share" / "whisper.cpp" / "models",
    Path("/opt/homebrew/share/whisper.cpp/models"),
    Path("/usr/local/share/whisper.cpp/models"),
)

# Optional voice-activity model. whisper.cpp runs it first and only decodes the
# parts it believes are speech, which is the single biggest accuracy win on
# stream audio: gameplay noise and music otherwise get "transcribed" as
# invented sentences. It is tiny (under 1 MB) but must be fetched separately.
VAD_NAMES = (
    "ggml-silero-v5.1.2.bin",
    "ggml-silero-v6.2.0.bin",
    "ggml-silero.bin",
)

HINT = ("subtitles need a speech-to-text model — run: "
        "brew install whisper-cpp && clipper models --get tiny.en")

# Transcription must share the machine with the browser and the window server.
# Grabbing every core makes the whole desktop stutter for the length of a clip,
# so whisper gets half the cores by default, capped at four. Override with
# ``--subs-threads N`` (N <= 0 means all cores) when nothing else needs the box.
DEFAULT_THREADS = max(2, min(4, (os.cpu_count() or 4) // 2))


def resolve_threads(threads: int | None) -> int:
    if threads is None or threads == 0:
        return DEFAULT_THREADS
    if threads < 0:
        return max(1, os.cpu_count() or 4)
    return max(1, threads)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str

    def shifted(self, delta: float) -> "Cue":
        return Cue(max(0.0, self.start + delta), max(0.0, self.end + delta), self.text)


class Cues(list):
    """A cue list that also carries the per-word timings it was built from.

    Popping captions swap one short chunk of words at a time, so they need to
    know *when* each word is spoken. whisper.cpp can report that directly (its
    token offsets), and when it does we keep the words here so the chunker can
    use the real timings instead of estimating them from word length. Being a
    plain ``list`` subclass means the rest of the code keeps treating it as one.
    """

    __slots__ = ("words",)

    def __init__(self, cues=(), words=()):
        super().__init__(cues)
        self.words: list[Cue] = list(words)


# --------------------------------------------------------------------------- #
# backend discovery
# --------------------------------------------------------------------------- #

def whisper_binary() -> str | None:
    for name in ("whisper-cli", "whisper-cpp", "main"):
        if have(name):
            return name
    return None


@lru_cache(maxsize=1)
def python_backend() -> str | None:
    for module in ("mlx_whisper", "whisper"):
        try:
            __import__(module)
            return module
        except Exception:  # noqa: BLE001 - absence is the normal case
            continue
    return None


def find_model(explicit: str | None = None, names: tuple[str, ...] = MODEL_NAMES,
               use_env: bool = True) -> Path | None:
    """Locate a whisper model: --model, $CLIPPER_WHISPER_MODEL, then the usual dirs."""
    candidates: list[Path] = []
    values = [explicit]
    if use_env:
        values.append(os.environ.get("CLIPPER_WHISPER_MODEL"))
    for value in values:
        if value:
            candidates.append(Path(value).expanduser())
    for directory in MODEL_DIRS:
        for name in names:
            candidates.append(directory / name)
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1024 * 1024:
                return path.resolve()
        except OSError:
            continue
    return None


def find_fast_model() -> Path | None:
    """The smallest installed model, for cheap candidate verification.

    Ignores --model and $CLIPPER_WHISPER_MODEL on purpose: those point at the
    accurate model, and verification is meant to be the fast one.
    """
    return find_model(None, names=FAST_MODEL_NAMES, use_env=False)


def find_vad_model() -> Path | None:
    """The optional silero VAD model, if it has been fetched."""
    for directory in MODEL_DIRS:
        for name in VAD_NAMES:
            path = directory / name
            try:
                if path.is_file() and path.stat().st_size > 100 * 1024:
                    return path.resolve()
            except OSError:
                continue
    return None


def status(explicit_model: str | None = None) -> tuple[bool, str]:
    """(ready, human description). Never raises."""
    if whisper_binary():
        model = find_model(explicit_model)
        if model:
            return True, f"whisper.cpp ({whisper_binary()}) · {model.name}"
        return False, f"whisper.cpp found but no model — {HINT}"
    name = python_backend()
    if name:
        return True, f"python backend: {name}"
    return False, HINT


# --------------------------------------------------------------------------- #
# SRT parsing (whisper.cpp) and JSON parsing (openai-whisper)
# --------------------------------------------------------------------------- #

_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _seconds(stamp: str) -> float:
    match = _TIME.search(stamp)
    if not match:
        return 0.0
    hours, minutes, seconds, frac = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(frac.ljust(3, "0")) / 1000.0)


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        stamp_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if stamp_index is None:
            continue
        left, _, right = lines[stamp_index].partition("-->")
        body = " ".join(lines[stamp_index + 1:]).strip()
        if not body:
            continue
        text = _clean(body)
        if not text:
            continue
        start, end = _seconds(left), _seconds(right)
        cues.append(Cue(start, end, text))
    return cues


def parse_whisper_json(payload: dict, offset: float = 0.0) -> list[Cue]:
    """openai-whisper / mlx_whisper verbose_json shape."""
    cues: list[Cue] = []
    for seg in payload.get("segments") or []:
        text = _clean(str(seg.get("text", "")))
        if not text:
            continue
        start = float(seg.get("start", 0.0)) + offset
        end = float(seg.get("end", start)) + offset
        cues.append(Cue(start, end, text))
    return cues


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[-–—\s]+", "", text)
    # Whisper leans on non-speech annotations; drop the pure ones.
    if re.fullmatch(r"[\[(（].*[\])）]", text):
        return ""
    return re.sub(r"[\[\]()（）]", "", text).strip()


# The shortest span a word may occupy. whisper's offsets are coarse, and a word
# nudged to at least this keeps the overlays from flickering on a single frame.
WORD_MIN = 0.10

# whisper.cpp decodes in 30 s windows and pads the final one with silence. On
# noisy stream audio it keeps generating into that padding, so a 32 s clip comes
# back with a phantom "Thank you." timed 30 s -> 60 s. A real word cannot end
# this far past the audio, so anything that does is padding, not speech.
PAD_MARGIN = 0.75

# The other padding failure: the model answers music or gameplay noise with one
# word stretched across a whole window ("So" timed 0 s -> 30 s). Speech is fast;
# a single word lasting more than a couple of seconds is the model guessing.
MAX_WORD_SECONDS = 2.5


def parse_token_words(payload: dict, offset: float = 0.0,
                      duration: float | None = None) -> list[Cue]:
    """Per-word timings from whisper.cpp's ``-ojf`` output. Never raises.

    whisper splits words into BPE tokens and marks a new word by a leading space,
    so ``' it'`` starts a word and ``"'s"`` continues it (giving "it's"), while
    punctuation like ``','`` is its own token that belongs to the word before it.
    Tokens are merged back into words and given the span of their first and last
    token.

    Two kinds of token are dropped rather than guessed at, because neither is
    real speech:

    * tokens that share one instant (whisper decoding into the padding with no
      time left to hand out), and
    * tokens that run past the real audio (``duration``), which is where those
      phantom trailing sentences come from.
    """
    limit = duration + PAD_MARGIN if duration and duration > 0 else None
    words: list[Cue] = []
    for segment in payload.get("transcription") or []:
        for token in segment.get("tokens") or []:
            raw = str(token.get("text", ""))
            if not raw or raw.startswith("<|"):
                continue
            offsets = token.get("offsets") or {}
            try:
                start = float(offsets["from"]) / 1000.0 + offset
                end = float(offsets["to"]) / 1000.0 + offset
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            if limit is not None and end > limit:
                continue
            piece = raw.strip()
            if not piece:
                continue
            if raw[:1] in (" ", "\t") or not words:
                words.append(Cue(start, end, piece))
            else:
                last = words[-1]
                words[-1] = Cue(last.start, end, last.text + piece)

    # Keep the words strictly ordered and long enough to be visible: whisper can
    # report a word that starts before the previous one ends, and overlapping
    # overlays just flicker. Words stretched across a padded window carry no
    # usable timing and are the signature of a hallucinated transcript, so they
    # go before ordering.
    out: list[Cue] = []
    clock = 0.0
    for word in words:
        if word.end - word.start > MAX_WORD_SECONDS:
            continue
        start = max(word.start, clock)
        end = max(word.end, start + WORD_MIN)
        out.append(Cue(start, end, word.text))
        clock = end
    return out


_SENTENCE_END = re.compile(r"[.!?][\"')\]]?$")


def cues_from_words(words: list[Cue], max_chars: int = 44,
                    max_seconds: float = 4.0) -> list[Cue]:
    """Build block-style cues from the trusted word timings.

    The SRT whisper writes carries the same words but also the tail it invented
    while decoding the padding, and it uses the model's own segment boundaries.
    The tokens are the source of truth (they are already cleaned in
    :func:`parse_token_words`), so the block captions are grouped from them
    instead: split on sentence punctuation, then on length or time.
    """
    cues: list[Cue] = []
    current: list[Cue] = []

    def flush() -> None:
        if current:
            cues.append(Cue(current[0].start, current[-1].end,
                            " ".join(word.text for word in current)))
            current.clear()

    for word in words:
        trial = " ".join([w.text for w in current] + [word.text])
        if current and (len(trial) > max_chars
                        or word.end - current[0].start > max_seconds):
            flush()
        current.append(word)
        if _SENTENCE_END.search(word.text):
            flush()
    flush()
    return cues


def clamp(cues: list[Cue], duration: float | None) -> list[Cue]:
    """Whisper pads audio to a 30s window; trim cues to the real media length."""
    if not duration or duration <= 0:
        return cues
    out: list[Cue] = []
    for cue in cues:
        if cue.start >= duration:
            continue
        out.append(Cue(cue.start, min(cue.end, duration), cue.text))
    return out


def resegment(cues: list[Cue], max_seconds: float = 4.0,
              max_chars: int = 44) -> list[Cue]:
    """Split long cues into caption-sized chunks, pacing by text length."""
    out: list[Cue] = []
    for cue in cues:
        words = cue.text.split()
        if not words:
            continue
        span = max(0.4, cue.end - cue.start)
        if span <= max_seconds and len(cue.text) <= max_chars:
            out.append(cue)
            continue

        chunks: list[list[str]] = []
        current: list[str] = []
        for word in words:
            trial = " ".join(current + [word])
            if current and len(trial) > max_chars:
                chunks.append(current)
                current = [word]
            else:
                current.append(word)
        if current:
            chunks.append(current)

        total = sum(len(w) for w in words) or 1
        clock = cue.start
        for chunk in chunks:
            share = sum(len(w) for w in chunk) / total
            length = span * share
            out.append(Cue(clock, clock + length, " ".join(chunk)))
            clock += length
    return out


def word_timings(cues: list[Cue], min_word: float = 0.12) -> list[Cue]:
    """Split cues into one-word cues, estimating each word's share by length.

    whisper gives cue-level timings, but popping captions need word-level. Long
    words take longer to say, so weighting each word by its length approximates
    the pacing closely enough that the words land roughly on the beat.
    """
    out: list[Cue] = []
    for cue in cues:
        words = cue.text.split()
        if not words:
            continue
        weights = [len(word) + 1 for word in words]
        total = float(sum(weights))
        span = max(0.2, cue.end - cue.start)
        clock = cue.start
        for word, weight in zip(words, weights):
            length = max(min_word, span * weight / total)
            out.append(Cue(clock, clock + length, word))
            clock += length
    return out


def pop_chunks(cues: list[Cue], max_words: int = 3, max_chars: int = 18,
               min_seconds: float = 0.45) -> list[Cue]:
    """Group word timings into the short, fast chunks popping captions use.

    Consecutive words are packed up to ``max_words``/``max_chars`` and shown for
    their combined span, then the next chunk replaces them. Chunks shorter than
    ``min_seconds`` are folded into the previous one so nothing flickers for a
    couple of frames.

    Word timings come from whisper's own token offsets when the transcript
    carries them (see ``Cues``); otherwise they are estimated from word length.
    """
    words = list(getattr(cues, "words", ()) or ())
    if not words:
        words = word_timings(cues)
    chunks: list[Cue] = []
    current: list[Cue] = []
    for word in words:
        trial = " ".join([c.text for c in current] + [word.text])
        if current and (len(current) >= max_words or len(trial) > max_chars):
            chunks.append(Cue(current[0].start, current[-1].end,
                              " ".join(c.text for c in current)))
            current = []
        current.append(word)
    if current:
        chunks.append(Cue(current[0].start, current[-1].end,
                          " ".join(c.text for c in current)))

    merged: list[Cue] = []
    for chunk in chunks:
        if merged and (chunk.end - chunk.start) < min_seconds:
            previous = merged[-1]
            combined = f"{previous.text} {chunk.text}"
            # Folding a stub into the previous chunk avoids a one-frame flash,
            # but only while the result still looks like a popping caption.
            if len(combined.split()) <= max_words and len(combined) <= max_chars:
                merged[-1] = Cue(previous.start, max(previous.end, chunk.end),
                                 combined)
                continue
        merged.append(chunk)

    # A stub that could not be folded (usually the last word of the clip) is
    # held on screen for min_seconds instead of flashing for two frames. Running
    # past the real clip end is harmless.
    shown: list[Cue] = []
    for index, chunk in enumerate(merged):
        end = chunk.end
        if end - chunk.start < min_seconds:
            limit = (merged[index + 1].start if index + 1 < len(merged)
                     else chunk.start + min_seconds)
            end = max(end, min(chunk.start + min_seconds, limit))
        shown.append(Cue(chunk.start, end, chunk.text))
    return shown


# --------------------------------------------------------------------------- #
# transcription
# --------------------------------------------------------------------------- #

def speech_score(words: list[Cue], duration: float | None) -> float:
    """How much of the clip the trusted words actually cover, 0..1."""
    if not words:
        return 0.0
    covered = sum(min(max(0.0, w.end - w.start), 1.5) for w in words)
    return covered / max(duration or 0.0, 1.0)


def trusted_chars(words: list[Cue]) -> int:
    return sum(len(w.text) for w in words)


def transcribe(wav: Path, model: str | None = None, language: str = "en",
               timeout: float = 300.0, prompt: str | None = None,
               max_chars: int = 44, threads: int | None = None) -> tuple[list[Cue], str]:
    """Transcribe a 16 kHz mono wav. Returns (cues, note). Never raises.

    The cues carry word-level timings when whisper provides them, so popping
    captions can sync to the actual speech.
    """
    wav = Path(wav)
    if not wav.exists():
        return Cues(), "audio file missing"

    words: list[Cue] = []
    has_tokens = False
    from .util import probe

    duration = probe(wav).get("duration")
    binary = whisper_binary()
    if binary:
        chosen = find_model(model)
        if not chosen:
            return Cues(), f"no whisper model found — {HINT}"
        threads_n = resolve_threads(threads)

        # Plain decode first: it keeps the context between words, which is what
        # handles a voice sitting under loud gameplay.
        cues, words, note, has_tokens = _run_whisper_cpp(
            binary, chosen, wav, language, timeout, prompt, threads_n, duration)

        # On music, a cutscene, or a mostly-silent moment the plain decode can
        # invent a sentence instead of saying nothing. If what it produced is
        # too sparse to be real speech, run the VAD pass as a rescue and keep
        # whichever pass heard more. VAD is not the default because it also
        # drops real lines when the game is louder than the streamer.
        vad = find_vad_model()
        if vad is not None and has_tokens and not (
                len(words) >= 3 and speech_score(words, duration) >= 0.35):
            vcues, vwords, vnote, vhas = _run_whisper_cpp(
                binary, chosen, wav, language, timeout, prompt, threads_n,
                duration, vad=vad, tag="_vad")
            if speech_score(vwords, duration) >= 0.10 and \
                    trusted_chars(vwords) > trusted_chars(words) * 1.25:
                cues, words, note, has_tokens = vcues, vwords, vnote, vhas
    else:
        module = python_backend()
        if module:
            cues, note = _run_python(module, wav, language)
        else:
            return Cues(), HINT

    if cues or words or has_tokens:
        # The tokens are the trustworthy half: they are filtered against the real
        # audio length, and they carry the timings the popping captions need. Use
        # them for both caption styles so block cues cannot show padding text.
        # When whisper produced tokens but every one was rejected, the clip has
        # no usable speech — do not fall back to the SRT, which still holds the
        # hallucinated text.
        if has_tokens:
            cues = cues_from_words(words, max_chars=max_chars)
        else:
            cues = clamp(cues, duration)
        words = clamp(words, duration)
        cues = drop_degenerate(cues)
        cues = resegment(cues, max_chars=max_chars)
    return Cues(cues, words), note


def drop_degenerate(cues: list[Cue]) -> list[Cue]:
    """Remove whisper's padding artefacts.

    whisper.cpp always decodes a 30s window, so a short clip comes back with
    trailing segments that are zero-length and/or repeat earlier text. Those
    burn in as invisible, duplicated captions, so drop them.
    """
    out: list[Cue] = []
    for cue in cues:
        if cue.end - cue.start < 0.15:
            continue
        if not cue.text.strip():
            continue
        if out and cue.text == out[-1].text:
            continue
        out.append(cue)
    return out


def _run_whisper_cpp(binary: str, model: Path, wav: Path, language: str,
                     timeout: float, prompt: str | None = None,
                     threads: int | None = None,
                     duration: float | None = None,
                     vad: Path | None = None, tag: str = ""
                     ) -> tuple[list[Cue], list[Cue], str, bool]:
    """Run whisper.cpp for a wav. Returns (cues, words, note, has_tokens).

    Asks for the SRT (the phrase cues) and the full JSON (the token offsets that
    become word timings) in one pass, so the words cost no extra decode. When
    the JSON was written, ``has_tokens`` is true even if every token was
    rejected: that means the model heard nothing usable, and the SRT must not be
    used as a fallback because it still contains the hallucinated text.

    ``vad`` enables whisper's voice-activity gate for this pass; ``tag`` keeps
    its output files from colliding with the plain pass.
    """
    out_prefix = Path(str(wav.with_suffix("")) + tag)
    srt = out_prefix.with_suffix(".srt")
    payload_path = out_prefix.with_suffix(".json")
    for stale in (srt, payload_path):
        try:
            stale.unlink()
        except OSError:
            pass
    cmd = [
        binary, "-m", str(model), "-f", str(wav),
        "-l", language, "-osrt", "-ojf", "-of", str(out_prefix),
        "-np", "-nt", "-sns",
        # Let whisper choose its own segment boundaries: its decoder uses the
        # running context, and forcing short segments loses that. Caption-sized
        # chunks are built afterwards from the token timings instead.
        "-t", str(resolve_threads(threads)),
    ]
    if prompt:
        # Vocabulary bias: streamer names, game names, recurring slang.
        cmd += ["--prompt", prompt]
    if vad is not None:
        cmd += ["--vad", "-vm", str(vad)]
    result = run(cmd, timeout=timeout)
    if result.returncode != 0 or not srt.exists():
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        return [], [], "whisper.cpp failed: " + " ".join(tail), False
    try:
        cues = parse_srt(srt.read_text(errors="replace"))
    except OSError as exc:
        return [], [], f"could not read transcript: {exc}", False
    finally:
        try:
            srt.unlink()
        except OSError:
            pass

    words: list[Cue] = []
    has_tokens = False
    if payload_path.exists():
        has_tokens = True
        try:
            words = parse_token_words(json.loads(payload_path.read_text(errors="replace")),
                                      duration=duration)
        except (OSError, ValueError):
            has_tokens = False
            words = []
        finally:
            try:
                payload_path.unlink()
            except OSError:
                pass
    return cues, words, f"whisper.cpp/{model.stem}" + ("/vad" if vad else ""), has_tokens


def _run_python(module: str, wav: Path, language: str) -> tuple[list[Cue], str]:
    try:
        if module == "mlx_whisper":
            import mlx_whisper  # type: ignore

            payload = mlx_whisper.transcribe(
                str(wav), path_or_hf_repo="mlx-community/whisper-tiny.en-mlx",
                language=language,
            )
        else:
            import whisper  # type: ignore

            model = whisper.load_model("tiny.en" if language == "en" else "tiny")
            payload = model.transcribe(str(wav), language=language, verbose=False)
    except Exception as exc:  # noqa: BLE001 - model download / runtime issues
        return [], f"{module} failed: {exc}"
    return parse_whisper_json(payload), module


# --------------------------------------------------------------------------- #
# model management (used by `clipper models`)
# --------------------------------------------------------------------------- #

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{name}"
VAD_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/{name}"

ALIASES = {
    "tiny": "ggml-tiny.bin",
    "tiny.en": "ggml-tiny.en.bin",
    "base": "ggml-base.bin",
    "base.en": "ggml-base.en.bin",
    "small": "ggml-small.bin",
    "small.en": "ggml-small.en.bin",
    "medium": "ggml-medium.bin",
    "medium.en": "ggml-medium.en.bin",
    "vad": "ggml-silero-v5.1.2.bin",
}


def download(name: str, dest_dir: Path | None = None) -> Path:
    """Fetch a whisper.cpp ggml model. Returns the path it wrote."""
    filename = ALIASES.get(name, name)
    if not filename.endswith(".bin"):
        filename = f"ggml-{filename}.bin"
    target_dir = Path(dest_dir) if dest_dir else Path("models")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists() and target.stat().st_size > 1024 * 1024:
        return target

    import urllib.request

    url = (VAD_URL if filename.startswith("ggml-silero") else MODEL_URL).format(name=filename)
    tmp = target.with_suffix(".part")
    print(f"  downloading {filename} ...", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as fh:
        total = int(response.headers.get("content-length") or 0)
        seen = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            seen += len(chunk)
            if total:
                pct = seen * 100 // total
                print(f"\r  {filename}  {pct:3d}%  ({seen >> 20}MB / {total >> 20}MB)",
                      end="", flush=True)
    print()
    os.replace(tmp, target)
    return target


def has_subtitles() -> bool:
    return status()[0]


if __name__ == "__main__":  # pragma: no cover - manual check
    ok, detail = status(sys.argv[1] if len(sys.argv) > 1 else None)
    print(("ready: " if ok else "not ready: ") + detail)
