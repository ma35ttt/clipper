# clipper

Watches a live stream, finds the moments people react to, and cuts them into
9:16 clips. It runs in realtime off a rolling buffer, so a clip is ready while
the moment is still happening.

The live default is **capture-only**: find the webcam, keep the split frame, cut
the clip. No captions, no hook and no editing are burned in while you stream, so
a long session stays cheap on an 8 GB laptop. When the session ends, pick the
clips worth posting and apply captions, a grounded hook and punch-ins in one
pass. The console does both; the CLI is `clipper style`.

No Python packages. Python 3.10+ and `ffmpeg` are all it needs.

```
stream  ->  rolling 4-minute buffer  ->  score the moment (chat + audio)
        ->  cut window  ->  capture-only 9:16 mp4 + metadata
                                   |
                     session ends  v
        chosen clips  ->  captions + hook + punch-ins  ->  styled mp4
```

---

## Windows: start here

You need four things installed once, then one command to run it.

### 1. Install Python

Download from <https://www.python.org/downloads/windows/> and tick **"Add
python.exe to PATH"** on the first screen. Check it worked:

```bat
cd %USERPROFILE%
py -3 --version
```

### 2. Install ffmpeg

```bat
cd %USERPROFILE%
winget install Gyan.FFmpeg
```

Close the terminal, open a new one, and check:

```bat
cd %USERPROFILE%
ffmpeg -version
```

If `winget` is not available, download a Windows build from
<https://www.gyan.dev/ffmpeg/builds/>, unzip it, and add its `bin` folder to your
PATH.

### 3. Install the caption tools

ImageMagick draws the real fonts:

```bat
cd %USERPROFILE%
winget install ImageMagick.ImageMagick
```

Speech-to-text comes from whisper.cpp. Download `whisper-bin-x64.zip` from
<https://github.com/ggerganov/whisper.cpp/releases>, unzip it somewhere permanent
(for example `C:\whisper`), and add that folder to your PATH so `whisper-cli.exe`
is found. Then open a new terminal and check:

```bat
cd %USERPROFILE%
whisper-cli --help
```

### 4. Get the repo and a model

Install Git first if you do not have it:

```bat
cd %USERPROFILE%
winget install Git.Git
```

Then clone the repo and fetch the speech model (about 500 MB):

```bat
cd %USERPROFILE%
git clone https://github.com/ma35ttt/clipper
cd clipper
py -3 -m clipper models --get small.en
py -3 -m clipper models --get vad
```

No Git? Download the repo as a ZIP from
<https://github.com/ma35ttt/clipper>, unzip it to `%USERPROFILE%\clipper`, and
run the same two `models` commands.

### 5. Check everything

```bat
cd %USERPROFILE%\clipper
py -3 -m clipper doctor
```

Every line should be a tick. The one exception is **camera detect**, which is
macOS-only — see [Windows and the webcam](#windows-and-the-webcam) below.

### Run it

```bat
cd %USERPROFILE%\clipper
py -3 -m clipper dashboard --open
```

The console opens in your browser. Paste a Twitch link into **add a stream** and
it starts clipping. Clips land in `clips\<streamer>\`.

To write clips somewhere else (another drive, an external SSD):

```bat
cd %USERPROFILE%\clipper
py -3 -m clipper dashboard --open --out D:\clips
```

---

## macOS

```bash
cd ~/clipper
brew install ffmpeg-full whisper-cpp imagemagick
python3 -m clipper models --get small.en
python3 -m clipper models --get vad
python3 -m clipper doctor
python3 -m clipper dashboard --open
```

Homebrew's regular `ffmpeg` bottle has no libass, so it cannot burn `.ass`
captions. `ffmpeg-full` is keg-only and installs alongside it; clipper prefers it
automatically. If it is missing, styled clips fall back to the PNG captions.

macOS also gets **automatic webcam detection** for the `split` and `duo` layouts,
using the system Vision framework. Nothing to install.

## Linux

Install `ffmpeg` and `imagemagick` from your package manager, build whisper.cpp so
`whisper-cli` is on your PATH, then use the macOS commands with `python3`.
There is no automatic webcam detection: pass `--cam x:y:w:h`, or use
`--layout blur`.

---

## Windows and the webcam

The camera layouts (`split` and `duo`) need to know where the webcam sits in the
source frame. On macOS clipper finds it by itself. On Windows it cannot, so you
have two options:

- **Use the blurred layout** — it needs no webcam box and works everywhere:

  ```bat
  cd %USERPROFILE%\clipper
  py -3 -m clipper dashboard --open --layout blur
  ```

- **Pin the box by hand.** Open the stream in a browser, screenshot one frame,
  and measure the webcam rectangle in pixels (`x:y:width:height`). Then:

  ```bat
  cd %USERPROFILE%\clipper
  py -3 -m clipper dashboard --open --layout split --cam 1400:40:480:270
  ```

You can also type the box into **Webcam box** on the console and the split layout
switches on immediately.

---

## The console

`dashboard` opens a local control panel at <http://127.0.0.1:8756>. It is bound to
your machine only; nothing is exposed to the network.

- **AUTO** — one button: split frame, webcam follow, capture-only, tuned trigger.
  Press again and your old settings come back.
- **Auto configure** — a per-streamer preset with the right names for the speech
  decoder. Still capture-only until you choose captions yourself.
- **Style selected** — tick capture clips, pick a look, and clipper transcribes
  them, writes the hook and adds punch-ins after the session (see below).
- **REC** and **CLIP NOW** — force a clip from the rolling buffer.
- **Edit & voice** — the two new toggles, described below.
- **Add a stream** — paste a link; each streamer gets its own worker, folder and
  console.
- **Clip a past stream** — paste a finished Twitch VOD or YouTube link and clipper
  analyses the whole thing and cuts its best moments (see below).
- **Update** — pull the newest version from GitHub without leaving the page.
- **Clip wall** — play, download, copy the path, or delete each clip.

Everything on the page is also a command-line flag, and per-streamer settings are
remembered in `.clipper/profiles/<channel>.json`.

---

## Clipping a past stream

Realtime clipping needs the stream to still be live. To mine a stream that has
already ended, paste its link — or point `vod` at a link or a recording — and
clipper scores the whole timeline instead of the last few minutes:

```bat
cd %USERPROFILE%\clipper
py -3 -m clipper vod https://www.twitch.tv/videos/123456789 --out D:\clips
```

By default one run takes **12 clips**, not everything the stream ever did. The
file `<work>/vod_history.json` remembers which moments each source has already
given you, so running the same link again takes the **next** batch instead of
duplicating the first one:

```bash
cd ~/clipper && python3 -m clipper vod https://www.twitch.tv/videos/123456789
cd ~/clipper && python3 -m clipper vod https://www.twitch.tv/videos/123456789
```

`--max-clips N` changes the batch size, and `--reset` starts that source over
from its strongest moments. On the console, leave the link in the box and press
**Clip past stream** again; tick **start over** only when you want the first
batch again.

The same knobs apply: `--layout`, `--subs`, `--edit coldopen`, `--voice on`,
`--threshold`, `--profile`, and so on. How it works:

- **The link is resolved to something seekable.** A Twitch VOD becomes a direct
  playback URL; a YouTube link or anything else goes through `yt-dlp`; a local
  recording or `.m3u8` is used as-is.
- **The chat is re-read from the VOD.** Twitch keeps a finished stream's chat on
  its own timeline, so the same chat-rate novelty that fires live is rebuilt
  offline (cached under `<work>/vod/`, so a re-run is instant).
- **The audio is measured in one pass** — the speech band is scanned once for its
  loudness curve, then chat and audio are fused into one hype score.
- **Each moment is cut out and rendered normally**, one at a time, so captions,
  hook, cold open and voiceover behave exactly as they do live.

Useful extras:

```bash
--max-clips 12           # how many new clips this run may take (default 12)
--reset                  # ignore the ledger and start this source over
--chat-mode off          # audio only, no chat
-c file:chat.jsonl       # score with a recorded chat instead
```

`vod` is also a button on the console: paste a past-stream link into **Clip a
past stream** and watch the progress bar in the panel.

---

## Style clips after the session

Capture clips are deliberately plain. Styling one is a separate, chosen step:

```bash
cd ~/clipper
python3 -m clipper style clips/20261119-2210_jynxzi_077.mp4 --style hype
python3 -m clipper style --last 5 --style clean        # newest five captures
python3 -m clipper styles                              # list every look
```

On Windows the same, with `py -3 -m clipper`:

```bat
cd %USERPROFILE%\clipper
py -3 -m clipper style clips\name.mp4 --style hype
```

A styled copy lands beside the capture as `<name>_<style>.mp4`; the original is
never overwritten. The sidecar records the transcript, the hook and the punch-in
plan. On the console, tick the clips in **clips**, choose a style in the dropdown
and press **Style selected**.

What a style controls:

- **Captions** — popping chunks, block lines, a held sentence (`static`), one
  huge word at a time (`word`), or off. Burned with libass (`.ass`) when the
  installed ffmpeg has it, PNG overlays otherwise. Subtitles stay white by
  default; only `karaoke` colours a stressed word.
- **Placement** — captions can sit at the `top`, `upper`, `middle`, `seam`,
  `lower` or `bottom` of the frame. `seam` is the join between camera and
  gameplay, which moves when the layout changes.
- **Hook** — the same grounded source as live: repeated chat, a real question, a
  quotable line, or nothing. Gold title face, black outline. `reveal` uses a
  calm white title at the top instead.
- **Punch-ins** — 0–3 short crops (max 1.25x) on the stressed words. One-pass
  `crop`/`scale`, never `zoompan`.
- **Cold open** — optional "payoff first, then rewind".
- **Voice** — the live rung is available, but off in the built-in looks.

Built-in looks:

| style | what it does | learned from |
| --- | --- | --- |
| `clean` | one punch-in, popping white captions, gold hook | default |
| `hype` | three quick punch-ins, harder pacing | default |
| `cinematic` | block captions, slow push-ins, no hook | default |
| `podcast` | block captions, no hook, no punch-ins | default |
| `raw` | no captions, no hook, no punch-ins | the capture itself |
| `story` | one static white sentence held at the seam | wixxey, lacy |
| `story-karaoke` | seam sentence plus one huge word over the camera | shadowzin |
| `meme` | three-line white caption in the lower third | jirolfs |
| `reveal` | persistent white top title, no spoken captions | ViceDetail |
| `karaoke` | one huge uppercase word, coloured on the beat | the GTA 6 Short |

The reference-derived looks are explained in
[docs/REFERENCES.md](docs/REFERENCES.md): the clips, what was measured, what was
borrowed and what was deliberately not.

## Styles from reference reels

Drop reference clips (your own or accounts you have permission to learn from)
into a folder and let clipper measure their pacing:

```bash
cd ~/clipper
python3 -m clipper styles --scan ~/Movies/references
```

It counts hard cuts, cuts per minute and average shot length, then categorises
each reference as `rapid-fire`, `hype`, `montage`, `clean` or `podcast` and saves
a matching style under `.clipper/styles/ref-*.json`. Use it like any other style:

```bash
python3 -m clipper style --last 3 --style ref-that-reel
```

This copies **rhythm**, not artwork: fonts, memes and B-roll still need to be
sourced and placed deliberately. The report is written to
`.clipper/styles/report.json`.

---

## Keeping it up to date

If you cloned the repo with `git`, updating is one step. On the console, press
**Update**; from a terminal:

```bash
cd ~/clipper && python3 -m clipper update
```

On Windows you can also double-click **`update.bat`** (macOS: `update.command`).
`clipper update --check` only reports what is waiting, without changing anything.
The updater fast-forwards and stops if your copy has local changes, so it never
throws work away; restart the console afterwards to load the new version.

---

## Edit structure (cold open)

A raw clip played start to finish is a reupload. `--edit coldopen` restructures it
like an editor would:

1. **The payoff plays first**, cold — the loudest, most-talked-about few seconds.
2. A short dip to black.
3. **The clip rewinds** and plays from the beginning, so the payoff lands a second
   time with its context.

Nothing is removed; the cold open is a repeat, so the finished clip is a few
seconds longer. Cut points are pushed into the silence between words, so no word
is chopped in half, and the burned captions are re-timed onto the new timeline.
If a clip is too short or there is no clear payoff, it renders in the normal order
and the log says so.

```bash
cd ~/clipper && python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --edit coldopen
```

On the console it is **Edit structure → coldopen** under *Edit & voice*.

## AI voiceover

`--voice on` writes a short, factual intro and speaks it over the top of the clip,
with the stream audio ducked underneath. It is not a reaction track: the narrator
frames what you are about to see, which is what makes the upload a transformed
work rather than a straight copy.

The line is built only from evidence in the clip:

- a repeated phrase from **chat**, and/or
- a quotable line from the **streamer's own transcript**.

Examples of the shape:

> "Chat called it first: 'look at the map'. Then ohnepixel said: 'Oh, I don't want
> to sell my car though'."

If neither exists, the clip gets **no voiceover** instead of an invented story.
Profanity is refused, because a synthetic narrator reading it back is a
demonetisation risk.

Speech engines, best first, all free:

| engine | quality | install |
| --- | --- | --- |
| `piper` | best, offline, neural | see below, then `models --get voice` |
| `edge-tts` | very good, online | `pip install edge-tts` (optional) |
| macOS `say` | good, offline | built in on macOS |
| Google Translate TTS | decent, online | nothing — used automatically |
| Windows SAPI | robotic, offline | built in on Windows |

Best result on either platform:

```bash
cd ~/clipper && python3 -m clipper models --get voice
```

That downloads the `piper` narrator voice. You also need the `piper` binary
itself:

```bash
cd ~/clipper && brew install piper-tts
```

On Windows, download `piper_windows_amd64.zip` from
<https://github.com/rhasspy/piper/releases>, unzip it into `models\piper\`, and
keep `piper.exe` next to the `espeak-ng-data` folder. `clipper doctor` reports
which engine it will use.

The mix costs one audio encode — the video is stream-copied, so it is quick. If
anything fails, the un-narrated clip is kept.

```bash
cd ~/clipper && python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --edit coldopen --voice on
```

On the console: **AI voiceover → on** under *Edit & voice*. `--voice-max 6` keeps
the narration shorter.

---

## Publishing

When a batch of clips is done, turn them into a posting queue:

```bash
cd ~/clipper && python3 -m clipper pack --out clips
```

That writes `posting-queue.csv` next to the clips: one row per clip with the hook
as the title, two alternates, a description and hashtags, plus columns for views
once you have posted. Open it in a spreadsheet, upload in batches, fill in the
numbers.

Uploading itself is deliberately not automated: every platform wants its own app
registration and OAuth approval, and browser automation of the upload form risks
the account. The last mile is about five minutes a day.

The full 14-day plan, title formulas and the reused-content reasoning are in
[docs/STRATEGY.md](docs/STRATEGY.md).

---

## Layouts

```bash
--layout split        # default: webcam on top, gameplay, captions below
--layout duo          # webcam on top, gameplay fills below, captions at the seam
--layout blur         # 9:16, blurred fill behind the full frame
--layout vertical     # 9:16 centre crop
--layout horizontal   # leave it 16:9
```

`split` stacks three bands into 1080x1920: the webcam across the top, the gameplay
underneath, and a blurred band below for the captions and the hook. The gameplay
is cropped away from the webcam side, so the game fills more of the frame and the
duplicate face is not shown twice.

`duo` is the reference-reel frame: the webcam gets the top 840 px and the gameplay
fills the remaining 1080 px edge to edge, with no caption band. Captions are
drawn over the join, which is where most of the reference clips put them. If a
style uses `seam` placement, `duo` resolves that to y=850 and `split` to y=1380.

The webcam box is a **face crop**: a little room under the chin, and the box
shrinks rather than slides when the camera is against a frame edge. A band
stretched more than about 2x gets a light sharpen so a small webcam does not look
mushy next to crisp gameplay. If no webcam is found, `split` and `duo` fall back
to the blurred frame and the worker log says so.

## Captions

Subtitles can be burned while capturing, but the default is **off**: post-session
styling is where captions normally happen, and the live path stays cheap.

```bash
--subs off             # default: capture only
--subs auto            # on if a model is installed
--subs on              # force captions on the live clip
--subs-style pop       # popping word-chunks (default)
--subs-style block     # multi-line block
--subs-model models/ggml-medium.en.bin
--subs-prompt "ohnepixel, GTA 6, streamer name"   # helps with names
```

They are timed from whisper's own word offsets, so each chunk appears with the
word rather than a guess. A pause longer than 0.32 s starts a new chunk, so
captions disappear while nobody is talking.

### Only the streamer's voice

A stream is one mixed audio track: microphone, game, music. Whisper happily
captions the game's dialogue, which is how a clip ends up quoting a cutscene.

There is no second audio track to separate, so clipper uses the camera. The
detected webcam is sampled six times a second and the lip opening is measured;
words spoken while the mouth is still are dropped. The threshold follows the clip
itself, so different cameras work without tuning. If the face is missing in most
frames, or no webcam exists, the transcript is left alone — wrong-speaker captions
beat deleting the streamer's real words.

The result is logged per clip, for example `caption gate: kept 12/31 word(s)`.

### Accuracy

- The biggest installed whisper model is used automatically (`medium.en` beats
  `small.en` beats `base.en`).
- Audio is high-pass filtered and loudness-normalised before whisper sees it.
- A Silero VAD pass verifies that speech really happened and where it starts, so
  silence and music get no fabricated words.
- whisper.cpp's 30-second padding is ignored: only tokens inside the real audio
  are trusted.
- `--subs-threads 4` (the default) leaves cores free for the stream.

## Hook captions

The default is `--title-mode none`; hooks are added by `clipper style` or by
turning this on. With `--title-mode auto` the hook comes from the clip, never
from a template:

1. a phrase **chat keeps repeating**, from at least two different people;
2. a **genuine question** the streamer asked;
3. a quotable line;
4. a one-word chat consensus;
5. nothing — an empty hook beats one that means nothing.

The hook uses a heavy title face in gold with a black outline; subtitles stay
white. They are placed so they never overlap.

One emoji can join the hook, but only one the audience actually used in this
clip's chat (two different people, from a short reaction set). A fixed
`--title` keeps whatever emoji you type. On macOS those are the real iPhone
emoji, read straight from the system font's colour bitmaps; elsewhere they
render as a tinted silhouette, and a hook never invents an emoji on its own.

---

## Command line reference

```bash
cd ~/clipper
python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc
```

Other useful flags:

```bash
--out clips              # where clips are written
--work .clipper          # buffer + profiles
--pre 12 --post 8        # seconds kept around the trigger
--threshold 0.62         # how sure before a clip fires
--profile sensitive      # more clips; conservative = fewer
--edit coldopen          # payoff first, then rewind and replay
--voice on               # short factual AI intro over ducked audio
--voice-max 8            # longest narration, seconds
--no-dashboard           # no console
--port 8756              # console port
--run-for 3600           # stop after an hour
```

`clips` (list what was captured), `models` (what speech models are installed),
`frame` (grab one frame, to line up `--cam`) and `doctor` (environment check) are
also available. `vod` clips a finished stream and `update` pulls the newest
version:

```bash
cd ~/clipper && python3 -m clipper vod https://www.twitch.tv/videos/123456789
cd ~/clipper && python3 -m clipper style clips/name.mp4 --style hype
cd ~/clipper && python3 -m clipper styles --scan ~/Movies/references
cd ~/clipper && python3 -m clipper update
```

## Profiles

The console's settings and the flags are the same knobs. Changes made on the page
are saved per streamer under `.clipper/profiles/<channel>.json` and reloaded next
time. A flag passed on the command line always wins over the saved file.

## Multiple streamers

```bash
cd ~/clipper && python3 -m clipper multi --channels xqc,kaicenat,caseoh
```

Each channel gets its own ingest, buffer, render queue and clip folder, plus its
own console. Renders are serialised across workers so two streams cannot trip the
memory limit. Two channels is comfortable on a laptop; three is about the ceiling.

## Demo without a live stream

```bash
cd ~/clipper && python3 scripts/make_demo.py && python3 -m clipper run -i demo/demo.mp4 --loop -c file:demo/chat.jsonl --layout blur
```

## Tests

```bash
cd ~/clipper && python3 -m unittest discover -s tests
```

## Troubleshooting

- **`ffmpeg` not found** — install it and open a *new* terminal so PATH refreshes.
- **`whisper-cli` not found** — put the whisper.cpp folder on PATH, or pass
  `--subs-model` and make sure the binary is named `whisper-cli.exe`.
- **`No such filter: 'drawtext'`** — handled automatically; captions go through
  ImageMagick or a built-in bitmap font.
- **Captions are missing** — the mouth gate may have decided the speech was not
  the streamer's. The log says `caption gate: ...`; it is deliberate.
- **`cam` is `none` in the clip metadata** — face detection found nothing. Pin the
  box with `--cam`, or use `--layout blur`.
- **Clips lag behind** — rendering is a background queue. Lower `--max-post`, use
  `--encoder h264_videotoolbox` on macOS, or `--layout horizontal`.
- **Everything stutters on an 8 GB laptop** — do not watch the same stream in a
  browser while clipping it, keep `--subs-threads 4`, and use a smaller model.
- **HLS copy fails or the source codec is odd** — add `--transcode`.
- **Twitch URL stops working mid-stream** — expected; playback URLs expire and
  clipper refreshes them automatically.

## Legal

Clipping other people's streams is normal Twitch/Kick/YouTube culture, and fine
when you have permission or a licence — but republishing or monetising somebody
else's broadcast can breach platform terms and copyright. Check the streamer's
rules and your platform's policies before building a business on it. Adding
structure and commentary reduces the risk of a reused-content claim; it does not
guarantee anything.

## License

MIT — see [LICENSE](LICENSE).