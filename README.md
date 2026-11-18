# clipper

Realtime livestream clip machine. It watches a live stream, scores how unusual every
moment is — **chat consensus** and **audio novelty in the speech band**, then verifies
flagged candidates with a quick transcript — and when the score is sustained and
corroborated it cuts the last N seconds straight out of a rolling buffer: already
reframed vertical, already captioned, ready to post while the moment is still happening.

No Python dependencies. Pure stdlib + `ffmpeg`.

```
 ingest (ffmpeg/HLS)  ->  rolling 4-minute buffer
                            |
                     signals (10 Hz): chat consensus, chat keywords,
                                      speech-band audio novelty
                            |
                     candidate?  ->  fast transcript verification (tiny.en)
                            |
                     score = 0.6*peak + 0.4*weighted mean
                             ->  agreement gate  ->  ~7s sustained average
                             ->  threshold + confirm
                            |
                     cut window [now-12s .. now+8s] -> 9:16 mp4 + metadata
```

## Quickstart

```bash
# 1. check the environment (ffmpeg, encoders, captions, output dir)
python3 -m clipper doctor

# 2. clip a live Twitch channel
python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --layout blur

# 3. clips land in ./clips, watch them at the local console
open http://127.0.0.1:8756
```

Or start with just the console and paste a link into it:

```bash
python3 -m clipper dashboard --open
```

Requirements: `ffmpeg` + `ffprobe` on `PATH` (with `libx264` and `aac`). Python 3.10+.
Optional: `yt-dlp` or `streamlink` for resolving non-Twitch page URLs; `whisper.cpp`
(`brew install whisper-cpp`) plus a model for subtitles; `imagemagick` for real-font
outlined captions. Automatic webcam detection uses the macOS Vision framework via the
system Swift compiler — no install needed on a Mac, and elsewhere you just pass `--cam`
by hand.

Install it as a command if you like: `pip install -e .` then `clipper run ...`.

### One streamer, best quality

If you are running a single channel and care about quality more than throughput:

```bash
python3 -m clipper models --get small.en

python3 -m clipper run -i https://twitch.tv/jynxzi -c twitch:jynxzi --layout blur --profile sensitive --subs on --subs-prompt "Jynxzi, R6 Siege, ranked, clutch, no way" --pre 14 --post 10 --max-post 25
```

One ingest, one render queue, no worker fan-out — the lightest the pipeline gets on an
8 GB laptop, with the accuracy settings the subtitles section describes.

## What it does

- **Ingests anything ffmpeg can read** — a Twitch HLS URL, a raw `.m3u8`, RTMP, a local
  file, or a page URL (`twitch.tv/x`, `youtube.com/watch?v=...`) resolved through
  yt-dlp/streamlink/Twitch's own playback API.
- **Keeps a rolling buffer** using ffmpeg's HLS muxer, so segment durations come straight
  from the playlist and the timeline never drifts out of sync with the stream.
- **Scores the moment** ten times a second from four signals (below), then verifies flagged
  candidates with a fast transcript.
- **Cuts on trigger**, with configurable lead-in (`--pre`) and tail (`--post`), auto
  extending while the hype continues (up to `--max-post`).
- **Exports post-ready clips**: 1080x1920 (fill) or blurred-background 9:16, a hook
  caption, `+faststart`, plus a `.json` sidecar with the score breakdown and chat excerpt.
- **Lets you drive it live** from a local control panel: tune every knob, hit **AUTO**,
  record on demand with **REC**, and manage clips — per-streamer settings are remembered.
- **Never writes to the platforms.** Read-only: anonymous chat, read-only stream.
- **Recovers**: restarts the ingest on failure and refreshes expiring stream URLs
  (Twitch playback URLs last ~20 minutes).

## Signals

| signal | how it works |
| --- | --- |
| `chat` | how far the **unique-user count** and the **consensus** (distinct users posting the *same* token) sit above their own rolling median, in robust (MAD) z-scores. Emote floods and raid spam stop looking special. |
| `text` | `CLIP IT`, `NO WAY`, `W`, `KEKW`, … density in the 6s window, scaled by how many *different* users said it — one account spamming keywords is not a reaction. |
| `audio` | per-segment loudness in the **speech band** (200-3400 Hz) on the segments we already wrote, scored as a departure from its own rolling median — a loud game or a bass-heavy ad sits near its baseline and no longer scores. |
| `transcript` | when a candidate is flagged, the smallest installed whisper model (`tiny.en`) transcribes ~10s of the moment off the hot path, and strong phrases become a signal. Off with `--verify off`. |

Fusion is `0.6 * strongest_signal + 0.4 * weighted_mean`, then two guards:

- **Agreement gate.** With chat and audio both live, at least **two** independent signals
  must agree before the score can pass (one loud thing alone is capped, which is exactly
  the false-positive shape). `--agree 1` disables it.
- **Sustained, not spiky.** The score you trigger on is a **time-weighted ~7s average**, so a
  single loud frame or one burst of messages cannot fire a clip on its own.

Honest limit: this correlates, it does not understand a joke. It is tuned to produce fewer
junk clips rather than to never miss a good one.

**Tuning is data, not guesswork**: every score is appended to
`.clipper/scores.jsonl` at 2 Hz. After a session look at the top scores, then pick a
threshold just under the moments you actually wanted.

```bash
python3 -c "
import json; rows=[json.loads(l) for l in open('.clipper/scores.jsonl')]
rows.sort(key=lambda r: -r['score'])
for r in rows[:10]: print(r['t'], r['score'], r['chat_users'], r['consensus'], r['audio_z'], r['gated'])"
```

Presets: `--profile sensitive | default | conservative`.

## Layouts and captions

```bash
--layout horizontal   # as-is (16:9), fastest render
--layout vertical     # scale + centre crop to 1080x1920 (fill)
--layout blur         # 9:16 with a blurred fill behind the full frame
--layout split        # webcam on top, gameplay in the middle, captions below
--cam auto            # find the streamer's webcam and follow it as they move (split)
--cam 0:539:367:294   # or pin the webcam box x:y:w:h by hand (split only)
--subs-style pop      # popping word-chunks on a pill (default)
--subs-style block    # calmer multi-line subtitle block
--title-mode auto     # hook caption quoted from the clip's own transcript
--title-mode fixed --title "HE COOKED"
--title-mode none
```

### The `split` layout

`split` stacks three bands into a 1080x1920 frame: the streamer's webcam cropped out of
the source across the top, the untouched 16:9 frame underneath, and a softly blurred band
below that which carries the burned subtitles and the hook. The small copy of the webcam
baked into the gameplay is blurred away so his face does not appear twice.

**The webcam is found automatically.** `--cam auto` is the default: clipper samples a
dozen frames of the stream and runs face detection on each, then keeps the face that
appears in *almost every* frame — that is the streamer's camera, whereas faces in the
gameplay come and go. So when the streamer drags their camera somewhere new on screen,
the crop follows within `--cam-interval` seconds (default 90). Detection runs in the
background, off the ingest loop.

```bash
clipper frame -i https://twitch.tv/xqc     # see the detected box drawn on a frame
```

`clipper frame` also prints the box to copy into a fixed `--cam x:y:w:h`, which always
overrides detection. Detection needs macOS (Vision); elsewhere, or if no face is found,
clipper falls back to the measured xQc box `0:539:367:294`.

### Hook captions

With `--title-mode auto` the hook is a short phrase lifted from the clip's own transcript —
`"NO WAY"`, `"WHAT IS HAPPENING"`, `"OH MY GOD"` — rather than a generic label. It says what
is unique about *this* clip instead of commenting on chat. When nothing quotable was said the
clip simply gets no hook, which beats a caption that means nothing.

Because the hook quotes the transcript, `auto` needs subtitles enabled to have something to
quote; `--title-mode fixed` always works.

Captions use `drawtext` when your ffmpeg was built with freetype; otherwise clipper
rasterises a bold bitmap caption to a PNG with the stdlib and overlays it, and prefers
ImageMagick-rendered real-font text when `magick` is installed.

## Subtitles

Subtitles are burned into the clip from speech recognition over the exact audio window of
the clip. It is **on by default when a speech-to-text model is available**, and silently
skipped when it is not.

```bash
python3 -m clipper models --get medium.en  # one-off ~1.5GB whisper.cpp model (best)
python3 -m clipper models --get vad        # ~0.9MB VAD model for the rescue pass
python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --layout blur

--subs auto          # default: on if a model is found, else off
--subs on            # force (warns if unavailable)
--subs off           # never
--subs-style pop     # popping word-chunks (default)
--subs-style block   # multi-line text block
--subs-model models/ggml-medium.en.bin      # pick a specific model
--subs-lang en                              # spoken language
--subs-prompt "xQc, Juicer, Kick, Twitch"   # bias names/game terms
```

Backend: **whisper.cpp** (`brew install whisper-cpp`), searched on `PATH` as
`whisper-cli`. If `mlx_whisper` or openai-`whisper` is importable, that is used instead.
No Python dependencies either way.

Models are looked up in `models/`, `.clipper/models`, `~/.cache/whisper.cpp/models`,
`~/.local/share/whisper.cpp/models`, and Homebrew's share dir; override with
`--subs-model` or `$CLIPPER_WHISPER_MODEL`. `python3 -m clipper models` shows what was
found and `python3 -m clipper doctor` reports subtitle readiness.

whisper.cpp pads short audio to a 30-second window and will happily invent words there,
so clipper trusts only the tokens that land inside the real audio: any token whose raw end
is past `duration + 0.75s` is dropped, and a word whose merged span exceeds 2.5s is
dropped too (that catches a stray "So" stretched across the whole padding). Both the
popping chunks and the block cues are built from those trusted word timings; if every
token was rejected the clip gets **no subtitles** rather than a guessed line.

### Accuracy

Accuracy matters more than latency here, and clipper leans that way:

- **Bigger models win, and are preferred automatically.** `medium.en` beats `small.en`
  beats `base.en` beats `tiny.en`, and clipper uses the best one installed. `small.en`
  is the sweet spot on an 8 GB M1; `medium.en` is the accuracy pick if you can spare the
  RAM. Grab either with `python3 -m clipper models --get small.en`.
- **The audio is cleaned before whisper sees it** — a high-pass filter plus loudness
  normalisation, because stream audio is loud, clipped and bass-heavy.
- **Word-level sync for popping captions.** Alongside the segments, clipper reads
  whisper's per-token offsets (`-ojf`), merges tokens back into words ("`it`" + "`'s`"
  → `it's`), and drives each popping chunk off the real spoken timing rather than a
  length estimate.
- **A VAD rescue pass, not a default.** On music, cutscenes or near-silence the plain
  decode can hallucinate a whole sentence. When what it produced is too sparse to be
  real speech (<3 trusted words, or less than 35% coverage), clipper re-runs whisper with
  the Silero VAD (`--vad`, model auto-downloaded by `clipper models --get vad`) and keeps
  that result only when it heard meaningfully more. VAD is not the default because it
  also drops real lines when the game is louder than the streamer.
- **`--subs-prompt` biases the decoder** toward a streamer's name, game and recurring
  slang. Keep it short and noun-heavy — long phrase lists get echoed back on noisy audio.
  **Auto configure** fills this in per streamer.
- **whisper shares the machine.** Transcription takes half the cores (capped at four)
  by default, so the desktop, the browser and the ingest keep running while a clip
  transcribes. Override with `--subs-threads N` (`0` = auto, negative = all cores) when
  the box is otherwise idle and you want each clip finished sooner.

### How they look

By default subtitles are **popping captions** — one to three words at a time, bold white
with a clean black outside outline on a rounded dark pill, swapped as they are spoken, the
look every Shorts/TikTok clipper uses. Each chunk is timed from whisper's word offsets.
The type is sized to the frame (about 9.5% of the width) so short reactions stay punchy
and full lines still fit on one row. Choose `--subs-style block` for a calmer, multi-line
subtitle block instead.

The hook caption and the popping captions are placed so they never fight for the same
space: with `pop` the hook moves up out of the caption band, with `block` it keeps its
place lower in the frame.

Captions are rendered with a real system font (Arial Bold, then Tahoma/Verdana/Trebuchet)
through ImageMagick when `magick` is available (`brew install imagemagick`), and fall back
to a built-in bold bitmap font otherwise — so subtitles never depend on the machine having
fonts or `drawtext`. The outline is built by dilating the text mask into a black
silhouette and compositing the untouched white fill on top: a centred ImageMagick stroke
would eat into the glyphs and make bold text look thin and hollow.

## Chat sources

```bash
-c twitch:xqc              # anonymous read-only Twitch IRC (no token needed)
-c file:chat.jsonl         # replay: {"t": seconds, "text": "...", "user": "..."}
-c sim                     # synthetic hype bursts, for tuning without a live stream
```

`--chat-lag 3` compensates for viewers seeing the stream a few seconds behind your
ingest — chat bursts are attributed to the moment they reacted to.

## Multiple streamers

Clip several channels at once. Each channel gets its own independent worker (its own
ingest, buffer, scoring, and clip folder), and one master console tiles them all.

```bash
# by Twitch channel name — chat is wired up automatically
python3 -m clipper multi --channels xqc,kaicenat,caseoh --layout blur

# or spell each one out:  name|input|chat
python3 -m clipper multi \
  --channel "kai|https://twitch.tv/kaicenat|twitch:kaicenat" \
  --channel "case|https://twitch.tv/caseoh|twitch:caseoh" \
  --layout blur --profile sensitive
```

- Every `run` flag works here too (`--pre`, `--post`, `--subs`, `--encoder`, ...) and is
  forwarded to all workers.
- Clips land in per-channel folders: `clips/xqc/`, `clips/kaicenat/`, ... Use `--out` to
  change the root.
- The master console is on `--port` (default `8756`); workers take the next free ports.
  `grid / single` toggles between the tiled view and one full channel.
- Each worker runs independently — one channel dropping never stops the others.

Keep it realistic: each worker is a live ffmpeg ingest plus render jobs, so 3–4 channels
is a sensible ceiling on a laptop. Prefer `--layout horizontal` or `--encoder
h264_videotoolbox` when running several at once.

## Operator console

`--port 8756` (default on) serves a local control panel at `http://127.0.0.1:8756`.
It binds loopback only, so it is private to the machine running it. Two people on
different computers each get their own console on their own `8756` with no collision.
If the default port is already taken on one machine, the second copy moves to the next
free port (8757, 8758, ...) and says so in its log; `--port 9000` is respected exactly.

You can open the console **without a stream** and add links later:

```bash
cd ~/clipper && python3 -m clipper dashboard --open
```

That mode hides the score card and clips from this console's own ingest (there is none),
keeps the tuning and Auto configure, and copies those settings into each worker started
from the page. The `dashboard` command also answers to `console`.

Running two copies from the same checkout on one machine also means they share `.clipper`
and `clips`, which would overwrite buffers and profiles. Give the second one its own
directories:

```bash
cd ~/clipper && python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --work .clipper-b --out clips-b
```

Everything you would otherwise pass as a flag is controllable from the page:

- **Live telemetry** — a hype-score sparkline with the trigger line drawn across it, a
  state chip (armed / clipping / recording / cooldown / offline), per-signal meters,
  session stats, and chips for the active layout, webcam box, caption style and encoder.
- **Tuning** — every trigger, frame, caption, verify, encoder, chat and weight setting,
  rendered from one spec (`clipper/profile.py`) so the page, the flags and the saved file
  can never drift apart. Changes apply as you edit; a few (chat connection, `--verify`
  model) restart their worker on the spot.
- **AUTO** — one button that configures the lot: 9:16 `split` frame, automatic webcam
  follow, synced popping captions, the hook quoted from the clip's own words, and a
  tuned trigger. Press it again and your previous settings come straight back. Keyboard
  `a`.
- **Auto configure** (on the tuning panel) — a one-shot preset for the current streamer:
  the same tuned frame and captions as AUTO, plus that channel's decoder vocabulary
  (`xQc` gets Juicer/Kick/GTA/slots, and so on) and, on YouTube where there is no chat,
  audio-led trigger weights. Unlike AUTO it is not a mode — it applies the values and
  leaves them editable, and turns the AUTO toggle off.
- **REC** — start a manual recording; press it again (or keyboard `r`) to cut the clip.
  The lead-in comes out of the rolling buffer, so you do not have to press it *before*
  the moment. The banner shows elapsed time and warns as you approach the buffer limit.
- **CLIP NOW** (keyboard `c`) — force a clip at the current moment when the score missed
  something.
- **Profiles** — tuning is saved per streamer under `.clipper/profiles/<channel>.json`
  and reloaded on startup. Anything you pass on the command line still wins over the
  file. *Save* / *Reload* / *Reset* are on the panel, plus **Find camera** to re-run face
  detection on demand.
- **Clip wall** — inline 9:16 playback, with download, copy-path and delete on each clip.
  An activity log shows the last minutes of events without leaving the page.
- **Add a stream** — paste a Twitch or YouTube link to start clipping another streamer
  (see [below](#adding-a-stream-from-the-console)). Each added stream gets its own
  folder, worker and console.

`--no-profile` turns off loading and saving the per-streamer file.

### Adding a stream from the console

The **add a stream** box takes a Twitch or YouTube link and starts clipping that
streamer immediately. Each added stream is a separate `clipper run` worker (the same
pattern as `multi`), so it gets its own ingest, buffer, chat, captions and clip folder
at `clips/<streamer>/`, plus its own console on the next free port (the **open** link
on its row).

If something is already being clipped, the page asks first: **Run both** starts the
second stream alongside the first; **Keep current only** leaves things untouched.
Each stream adds a live ingest plus a caption/render pipeline, so two is comfortable
and three is about the ceiling on a laptop. Added workers inherit the console's
current tuning, with webcam detection forced back to `auto` (so each streamer's own
face is found) and any fixed hook text dropped (so the hook is quoted from each clip).
Tune any of them independently on its own console.

**stop** frees the machine and leaves its clips on disk. **retry** restarts a worker
that died (offline channel, lost connection). YouTube links have no supported chat
source, so they trigger on audio only; put a name in the optional box to get
`clips/<name>/` instead of a video id.

## Profiles and the Auto button

The console's settings and the CLI flags are the same knobs. A handful map to
`clipper.profile` fields; the rest (source, output dir, dashboard port) are fixed at
launch. When you run the same channel again, the saved profile is applied *only* to
fields you left at their default on the command line — an explicit flag always wins:

```bash
python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --layout split --cam auto
```

Everything you change from the page after that is written back to
`.clipper/profiles/xqc.json`, so the next launch starts where you left off.

## Tuning cheatsheet

| goal | flags |
| --- | --- |
| more clips, accept some noise | `--profile sensitive` |
| only unmistakable moments | `--profile conservative` |
| react slower, catch build-ups | `--confirm 2 --chat-window 10` |
| longer clips | `--pre 20 --post 15 --max-post 40` |
| punchy shorts | `--pre 8 --post 6 --max-post 15` |
| don't overwhelm the chat-score | `--threshold 0.7` |
| a single signal is enough (no corroboration) | `--agree 1` |
| more transcript weight / less CPU | `--verify auto` / `--verify off` |
| the familiar popping captions | `--subs-style pop` (default) |
| calmer multi-line subtitles | `--subs-style block` |
| follow the streamer's moving camera | `--cam auto --layout split` (default on macOS) |
| re-detect the camera more often | `--cam-interval 45` |
| lower render latency | `--encoder h264_videotoolbox` (macOS), `--encoder-preset ultrafast` |
| tune it live instead of by flag | open the console, or press `a` for auto |

## Demo without a live stream

```bash
python3 scripts/make_demo.py          # 90s synthetic stream + matching chat replay
python3 -m clipper run -i demo/demo.mp4 --loop -c file:demo/chat.jsonl --layout blur
```

## Tests

```bash
python3 -m unittest discover -s tests
```

Covers playlist parsing/timeline/restart handling, scoring behaviour for busy vs. quiet
chat and loud vs. quiet audio, caption rasterisation, and an end-to-end clip render.

## Troubleshooting

- **`No such filter: 'drawtext'`** — handled automatically (bitmap fallback). Install an
  ffmpeg with `--enable-libfreetype` for nicer text.
- **HLS copy fails / weird source codec** — add `--transcode`.
- **`ffmpeg ingest exited`** — the tail of the ingest error is in `.clipper/ingest.log`.
- **Twitch URL stopped working mid-stream** — expected; playback URLs expire and clipper
  refreshes them automatically ~2 minutes before expiry.
- **Clips lag behind realtime** — the scorer is realtime; rendering is a background queue.
  Lower `--max-post`, use a hardware encoder, or `--layout horizontal`.
- **The whole desktop stutters while clipping (8 GB machines).** Clipping is light in
  steady state — one `-c copy` ffmpeg — but each clip spikes: whisper loads a 465 MB
  model and the `blur` render encodes 1080×1920. On a small machine:
  - don't watch the same stream in a browser while clipping it, or watch at 480p: a
    Chromium browser with one tab can hold ~1.7 GB and spike 40–60% CPU on its own;
  - keep the threads cap (`--subs-threads 4`, the default) so whisper leaves cores free;
  - use a smaller model (`--subs-model models/ggml-base.en.bin`, 141 MB vs 465 MB);
  - `--layout horizontal` skips the 1080×1920 blur pass entirely;
  - raise `--threshold` / `--cooldown` so fewer clips fire.

## Legal

Clipping other people's streams is fine when you have permission or a licence, and it is
a normal part of Twitch/Kick/YouTube culture — but republishing or monetising somebody
else's broadcast can breach platform terms and copyright. Check the streamer's rules and
your platform's clip/partner policies before you build a business on it.

## License

MIT — see [LICENSE](LICENSE).
