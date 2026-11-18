"""A short, factual voiceover on top of a clip — the "your voice" layer.

The point is transformation, not reaction: the narrator frames what the viewer
is about to see, so the clip is no longer a straight reupload. The line is built
only from things that exist in the clip: the streamer's own words and what chat
actually said. When neither is usable, no voiceover is written at all rather
than inventing a story.

Speech comes from whatever free engine is installed, best first:

    piper        neural, offline, closest to a real narrator   (best)
    edge-tts     neural, online, needs the ``edge-tts`` command
    say          macOS system voice, offline
    google       Translate's TTS endpoint, online, no install
    sapi         Windows system voice via PowerShell, offline

``piper`` is optional; the rest have no Python dependencies. The mix ducks the
stream audio under the narration and leaves the rest of the clip untouched, and
the video is stream-copied, so this pass costs one audio encode.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import zlib
from pathlib import Path
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen

from .util import FFMPEG, have, probe

# A narrator reading slurs or swearing is a demonetisation risk, and a bleep in
# a synthetic voice sounds broken. When the only quotable line is profane, the
# clip gets no narration instead.
PROFANITY = {
    "fuck", "fucking", "fucked", "fucker", "shit", "shitty", "bitch", "cunt",
    "nigga", "nigger", "retard", "retarded", "dick", "pussy", "asshole",
    "motherfucker", "bullshit", "prick", "whore", "slut",
}

MAX_SCRIPT_WORDS = 34
MAX_SCRIPT_CHARS = 190        # the Google endpoint rejects longer requests
ROOT = Path(__file__).resolve().parent.parent
VOICE_DIRS = (ROOT / "models" / "piper", ROOT / ".clipper" / "models" / "piper",
              Path.home() / ".local" / "share" / "piper" / "voices")


# --------------------------------------------------------------------------- #
# the line
# --------------------------------------------------------------------------- #

def _profane(text: str) -> bool:
    return any(word.strip(".,!?").lower() in PROFANITY
               for word in (text or "").split())


def _quotable(text: str) -> str:
    """The most self-contained phrase the streamer said, or "".

    Prefers a whole short sentence; falls back to a clause. Anything profane is
    refused outright — a narrator should not read it back.
    """
    text = " ".join((text or "").split())
    if not text or _profane(text):
        return ""
    pieces = [piece.strip() for piece in text.replace("?", ".").replace("!", ".")
              .split(".") if piece.strip()]
    pieces.sort(key=len, reverse=True)
    for piece in pieces:
        words = piece.split()
        if 3 <= len(words) <= 16:
            return " ".join(words)
    words = text.split()
    if 3 <= len(words) <= 16:
        return " ".join(words)
    return ""


def _chat_phrase(chat) -> str:
    """What chat kept repeating, lower-cased for speech. "" when nothing."""
    if not chat:
        return ""
    try:
        from .cutter import hook_from_chat
    except Exception:  # noqa: BLE001 - never let a hook break the audio
        return ""
    phrase = hook_from_chat(chat)
    if not phrase or _profane(phrase):
        return ""
    return phrase.lower()


def _channel_name(channel: str) -> str:
    name = " ".join((channel or "").replace("_", " ").split())
    return name or "this streamer"


def _trim(text: str, limit: int = MAX_SCRIPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind("."), cut.rfind(","), cut.rfind(" "))
    return (cut[:stop] if stop > 40 else cut).strip()


def script_for(channel: str, cues, chat=(), max_words: int = MAX_SCRIPT_WORDS) -> str:
    """A grounded framing line, or "" when there is nothing honest to say."""
    said = " ".join(getattr(cue, "text", "") for cue in (cues or ())).strip()
    if not said:
        said = " ".join(getattr(word, "text", "")
                        for word in (getattr(cues, "words", ()) or ())).strip()
    quote = _quotable(said)
    phrase = _chat_phrase(chat)
    who = _channel_name(channel)

    options: list[str] = []
    if quote and phrase:
        options.append(f'Chat called it first: "{phrase}". '
                       f'Then {who} said: "{quote}".')
        options.append(f'{who} says "{quote}" — chat had already '
                       f'typed "{phrase}".')
    if quote:
        options.append(f'This is {who}, live. He says: "{quote}".')
        options.append(f'Here is the moment. {who} says: "{quote}".')
        options.append(f'{who} on stream: "{quote}".')
    if phrase:
        options.append(f'Chat said it first: "{phrase}". Then this happened.')
        options.append(f'{who}, live. Chat: "{phrase}".')
    if not options:
        return ""

    seed = (quote or phrase or who).encode("utf-8")
    text = options[zlib.crc32(seed) % len(options)]
    text = " ".join(text.split())
    limit = max(6, int(max_words))
    if len(text.split()) > limit:
        words = text.split()[:limit]
        text = " ".join(words).rstrip(",;:") + "."
    return _trim(text)


# --------------------------------------------------------------------------- #
# the voice
# --------------------------------------------------------------------------- #

def piper_binary() -> str | None:
    found = shutil.which("piper")
    if found:
        return found
    names = ("piper.exe", "piper") if os.name == "nt" else ("piper",)
    for folder in (ROOT / "models" / "piper", ROOT / "models" / "piper" / "piper",
                   ROOT / ".clipper" / "models" / "piper"):
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return str(candidate)
    return None


def piper_model(explicit: str | None = None) -> Path | None:
    for value in (explicit, os.environ.get("CLIPPER_VOICE")):
        if not value:
            continue
        path = Path(value)
        if path.is_dir():
            found = sorted(path.glob("*.onnx"))
            if found:
                return found[0]
        elif path.exists():
            return path
    for folder in VOICE_DIRS:
        try:
            found = sorted(folder.glob("*.onnx"))
        except OSError:
            continue
        if found:
            return found[0]
    return None


def engine_name(explicit: str | None = None) -> tuple[str, str]:
    """Pick the best available engine. Returns ``(name, description)``."""
    if piper_binary() and piper_model(explicit):
        model = piper_model(explicit)
        return "piper", f"piper · {Path(model).stem}"
    if have("edge-tts"):
        return "edge-tts", "edge-tts · en-US-GuyNeural"
    if platform.system() == "Darwin" and have("say"):
        return "say", "macOS say"
    return "google", "Google Translate TTS (online)"


def _exec(cmd: list[str], text: str | None = None, timeout: float = 120.0):
    return subprocess.run(cmd, input=text, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout)


def _to_wav(source: Path, out: Path) -> bool:
    result = _exec([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", str(source), "-ar", "48000", "-ac", "2",
                    str(out)], timeout=120)
    return result.returncode == 0 and out.exists()


def _speak_piper(text: str, out: Path, explicit: str | None) -> bool:
    binary = piper_binary()
    model = piper_model(explicit)
    if not binary or not model:
        return False
    result = _exec([binary, "--model", str(model), "--output_file", str(out)],
                   text=text + "\n", timeout=180)
    return result.returncode == 0 and out.exists()


def _speak_edge(text: str, out: Path) -> bool:
    raw = out.with_suffix(".mp3")
    result = _exec(["edge-tts", "--voice", "en-US-GuyNeural",
                    "--text", text, "--write-media", str(raw)], timeout=180)
    ok = result.returncode == 0 and raw.exists()
    if ok:
        ok = _to_wav(raw, out)
    try:
        raw.unlink()
    except OSError:
        pass
    return ok


def _speak_say(text: str, out: Path, explicit: str | None) -> bool:
    raw = out.with_suffix(".aiff")
    args = ["say", "-r", "180", "-o", str(raw)]
    if explicit:
        args += ["-v", explicit]
    result = _exec(args + [text], timeout=180)
    if result.returncode != 0 and explicit:
        result = _exec(["say", "-o", str(raw), text], timeout=180)
    ok = result.returncode == 0 and raw.exists()
    if ok:
        ok = _to_wav(raw, out)
    try:
        raw.unlink()
    except OSError:
        pass
    return ok


def _speak_sapi(text: str, out: Path, work: Path) -> bool:
    if not have("powershell") and not have("powershell.exe"):
        return False
    script = work / "speak.ps1"
    note = work / "speak.txt"
    note.write_text(text, encoding="utf-8")
    wav = str(out).replace("'", "''")
    txt = str(note).replace("'", "''")
    script.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        f"$t = Get-Content -Raw -Encoding UTF8 '{txt}'\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.Rate = 1\n"
        f"$s.SetOutputToWaveFile('{wav}')\n"
        "$s.Speak($t)\n"
        "$s.Dispose()\n",
        encoding="utf-8")
    shell = "powershell.exe" if have("powershell.exe") else "powershell"
    result = _exec([shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script)], timeout=180)
    return result.returncode == 0 and out.exists()


def _speak_google(text: str, out: Path, work: Path) -> bool:
    payload = urlquote(text[:MAX_SCRIPT_CHARS])
    url = ("https://translate.google.com/translate_tts"
           f"?ie=UTF-8&client=tw-ob&tl=en&q={payload}")
    request = Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Referer": "https://translate.google.com/",
    })
    raw = work / "voice.mp3"
    try:
        with urlopen(request, timeout=25) as response:
            data = response.read()
    except Exception:  # noqa: BLE001 - network is unreliable by nature
        return False
    if not data:
        return False
    raw.write_bytes(data)
    ok = _to_wav(raw, out)
    try:
        raw.unlink()
    except OSError:
        pass
    return ok


def speak(text: str, out: Path, name: str | None = None,
          log=lambda message: None) -> tuple[Path | None, str]:
    """Render ``text`` to a wav. Returns ``(path, engine)`` or ``(None, note)``."""
    text = (text or "").strip()
    if not text:
        return None, "empty script"
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent
    order = ["piper", "edge-tts", "say", "google", "sapi"]
    preferred, _ = engine_name(name)
    if preferred:
        order = [preferred] + [item for item in order if item != preferred]

    for engine in order:
        try:
            if engine == "piper" and _speak_piper(text, out, name):
                return out, "piper"
            if engine == "edge-tts" and _speak_edge(text, out):
                return out, "edge-tts"
            if engine == "say" and platform.system() == "Darwin" \
                    and _speak_say(text, out, name):
                return out, "say"
            if engine == "google" and _speak_google(text, out, work):
                return out, "google"
            if engine == "sapi" and os.name == "nt" \
                    and _speak_sapi(text, out, work):
                return out, "sapi"
        except Exception as exc:  # noqa: BLE001 - one engine failing is normal
            log(f"voice: {engine} failed — {str(exc)[:120]}")
    return None, "no speech engine produced audio"


# --------------------------------------------------------------------------- #
# the mix
# --------------------------------------------------------------------------- #

def mix(media: Path, voice_wav: Path, out: Path,
        log=lambda message: None) -> bool:
    """Duck the clip under the narration and write ``out``.

    The video is stream-copied; only the audio is re-encoded, so a 30 s clip
    costs about a second. On failure the caller keeps the un-narrated render.
    """
    duration = float(probe(media).get("duration") or 0.0)
    spoken = float(probe(voice_wav).get("duration") or 0.0)
    if duration <= 0 or spoken <= 0:
        log("voice: nothing to mix")
        return False

    lead = min(duration * 0.6, max(spoken + 0.35, 2.0))
    duck = 0.18
    ramp = 0.45
    expr = (f"if(lt(t,{lead:.3f}),{duck:.3f},"
            f"if(lt(t,{lead + ramp:.3f}),"
            f"{duck:.3f}+{1.0 - duck:.3f}*(t-{lead:.3f})/{ramp:.3f},1))")

    for normalize in ("0", ""):
        graph = (
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume='{expr}':eval=frame[base];"
            "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo,adelay=180|180,volume=1.0[vo];"
            f"[base][vo]amix=inputs=2:duration=first:dropout_transition=0"
            + (":normalize=0" if normalize else "")
            + ",alimiter=limit=0.95[aout]"
        )
        tmp = out.with_suffix(".voice.mp4")
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(media), "-i", str(voice_wav),
            "-filter_complex", graph,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart", "-t", f"{duration:.3f}", str(tmp),
        ]
        result = _exec(args, timeout=300)
        if result.returncode == 0 and tmp.exists():
            os.replace(tmp, out)
            log(f"voice: {spoken:.1f}s narration over {lead:.1f}s, "
                f"stream audio ducked")
            return True
        try:
            tmp.unlink()
        except OSError:
            pass
    detail = (result.stderr or "").strip().splitlines()
    log(f"voice: mix failed — {detail[-1][:160] if detail else 'ffmpeg failed'}")
    return False


PIPER_VOICE = "en_US-ryan-high"
PIPER_VOICE_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                   "en/en_US/ryan/high")


def _fetch(url: str, dest: Path, timeout: float = 300.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "clipper/1"})
    partial = dest.with_suffix(dest.suffix + ".part")
    with urlopen(request, timeout=timeout) as response, open(partial, "wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(partial, dest)
    return dest


def download(dest: Path) -> list[Path]:
    """Fetch the piper narrator voice (model + config) into ``dest``."""
    dest = Path(dest)
    files: list[Path] = []
    for name in (f"{PIPER_VOICE}.onnx", f"{PIPER_VOICE}.onnx.json"):
        path = dest / name
        if not path.exists():
            _fetch(f"{PIPER_VOICE_URL}/{name}", path)
        files.append(path)
    return files


def preview(text: str, out: Path, name: str | None = None) -> tuple[bool, str]:
    """Write a voice sample for the console (no clip needed)."""
    path, note = speak(text, out, name)
    return (path is not None), note