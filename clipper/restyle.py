"""Apply an editing style to clips after the session.

The live engine writes capture-only clips: split frame, no burned-in captions,
no hook. That keeps a long stream cheap. This module is the second half: point
it at the clips worth posting, pick a style, and it transcribes, picks a
grounded hook, chooses a few punch-ins, and renders one styled copy per input.

Nothing here runs on the live hot path, so it is allowed to be slower and more
thorough than the engine.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import styles as styles_mod
from .cutter import pick_title
from .util import safe_name

# Words that make a punch-in land: long, concrete, and usually the stressed
# part of a sentence. Stopwords are noise at any length.
PUNCH_STOPWORDS = {
    "because", "actually", "literally", "something", "anything", "everything",
    "probably", "definitely", "understand", "remember", "anyway", "already",
    "really", "maybe", "people", "little", "gonna", "wanna",
}


def _sidecar(clip: Path) -> dict:
    try:
        data = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _words(cues) -> list:
    words = list(getattr(cues, "words", ()) or ())
    if words:
        return words
    found = []
    for cue in cues or ():
        text = re.sub(r"\s+", " ", str(getattr(cue, "text", ""))).strip()
        if not text:
            continue
        parts = text.split()
        span = max(0.01, (cue.end - cue.start) / max(1, len(parts)))
        found += [type(cue)(cue.start + i * span,
                            cue.start + (i + 1) * span, word)
                  for i, word in enumerate(parts)]
    return found


def pick_emphasis(cues, limit: int = 6) -> list[str]:
    """The words worth colouring gold, in spoken order, without duplicates."""
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for word in _words(cues):
        text = re.sub(r"^[^A-Za-z0-9']+|[^A-Za-z0-9']+$", "", word.text)
        key = text.lower()
        if len(text) < 5 or key in PUNCH_STOPWORDS or key in seen:
            continue
        score = len(text)
        if text.isupper():
            score += 4
        if any(ch in word.text for ch in "!?"):
            score += 3
        ranked.append((score, text))
        seen.add(key)
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [word for _, word in ranked[:max(0, limit)]]


def plan_punches(cues, duration: float, style: styles_mod.Style) -> list[dict]:
    """Pick 0-3 short punch-ins on the most stressed words, spread apart."""
    if style.punch_count <= 0 or duration < 2.0:
        return []
    candidates = []
    for word in _words(cues):
        text = re.sub(r"[^A-Za-z0-9']", "", word.text)
        if len(text) < 4 or text.lower() in PUNCH_STOPWORDS:
            continue
        if word.start < 1.0 or word.start > duration - 1.0:
            continue
        score = len(text) + (3 if text.isupper() else 0)
        candidates.append((score, float(word.start)))
    candidates.sort(reverse=True)
    chosen: list[float] = []
    for _, start in candidates:
        if len(chosen) >= style.punch_count:
            break
        if all(abs(start - other) >= 2.4 for other in chosen):
            chosen.append(start)
    punches = []
    for start in sorted(chosen):
        punches.append({
            "start": round(max(0.0, start - 0.12), 3),
            "end": round(min(duration, start + style.punch_len), 3),
            "zoom": style.punch_zoom,
            "x": 0.5,
            "y": 0.55,
        })
    return punches


def _generated_title(title: str, clip: Path) -> bool:
    """True when a sidecar title is just the file's own generated name."""
    text = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    if not text:
        return True
    if text == clip.stem.lower():
        return True
    return bool(re.search(r"\d{8}[-_]\d{6}", text))


def describe_cues(cues, max_words: int = 7, max_chars: int = 42) -> str:
    """A short descriptive line from the opening words, for a top title.

    Some looks (a letterbox reveal) want a statement about the clip rather than
    a quote. The hook picker handles quotes, questions and chat consensus; when
    it finds none, this falls back to the first useful clause so the style still
    gets a title instead of silently rendering bare.
    """
    text = " ".join(str(cue.text) for cue in cues or ())
    text = re.sub(r"\s+", " ", text).strip()
    # Strip spoken filler from the front, repeatedly: "I think like she just
    # I think we go haystack" opens with five filler words before the point.
    filler = (r"(?:oh|bro|yo|like|and|so|um+|uh+|just|man|dude|literally|"
              r"actually|basically|i mean|i think|you know)")
    for _ in range(4):
        text = re.sub(rf"^{filler}\b[ ,]+", "", text, flags=re.IGNORECASE)
    words = text.split()
    if not words:
        return ""
    line = " ".join(words[:max_words])
    if len(line) > max_chars:
        line = line[:max_chars].rsplit(" ", 1)[0]
    return line.strip(" ,.!?-").upper()


def _output_path(folder: Path, stem: str, style: str) -> Path:
    base = f"{stem}_{safe_name(style, 20) or 'style'}"
    path = folder / f"{base}.mp4"
    index = 2
    while path.exists():
        path = folder / f"{base}-{index}.mp4"
        index += 1
    return path


def style_clip(cfg, clip: Path, style: styles_mod.Style, log,
               should_stop=lambda: False) -> dict | None:
    """Render one styled copy of ``clip``. Returns metadata, or None on failure."""
    from . import edit as edit_mod
    from .cutter import Cutter

    cutter = Cutter(cfg)
    prepared = cutter.prepare_file(clip, f"style_{int(time.time() * 1000)}")
    if prepared is None:
        log(f"style: could not open {clip.name}")
        return None
    try:
        meta = _sidecar(clip)
        cues: list = []
        note = "not transcribed"
        wants_words = (style.captions != "off" or style.hook
                       or style.cold_open)
        if wants_words:
            cues, note = cutter.transcribe(prepared, streamer=False, log=log)
            if cues:
                log(f"style: {clip.name} — {len(cues)} cue(s) via {note}")
            else:
                log(f"style: {clip.name} — no speech found ({note})")

        caption_mode = style.captions if style.captions != "off" else "block"
        caption_style = caption_mode if caption_mode in ("pop", "block") else "pop"
        emphasis = pick_emphasis(cues)
        punches = plan_punches(cues, prepared.duration, style)

        if style.cold_open and cues:
            plan = edit_mod.plan(cues, focus=None, duration=prepared.duration)
            if plan is not None:
                edited = edit_mod.assemble(prepared, plan,
                                           encoder=cutter.encoder, log=log)
                if edited is not None:
                    cues = edit_mod.retime(cues, plan)
                    prepared = edited
                    log(f"style: {clip.name} — cold open added")

        title = ""
        if style.hook:
            from .config import Config

            hook_cfg = Config(title_mode="auto")
            hook_cfg.subs_prompt = getattr(cfg, "subs_prompt", None)
            hook_cfg.title = getattr(cfg, "title", "") or ""
            if hook_cfg.title:
                hook_cfg.title_mode = "fixed"
            title = pick_title(hook_cfg, meta.get("signals", {}) or {},
                               cues, meta.get("chat_excerpt", []) or [])
            if not title:
                # A live trigger titled the clip from chat, which is better than
                # a description of the opening line. A dropped file has only
                # its filename there, which is never a hook.
                carried = str(meta.get("title") or "").strip()
                if carried and not _generated_title(carried, clip):
                    title = carried
            if not title and (style.hook_fallback or style.hook):
                title = describe_cues(cues)

        out = _output_path(clip.parent, clip.stem, style.name)
        captured = str(meta.get("layout_used") or getattr(cfg, "layout", "split"))
        options = {
            "composed": True,
            "composed_layout": captured,
            "caption_style": caption_style,
            "caption_mode": caption_mode,
            "caption_anchor": style.caption_anchor,
            "caption_px": int(1080 * style.caption_size) if style.caption_size else 0,
            "kicker": style.kicker,
            "kicker_anchor": style.kicker_anchor,
            "kicker_px": int(1080 * style.kicker_size) if style.kicker_size else 0,
            "colour_emphasis": style.colour_emphasis,
            "subs_backend": style.backend,
            "caption_fill": style.caption_fill,
            "emphasis_fill": style.emphasis_fill,
            "uppercase": style.uppercase,
            "beat_fill": style.beat_fill,
            "emphasis": emphasis,
            "punchins": punches,
            "hook_fill": style.hook_fill,
            "hook_anchor": style.hook_anchor,
            "hook_size": style.hook_size,
        }
        if should_stop():
            return None
        ok, error, warning = cutter.render(prepared, out, title,
                                           cues if style.captions != "off" else [],
                                           options=options)
        if not ok:
            log(f"style: {clip.name} failed — {error[-240:]}")
            return None
        if warning:
            log(f"style: {clip.name} — {warning}")
        result = dict(meta)
        result.update({
            "file": out.name,
            "styled_from": clip.name,
            "style": style.name,
            "style_label": style.label,
            "title": title,
            "transcript": " ".join(cue.text for cue in cues),
            "subtitles": {"engine": note, "cues": len(cues)}
                          if style.captions != "off" else {},
            "punchins": punches,
            "emphasis": emphasis,
            "styled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        try:
            out.with_suffix(".json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
        log(f"style: {clip.name} → {out.name} "
            f"({style.label}, {len(punches)} punch-in(s))")
        return result
    finally:
        shutil.rmtree(prepared.work, ignore_errors=True)


def run(cfg, files: list[str | Path], style_name: str, *,
        log=lambda message: None, should_stop=lambda: False,
        state: dict | None = None) -> int:
    """Style a chosen list of clips. Returns a process-style exit code."""
    state = state if state is not None else {}
    style = styles_mod.resolve(style_name, Path(cfg.work_dir))
    out_root = Path(cfg.out_dir).resolve()
    chosen = []
    for name in files or []:
        path = Path(name)
        if not path.is_absolute():
            # Accept both "clip.mp4" and "xqc/clip.mp4" relative to --out,
            # and a bare name from the console's clip wall.
            path = out_root / (path if len(path.parts) > 1 else path.name)
        try:
            path = path.resolve()
            path.relative_to(out_root)
        except (OSError, ValueError):
            continue
        if path.exists() and path.suffix == ".mp4":
            chosen.append(path)
    state.update(phase="styling", total=len(chosen), index=0, clips=0,
                 style=style.name)
    if not chosen:
        state.update(phase="error", error="no clips were selected")
        return 1
    log(f"style: {style.label} on {len(chosen)} clip(s)")
    done = 0
    for index, clip in enumerate(chosen, 1):
        if should_stop():
            log("style: stopped — clips finished so far are kept")
            break
        state.update(index=index, clips=done, file=clip.name)
        result = style_clip(cfg, clip, style, log, should_stop)
        if result:
            done += 1
    state.update(phase="done", clips=done, index=min(done, len(chosen)))
    log(f"style: finished — {done} styled clip(s) → {cfg.out_dir}")
    return 0 if done else 1