"""Keep burned captions to the streamer's own voice.

Stream audio arrives as one mixed track: the game's dialogue, music and the
streamer all share it. Whisper transcribes whoever is clearest, which is often
the game, so a clip of a silent streamer ends up captioned with a cutscene.

There is no separate microphone to lean on, so clipper looks for a visual
signal instead: the webcam. When the streamer talks their lips move — when the
game talks they do not. Words spoken while the mouth is still belong to
somebody else and are dropped.

If the webcam is missing (not detected, no Vision, a face that is never
visible) the fallback is whisper.cpp's tinydiarize: it marks speaker turns, and
the speaker talking around the clip trigger is kept. That is weaker and needs a
separate model, so it is only a fallback.

Everything here is best-effort. When neither signal is available the transcript
is returned untouched: wrong-speaker captions are better than a clip with the
streamer's own words deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import camera, transcribe
from .transcribe import Cue, Cues


def rebuild(words: list[Cue], max_chars: int = 44) -> Cues:
    """Re-group gated words into block cues, the same way transcribe() does."""
    cues = transcribe.cues_from_words(words, max_chars=max_chars)
    cues = transcribe.drop_degenerate(cues)
    cues = transcribe.resegment(cues, max_chars=max_chars)
    return Cues(cues, words)


def streamer_only(cues: list[Cue], prepared, cam: str, wav: Path | None,
                  work_dir: Path, focus: float | None = None,
                  log=lambda message: None) -> tuple[list[Cue], str]:
    """Drop words that were not spoken by the streamer. Returns (cues, note)."""
    words = list(getattr(cues, "words", ()) or ())
    if not words:
        return cues, ""

    if cam:
        timeline = camera.analyse(
            ["-f", "concat", "-safe", "0", "-i", str(prepared.list_path)],
            work_dir, cam, offset=prepared.offset, duration=prepared.duration,
            log=log,
        )
        if timeline is not None:
            kept = [w for w in words if timeline.talking(w.start, w.end)]
            note = (f"webcam gate · mouth moving "
                    f"{timeline.activity * 100:.0f}% of the clip")
            if len(kept) == len(words):
                return cues, note
            if not kept:
                log(f"caption gate: no words while the streamer's mouth moved "
                    f"({len(words)} cue word(s) belong to the game)")
                return Cues(), note + " · every cue was another voice"
            dropped = len(words) - len(kept)
            log(f"caption gate: kept {len(kept)}/{len(words)} word(s) — "
                f"dropped {dropped} spoken while the mouth was still")
            return rebuild(kept), note + f" · {dropped} word(s) from other voices"

    if wav is None:
        return cues, ""
    turns = _speaker_turns(wav, prepared.duration)
    if not turns:
        return cues, ""
    chosen = _streamer_turn(turns, focus)
    if chosen is None:
        return cues, ""
    kept = [w for w in words if _in_turn(w, turns, chosen)]
    if not kept or len(kept) == len(words):
        return cues, "diarization · one speaker, nothing to separate"
    log(f"caption gate: diarization kept {len(kept)}/{len(words)} word(s)")
    return (rebuild(kept),
            f"diarization · kept speaker {chosen + 1} of "
            f"{max(t[2] for t in turns) + 1}")


def _in_turn(word: Cue, turns: list[tuple[float, float, int]],
             speaker: int) -> bool:
    middle = (word.start + word.end) / 2.0
    for start, end, index in turns:
        if index != speaker:
            continue
        if start - 0.20 <= middle <= end + 0.20:
            return True
    return False


def _streamer_turn(turns: list[tuple[float, float, int]],
                   focus: float | None) -> int | None:
    """Which speaker is the streamer, or None when it is not clear.

    The streamer is the one talking when the clip was triggered: the trigger is
    either his own reaction or chat reacting to him. Without a trigger point the
    longest-speaking turn wins, and a transcript that is almost all one speaker
    is left alone — there is nothing to separate.
    """
    speakers = {index for _, _, index in turns}
    if len(speakers) < 2:
        return None
    totals: dict[int, float] = {index: 0.0 for index in speakers}
    near: dict[int, float] = {index: 0.0 for index in speakers}
    for start, end, index in turns:
        totals[index] += end - start
        if focus is not None and start - 3.0 <= focus <= end + 3.0:
            near[index] += end - start
    total = sum(totals.values())
    if total <= 0:
        return None
    pick = max(near, key=lambda index: near[index]) if any(near.values()) \
        else max(totals, key=lambda index: totals[index])
    share = totals[pick] / total
    if share < 0.05 or share > 0.95:
        return None
    return pick


def _speaker_turns(wav: Path, duration: float | None
                   ) -> list[tuple[float, float, int]]:
    """Label the wav with speaker turns using a tinydiarize model.

    Returns ``[(start, end, speaker)]`` or [] when the model is missing, the
    decode fails, or it hears a single speaker (the common case on a solo
    stream, where there is nothing to filter).
    """
    model = transcribe.find_tdrz_model()
    binary = transcribe.whisper_binary()
    if model is None or binary is None:
        return []
    prefix = wav.with_suffix("")
    payload_path = Path(str(prefix) + "_tdrz.json")
    try:
        payload_path.unlink()
    except OSError:
        pass
    result = None
    try:
        from .util import run

        result = run([binary, "-m", str(model), "-f", str(wav), "-tdrz",
                      "-ojf", "-nt", "-np", "-of", str(prefix) + "_tdrz"],
                     timeout=300)
    except Exception:  # noqa: BLE001 - the fallback is optional
        return []
    try:
        payload = json.loads(payload_path.read_text(errors="replace"))
    except (OSError, ValueError):
        return []
    finally:
        try:
            payload_path.unlink()
        except OSError:
            pass
    if result is None or result.returncode != 0:
        return []

    turns: list[tuple[float, float, int]] = []
    speaker = 0
    for segment in payload.get("transcription", []):
        offsets = segment.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        if end <= start:
            continue
        text = "".join(token.get("text", "")
                       for token in segment.get("tokens", []))
        # tinydiarize marks a change of speaker with [_TT_n]; the text is not
        # trustworthy but the boundary is, and that is all this needs.
        if "[_TT" in text or "[SPEAKER_TURN]" in text:
            speaker += 1
        turns.append((start, end, speaker))
    if not turns or len({t[2] for t in turns}) < 2:
        return []
    if duration and duration > 0:
        turns = [(min(a, duration), min(b, duration), i) for a, b, i in turns]
    return turns