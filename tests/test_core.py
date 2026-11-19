"""Core tests. No third-party test runner required.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipper import caption  # noqa: E402
from clipper import camera  # noqa: E402
from clipper import edit  # noqa: E402
from clipper import emoji as emoji_mod  # noqa: E402
from clipper import pack  # noqa: E402
from clipper import profile  # noqa: E402
from clipper import streams  # noqa: E402
from clipper import transcribe  # noqa: E402
from clipper import update as update_mod  # noqa: E402
from clipper import voice  # noqa: E402
from clipper import vod as vod_mod  # noqa: E402
from clipper.chat import ChatEvent, normalize_channel  # noqa: E402
from clipper.config import Config, parse_cam  # noqa: E402
from clipper.cutter import (Cutter, cam_window, caption_anchor_y,  # noqa: E402
                            chat_reaction, duo_geometry, duo_graph,
                            hook_from_chat,
                            hook_from_cues, hook_keyword, hook_question,
                            pick_title, split_geometry, split_graph)
from clipper.engine import Engine  # noqa: E402
from clipper.hls import HLSBuffer, parse_playlist  # noqa: E402
from clipper.hls import Segment  # noqa: E402
from clipper.restyle import _generated_title, describe_cues  # noqa: E402
from clipper.signals import AudioPoint, Signals  # noqa: E402


def playlist(seq: int, entries: list[tuple[float, str]]) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for duration, name in entries:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(name)
    return "\n".join(lines) + "\n"


class HLSBufferTest(unittest.TestCase):
    def test_parse(self):
        seq, items = parse_playlist(playlist(7, [(2.0, "seg_7.ts"), (1.5, "seg_8.ts")]))
        self.assertEqual(seq, 7)
        self.assertEqual(items, [(2.0, "seg_7.ts"), (1.5, "seg_8.ts")])

    def test_timeline_survives_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = HLSBuffer(Path(tmp))
            (Path(tmp) / "index.m3u8").write_text(
                playlist(0, [(2.0, "a.ts"), (2.0, "b.ts")])
            )
            self.assertTrue(buf.poll())
            self.assertAlmostEqual(buf.stream_now, 4.0)
            # muxer rotates the window: first segment deleted, one new appended
            (Path(tmp) / "index.m3u8").write_text(
                playlist(1, [(2.0, "b.ts"), (3.0, "c.ts")])
            )
            buf.poll()
            self.assertAlmostEqual(buf.stream_now, 7.0)
            self.assertEqual([s.seq for s in buf.segments], [1, 2])
            self.assertAlmostEqual(buf.segments[1].start, 4.0)
            self.assertAlmostEqual(buf.segments[1].end, 7.0)
            self.assertEqual(len(buf.between(2.5, 5.0)), 2)

    def test_restart_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = HLSBuffer(Path(tmp))
            path = Path(tmp) / "index.m3u8"
            path.write_text(playlist(40, [(2.0, "x.ts")]))
            buf.poll()
            path.write_text(playlist(0, [(2.0, "x.ts")]))
            buf.poll()
            self.assertEqual(buf.restarts, 1)
            self.assertAlmostEqual(buf.stream_now, 2.0)


class SignalsTest(unittest.TestCase):
    def cfg(self, **kwargs) -> Config:
        base = Config(input="x", chat="twitch:test")
        for key, value in kwargs.items():
            setattr(base, key, value)
        return base

    def _calm(self, signals: Signals, seconds: float = 40.0,
              users: int = 4, audio: bool = True) -> None:
        """Feed a quiet chat (+ steady audio) baseline and score it in."""
        t = 0.0
        while t < seconds:
            signals.add_chat(ChatEvent(t=t, text="nice", user=f"u{int(t) % users}"))
            if audio:
                signals.add_audio(AudioPoint(t, t + 2, -45.0, -20.0))
            signals.score(t + 2)
            t += 2.0

    def _feed(self, signals: Signals, t: float, seconds: float,
              db: float, peak_db: float) -> tuple[float, float]:
        """Advance the clock: a segment every 2s, a score every 0.2s."""
        next_seg = t
        end = t + seconds
        peak = 0.0
        while t < end:
            if t >= next_seg - 1e-9:
                signals.add_audio(AudioPoint(t, t + 2, db, peak_db))
                next_seg += 2.0
            peak = max(peak, signals.score(t)["score"])
            t += 0.2
        return t, peak

    def test_chat_consensus_burst_triggers(self):
        signals = Signals(self.cfg(chat="twitch:test"))
        self._calm(signals, audio=False)
        t = 40.0
        # 5s of many *different* users saying the same thing
        for i in range(40):
            signals.add_chat(ChatEvent(t=40 + i * 0.12, text="NO WAY KEKW",
                                       user=f"fan{i % 10}"))
        peak = 0.0
        for _ in range(30):
            peak = max(peak, signals.score(t)["score"])
            t += 0.2
        self.assertGreater(peak, 0.62, signals.last)

    def test_single_user_spam_does_not_trigger(self):
        signals = Signals(self.cfg(chat="twitch:test"))
        t = 0.0
        while t < 40:
            signals.add_chat(ChatEvent(t=t, text="nice", user="a"))
            signals.score(t + 2)
            t += 2.0
        for i in range(80):
            signals.add_chat(ChatEvent(t=40 + i * 0.05, text="NO WAY KEKW",
                                       user="spammer"))
        peak = 0.0
        for _ in range(40):
            peak = max(peak, signals.score(t)["score"])
            t += 0.2
        self.assertLess(peak, 0.5, signals.last)

    def test_quiet_chat_does_not_trigger(self):
        signals = Signals(self.cfg())
        t = 0.0
        peak = 0.0
        next_message = 0.0
        while t < 40:
            if t >= next_message:
                signals.add_chat(ChatEvent(t=t, text="nice", user="a"))
                next_message += 3.0
            peak = max(peak, signals.score(t)["score"])
            t += 0.2
        self.assertLess(peak, 0.4, signals.last)

    def test_audio_novelty_beats_its_baseline(self):
        signals = Signals(self.cfg(chat=None))
        t, _ = self._feed(signals, 0.0, 60.0, -45.0, -20.0)
        _, peak = self._feed(signals, t, 12.0, -22.0, -3.0)
        self.assertGreater(peak, 0.7, signals.last)

    def test_steady_loud_audio_is_not_novel(self):
        # The whole point: a stream that is *always* loud must not trigger.
        signals = Signals(self.cfg(chat=None))
        _, peak = self._feed(signals, 0.0, 140.0, -20.0, -3.0)
        self.assertLess(peak, 0.4, signals.last)

    def test_a_single_audio_spike_is_not_sustained(self):
        signals = Signals(self.cfg(chat=None))
        t, _ = self._feed(signals, 0.0, 80.0, -45.0, -20.0)
        signals.add_audio(AudioPoint(t, t + 2, -22.0, -3.0))
        peak = 0.0
        end = t + 12.0
        while t < end:
            peak = max(peak, signals.score(t)["score"])
            t += 0.2
        self.assertLess(peak, 0.58, signals.last)

    def test_two_live_signals_must_agree(self):
        signals = Signals(self.cfg())  # chat and audio both enabled
        self._calm(signals)
        t = 40.0
        # audio spikes but chat stays calm: one signal alone must be capped
        for i in range(6):
            t += 2.0
            signals.add_audio(AudioPoint(t, t + 2, -22.0, -3.0))
        peak = 0.0
        for _ in range(30):
            t += 0.2
            peak = max(peak, signals.score(t)["score"])
        self.assertLess(peak, 0.55, signals.last)

    def test_transcript_verification_is_a_signal(self):
        signals = Signals(self.cfg())
        signals.add_transcript("oh my god what is happening", 1.0)
        result = signals.score(1.0)
        self.assertGreater(result["transcript"], 0.5)
        self.assertGreaterEqual(result["agreeing"], 1)

    def test_no_chat_means_audio_only(self):
        signals = Signals(self.cfg(chat=None))
        result = signals.score(1.0)
        self.assertEqual(result["chat"], 0.0)
        self.assertNotIn("chat_rate", [k for k, v in result.items() if v])


class CaptionTest(unittest.TestCase):
    def test_png_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.png"
            width, height = caption.render("INSANE REACTION", 70, path, 972)
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG"))
            self.assertGreater(width, 100)
            self.assertGreater(height, 40)
            self.assertLessEqual(width, 972)

    def test_wrap_and_sanitize(self):
        self.assertEqual(caption.sanitize("héllo💀"), "H?LLO?")
        self.assertEqual(caption.wrap("A B C", 3), ["A B", "C"])

    def test_overlay_expressions_use_main_height(self):
        x, y = caption.overlay_expr("vertical")
        self.assertIn("main_h", y)
        self.assertIn("main_w", x)

    def test_subtitle_sits_below_the_hook_caption(self):
        _, hook_y = caption.overlay_expr("vertical")
        _, sub_y = caption.subtitle_expr("vertical")
        self.assertEqual(hook_y, "main_h*0.66")
        self.assertEqual(sub_y, "main_h*0.80")

    def test_split_layout_keeps_captions_under_the_gameplay(self):
        # gameplay ends at (760 + 608) / 1920 ≈ 0.7125; subtitles must sit below it
        _, sub_y = caption.subtitle_expr("split")
        _, hook_y = caption.overlay_expr("split")
        self.assertIn("main_h*0.765", sub_y)
        self.assertIn("main_h*0.905", hook_y)

    def test_duo_hook_sits_under_the_camera_band(self):
        # Camera is the top 840/1920 = 0.4375; the hook starts just below it.
        _, hook_y = caption.overlay_expr("duo", "pop")
        self.assertEqual(hook_y, "main_h*0.46")

    @unittest.skipUnless(caption.has_styled(), "ImageMagick not installed")
    def test_styled_caption_is_a_transparent_rgba_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styled.png"
            size = caption.render_styled("NO WAY DUDE THAT WAS INSANE", 60, path, 900)
            self.assertIsNotNone(size)
            width, height = size
            self.assertGreater(width, 100)
            self.assertGreater(height, 40)
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG"))
            # IHDR byte 25 is the colour type; 6 == truecolour with alpha.
            self.assertEqual(data[25], 6)


class EmojiTest(unittest.TestCase):
    def test_emoji_are_detected_and_split_into_runs(self):
        self.assertTrue(caption.has_emoji("NO WAY 🔥"))
        self.assertFalse(caption.has_emoji("NO WAY (100%) ©"))
        self.assertEqual(caption.strip_emoji("NO WAY 🔥 💀"), "NO WAY")
        self.assertEqual(caption.runs("NO WAY 🔥"),
                         [("NO WAY ", False), ("🔥", True)])
        self.assertEqual(caption.runs("🔥🔥 ok"),
                         [("🔥🔥", True), (" ok", False)])

    def test_emoji_survive_sanitize_only_when_asked(self):
        self.assertEqual(caption.sanitize("OK 🔥"), "OK ?")
        self.assertEqual(caption.sanitize("OK 🔥", emoji=True), "OK 🔥")

    def test_one_user_is_not_a_reaction(self):
        self.assertEqual(chat_reaction([{"user": "a", "text": "🔥🔥"}]), "")
        self.assertEqual(chat_reaction([{"user": "a", "text": "🔥"},
                                        {"user": "b", "text": "lol"}]), "")

    def test_two_users_make_the_reaction(self):
        chat = [{"user": "a", "text": "🔥"},
                {"user": "b", "text": "🔥🔥"},
                {"user": "c", "text": "😂"}]
        self.assertEqual(chat_reaction(chat), "🔥")

    def test_hook_borrows_the_chat_reaction(self):
        chat = [{"user": "a", "text": "LOOK AT THE MAP 🔥"},
                {"user": "b", "text": "look at the map 🔥"}]
        self.assertEqual(pick_title(Config(title_mode="auto"), {}, [], chat),
                         "CHAT SAYS LOOK AT THE MAP 🔥")

    def test_a_fixed_hook_keeps_the_author_s_emoji(self):
        cfg = Config(title_mode="fixed", title="COOKED 💀")
        self.assertEqual(pick_title(cfg, {}, [], []), "COOKED 💀")

    @unittest.skipUnless(caption.can_render_emoji(), "no emoji face")
    def test_styled_hook_draws_the_emoji_as_colour(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hook.png"
            size = caption.render_styled(
                "NO WAY 🔥", 60, path, 900, font=caption.hook_font(),
                fill=caption.HOOK_FILL, char_width=0.72, emoji=True)
            self.assertIsNotNone(size)
            # The gold text alone is a handful of flat shades; a real colour
            # emoji brings dozens.
            out = subprocess.run([caption.magick_binary(), str(path),
                                  "-format", "%k", "info:"],
                                 capture_output=True, text=True)
            self.assertGreater(int(out.stdout.strip() or 0), 40)

    @unittest.skipUnless(caption.emoji_font(), "no emoji font installed")
    def test_the_font_hands_back_the_real_bitmap(self):
        data = emoji_mod.glyph_png(caption.emoji_font() or "", "🔥")
        if data is None:
            self.skipTest("font has no sbix bitmaps")
        self.assertTrue(data.startswith(b"\x89PNG"))


class PopCaptionTest(unittest.TestCase):
    def test_pop_text_keeps_only_safe_characters(self):
        self.assertEqual(caption.pop_text("oh @my %god"), "OH MY GOD")
        self.assertEqual(caption.pop_text("NO WAY!"), "NO WAY!")
        self.assertEqual(caption.pop_text(""), "")

    def test_pop_moves_the_hook_off_the_caption_band(self):
        _, hook = caption.overlay_expr("vertical", "pop")
        _, sub = caption.subtitle_expr("vertical", "pop")
        self.assertIn("main_h*0.085", hook)
        self.assertIn("main_h*0.70", sub)

    def test_block_positions_are_unchanged(self):
        self.assertEqual(caption.overlay_expr("vertical")[1], "main_h*0.66")
        self.assertEqual(caption.subtitle_expr("vertical")[1], "main_h*0.80")

    @unittest.skipUnless(caption.has_styled(), "ImageMagick not installed")
    def test_render_pop_is_a_transparent_pill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pop.png"
            size = caption.render_pop("WHAT IS HAPPENING", path, 972, 100)
            self.assertIsNotNone(size)
            width, height = size
            self.assertGreater(width, 100)
            self.assertLessEqual(width, 972)
            self.assertGreater(height, 40)
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG"))
            self.assertEqual(data[25], 6)


class PopChunkTest(unittest.TestCase):
    def test_word_timings_cover_the_cue(self):
        cues = [transcribe.Cue(1.0, 3.0, "NO WAY DUDE")]
        words = transcribe.word_timings(cues)
        self.assertEqual([w.text for w in words], ["NO", "WAY", "DUDE"])
        self.assertAlmostEqual(words[0].start, 1.0)
        self.assertAlmostEqual(words[-1].end, 3.0)
        self.assertTrue(all(w.end > w.start for w in words))

    def test_pop_chunks_are_short_and_ordered(self):
        cues = [transcribe.Cue(0.0, 9.0,
                               "oh my god dude that was the craziest thing "
                               "i have ever seen clip it")]
        chunks = transcribe.pop_chunks(cues)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text.split()), 3, chunk.text)
        self.assertAlmostEqual(chunks[0].start, 0.0)
        self.assertTrue(all(a.start <= b.start for a, b in zip(chunks, chunks[1:])))
        self.assertLessEqual(max(len(c.text) for c in chunks), 18)

    def test_a_short_cue_stays_whole(self):
        chunks = transcribe.pop_chunks([transcribe.Cue(0.0, 2.0, "NO WAY DUDE")])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "NO WAY DUDE")

    def test_real_word_timings_are_used_when_present(self):
        # A cue spanning 9s but word offsets that say the words are much
        # shorter: the chunks must follow the real offsets, not a guess.
        words = [transcribe.Cue(0.0, 0.5, "OH"),
                 transcribe.Cue(2.0, 2.5, "MY"),
                 transcribe.Cue(2.5, 3.0, "GOD")]
        cues = transcribe.Cues([transcribe.Cue(0.0, 9.0, "OH MY GOD")], words)
        chunks = transcribe.pop_chunks(cues)
        self.assertEqual(" ".join(c.text for c in chunks), "OH MY GOD")
        self.assertAlmostEqual(chunks[-1].end, 3.0)

    def test_a_trailing_stub_is_held_not_flashed(self):
        words = [transcribe.Cue(0.0, 2.0, "NO"),
                 transcribe.Cue(2.0, 2.9, "WAY"),
                 transcribe.Cue(2.9, 2.95, "BRO")]
        chunks = transcribe.pop_chunks(transcribe.Cues([], words))
        self.assertGreaterEqual(chunks[-1].end - chunks[-1].start, 0.45)

    def test_a_pause_starts_a_new_chunk_and_leaves_a_gap(self):
        words = [transcribe.Cue(0.0, 0.6, "BRO"),
                 transcribe.Cue(0.6, 1.2, "LOOK"),
                 transcribe.Cue(3.4, 4.0, "MAP")]
        chunks = transcribe.pop_chunks(transcribe.Cues([], words))
        self.assertEqual([c.text for c in chunks], ["BRO LOOK", "MAP"])
        self.assertGreater(chunks[1].start - chunks[0].end, 1.0)

    def test_a_finished_sentence_ends_the_chunk(self):
        words = [transcribe.Cue(0.0, 0.5, "HAYSTACK"),
                 transcribe.Cue(0.5, 0.9, "CHAT."),
                 transcribe.Cue(0.9, 1.1, "I"),
                 transcribe.Cue(1.1, 1.5, "THINK")]
        chunks = transcribe.pop_chunks(transcribe.Cues([], words))
        self.assertEqual([c.text for c in chunks],
                         ["HAYSTACK CHAT.", "I THINK"])

    def test_a_long_word_does_not_hold_the_caption(self):
        words = [transcribe.Cue(0.0, 0.4, "WAIT"),
                 transcribe.Cue(0.4, 2.4, "WHAT")]
        chunks = transcribe.pop_chunks(transcribe.Cues([], words))
        self.assertLessEqual(chunks[-1].end - chunks[-1].start, 2.0)

    def test_cues_behave_like_a_plain_list(self):
        cues = transcribe.Cues([transcribe.Cue(0.0, 1.0, "HI")],
                               [transcribe.Cue(0.0, 0.4, "HI")])
        self.assertEqual(cues, [transcribe.Cue(0.0, 1.0, "HI")])
        self.assertEqual([c.text for c in cues], ["HI"])
        self.assertIsInstance(cues, list)


class TranscribeTest(unittest.TestCase):
    SRT = (
        "1\n"
        "00:00:00,000 --> 00:00:04,000\n"
        " no way dude that was\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:30,000\n"
        " insane, clip it right now!\n"
    )

    def test_parse_srt(self):
        cues = transcribe.parse_srt(self.SRT)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 0.0)
        self.assertAlmostEqual(cues[0].end, 4.0)
        self.assertEqual(cues[0].text, "no way dude that was")
        self.assertAlmostEqual(cues[1].start, 4.5)
        self.assertAlmostEqual(cues[1].end, 30.0)
        self.assertEqual(cues[1].text, "insane, clip it right now!")

    def test_parse_srt_skips_non_speech_annotations(self):
        cues = transcribe.parse_srt(
            "1\n00:00:00,000 --> 00:00:01,000\n[Music]\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n(Applause)\n"
        )
        self.assertEqual(cues, [])
        self.assertEqual(transcribe.parse_srt("garbage"), [])

    def test_parse_token_words_merges_subwords_and_punctuation(self):
        payload = {"transcription": [{"tokens": [
            {"text": " Okay", "offsets": {"from": 0, "to": 1700}},
            {"text": ",", "offsets": {"from": 1710, "to": 2430}},
            {"text": " it", "offsets": {"from": 2610, "to": 3300}},
            {"text": "'s", "offsets": {"from": 3400, "to": 4260}},
            {"text": " Sub", "offsets": {"from": 4260, "to": 5000}},
            {"text": "ic", "offsets": {"from": 5000, "to": 5400}},
        ]}]}
        words = transcribe.parse_token_words(payload)
        self.assertEqual([w.text for w in words], ["Okay,", "it's", "Subic"])
        self.assertAlmostEqual(words[0].start, 0.0)
        self.assertAlmostEqual(words[0].end, 2.43)
        self.assertAlmostEqual(words[1].start, 2.61)
        self.assertAlmostEqual(words[1].end, 4.26)

    def test_parse_token_words_drops_collapsed_padding(self):
        # whisper keeps decoding past the audio and pins every trailing token to
        # one instant; those must not become captions.
        payload = {"transcription": [{"tokens": [
            {"text": " real", "offsets": {"from": 0, "to": 900}},
            {"text": " words", "offsets": {"from": 900, "to": 1500}},
            {"text": " garbage", "offsets": {"from": 11990, "to": 11990}},
            {"text": " noise", "offsets": {"from": 11990, "to": 11990}},
            {"text": "<|endoftext|>", "offsets": {"from": 30000, "to": 30000}},
        ]}]}
        words = transcribe.parse_token_words(payload)
        self.assertEqual([w.text for w in words], ["real", "words"])

    def test_parse_token_words_never_raises(self):
        self.assertEqual(transcribe.parse_token_words({}), [])
        self.assertEqual(transcribe.parse_token_words({"transcription": [{}]}), [])
        self.assertEqual(transcribe.parse_token_words(
            {"transcription": [{"tokens": [{"text": "x"}]}]}), [])

    def test_clamp_trims_the_whisper_30s_window(self):
        cues = [transcribe.Cue(0.0, 30.0, "short clip")]
        clamped = transcribe.clamp(cues, 3.94)
        self.assertEqual(len(clamped), 1)
        self.assertAlmostEqual(clamped[0].end, 3.94)
        # cues that start after the audio end are dropped entirely
        self.assertEqual(transcribe.clamp([transcribe.Cue(9.0, 12.0, "x")], 3.0), [])
        self.assertEqual(transcribe.clamp(cues, None), cues)

    def test_resegment_splits_long_cues_by_length(self):
        cues = [transcribe.Cue(0.0, 20.0,
                               "oh my god dude that was the craziest thing "
                               "i have ever seen in my entire life clip it")]
        pieces = transcribe.resegment(cues, max_seconds=4.0, max_chars=44)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece.text), 44, piece.text)
        self.assertAlmostEqual(pieces[0].start, 0.0)
        self.assertAlmostEqual(pieces[-1].end, 20.0)
        # short cues pass through untouched
        short = [transcribe.Cue(1.0, 3.0, "clip it")]
        self.assertEqual(transcribe.resegment(short), short)

    def test_words_shift_onto_the_speech_onset_and_clamp(self):
        words = [transcribe.Cue(0.0, 1.0, "BRO"),
                 transcribe.Cue(1.0, 2.0, "HI")]
        shifted = transcribe._shift_words(words, 6.3, 8.0)
        self.assertAlmostEqual(shifted[0].start, 6.3)
        self.assertAlmostEqual(shifted[-1].end, 8.0)
        self.assertEqual(transcribe._shift_words(words, 20.0, 8.0), [])

    def test_words_inside_a_vad_silence_are_dropped(self):
        speech = [transcribe.Cue(0.0, 2.0, "a"),
                  transcribe.Cue(5.0, 8.0, "b")]
        words = [transcribe.Cue(0.2, 1.0, "keep"),
                 transcribe.Cue(2.5, 4.0, "drop"),
                 transcribe.Cue(5.2, 6.0, "keep2"),
                 transcribe.Cue(9.0, 9.5, "tail")]
        kept = transcribe._clip_to_speech(words, speech, 10.0)
        self.assertEqual([w.text for w in kept], ["keep", "keep2"])

    def test_status_never_raises(self):
        ready, detail = transcribe.status()
        self.assertIsInstance(ready, bool)
        self.assertTrue(detail)

    def test_fast_model_is_the_smallest_installed(self):
        fast = transcribe.find_fast_model()
        self.assertTrue(fast is None or fast.name in transcribe.FAST_MODEL_NAMES)

    def test_threads_leave_headroom_for_the_rest_of_the_machine(self):
        # Default must not grab every core: whisper sharing the box with a
        # browser is exactly what made the desktop stutter.
        default = transcribe.resolve_threads(None)
        self.assertGreaterEqual(default, 1)
        self.assertLessEqual(default, 4)
        self.assertEqual(transcribe.resolve_threads(0), default)
        self.assertEqual(transcribe.resolve_threads(2), 2)
        self.assertGreaterEqual(transcribe.resolve_threads(-1), 1)

    def test_drop_degenerate_removes_padding_artifacts(self):
        cues = [
            transcribe.Cue(0.0, 4.0, "a"),
            transcribe.Cue(4.0, 8.0, "b"),
            transcribe.Cue(8.0, 8.0, "b"),   # zero length, whisper padding
            transcribe.Cue(8.0, 8.0, "c"),   # zero length
            transcribe.Cue(5.0, 9.0, "b"),   # repeats the previous cue
        ]
        self.assertEqual([c.text for c in transcribe.drop_degenerate(cues)],
                         ["a", "b"])


class SplitLayoutTest(unittest.TestCase):
    def test_parse_cam(self):
        self.assertEqual(parse_cam("0:539:367:294"), (0, 539, 367, 294))
        self.assertEqual(parse_cam("0,539,367,294"), (0, 539, 367, 294))

    def test_parse_cam_rejects_junk(self):
        for bad in ("", "1:2:3", "a:b:c:d", "0:0:0:10", "0:0:10:-1"):
            with self.assertRaises(ValueError):
                parse_cam(bad)

    def test_split_graph_crops_the_cam_and_stacks_three_bands(self):
        graph = ";".join(split_graph("0:539:367:294"))
        x, y, w, h = cam_window("0:539:367:294", 1920, 1080,
                                1080 / split_geometry("0:539:367:294").cam_h)
        # The face box is widened so the band is face-and-shoulders, not a
        # 4x blow-up of the nose, and the window stays inside the frame.
        self.assertGreater(w, 367)
        self.assertIn(f"crop={w}:{h}:{x}:{y}", graph)
        self.assertIn("vstack=inputs=2", graph)
        # the three bands must add up to a full 1080x1920 frame
        geo = split_geometry("0:539:367:294")
        self.assertEqual(geo.cam_h + geo.game_h + geo.bot_h, 1920)
        self.assertIn(f"scale=1080:{geo.cam_h}", graph)
        self.assertIn(f"scale=1080:{geo.game_h}", graph)
        self.assertIn(f"scale=1080:{geo.bot_h}", graph)

    def test_the_game_is_cropped_away_from_a_side_webcam(self):
        # Jason: webcam bottom-right. The crop keeps the 1504 px on its left.
        geo = split_geometry("1504:550:294:266", 1920, 1080)
        self.assertIsNotNone(geo.crop)
        x, y, w, h = geo.crop
        self.assertEqual((x, y), (0, 0))
        self.assertLessEqual(x + w, 1504)
        self.assertGreater(w, 1920 * 0.7)
        self.assertGreater(geo.game_h, 700)      # bigger than the old 608 band
        self.assertGreaterEqual(geo.bot_h, 400)  # still room for two captions
        graph = ";".join(split_graph("1504:550:294:266", 1920, 1080))
        self.assertNotIn("patchblur", graph)     # no blurred duplicate face
        self.assertIn(f"crop={w}:{h}:0:0", graph)

    def test_a_centred_webcam_falls_back_to_blurring_the_inset(self):
        geo = split_geometry("760:400:400:300", 1920, 1080)
        self.assertIsNone(geo.crop)
        self.assertEqual(geo.game_h, 608)
        self.assertIn("patchblur", ";".join(split_graph("760:400:400:300")))

    def test_split_without_a_detected_camera_falls_back_to_blur(self):
        from clipper.cutter import Prepared
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prep = Prepared(work=tmp_path, list_path=tmp_path / "list.txt",
                            offset=0.0, duration=5.0, width=1920, height=1080,
                            title_file=tmp_path / "title.txt")
            auto = Cutter(Config(layout="split", cam="auto",
                                 work_dir=tmp_path / "work"))
            graph, _, _ = auto._graph(prep, "", [])
            self.assertNotIn("vstack", graph)
            self.assertIn("gblur=sigma=7", graph)

            pinned = Cutter(Config(layout="split", cam="1504:550:294:266",
                                   work_dir=tmp_path / "work2"))
            graph, _, _ = pinned._graph(prep, "", [])
            self.assertIn("vstack=inputs=2", graph)

    def test_hook_quotes_the_clip_not_chat(self):
        cues = [transcribe.Cue(0, 2, "no way dude"),
                transcribe.Cue(2, 4, "look at that car")]
        self.assertEqual(hook_from_cues(cues), "NO WAY")
        self.assertEqual(hook_from_cues([]), "")

    def test_hook_prefers_the_strongest_phrase(self):
        cues = [transcribe.Cue(0, 2, "oh my god what is happening")]
        self.assertEqual(hook_from_cues(cues), "WHAT IS HAPPENING")

    def test_chat_consensus_becomes_the_hook(self):
        chat = [
            {"user": "a", "text": "LOOK AT THE MAP U BLIND"},
            {"user": "b", "text": "look at the map"},
            {"user": "c", "text": "LOOK AT THE MAP"},
            {"user": "d", "text": "lol"},
        ]
        self.assertEqual(hook_from_chat(chat), "LOOK AT THE MAP")
        self.assertEqual(pick_title(Config(title_mode="auto"), {}, [], chat),
                         "CHAT SAYS LOOK AT THE MAP")

    def test_one_loud_user_cannot_invent_a_chat_hook(self):
        chat = [{"user": "a", "text": "LOOK AT THE MAP"}] * 6
        self.assertEqual(hook_from_chat(chat), "")
        self.assertEqual(hook_from_chat([{"user": "a", "text": "LOL"},
                                         {"user": "b", "text": "LOL"}]), "")

    def test_a_short_question_is_a_hook(self):
        cues = [transcribe.Cue(0, 1.5, "Where is my Tia?")]
        self.assertEqual(pick_title(Config(title_mode="auto"), {}, cues, []), "WHERE IS MY TIA?")

    def test_a_question_beats_a_generic_reaction(self):
        cues = [transcribe.Cue(0, 4.0,
                               "play. Holy fucking map! Is this the fucking "
                               "bridge I jumped off Teef")]
        self.assertEqual(pick_title(Config(title_mode="auto"), {}, cues, []),
                         "IS THIS THE FUCKING BRIDGE I JUMPED OFF?")

    def test_an_interrogative_without_a_question_mark_still_counts(self):
        cues = [transcribe.Cue(0, 2.0, "Where is the boat going")]
        self.assertEqual(hook_question(cues), "WHERE IS THE BOAT GOING?")

    def test_a_question_split_across_cues_is_kept_whole(self):
        # Whisper ends a cue on a pause, not on punctuation. Asking for the
        # hook from one cue alone used to return "WHAT ARE YOU?".
        cues = [transcribe.Cue(0.0, 1.0, "Mix it up."),
                transcribe.Cue(1.0, 3.0, "What are you"),
                transcribe.Cue(3.0, 5.0, "thinking?")]
        self.assertEqual(hook_question(cues), "WHAT ARE YOU THINKING?")

    def test_one_word_chat_consensus_is_the_last_resort(self):
        chat = [{"user": name, "text": "back"}
                for name in ("a", "b", "c", "d")]
        cues = [transcribe.Cue(0, 2.0, "The way back back I")]
        self.assertEqual(hook_keyword(chat), "BACK")
        self.assertEqual(pick_title(Config(title_mode="auto"), {}, cues, chat), "CHAT SAYS BACK")

    def test_one_person_spamming_a_word_is_not_consensus(self):
        chat = [{"user": "a", "text": "BACK"}] * 6
        self.assertEqual(hook_keyword(chat), "")

    def test_duo_is_the_default_layout(self):
        # No filler band: camera on top, gameplay filling the rest.
        self.assertEqual(Config().layout, "duo")


class CameraTest(unittest.TestCase):
    def test_persistent_face_beats_a_gameplay_guest(self):
        # The streamer is in every frame; somebody on the gameplay screen is not.
        per_frame = [[camera.Face(0.09, 0.59, 0.047, 0.085, 0.75)]
                     for _ in range(12)]
        per_frame[0].append(camera.Face(0.38, 0.22, 0.10, 0.18, 0.80))
        per_frame[1].append(camera.Face(0.38, 0.22, 0.10, 0.18, 0.80))
        face = camera.pick_face(per_frame, min_frames=5)
        self.assertIsNotNone(face)
        self.assertLess(face.cx, 0.2)  # the webcam face, not the gameplay face

    def test_a_guest_only_is_rejected(self):
        per_frame = [[camera.Face(0.4, 0.2, 0.1, 0.18, 0.8)] for _ in range(2)]
        per_frame += [[] for _ in range(8)]
        self.assertIsNone(camera.pick_face(per_frame, min_frames=5))

    def test_expand_keeps_the_face_inside_and_the_box_in_frame(self):
        face = camera.Face(0.089, 0.592, 0.047, 0.085, 0.78)
        x, y, w, h = camera.expand_to_cam(face, 1920, 1080)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 1920)
        self.assertLessEqual(y + h, 1080)
        cx = (face.x + face.w / 2) * 1920
        cy = (face.y + face.h / 2) * 1080
        self.assertTrue(x <= cx <= x + w)
        self.assertTrue(y <= cy <= y + h)

    def test_expand_clamps_a_corner_face(self):
        x, y, w, h = camera.expand_to_cam(
            camera.Face(0.0, 0.0, 0.05, 0.09, 0.7), 1920, 1080)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertGreaterEqual(w, 2)
        self.assertGreaterEqual(h, 2)

    def test_format_box(self):
        self.assertEqual(camera.format_box((14, 552, 344, 312)), "14:552:344:312")

    def test_talking_intervals_follow_the_mouth(self):
        # Eight seconds of a still mouth with one second of speech in it.
        ratios = [0.75, 0.76, 0.74, 0.75] * 12
        ratios[18:24] = [0.86, 0.90, 0.92, 0.88, 0.90, 0.86]
        timeline = camera.talking_intervals(ratios, fps=6.0, duration=8.0)
        self.assertIsNotNone(timeline)
        self.assertGreater(timeline.activity, 0.08)
        self.assertTrue(timeline.talking(3.1, 3.4))
        self.assertFalse(timeline.talking(6.0, 6.5))

    def test_a_still_mouth_is_not_talking(self):
        ratios = [0.75 + 0.004 * (index % 3) for index in range(60)]
        timeline = camera.talking_intervals(ratios, fps=6.0)
        self.assertIsNotNone(timeline)
        self.assertEqual(timeline.activity, 0.0)

    def test_talking_intervals_refuse_an_unreliable_face(self):
        ratios = [None] * 8 + [0.72] * 4
        self.assertIsNone(camera.talking_intervals(ratios, fps=6.0))

    def test_expand_keeps_a_bottom_anchored_webcam_inside_the_frame(self):
        # Measured on ohnepixel: the facecam sits on the bottom edge.
        face = camera.Face(384 / 1920, 823 / 1080, 163 / 1920, 163 / 1080)
        x, y, w, h = camera.expand_to_cam(face, 1920, 1080)
        self.assertLessEqual(y + h, 1080)
        self.assertLessEqual(x + w, 1920)
        face_middle = (face.y + face.h / 2) * 1080
        # The face stays in the upper part of the crop instead of being pushed
        # to the bottom by clamping the box to the frame.
        self.assertLessEqual(face_middle - y, h * 0.8)


class SpeakerGateTest(unittest.TestCase):
    @staticmethod
    def _cues() -> "transcribe.Cues":
        words = [transcribe.Cue(0.0, 0.3, "Game"),
                 transcribe.Cue(1.0, 1.3, "me"),
                 transcribe.Cue(5.0, 5.3, "talking")]
        return transcribe.Cues(words, words)

    def _prepared(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(list_path=Path("concat.txt"), offset=0.0,
                                     duration=6.0, work=Path(tempfile.mkdtemp()))

    def test_words_spoken_while_the_mouth_is_still_are_dropped(self):
        from clipper import speaker

        timeline = camera.TalkTimeline(((0.85, 1.6), (4.85, 5.6)), 1.0, 6.0)
        with mock.patch.object(camera, "analyse", return_value=timeline):
            gated, note = speaker.streamer_only(
                self._cues(), self._prepared(), "0:0:100:100", None, Path("."))
        self.assertEqual([word.text for word in gated.words], ["me", "talking"])
        self.assertIn("webcam gate", note)
        self.assertIn("1 word", note)

    def test_everything_is_dropped_when_only_other_voices_talk(self):
        from clipper import speaker

        timeline = camera.TalkTimeline((), 1.0, 6.0)
        with mock.patch.object(camera, "analyse", return_value=timeline):
            gated, note = speaker.streamer_only(
                self._cues(), self._prepared(), "0:0:100:100", None, Path("."))
        self.assertEqual(list(gated), [])
        self.assertIn("another voice", note)

    def test_without_a_camera_or_a_model_nothing_is_touched(self):
        from clipper import speaker

        cues = self._cues()
        with mock.patch.object(transcribe, "find_tdrz_model", return_value=None):
            gated, note = speaker.streamer_only(
                cues, self._prepared(), "", Path("speech.wav"), Path("."))
        self.assertIs(gated, cues)
        self.assertEqual(note, "")

    def test_an_unreliable_webcam_keeps_every_cue(self):
        from clipper import speaker

        cues = self._cues()
        with mock.patch.object(camera, "analyse", return_value=None):
            gated, _ = speaker.streamer_only(
                cues, self._prepared(), "0:0:100:100", None, Path("."))
        self.assertIs(gated, cues)


class EditStructureTest(unittest.TestCase):
    @staticmethod
    def _cues(duration: float = 16.0) -> "transcribe.Cues":
        words = [transcribe.Cue(index * 0.6, index * 0.6 + 0.35, f"w{index}")
                 for index in range(16)]
        lines = [transcribe.Cue(0.0, duration, " ".join(w.text for w in words))]
        return transcribe.Cues(lines, words)

    def test_plan_teases_a_middle_payoff_and_leaves_context(self):
        plan = edit.plan(self._cues(), focus=4.0, duration=16.0)
        self.assertIsNotNone(plan)
        low, high = plan.payoff
        self.assertGreater(low, 2.0)
        self.assertLess(high, 16.0)
        self.assertGreater(high - low, 2.0)
        # The tease and the replay together are longer than the source clip.
        self.assertGreater(plan.total, 16.0)

    def test_retime_duplicates_the_cold_open_words(self):
        cues = self._cues()
        plan = edit.plan(cues, focus=4.0, duration=16.0)
        retimed = edit.retime(cues, plan)
        self.assertEqual(len(retimed), 2)          # the line exists twice
        self.assertGreater(len(retimed.words), len(cues.words))
        self.assertGreaterEqual(retimed.words[0].start, 0.0)
        self.assertLess(retimed.words[0].start, plan.cold_len)
        starts = [word.start for word in retimed.words]
        self.assertEqual(starts, sorted(starts))

    def test_a_short_clip_gets_no_cold_open(self):
        self.assertIsNone(edit.plan(self._cues(), focus=2.0, duration=6.0))

    def test_a_payoff_at_the_very_start_is_not_worth_rewinding(self):
        cues = self._cues()
        self.assertIsNone(edit.plan(cues, focus=0.2, duration=16.0))


class VoiceScriptTest(unittest.TestCase):
    @staticmethod
    def _cues(text: str) -> "transcribe.Cues":
        return transcribe.Cues([transcribe.Cue(0.0, 3.0, text)], [])

    def test_the_line_quotes_what_was_actually_said(self):
        text = voice.script_for(
            "ohnepixel", self._cues("Oh, I don't want to sell my car though."), [])
        self.assertIn("sell my car", text)
        self.assertIn("ohnepixel", text)

    def test_profanity_is_never_narrated(self):
        self.assertEqual(
            voice.script_for("xqc", self._cues("this is fucking crazy"), []), "")

    def test_no_transcript_and_no_chat_means_no_voiceover(self):
        self.assertEqual(voice.script_for("xqc", [], []), "")

    def test_a_repeated_chat_phrase_can_carry_the_line(self):
        chat = [{"user": "a", "text": "LOOK AT THE MAP"},
                {"user": "b", "text": "look at the map"}]
        text = voice.script_for("jasontheween", [], chat)
        self.assertIn("look at the map", text.lower())


class PostingPackTest(unittest.TestCase):
    def test_one_row_per_clip_with_a_title_and_hashtags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.mp4").write_bytes(b"x")
            (root / "clip.json").write_text(json.dumps({
                "title": "CHAT SAYS LOOK AT THE MAP",
                "transcript": "look at the map before you jump",
                "channel": "jasontheween",
                "duration_s": 27.0,
                "created_at": "2026-11-18T18:30:00",
                "chat_excerpt": [{"user": "a", "text": "LOOK AT THE MAP"}],
            }), encoding="utf-8")

            target, rows = pack.build(root)

            self.assertEqual(rows, 1)
            text = target.read_text(encoding="utf-8-sig")
            self.assertIn("jasontheween", text)
            self.assertIn("#shorts", text)
            self.assertIn("Chat says look at the map", text)

    def test_sidecars_without_a_clip_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gone.json").write_text("{}", encoding="utf-8")
            target, rows = pack.build(root)
            self.assertEqual(rows, 0)
            self.assertTrue(target.exists())


class CutterTest(unittest.TestCase):
    @staticmethod
    def _source(tmp_path: Path) -> Path:
        source = tmp_path / "src.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=640x360:rate=30:duration=6",
             "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(source)],
            check=True,
        )
        return source

    def test_renders_a_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = self._source(tmp_path)
            cfg = Config(input=str(source), out_dir=tmp_path / "out",
                         work_dir=tmp_path / "work", layout="vertical")
            cutter = Cutter(cfg)
            segment = Segment(seq=0, name=source.name, path=source, duration=6.0,
                              start=0.0, end=6.0)
            prepared = cutter.prepare([segment], 1.0, 5.0, "test")
            self.assertIsNotNone(prepared)
            out = tmp_path / "out" / "clip.mp4"
            ok, error, warning = cutter.render(prepared, out, "TEST CLIP")
            self.assertTrue(ok, error)
            self.assertEqual(warning, "")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                 "-of", "json", str(out)], capture_output=True, text=True, check=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            self.assertEqual((streams[0]["width"], streams[0]["height"]), (1080, 1920))

    def test_renders_a_clip_with_subtitle_cues(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = self._source(tmp_path)
            cfg = Config(input=str(source), out_dir=tmp_path / "out",
                         work_dir=tmp_path / "work", layout="blur", subs="on")
            cutter = Cutter(cfg)
            segment = Segment(seq=0, name=source.name, path=source, duration=6.0,
                              start=0.0, end=6.0)
            prepared = cutter.prepare([segment], 0.5, 5.5, "subs")
            cues = [transcribe.Cue(0.5, 2.5, "NO WAY DUDE"),
                    transcribe.Cue(2.5, 4.5, "THAT WAS INSANE")]
            out = tmp_path / "out" / "clip.mp4"
            ok, error, warning = cutter.render(prepared, out, "THIS WAS INSANE", cues)
            self.assertTrue(ok, error)
            self.assertEqual(warning, "")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(out)],
                capture_output=True, text=True, check=True,
            )
            self.assertGreater(float(probe.stdout.strip()), 4.0)


class ChatTest(unittest.TestCase):
    def test_channel_normalisation(self):
        for raw in ("xqc", "#xqc", "twitch.tv/xqc", "https://www.twitch.tv/xqc/",
                    "https://m.twitch.tv/xqc?foo=1"):
            self.assertEqual(normalize_channel(raw), "xqc", raw)


class ConfigTest(unittest.TestCase):
    def test_verify_mode_is_validated(self):
        with self.assertRaises(SystemExit):
            Config(input="x", verify="bogus").validate()
        Config(input="x", verify="auto").validate()

    def test_defaults_are_capture_only(self):
        cfg = Config()
        self.assertEqual(cfg.verify, "off")
        self.assertEqual(cfg.subs, "off")
        self.assertEqual(cfg.title_mode, "none")
        self.assertEqual(cfg.layout, "duo")

    def test_cam_auto_and_caption_styles_are_valid(self):
        Config(input="x", cam="auto", subs_style="pop").validate()
        Config(input="x", cam="0:0:100:100", subs_style="block").validate()
        with self.assertRaises(SystemExit):
            Config(input="x", subs_style="bogus").validate()

    def test_default_camera_is_automatic(self):
        self.assertEqual(Config().cam, "auto")
        self.assertEqual(Config().subs_style, "pop")

    def test_profiles_are_on_by_default(self):
        self.assertTrue(Config().profiles)

    def test_console_mode_needs_no_input(self):
        with self.assertRaises(SystemExit):
            Config().validate()
        Config(console=True).validate()
        self.assertTrue(Config(console=True).console)


class ProfileTest(unittest.TestCase):
    def test_every_field_maps_to_a_config_attribute(self):
        cfg = Config()
        for field in profile.FIELDS:
            self.assertTrue(hasattr(cfg, field.key), field.key)
            self.assertIn(field.group, profile.GROUP_ORDER, field.key)
        self.assertEqual(set(profile.defaults()), {f.key for f in profile.FIELDS})

    def test_coercion_is_typed_and_bounded(self):
        by = profile.BY_KEY
        self.assertEqual(profile.coerce(by["threshold"], "0.4"), 0.4)
        self.assertEqual(profile.coerce(by["threshold"], 5.0), 1.0)
        self.assertEqual(profile.coerce(by["agree"], 2.9), 3)
        self.assertIs(profile.coerce(by["bot_filter"], "off"), False)
        self.assertIs(profile.coerce(by["bot_filter"], True), True)
        self.assertIsNone(profile.coerce(by["subs_model"], ""))
        self.assertEqual(profile.coerce(by["cam"], ""), "auto")
        self.assertEqual(profile.coerce(by["cam"], "1:2:3:4"), "1:2:3:4")
        with self.assertRaises(ValueError):
            profile.coerce(by["layout"], "diagonal")
        with self.assertRaises(ValueError):
            profile.coerce(by["threshold"], "loud")
        with self.assertRaises(ValueError):
            profile.coerce(by["cam"], "nonsense")

    def test_apply_reports_only_real_changes(self):
        cfg = Config()
        changed = profile.apply(cfg, {"threshold": 0.4, "layout": "blur",
                                      "bot_filter": False})
        self.assertEqual(sorted(changed), ["bot_filter", "layout", "threshold"])
        self.assertEqual(cfg.threshold, 0.4)
        self.assertEqual(profile.apply(cfg, {"threshold": 0.4}), [])

    def test_roundtrip_under_the_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = profile.path_for(Path(tmp), "xQc!/../evil")
            self.assertEqual(path.parent, Path(tmp) / "profiles")
            self.assertNotIn("..", path.name)
            values = {"threshold": 0.31, "layout": "split", "subs_prompt": "xQc"}
            self.assertTrue(profile.save(path, values, "xqc"))
            self.assertEqual(profile.load(path), values)
            self.assertTrue(profile.saved(path))
            self.assertTrue(profile.clear(path))
            self.assertFalse(path.exists())
            self.assertEqual(profile.load(path), {})

    def test_load_tolerates_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text("{ not json")
            self.assertEqual(profile.load(path), {})
            path.write_text(json.dumps({"threshold": 0.2, "bogus": 1}))
            self.assertEqual(profile.load(path), {"threshold": 0.2})

    def test_auto_bundle_only_names_real_fields(self):
        for key in profile.AUTO:
            self.assertIn(key, profile.BY_KEY)


class AutoConfigureTest(unittest.TestCase):
    def test_named_streamer_gets_their_vocabulary(self):
        values, note = profile.autoconfigure("xQc", "twitch:xqc")
        self.assertIn("xQc", values["subs_prompt"])
        self.assertIn("Juicer", values["subs_prompt"])
        self.assertEqual(values["subs"], "off")
        self.assertIsNone(values["subs_model"])
        self.assertEqual(values["encoder"], "auto")
        self.assertIn("xQc", note)
        # Twitch keeps the chat-led weights.
        self.assertEqual(values.get("w_chat", Config().w_chat), Config().w_chat)
        for key in values:
            self.assertIn(key, profile.BY_KEY, key)

    def test_unknown_streamer_falls_back_to_their_name(self):
        values, _ = profile.autoconfigure("SomeNewGuy", "twitch:somenewguy")
        self.assertIn("SomeNewGuy", values["subs_prompt"])
        self.assertNotIn("Juicer", values["subs_prompt"])

    def test_youtube_has_no_chat_so_audio_leads(self):
        values, note = profile.autoconfigure("Kai Cenat", "https://youtube.com/@kaicenat/live")
        self.assertEqual(values["w_chat"], 0.0)
        self.assertGreaterEqual(values["w_audio"], 0.5)
        self.assertGreaterEqual(values["w_text"], 0.3)
        self.assertAlmostEqual(values["w_audio"] + values["w_text"], 1.0)
        self.assertIn("audio-led", note)

    def test_values_apply_to_a_config(self):
        cfg = Config()
        values, _ = profile.autoconfigure("xQc", "twitch:xqc")
        changed = profile.apply(cfg, values)
        self.assertIn("subs_prompt", changed)
        self.assertEqual(cfg.subs_prompt, values["subs_prompt"])


class _BufStub:
    """Just enough of HLSBuffer for the recording tests."""

    def __init__(self, now: float = 200.0):
        self.stream_now = now
        self.segments: list = []
        self.restarts = 0

    def between(self, a, b):
        return []


class ConsoleTest(unittest.TestCase):
    """The engine's console-facing API, without a live stream or a render."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(input="demo.mp4", out_dir=root / "out",
                          work_dir=root / "work")
        self.engine = Engine(self.cfg)
        self.engine.buf = _BufStub(now=200.0)
        # Never spin up real background workers from a settings change.
        self.engine._restart_verify = lambda: None
        self.engine._restart_cam_watch = lambda: None
        self.captured = []
        self.engine._submit = lambda cap, now: self.captured.append(
            (cap.start, cap.end, cap.peak, cap.forced))

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_apply_and_reject(self):
        result = self.engine.apply_settings(
            {"threshold": "0.4", "layout": "split", "bot_filter": "false"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.cfg.threshold, 0.4)
        self.assertEqual(self.cfg.layout, "split")
        self.assertIs(self.cfg.bot_filter, False)
        bad = self.engine.apply_settings({"layout": "nope"})
        self.assertFalse(bad["ok"])
        self.assertIn("must be one of", bad["error"])

    def test_thin_transcript_flags_a_clip_that_lost_its_speech(self):
        thin = transcribe.Cues([transcribe.Cue(0.0, 1.0, "me")],
                               [transcribe.Cue(0.0, 1.0, "me")])
        self.assertTrue(self.engine._thin_transcript(thin, {"duration_s": 20}))
        words = [transcribe.Cue(index, index + 0.5, f"w{index}")
                 for index in range(8)]
        self.assertFalse(self.engine._thin_transcript(
            transcribe.Cues(words, words), {"duration_s": 20}))
        # Short clips have no expectations to fail.
        self.assertFalse(self.engine._thin_transcript(thin, {"duration_s": 3}))

    def test_auto_applies_and_restores(self):
        self.cfg.cam = "1:2:3:4"
        self.engine._cam_setting = "1:2:3:4"
        on = self.engine.apply_auto(True)
        self.assertTrue(on["auto"])
        self.assertTrue(self.engine._auto)
        self.assertEqual(self.cfg.layout, "duo")
        self.assertEqual(self.cfg.cam, "auto")
        self.assertEqual(self.cfg.subs, "off")
        off = self.engine.apply_auto(False)
        self.assertFalse(off["auto"])
        self.assertEqual(self.cfg.cam, "1:2:3:4")

    def test_console_engine_names_itself_and_skips_ingest(self):
        cfg = Config(console=True, out_dir=self.cfg.out_dir,
                     work_dir=self.cfg.work_dir)
        engine = Engine(cfg)
        self.assertEqual(engine.channel, "console")
        self.assertEqual(engine.snapshot()["channel"], "console")
        self.assertTrue(engine.snapshot()["console"])

    def test_auto_saves_cam_as_auto_not_the_detected_box(self):
        self.cfg.cam = "auto"
        self.engine._cam_setting = "auto"
        self.engine.apply_auto(True)
        self.engine.adopt_cam((14, 552, 344, 312))  # detection moves the live box
        self.engine.save_profile()
        saved = profile.load(self.engine._profile_path())
        self.assertEqual(saved["cam"], "auto")

    def test_profile_persists_across_a_restart(self):
        # Auto-save keeps the file current, so a fresh engine on the same work
        # dir picks the tuning straight back up.
        self.engine.apply_settings({"threshold": 0.4, "layout": "split"})
        self.assertTrue(profile.saved(self.engine._profile_path()))
        fresh_cfg = Config(input="demo.mp4", out_dir=self.cfg.out_dir,
                           work_dir=self.cfg.work_dir)
        fresh = Engine(fresh_cfg)
        fresh._restart_verify = lambda: None
        fresh._restart_cam_watch = lambda: None
        fresh._maybe_load_profile()
        self.assertEqual(fresh_cfg.threshold, 0.4)
        self.assertEqual(fresh_cfg.layout, "split")

    def test_a_cli_flag_beats_the_saved_profile(self):
        self.engine.apply_settings({"threshold": 0.4})
        cfg = Config(input="demo.mp4", out_dir=self.cfg.out_dir,
                     work_dir=self.cfg.work_dir, threshold=0.77)
        engine = Engine(cfg)
        engine._maybe_load_profile()
        self.assertEqual(cfg.threshold, 0.77)

    def test_load_reapplies_the_saved_file(self):
        self.engine.apply_settings({"threshold": 0.4})
        self.engine.save_profile()
        profile.save(self.engine._profile_path(), {"threshold": 0.2}, "demo")
        self.engine.load_profile()
        self.assertEqual(self.cfg.threshold, 0.2)

    def test_reset_restores_defaults_and_clears_the_file(self):
        self.engine.apply_settings({"threshold": 0.4})
        self.engine.save_profile()
        self.assertTrue(profile.saved(self.engine._profile_path()))
        self.engine.reset_profile()
        self.assertEqual(self.cfg.threshold, 0.62)
        self.assertFalse(profile.saved(self.engine._profile_path()))

    def test_manual_recording_start_stop(self):
        self.assertTrue(self.engine.start_recording()["recording"])
        self.assertIsNotNone(self.engine._rec_cap)
        self.assertEqual(self.engine._rec_cap.start, 188.0)  # now - pre(12)
        self.engine.buf.stream_now = 205.0
        self.engine.toggle_recording()  # stop
        self.assertIsNone(self.engine._rec_cap)
        self.assertEqual(self.captured, [(188.0, 205.0, 0.0, True)])

    def test_a_too_short_recording_is_discarded(self):
        self.cfg.pre = 0.0
        self.engine.buf.stream_now = 200.0
        self.engine.start_recording()
        self.engine.buf.stream_now = 200.4
        result = self.engine.stop_recording()
        self.assertEqual(result.get("note"), "too short")
        self.assertEqual(self.captured, [])

    def test_recording_holds_off_auto_trigger(self):
        self.engine.start_recording()
        self.engine._score = {"score": 0.95}
        self.engine._advance(201.0)
        self.assertIsNone(self.engine.capture)     # no auto capture started
        self.assertAlmostEqual(self.engine._rec_peak, 0.95)
        self.assertEqual(self.engine._rec_cap.end, 201.0)

    def test_snapshot_exposes_console_state(self):
        snap = self.engine.snapshot()
        for key in ("auto", "recording", "recording_s", "record_max_s",
                    "encoder", "profiles", "profile_saved", "out_dir", "log"):
            self.assertIn(key, snap)

    def test_snapshot_marks_raw_captures_and_drops(self):
        out = Path(self.cfg.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "live.mp4").write_bytes(b"")
        (out / "live.json").write_text(json.dumps(
            {"title": "", "score": 0.71, "capture_only": True}))
        (out / "dropped.mp4").write_bytes(b"")
        (out / "dropped.json").write_text(json.dumps(
            {"title": "Dropped", "score": 0, "source": "upload"}))
        (out / "styled.mp4").write_bytes(b"")
        (out / "styled.json").write_text(json.dumps(
            {"title": "Dropped", "score": 0, "source": "upload",
             "styled_from": "dropped.mp4", "style": "story"}))
        rows = {c["file"]: c for c in self.engine.snapshot()["clips"]}
        self.assertTrue(rows["live.mp4"]["capture_only"])
        self.assertFalse(rows["live.mp4"]["uploaded"])
        self.assertTrue(rows["dropped.mp4"]["uploaded"])
        self.assertFalse(rows["styled.mp4"]["uploaded"])


class _ProcStub:
    """A process handle that starts alive and can be stopped or killed."""

    def __init__(self, code=None, pid=4242):
        self.returncode = code
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class StreamsTest(unittest.TestCase):
    """The link section: derive a name, spawn a worker, ask before doubling up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(input="demo.mp4", chat="twitch:xqc",
                          out_dir=root / "clips" / "xqc",
                          work_dir=root / "work")
        self.engine = Engine(self.cfg)
        self.engine._restart_verify = lambda: None
        self.engine._restart_cam_watch = lambda: None
        self.calls = []
        self.procs = []

        def spawn(cmd, log_path):
            self.calls.append((cmd, log_path))
            proc = _ProcStub()
            self.procs.append(proc)
            return proc

        self.engine.streams = streams.StreamManager(self.engine, spawn=spawn)

    def tearDown(self):
        self.tmp.cleanup()

    def test_derive_twitch_link(self):
        target = streams.derive_target("https://www.twitch.tv/KaiCenat?x=1")
        self.assertEqual(target.name, "kaicenat")
        self.assertEqual(target.kind, "twitch")
        self.assertEqual(target.chat, "twitch:kaicenat")
        self.assertEqual(target.input, "https://www.twitch.tv/KaiCenat?x=1")

    def test_derive_youtube_links(self):
        handle = streams.derive_target("https://youtube.com/@SomeHandle/live")
        self.assertEqual(handle.name, "SomeHandle")
        self.assertIsNone(handle.chat)
        video = streams.derive_target("https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(video.name.startswith("yt_dQw4"))
        self.assertEqual(video.kind, "youtube")

    def test_derive_keeps_an_explicit_name(self):
        target = streams.derive_target("https://twitch.tv/xqc", "Kai Cenat")
        self.assertEqual(target.name, "Kai_Cenat")

    def test_derive_accepts_direct_media(self):
        target = streams.derive_target("https://example.com/live.m3u8")
        self.assertEqual(target.kind, "direct")
        self.assertEqual(target.name, "live")

    def test_derive_rejects_unknown_links(self):
        for link in ("", "https://example.com/whatever", "https://twitch.tv/videos/123"):
            with self.assertRaises(ValueError, msg=link):
                streams.derive_target(link)

    def test_add_spawns_a_worker_with_the_console_tuning(self):
        self.engine.apply_settings(
            {"threshold": 0.41, "layout": "split", "title_mode": "fixed",
             "title": "an xqc joke"})
        result = self.engine.add_stream("https://twitch.tv/kaicenat")
        self.assertTrue(result["ok"], result)
        stream = result["stream"]
        self.assertEqual(stream["name"], "kaicenat")
        self.assertEqual(Path(stream["out_dir"]), self.cfg.out_dir.parent / "kaicenat")
        self.assertEqual(len(self.calls), 1)

        cmd, log_path = self.calls[0]
        self.assertIn("--name", cmd)
        self.assertIn("kaicenat", cmd)
        self.assertIn("twitch:kaicenat", cmd)
        self.assertEqual(log_path.name, "worker.log")

        work = self.cfg.work_dir / "streams" / "kaicenat"
        saved = profile.load(profile.path_for(work, "kaicenat"))
        self.assertEqual(saved["threshold"], 0.41)
        self.assertEqual(saved["layout"], "split")
        self.assertEqual(saved["cam"], "auto")          # detect this streamer's face
        self.assertEqual(saved["title_mode"], "auto")   # no borrowed fixed hook
        self.assertIn("Kai Cenat", saved["subs_prompt"])  # vocabulary for the new link

    def test_console_workers_land_in_the_clips_root(self):
        cfg = Config(console=True, out_dir=Path(self.tmp.name) / "clips",
                     work_dir=Path(self.tmp.name) / "work")
        engine = Engine(cfg)
        engine._restart_verify = lambda: None
        engine._restart_cam_watch = lambda: None
        engine.streams = streams.StreamManager(engine, spawn=lambda cmd, log: _ProcStub())
        result = engine.add_stream("https://twitch.tv/xQc")
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["stream"]["out_dir"]),
                         Path(self.tmp.name) / "clips" / "xqc")
        saved = profile.load(profile.path_for(
            Path(self.tmp.name) / "work" / "streams" / "xqc", "xqc"))
        self.assertIn("Juicer", saved["subs_prompt"])

    def test_adding_asks_before_running_two(self):
        self.engine._ingest = _ProcStub()  # the main stream is live
        asked = self.engine.add_stream("https://twitch.tv/kaicenat")
        self.assertTrue(asked["needs_choice"])
        self.assertEqual(asked["busy"], ["xqc"])
        self.assertEqual(asked["target"]["name"], "kaicenat")
        self.assertEqual(self.calls, [])

        both = self.engine.add_stream("https://twitch.tv/kaicenat", mode="both")
        self.assertTrue(both["ok"], both)
        self.assertEqual(len(self.calls), 1)

    def test_cancel_adds_nothing(self):
        self.engine._ingest = _ProcStub()
        result = self.engine.add_stream("https://twitch.tv/kaicenat", mode="cancel")
        self.assertTrue(result.get("cancelled"))
        self.assertEqual(self.calls, [])

    def test_the_primary_stream_cannot_be_added_again(self):
        self.engine._ingest = _ProcStub()
        result = self.engine.add_stream("https://twitch.tv/xqc")
        self.assertFalse(result["ok"])
        self.assertIn("this console is clipping", result["error"])

    def test_a_stream_can_only_be_added_once(self):
        self.engine.add_stream("https://twitch.tv/kaicenat")
        again = self.engine.add_stream("https://twitch.tv/kaicenat")
        self.assertFalse(again["ok"])
        self.assertIn("already being clipped", again["error"])

    def test_stop_terminates_the_worker(self):
        self.engine.add_stream("https://twitch.tv/kaicenat")
        result = self.engine.stop_stream("kaicenat")
        self.assertTrue(result["ok"])
        self.assertTrue(self.procs[0].terminated)
        self.assertEqual(result["stream"]["status"], "stopped")

    def test_retry_restarts_a_stopped_worker(self):
        self.engine.add_stream("https://twitch.tv/kaicenat")
        self.procs[0].returncode = 1  # the worker died
        result = self.engine.retry_stream("kaicenat")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.calls), 2)

    def test_snapshot_lists_added_streams(self):
        self.engine.add_stream("https://twitch.tv/kaicenat")
        snap = self.engine.snapshot()
        self.assertEqual([s["name"] for s in snap["streams"]], ["kaicenat"])
        self.assertEqual(snap["streams_root"], str(self.cfg.out_dir.parent))
        self.assertEqual(snap["streams"][0]["kind"], "twitch")

    def test_an_explicit_name_labels_the_worker(self):
        cfg = Config(input="https://youtube.com/@foo/live", name="Foo",
                     out_dir=Path(self.tmp.name) / "clips",
                     work_dir=Path(self.tmp.name) / "work")
        engine = Engine(cfg)
        self.assertEqual(engine.channel, "Foo")


class DashboardTest(unittest.TestCase):
    def test_page_ids_are_unique_and_resolve(self):
        import re

        from clipper.dashboard import PAGE

        ids = re.findall(r'id="([^"]+)"', PAGE)
        concrete = [i for i in ids if "$" not in i]
        self.assertEqual(len(concrete), len(set(concrete)), "duplicate id in page")
        refs = set(re.findall(r"\$\('([^']+)'\)", PAGE))
        self.assertEqual(refs - set(concrete), set(), "page references a missing id")

    def test_page_wires_every_endpoint(self):
        from clipper.dashboard import PAGE

        for endpoint in ("/api/state", "/api/settings", "/api/auto",
                         "/api/autoconfigure", "/api/profile", "/api/camera",
                         "/api/clips/delete", "/api/clip", "/api/streams",
                         "/api/streams/stop", "/api/streams/retry",
                         "/api/vod", "/api/upload", "/api/style", "?download=1"):
            self.assertIn(endpoint, PAGE, endpoint)

    def test_page_has_local_file_drop_and_a_select_button(self):
        import re

        from clipper.dashboard import PAGE

        ids = set(re.findall(r'id="([^"]+)"', PAGE))
        for key in ("dropzone", "fileinput", "localfile",
                    "uploadbar", "uploadfill", "uploadnote"):
            self.assertIn(key, ids, key)
        self.assertIn("Select from local", PAGE)
        self.assertIn("Drop raw clips here", PAGE)
        self.assertIn("queueLocal(", PAGE)
        self.assertIn("type=\"file\"", PAGE)

    def test_page_has_auto_configure_button(self):
        import re

        from clipper.dashboard import PAGE

        ids = set(re.findall(r'id="([^"]+)"', PAGE))
        self.assertIn("autocfg", ids)
        self.assertIn("autoConfigure()", PAGE)

    def test_default_dashboard_port_can_move_but_explicit_does_not(self):
        from pathlib import Path

        from clipper.dashboard import Dashboard

        default = Dashboard(None, 8756, Path("."))
        ports = default._ports_to_try()
        self.assertEqual(ports[0], 8756)
        self.assertGreater(len(ports), 1)
        explicit = Dashboard(None, 9000, Path("."))
        self.assertEqual(explicit._ports_to_try(), [9000])

    def test_page_wires_the_past_stream_and_update_controls(self):
        from clipper.dashboard import PAGE

        for endpoint in ("/api/vod", "/api/vod/stop", "/api/update"):
            self.assertIn(endpoint, PAGE, endpoint)
        for key in ("vodinput", "vodbtn", "updbtn"):
            self.assertIn(f'id="{key}"', PAGE, key)
        self.assertIn("startVod()", PAGE)
        self.assertIn("updateClipper()", PAGE)


class UploadTest(unittest.TestCase):
    """Dropping a raw clip files it on the wall, ready for styling."""

    class _Engine:
        def __init__(self, work_dir: Path, out_dir: Path):
            self.cfg = types.SimpleNamespace(work_dir=work_dir, out_dir=out_dir)
            self.logs: list[str] = []

        def log(self, message: str) -> None:
            self.logs.append(message)

    def test_upload_name_accepts_video_and_strips_paths(self):
        from clipper.dashboard import upload_name

        self.assertEqual(upload_name("/tmp/My Clip.MP4"),
                         ("My_Clip", ".mp4"))
        self.assertEqual(upload_name("../../stream.mkv"),
                         ("stream", ".mkv"))
        self.assertEqual(upload_name(r"C:\Users\me\Videos\Run.MOV"),
                         ("Run", ".mov"))
        with self.assertRaises(ValueError):
            upload_name("notes.txt")
        with self.assertRaises(ValueError):
            upload_name("no-extension")

    def test_upload_streams_and_probes_a_video(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.parse import quote

        from clipper.dashboard import _Handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "clips"
            source = root / "source.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=15:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", str(source)],
                check=True)
            payload = source.read_bytes()
            engine = self._Engine(root / "work", out)
            handler = type("UploadHandler", (_Handler,), {
                "engine": engine, "clips_dir": root / "clips",
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]

            def post(name: str, body: bytes, origin: str | None = None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                headers = {"Content-Type": "application/octet-stream"}
                if origin:
                    headers["Origin"] = origin
                conn.request("POST", "/api/upload?name=" + quote(name),
                             body=body, headers=headers)
                reply = conn.getresponse()
                data = json.loads(reply.read().decode("utf-8"))
                conn.close()
                return reply.status, data

            try:
                status, data = post("My Clip.MP4", payload)
                self.assertEqual(status, 200, data)
                self.assertTrue(data.get("ok"), data)
                self.assertEqual(data.get("file"), "My_Clip.mp4")
                self.assertEqual(data.get("title"), "My Clip")
                stored = out / data["file"]
                self.assertTrue(stored.exists())
                self.assertGreater(data.get("duration") or 0, 0.5)
                self.assertEqual((data.get("width"), data.get("height")),
                                 (320, 240))
                self.assertEqual(data.get("layout"), "horizontal")

                side = stored.with_suffix(".json")
                self.assertTrue(side.exists())
                meta = json.loads(side.read_text())
                self.assertEqual(meta.get("source"), "upload")
                self.assertEqual(meta.get("layout_used"), "horizontal")
                self.assertEqual(meta.get("title"), "My Clip")
                self.assertEqual(meta.get("score"), 0)
                self.assertTrue(any("raw clip added" in line
                                    for line in engine.logs))

                # Dropping the same name again must not overwrite the first.
                status, again = post("My Clip.MP4", payload)
                self.assertEqual(status, 200, again)
                self.assertEqual(again.get("file"), "My_Clip-2.mp4")
                self.assertTrue((out / "My_Clip-2.mp4").exists())
                self.assertTrue((out / "My_Clip.mp4").exists())

                status, data = post("notes.txt", b"hello")
                self.assertEqual(status, 400, data)
                self.assertIn("cannot clip", data.get("error", ""))

                status, data = post("evil.mp4", payload,
                                    origin="http://evil.example")
                self.assertEqual(status, 403, data)
            finally:
                server.shutdown()
                server.server_close()

    def test_upload_rejects_an_empty_body(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.parse import quote

        from clipper.dashboard import _Handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = self._Engine(root / "work", root / "clips")
            handler = type("UploadHandler", (_Handler,), {
                "engine": engine, "clips_dir": root / "clips",
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=10)
                conn.request("POST", "/api/upload?name=" + quote("empty.mp4"),
                             body=b"")
                reply = conn.getresponse()
                data = json.loads(reply.read().decode("utf-8"))
                conn.close()
                self.assertEqual(reply.status, 400, data)
                self.assertIn("no file data", data.get("error", ""))
            finally:
                server.shutdown()
                server.server_close()


class VodTest(unittest.TestCase):
    """Scoring and cutting a finished stream, without a live ingest."""

    @staticmethod
    def _source(tmp_path: Path, seconds: int = 8) -> Path:
        source = tmp_path / "past.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"testsrc2=size=320x240:rate=15:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(source)],
            check=True,
        )
        return source

    def test_pick_moments_takes_one_clip_per_sustained_burst(self):
        cfg = Config(input="x")
        cfg.threshold, cfg.confirm, cfg.cooldown = 0.5, 2.0, 10.0
        curve = [0.0] * 60
        for i in range(20, 25):
            curve[i] = 0.9          # a sustained burst
        curve[40] = 0.95            # a single-second spike: too short to confirm
        moments = vod_mod.pick_moments(curve, cfg)
        self.assertEqual(len(moments), 1)
        self.assertGreaterEqual(moments[0].t, 20.0)
        self.assertLessEqual(moments[0].t, 24.0)
        self.assertAlmostEqual(moments[0].score, 0.9, delta=0.01)

    def test_max_clips_keeps_the_strongest_moments(self):
        cfg = Config(input="x")
        cfg.threshold, cfg.confirm, cfg.cooldown = 0.5, 2.0, 1.0
        curve = [0.0] * 100
        for start, height in ((10, 0.6), (30, 0.9), (50, 0.7)):
            for i in range(start, start + 4):
                curve[i] = height
        moments = vod_mod.pick_moments(curve, cfg, max_clips=2)
        self.assertEqual(len(moments), 2)
        self.assertAlmostEqual(min(m.score for m in moments), 0.7, delta=0.01)

    def test_rerun_skips_windows_already_clipped(self):
        cfg = Config(input="x")
        cfg.pre, cfg.post = 5.0, 5.0
        moments = [vod_mod.Moment(t=10.0, score=0.9),
                   vod_mod.Moment(t=40.0, score=0.8),
                   vod_mod.Moment(t=80.0, score=0.7)]
        used = [{"t": 10.0, "start": 5.0, "end": 15.0, "file": "a.mp4"}]
        fresh = vod_mod.fresh_moments(moments, used, cfg, max_clips=12)
        self.assertEqual([m.t for m in fresh], [40.0, 80.0])

    def test_fresh_moments_keeps_only_the_strongest_batch(self):
        cfg = Config(input="x")
        cfg.pre, cfg.post = 2.0, 2.0
        moments = [vod_mod.Moment(t=float(t), score=float(t) / 100.0)
                   for t in (5, 20, 35, 50)]
        fresh = vod_mod.fresh_moments(moments, [], cfg, max_clips=2)
        self.assertEqual([m.t for m in fresh], [35.0, 50.0])

    def test_vod_history_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(input="x", work_dir=Path(tmp))
            self.assertEqual(vod_mod.load_history(cfg), {})
            vod_mod.save_history(cfg, {"twitch:1": {"used": [{"t": 5}]}})
            self.assertEqual(vod_mod.load_history(cfg)["twitch:1"]["used"][0]["t"], 5)

    def test_build_curve_reacts_to_a_chat_and_audio_burst(self):
        cfg = Config(input="x")
        levels = [-50.0] * 120
        for i in range(60, 66):
            levels[i] = -20.0
        chat = [(float(t), user, "LETS GO") for t in range(60, 66)
                for user in ("a", "b", "c", "d", "e")]
        curve = vod_mod.build_curve(levels, chat, cfg)
        self.assertEqual(len(curve), 120)
        self.assertGreater(max(curve[60:70]), 0.4)
        self.assertLess(max(curve[:30]), 0.25)

    def test_build_curve_is_audio_led_without_chat(self):
        cfg = Config(input="x")
        levels = [-50.0] * 120
        for i in range(40, 46):
            levels[i] = -18.0
        curve = vod_mod.build_curve(levels, [], cfg)
        self.assertEqual(len(curve), 120)
        self.assertGreater(max(curve[40:50]), 0.2)

    def test_resolve_vod_reads_a_local_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._source(Path(tmp), seconds=4)
            source = vod_mod.resolve_vod(str(path), Path(tmp))
            self.assertEqual(source.kind, "file")
            self.assertGreater(source.duration, 3.0)
            self.assertEqual(source.url, str(path))

    def test_resolve_vod_rejects_an_empty_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                vod_mod.resolve_vod("   ", Path(tmp))

    def test_prepare_window_cuts_the_window_out_of_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = self._source(tmp_path, seconds=8)
            cfg = Config(input=str(path), out_dir=tmp_path / "out",
                         work_dir=tmp_path / "work")
            cutter = Cutter(cfg)
            prepared = cutter.prepare_window(path, 2.0, 5.0, "vodwin")
            self.assertIsNotNone(prepared)
            self.assertAlmostEqual(prepared.duration, 3.0, delta=0.4)
            self.assertIsNone(cutter.prepare_window(path, 2.0, 2.5, "tooshort"))

    def test_intermediate_encoder_flags_are_known_encoders(self):
        from clipper.cutter import intermediate_args

        self.assertIn("libx264", intermediate_args("libx264"))
        self.assertIn("h264_videotoolbox", intermediate_args("h264_videotoolbox"))


class VodConsoleTest(unittest.TestCase):
    """The console's past-stream job lifecycle, without spawning a real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(input="demo.mp4", out_dir=root / "out",
                          work_dir=root / "work")
        self.engine = Engine(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_empty_link_is_rejected(self):
        result = self.engine.start_vod("   ")
        self.assertFalse(result["ok"])
        self.assertIn("paste a link", result["error"])

    def test_only_one_past_stream_runs_at_a_time(self):
        self.engine._vod_job = {"running": True, "state": {}}
        result = self.engine.start_vod("https://twitch.tv/videos/1")
        self.assertFalse(result["ok"])
        self.assertIn("already being clipped", result["error"])

    def test_stopping_when_nothing_is_running_is_reported(self):
        self.assertFalse(self.engine.stop_vod()["ok"])

    def test_snapshot_reports_the_past_stream(self):
        self.engine._vod_job = {
            "running": True, "link": "https://twitch.tv/videos/1", "name": "xqc",
            "error": "", "state": {"phase": "rendering", "total": 5, "index": 2,
                                   "clips": 1, "found": 5},
        }
        vod = self.engine.snapshot()["vod"]
        self.assertTrue(vod["running"])
        self.assertEqual(vod["phase"], "rendering")
        self.assertEqual(vod["total"], 5)
        self.assertEqual(vod["rendered"], 1)
        self.assertEqual(vod["found"], 5)

    def test_no_past_stream_snapshots_as_empty(self):
        self.assertEqual(self.engine.snapshot()["vod"], {})


class UpdateTest(unittest.TestCase):
    """The self-updater: it must never overwrite local work."""

    def test_this_checkout_is_a_git_repo(self):
        self.assertTrue(update_mod.is_repo())
        self.assertIn("github.com/ma35ttt/clipper", update_mod.version_line())

    def test_missing_git_is_explained_not_crashed(self):
        with mock.patch.object(update_mod.shutil, "which", return_value=None):
            result = update_mod.check()
        self.assertFalse(result["ok"])
        self.assertFalse(result["git"])
        self.assertIn("git is not installed", result["message"])

    def test_a_zip_copy_is_told_to_re_download(self):
        with mock.patch.object(update_mod, "is_repo", return_value=False):
            result = update_mod.check()
        self.assertFalse(result["ok"])
        self.assertIn("not cloned", result["message"])


class StyleTest(unittest.TestCase):
    """Post-session looks: styles, caption files and punch-in planning."""

    def test_catalogue_has_the_builtin_styles(self):
        from clipper import styles

        with tempfile.TemporaryDirectory() as tmp:
            names = {row["name"] for row in styles.catalogue(Path(tmp))}
        self.assertTrue({"clean", "hype", "cinematic", "podcast", "raw"} <= names)
        self.assertTrue({"story", "story-karaoke", "meme", "reveal",
                         "karaoke"} <= names)

    def test_a_filename_is_never_a_hook(self):
        clip = Path("20261119-144743_caseoh_061.mp4")
        self.assertTrue(_generated_title(clip.stem, clip))
        self.assertTrue(_generated_title("20261119-144743 casEoh 061", clip))
        self.assertFalse(_generated_title("CHAT SAYS COOKED", clip))

    def test_the_opening_line_fallback_drops_filler(self):
        cues = [transcribe.Cue(0.0, 2.0,
                               "I think like she just I think we go haystack "
                               "chat.")]
        line = describe_cues(cues)
        self.assertTrue(line)
        self.assertFalse(line.startswith("I THINK LIKE"))
        self.assertLessEqual(len(line.split()), 7)

    def test_unknown_style_falls_back_to_clean(self):
        from clipper import styles

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(styles.resolve("does-not-exist", Path(tmp)).name, "clean")

    def test_reference_styles_carry_their_caption_treatment(self):
        from clipper import styles

        with tempfile.TemporaryDirectory() as tmp:
            story = styles.resolve("story", Path(tmp))
            karaoke = styles.resolve("karaoke", Path(tmp))
            reveal = styles.resolve("reveal", Path(tmp))
        self.assertEqual(story.captions, "static")
        self.assertEqual(story.caption_anchor, "seam")
        self.assertEqual(karaoke.captions, "word")
        self.assertTrue(karaoke.colour_emphasis)
        self.assertFalse(karaoke.hook)
        self.assertEqual(reveal.captions, "off")
        self.assertTrue(reveal.hook_fallback)
        self.assertEqual(reveal.hook_anchor, "top")

    def test_ass_file_keeps_subtitles_white(self):
        cues = transcribe.Cues(
            [transcribe.Cue(0.2, 1.4, "THIS IS THE MOMENT")],
            [transcribe.Cue(0.2, 0.6, "THIS"),
             transcribe.Cue(0.6, 1.0, "IS"),
             transcribe.Cue(1.0, 1.4, "MOMENT")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subs.ass"
            self.assertTrue(caption.build_ass(cues, path, y=1700))
            text = path.read_text()
        self.assertIn("PlayResY: 1920", text)
        self.assertIn("Dialogue:", text)
        self.assertIn("THIS IS MOMENT", text)
        self.assertIn("&H00FFFFFF&", text)          # white primary
        self.assertNotIn("&H003FD2FF&", text.split("[Events]")[1])  # no gold subs

    def test_ass_static_holds_whole_sentences(self):
        cues = transcribe.Cues(
            [transcribe.Cue(0.2, 2.0, "THIS IS THE FIRST THOUGHT"),
             transcribe.Cue(2.1, 4.0, "AND IT KEEPS GOING")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "story.ass"
            self.assertTrue(caption.build_ass(
                cues, path, mode="static", align="top", y=1390))
            text = path.read_text()
        events = text.split("[Events]")[1]
        self.assertEqual(events.count("Dialogue:"), 1)
        self.assertIn(r"\an8", events)
        self.assertIn(r"\pos(540,1390)", events)
        self.assertIn(r"\N", events)                # explicit balanced break
        self.assertIn("THIS IS THE FIRST", events)
        self.assertIn("THOUGHT AND IT KEEPS", events)

    def test_ass_word_mode_puts_one_word_per_line(self):
        cues = transcribe.Cues(
            [transcribe.Cue(0.2, 1.0, "THE MOMENT")],
            [transcribe.Cue(0.2, 0.6, "THE"),
             transcribe.Cue(0.6, 1.0, "MOMENT")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "word.ass"
            self.assertTrue(caption.build_ass(
                cues, path, mode="word", align="center", y=940,
                colour_emphasis=True, emphasis=["moment"],
                emphasis_fill="#FF7A1A"))
            text = path.read_text()
        events = text.split("[Events]")[1]
        dialogues = [line for line in events.splitlines()
                     if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 2)
        self.assertTrue(all(r"\fscx86" in line for line in dialogues))
        self.assertIn(r"\c&H001A7AFF&", dialogues[1])
        self.assertNotIn(r"\c&H001A7AFF&", dialogues[0])

    def test_beat_fill_lights_one_word_at_a_time(self):
        cues = transcribe.Cues(
            [transcribe.Cue(0.2, 1.4, "THIS IS IT")],
            [transcribe.Cue(0.2, 0.6, "THIS"),
             transcribe.Cue(0.6, 1.0, "IS"),
             transcribe.Cue(1.0, 1.4, "IT")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "beat.ass"
            self.assertTrue(caption.build_ass(
                cues, path, y=1500, mode="pop", uppercase=True,
                beat_fill="#FFD23F", font_px=120))
            text = path.read_text()
        events = text.split("[Events]")[1]
        dialogues = [line for line in events.splitlines()
                     if line.startswith("Dialogue:")]
        # One line per word, so exactly one word is lit at any instant.
        self.assertEqual(len(dialogues), 3)
        self.assertTrue(all("&H003FD2FF&" in line for line in dialogues))
        self.assertLessEqual(dialogues[0].count("&H003FD2FF&"), 1)
        self.assertIn("THIS", dialogues[0])
        self.assertIn("IS", dialogues[1])

    def test_a_wide_chunk_shrinks_instead_of_overflowing(self):
        cues = transcribe.Cues(
            [transcribe.Cue(0.2, 2.0, "ARE YOU THINKING?")],
            [transcribe.Cue(0.2, 0.6, "ARE"),
             transcribe.Cue(0.6, 1.0, "YOU"),
             transcribe.Cue(1.0, 2.0, "THINKING?")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.ass"
            self.assertTrue(caption.build_ass(
                cues, path, y=1500, mode="pop", uppercase=True,
                font_px=121, max_width=993))
            text = path.read_text()
        events = text.split("[Events]")[1]
        self.assertIn(r"\fs", events)
        sizes = [int(m) for m in re.findall(r"\\fs(\d+)", events)]
        self.assertTrue(sizes and all(size < 121 for size in sizes))

    def test_story_chunks_group_sentences_and_respect_pauses(self):
        cues = [transcribe.Cue(0.2, 1.4, "this is a long first thought"),
                transcribe.Cue(1.5, 2.9, "and it continues here"),
                transcribe.Cue(4.2, 5.0, "a new sentence starts now.")]
        chunks = transcribe.story_chunks(cues)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].text,
                         "this is a long first thought and it continues here")
        self.assertEqual(chunks[1].start, 4.2)
        self.assertGreaterEqual(chunks[1].end, 5.0)
        self.assertLessEqual(chunks[1].end - chunks[1].start, 6.5)

    def test_story_chunks_split_on_a_finished_sentence(self):
        cues = [transcribe.Cue(0.2, 1.0, "first thing happened."),
                transcribe.Cue(1.1, 2.0, "second thing happened.")]
        chunks = transcribe.story_chunks(cues)
        self.assertEqual([chunk.text for chunk in chunks],
                         ["first thing happened.", "second thing happened."])

    def test_duo_layout_fills_the_frame_below_the_camera(self):
        geo = duo_geometry("1280:0:640:360")
        self.assertEqual((geo.cam_h, geo.game_h), (840, 1080))
        self.assertEqual(geo.crop, (0, 0, 1270, 1080))
        parts = "\n".join(duo_graph("1280:0:640:360"))
        self.assertIn("split=3", parts)
        self.assertIn("vstack=inputs=2", parts)
        self.assertIn("crop=1270:1080:0:0", parts)
        self.assertNotIn("patchblur", parts)

    def test_duo_layout_blurs_a_floating_camera_inset(self):
        geo = duo_geometry("640:200:640:360")
        self.assertIsNone(geo.crop)
        parts = "\n".join(duo_graph("640:200:640:360"))
        self.assertIn("split=4", parts)
        self.assertIn("patchblur", parts)

    def test_duo_graph_renders_a_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "wide.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=1920x1080:rate=15:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", str(source)],
                check=True)
            for cam in ("1280:0:640:540", "640:200:640:540"):
                result = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(source),
                     "-filter_complex", ";".join(duo_graph(cam)),
                     "-map", "[vbase]", "-frames:v", "1", "-f", "null", "-"],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr[-400:])

    def test_caption_anchors_follow_the_layout_seam(self):
        self.assertEqual(caption_anchor_y("seam", "split"), (1380, "top"))
        self.assertEqual(caption_anchor_y("seam", "duo"), (850, "top"))
        self.assertEqual(caption_anchor_y("auto", "duo"), (850, "top"))
        self.assertEqual(caption_anchor_y("middle", "duo"), (940, "center"))
        self.assertEqual(caption_anchor_y("lower", "duo"), (1680, "bottom"))

    def test_punch_plan_stays_short_and_close(self):
        from clipper import restyle, styles

        cues = transcribe.Cues(
            [transcribe.Cue(0, 4, "MASSIVE explosion happened somehow")],
            [transcribe.Cue(0.4, 0.9, "MASSIVE"),
             transcribe.Cue(1.2, 1.6, "explosion"),
             transcribe.Cue(2.0, 2.4, "happened"),
             transcribe.Cue(2.8, 3.2, "somehow")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            style = styles.resolve("hype", Path(tmp))
        punches = restyle.plan_punches(cues, 4.0, style)
        self.assertTrue(punches)
        self.assertLessEqual(len(punches), 3)
        for punch in punches:
            self.assertGreater(punch["end"], punch["start"])
            self.assertLessEqual(punch["zoom"], 1.25)
            self.assertGreaterEqual(punch["start"], 0.0)

    def test_restyle_raw_writes_one_styled_copy(self):
        from clipper import restyle

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            clip = tmp_path / "capture.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=15:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                check=True)
            clip.with_suffix(".json").write_text(json.dumps(
                {"title": "", "score": 0.5, "layout_used": "split"}))
            cfg = Config(input="x", out_dir=tmp_path,
                         work_dir=tmp_path / "work")
            code = restyle.run(cfg, [clip], "raw")
            self.assertEqual(code, 0)
            self.assertTrue((tmp_path / "capture_raw.mp4").exists())

    def test_capture_only_never_runs_whisper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "window.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=15:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                check=True)
            cfg = Config(input="x", out_dir=tmp_path / "out",
                         work_dir=tmp_path / "work", layout="blur")
            engine = Engine(cfg)
            prepared = engine.cutter.prepare_window(source, 0, 2, "capture")
            meta = {"stamp": "20261119-130000", "score": 0.6,
                    "signals": {"t": 1.0}, "stream_start": 0.0,
                    "stream_end": 2.0, "chat_excerpt": []}
            with mock.patch.object(Cutter, "transcribe",
                                   side_effect=AssertionError("whisper ran")):
                engine._render(prepared, meta, False, [])
            self.assertTrue(meta.get("capture_only"))
            self.assertTrue((tmp_path / "out" / meta["file"]).exists())

    def test_duo_without_a_camera_falls_back_to_blur(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "wide.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=15:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                check=True)
            cfg = Config(input="x", out_dir=tmp_path / "out",
                         work_dir=tmp_path / "work", layout="duo", cam="auto")
            engine = Engine(cfg)
            prepared = engine.cutter.prepare_window(source, 0, 2, "duo")
            meta = {"stamp": "20261119-131000", "score": 0.6,
                    "signals": {"t": 1.0}, "stream_start": 0.0,
                    "stream_end": 2.0, "chat_excerpt": []}
            engine._render(prepared, meta, False, [])
            self.assertEqual(meta.get("layout_used"), "blur")
            self.assertTrue((tmp_path / "out" / meta["file"]).exists())

    def test_style_subcommands_are_wired(self):
        from clipper.cli import build_parser

        args = build_parser().parse_args(["style", "a.mp4", "--style", "hype"])
        self.assertEqual(args.func.__name__, "cmd_style")
        self.assertEqual(args.style, "hype")
        listed = build_parser().parse_args(["styles"])
        self.assertEqual(listed.func.__name__, "cmd_styles")


class CliTest(unittest.TestCase):
    def test_vod_and_update_subcommands_are_wired(self):
        from clipper.cli import build_parser

        vod_args = build_parser().parse_args(["vod", "https://twitch.tv/videos/1"])
        self.assertEqual(vod_args.func.__name__, "cmd_vod")
        self.assertEqual(vod_args.link, "https://twitch.tv/videos/1")
        self.assertEqual(vod_args.max_clips, 12)

        upd = build_parser().parse_args(["update", "--check"])
        self.assertEqual(upd.func.__name__, "cmd_update")
        self.assertTrue(upd.check)


if __name__ == "__main__":
    unittest.main()
