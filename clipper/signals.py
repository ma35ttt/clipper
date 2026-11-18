"""Signal fusion: turn chat + audio into one 0..1 "hype" score.

Precision first. Four ideas keep the junk clips out:

* **Audio is novelty, not level.** The speech band is compared against a
  rolling median in robust (MAD) standard deviations. A loud game, a bass-heavy
  ad or someone who is simply always shouting all sit near their own baseline,
  so they stop scoring. Only a real departure moves the needle.
* **Chat is consensus, not throughput.** Both the unique-user count and the
  number of distinct users posting the *same* token are measured as deviations
  from their own baselines. Emote floods and raid spam stop looking special.
* **Sustained, not spiky.** The returned score is a rolling average, so one
  loud frame or one burst of messages cannot trigger a clip on its own.
* **Corroborated.** When chat and audio are both live, at least two independent
  signals must agree before the score is allowed past a hard cap.

None of this understands a joke. It correlates. Expect fewer junk clips rather
than never missing a good one.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from .chat import ChatEvent
from .config import Config
from .util import clamp01

EMOTES = {"kekw", "lul", "omegalul", "pog", "poggers", "lulw", "pepehands",
          "sadge", "monkas", "catjam", "pogu", "widepeepohappy", "clap"}
PHRASES = (
    "clip it", "clip that", "clip this", "no way", "wtf", "omg", "lets go",
    "let's go", "insane", "holy", "cooked", "sheesh", "how", "what", "bro",
)
SINGLE_EMOTES = {"w", "f", "ww", "www", "ffff", "gg"}

# Words that carry a moment when they come out of his mouth (a clip's own
# transcript) rather than out of chat. Used by transcript verification.
TRANSCRIPT_PHRASES = (
    "oh my god", "what the", "what is he doing", "are you serious",
    "you're trolling", "you are trolling", "no shot", "no way",
    "what is happening", "look at this", "watch this", "i can't believe",
    "i cant believe", "get out of here", "shut up", "what happened",
    "let's go", "lets go", "bro what", "holy crap",
)
TRANSCRIPT_WORDS = ("insane", "holy", "cooked", "wild", "crazy",
                    "unbelievable", "trolling", "clip")

# --- tuning -----------------------------------------------------------------
MIN_BASE = 12              # samples before a median/MAD baseline is trusted

AUDIO_WINDOW = 1.5         # seconds of audio averaged into the "now" level
AUDIO_BASE_LAG = 8.0       # a burst must not contaminate its own baseline
AUDIO_BASE_SPAN = 90.0     # how far back the baseline reaches
AUDIO_SCALE_FLOOR = 1.0    # dB; never divide by a MAD this small
AUDIO_Z_FLOOR = 1.0        # robust z at which audio novelty starts scoring
AUDIO_Z_SPAN = 2.5         # z above the floor that reaches 1.0
AUDIO_DELTA_SPAN = 9.0     # dB above the median that reaches 1.0 on its own
AUDIO_Z_WEIGHT = 0.55      # blend of "statistically odd" vs "absolutely louder"

CHAT_SAMPLE_DT = 1.0       # baseline is sampled this often, in seconds
CHAT_BASE_LAG = 12.0       # seconds excluded from the chat baseline
CHAT_SCALE_FLOOR = 1.0
CHAT_Z_FLOOR = 1.5
CHAT_Z_SPAN = 3.0

BURST_WINDOW = 7.0         # seconds; the returned score is averaged over this
AGREE_BAR = 0.55           # a signal at/above this counts as "agreeing"
AGREE_CAP = 0.45           # score ceiling when signals do not agree
TRANSCRIPT_TTL = 20.0      # a verification stays relevant this long


@dataclass
class AudioPoint:
    start: float
    end: float
    mean_db: float
    peak_db: float


# --------------------------------------------------------------------------- #
# small numeric helpers
# --------------------------------------------------------------------------- #

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return 0.5 * (data[mid - 1] + data[mid])


def _robust_z(value: float, samples: list[float],
              floor_scale: float = 1.0) -> float | None:
    """Median/MAD z-score. ``None`` until the baseline has enough samples."""
    if len(samples) < MIN_BASE:
        return None
    med = _median(samples)
    mad = _median([abs(s - med) for s in samples])
    scale = max(1.4826 * mad, floor_scale)
    return (value - med) / scale


def _z_to_score(z: float | None, floor: float, span: float) -> float:
    if z is None:
        return 0.0
    return clamp01((z - floor) / span)


def _consensus_users(events: list[ChatEvent]) -> int:
    """How many distinct users posted the same token in the window."""
    per_token: dict[str, set[str]] = {}
    for event in events:
        for token in set(re.findall(r"[a-z']+", event.text.lower())):
            per_token.setdefault(token, set()).add(event.user)
    return max((len(users) for users in per_token.values()), default=0)


def _keyword_hits(text: str) -> int:
    low = text.lower().strip()
    hits = 0
    for phrase in PHRASES:
        if phrase in low:
            hits += 2
    tokens = re.findall(r"[a-z']+", low)
    for token in tokens:
        if token in EMOTES:
            hits += 1
    if low in SINGLE_EMOTES:
        hits += 1
    return min(hits, 4)


def _transcript_score(text: str) -> float:
    if not text:
        return 0.0
    low = re.sub(r"[^a-z' ]+", " ", text.lower())
    low = f" {re.sub(r'\s+', ' ', low).strip()} "
    hits = sum(2 for phrase in TRANSCRIPT_PHRASES if phrase in low)
    hits += sum(1 for word in TRANSCRIPT_WORDS if f" {word} " in low)
    return clamp01(hits / 4.0)


class Signals:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: deque[ChatEvent] = deque(maxlen=6000)
        self.audio: deque[AudioPoint] = deque(maxlen=300)
        self.chat_enabled = cfg.chat is not None
        self._chat_hist: deque[tuple[float, int, int]] = deque(maxlen=240)
        self._last_chat_sample = -1.0
        self._transcript_t = 0.0
        self._transcript_value = 0.0
        self._transcript_text = ""
        self._raw_hist: deque[tuple[float, float]] = deque()
        self._last_t: float | None = None
        self.composite = 0.0
        self.last: dict = {}

    def reset(self) -> None:
        self.events.clear()
        self.audio.clear()
        self.chat_enabled = self.cfg.chat is not None
        self._chat_hist.clear()
        self._last_chat_sample = -1.0
        self._transcript_t = 0.0
        self._transcript_value = 0.0
        self._transcript_text = ""
        self._raw_hist.clear()
        self._last_t = None
        self.composite = 0.0

    # -- inputs --------------------------------------------------------
    def add_chat(self, event: ChatEvent) -> None:
        self.events.append(event)

    def add_audio(self, point: AudioPoint) -> None:
        self.audio.append(point)

    def add_transcript(self, text: str, t: float) -> None:
        """Feed the transcript of a candidate window, from the verifier."""
        self._transcript_text = text or ""
        self._transcript_value = _transcript_score(self._transcript_text)
        self._transcript_t = t

    # -- scoring -------------------------------------------------------
    def score(self, now: float) -> dict:
        if self._last_t is not None and now < self._last_t - 1.0:
            self.reset()  # stream clock rewound (ingest restart)
        self._last_t = now

        window = max(2.0, self.cfg.chat_window)
        lo = now - window
        if self.events and self.events[0].t < lo - 60:
            while self.events and self.events[0].t < lo - 60:
                self.events.popleft()

        recent_chat = [e for e in self.events if e.t >= lo] if self.chat_enabled else []
        rate = len(recent_chat) / window if self.chat_enabled else 0.0

        # --- chat: novelty of unique users and of consensus ------------
        unique_users = len({e.user for e in recent_chat})
        consensus = _consensus_users(recent_chat)
        chat_z = consensus_z = None
        if self.chat_enabled:
            self._sample_chat(now, unique_users, consensus)
            users_base = [u for t, u, _ in self._chat_hist if t <= now - CHAT_BASE_LAG]
            cons_base = [c for t, _, c in self._chat_hist if t <= now - CHAT_BASE_LAG]
            chat_z = _robust_z(unique_users, users_base, CHAT_SCALE_FLOOR)
            consensus_z = _robust_z(consensus, cons_base, CHAT_SCALE_FLOOR)
        chat_score = max(_z_to_score(chat_z, CHAT_Z_FLOOR, CHAT_Z_SPAN),
                         _z_to_score(consensus_z, CHAT_Z_FLOOR, CHAT_Z_SPAN))
        if not self.chat_enabled:
            chat_score = 0.0

        hits = sum(_keyword_hits(e.text) for e in recent_chat)
        # A keyword flood from one account is not a reaction — require a crowd.
        text_score = clamp01(hits / 6.0) * clamp01(unique_users / 3.0)

        # --- audio: novelty in the speech band ------------------------
        recent_audio = [a for a in self.audio
                        if a.end >= now - AUDIO_WINDOW and a.start <= now]
        level = (sum(a.mean_db for a in recent_audio) / len(recent_audio)
                 if recent_audio else None)
        audio_score = 0.0
        audio_z = None
        audio_delta = 0.0
        if level is not None:
            base = [a.mean_db for a in self.audio
                    if now - AUDIO_BASE_SPAN <= a.end <= now - AUDIO_BASE_LAG]
            audio_z = _robust_z(level, base, AUDIO_SCALE_FLOOR)
            if len(base) >= MIN_BASE:
                audio_delta = level - _median(base)
                audio_score = clamp01(
                    AUDIO_Z_WEIGHT * _z_to_score(audio_z, AUDIO_Z_FLOOR, AUDIO_Z_SPAN)
                    + (1.0 - AUDIO_Z_WEIGHT) * clamp01(audio_delta / AUDIO_DELTA_SPAN)
                )

        # --- transcript (candidate verification) ----------------------
        transcript_score = 0.0
        if self._transcript_t and now - self._transcript_t <= TRANSCRIPT_TTL:
            transcript_score = self._transcript_value

        # --- fuse ------------------------------------------------------
        named: list[tuple[str, float, float]] = []   # (name, weight, score)
        groups: dict[str, float] = {}
        if self.chat_enabled:
            named.append(("chat", self.cfg.w_chat, chat_score))
            named.append(("text", self.cfg.w_text, text_score))
            groups["chat"] = max(chat_score, text_score)
        if level is not None:
            named.append(("audio", self.cfg.w_audio, audio_score))
            groups["audio"] = audio_score
        if transcript_score > 0.0:
            named.append(("transcript", self.cfg.w_text, transcript_score))
            groups["transcript"] = transcript_score

        total_w = sum(w for _, w, _ in named)
        mean_score = sum(w * s for _, w, s in named) / total_w if total_w else 0.0
        peak_score = max((s for _, _, s in named), default=0.0)
        # A single overwhelming signal should be enough on its own; the weighted
        # mean keeps weak-but-broad moments from firing.
        raw = 0.6 * peak_score + 0.4 * mean_score

        # Agreement gate: with two independent modalities live, one of them
        # alone is not enough — that is exactly the false-positive shape.
        required = self.cfg.agree or (2 if (self.chat_enabled and self.audio) else 1)
        agreeing = sum(1 for s in groups.values() if s >= AGREE_BAR)
        gated = agreeing < required
        if gated:
            raw = min(raw, AGREE_CAP)

        # --- sustained burst ------------------------------------------
        self._raw_hist.append((now, raw))
        cutoff = now - BURST_WINDOW
        while self._raw_hist and self._raw_hist[0][0] < cutoff:
            self._raw_hist.popleft()
        sustained = self._sustained(now, cutoff)
        self.composite = sustained

        self.last = {
            "t": round(now, 3),
            "score": round(sustained, 4),
            "raw": round(raw, 4),
            "chat_rate": round(rate, 3),
            "chat_users": unique_users,
            "consensus": consensus,
            "chat_z": round(chat_z, 2) if chat_z is not None else 0.0,
            "consensus_z": round(consensus_z, 2) if consensus_z is not None else 0.0,
            "chat": round(chat_score, 3),
            "text": round(text_score, 3),
            "keywords": hits,
            "audio_db": round(level, 1) if level is not None else None,
            "audio_z": round(audio_z, 2) if audio_z is not None else 0.0,
            "audio_delta": round(audio_delta, 1),
            "audio": round(audio_score, 3),
            "transcript": round(transcript_score, 3),
            "agreeing": agreeing,
            "required": required,
            "gated": gated,
        }
        return self.last

    def _sample_chat(self, now: float, users: int, consensus: int) -> None:
        if self._last_chat_sample >= 0 and now - self._last_chat_sample < CHAT_SAMPLE_DT:
            return
        self._last_chat_sample = now
        self._chat_hist.append((now, users, consensus))

    def _sustained(self, now: float, cutoff: float) -> float:
        """Time-weighted mean of the raw score over the last few seconds.

        Weighting by elapsed time rather than by sample count keeps the average
        honest: the loop ticks fast while the score is high and slowly when it
        is not, and a plain mean would let the burst over-weight itself.
        """
        span = max(1e-6, now - cutoff)
        acc = 0.0
        prev_r = 0.0
        cur = cutoff
        for t, r in self._raw_hist:
            if t > cur:
                acc += prev_r * (t - cur)
            cur = max(cur, t)
            prev_r = r
        if now > cur:
            acc += prev_r * (now - cur)
        return acc / span
