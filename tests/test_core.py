"""Core tests. No third-party test runner required.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipper import caption  # noqa: E402
from clipper import camera  # noqa: E402
from clipper import profile  # noqa: E402
from clipper import streams  # noqa: E402
from clipper import transcribe  # noqa: E402
from clipper.chat import ChatEvent, normalize_channel  # noqa: E402
from clipper.config import Config, parse_cam  # noqa: E402
from clipper.cutter import Cutter, hook_from_cues, split_graph  # noqa: E402
from clipper.engine import Engine  # noqa: E402
from clipper.hls import HLSBuffer, parse_playlist  # noqa: E402
from clipper.hls import Segment  # noqa: E402
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
        self.assertIn("crop=367:294:0:539", graph)
        self.assertIn("vstack=inputs=2", graph)
        # the three bands must add up to a full 1080x1920 frame
        self.assertIn("scale=1080:760", graph)
        self.assertIn("scale=1080:608", graph)
        self.assertIn("scale=1080:552", graph)

    def test_hook_quotes_the_clip_not_chat(self):
        cues = [transcribe.Cue(0, 2, "no way dude"),
                transcribe.Cue(2, 4, "look at that car")]
        self.assertEqual(hook_from_cues(cues), "NO WAY")
        self.assertEqual(hook_from_cues([]), "")

    def test_hook_prefers_the_strongest_phrase(self):
        cues = [transcribe.Cue(0, 2, "oh my god what is happening")]
        self.assertEqual(hook_from_cues(cues), "WHAT IS HAPPENING")


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

    def test_default_verify_is_auto(self):
        self.assertEqual(Config().verify, "auto")

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
        changed = profile.apply(cfg, {"threshold": 0.4, "layout": "split",
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
        self.assertEqual(values["subs"], "on")
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

    def test_auto_applies_and_restores(self):
        self.cfg.cam = "1:2:3:4"
        self.engine._cam_setting = "1:2:3:4"
        on = self.engine.apply_auto(True)
        self.assertTrue(on["auto"])
        self.assertTrue(self.engine._auto)
        self.assertEqual(self.cfg.layout, "split")
        self.assertEqual(self.cfg.cam, "auto")
        self.assertEqual(self.cfg.subs, "on")
        off = self.engine.apply_auto(False)
        self.assertFalse(off["auto"])
        self.assertEqual(self.cfg.cam, "1:2:3:4")

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
                         "/api/streams/stop", "/api/streams/retry", "?download=1"):
            self.assertIn(endpoint, PAGE, endpoint)

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


if __name__ == "__main__":
    unittest.main()
