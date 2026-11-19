# Reference reels and the styles they inspired

The presets in `clipper/styles.py` were not invented from a mood board. Each one
was built after measuring a real short-form clip and watching how it is cut. The
goal is to copy **structure** — pacing, caption placement, when a word is
emphasised — not somebody else's footage, font file or meme.

The reference files live in `demo/references/` locally. They are downloads for
analysis, not assets that ship with clipper, and that folder is gitignored.

## How a reference was read

Two passes per clip:

1. **`clipper styles --scan <folder>`** counts hard scene cuts with ffmpeg's
   `select='gt(scene,…)'`, then derives cuts per minute and average shot length.
   That is the measurable part, and it is reproducible on any machine.
2. **Frames and transcript.** One frame every few seconds (and the spoken words,
   when the clip has them) show where the camera, gameplay and captions sit, and
   whether the words are a talking-head voiceover or the streamer's own audio.

The scanner's category is a description of the reference, not a command. A clip
can measure as `hype` and still be better served by a caption treatment that the
scanner cannot see (the YouTube Short below is exactly that case).

Measured on 19 November 2026 with the local copies:

| Reference | Handle | Length | Cuts | Cuts/min | Avg shot | Scanner category |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `Dcod99JqRXy` | fxy | 25.1 s | 6 | 14.4 | 3.6 s | montage |
| `Df4pyxVA17V` | ViceDetail.HQ | 97.6 s | 0 | 0.0 | 97.6 s | podcast |
| `Df40VuaTRem` | wixxey | 15.0 s | 1 | 4.0 | 7.5 s | clean |
| `Df4Z56nJmDu` | lacy.himself | 14.6 s | 1 | 4.1 | 7.3 s | clean |
| `Df5MaMLMqfz` | jirolfs | 9.7 s | 1 | 6.2 | 4.9 s | clean |
| `Df6haSQzqSh` | \_shadowzin\_\_ | 19.0 s | 0 | 0.0 | 19.0 s | clean |
| `DDBh-a5HNAk` | Streamy Wave (YouTube Short) | 36.4 s | 15 | 24.8 | 2.3 s | hype |

## The six Instagram references

### fxy — `Dcod99JqRXy`

A 25-second GTA 6 reaction. Gameplay sits on top, the streamer's camera takes a
large band underneath, and the captions run across the join. There are six hard
cuts in 25 seconds, so the picture changes every 3–4 seconds even though the
audio is one continuous reaction.

What it does:

- a short gold **opening hook** names the moment before any dialogue;
- white condensed uppercase chunks appear at the seam, not at the bottom;
- exactly one word is coloured (green in the frame sampled) as the beat;
- the camera is huge and close, so the reaction carries the clip.

Borrowed: seam captions, an opening hook, one emphasised word, punchy pacing.
Not borrowed: the exact font, the green, the streamer's footage.

**Preset:** `clean` with punch-ins, plus the general `pop` caption path. The
`duo` layout keeps the seam caption but puts the camera on top, which is how the
other references in this set frame it.

### ViceDetail.HQ — `Df4pyxVA17V`

A 97-second GTA 6 airport tour with **no cuts at all**. The gameplay is
letterboxed into the vertical frame with a small picture-in-picture camera at the
top-left, and a persistent white title stays on screen. There are no spoken
captions: the clip is presented as a calm reveal, and the voice is the game's
own ambience rather than a narrator explaining the joke.

Borrowed: persistence. A title that stays put, and the confidence to leave the
footage alone instead of stacking captions on a slow pan.

**Preset:** `reveal` — white top title, no spoken captions, no punch-ins. It uses
`hook_fallback` (a short descriptive line from the transcript) so a calm clip
still gets a title instead of rendering bare.

### wixxey — `Df40VuaTRem`

A 15-second IShowSpeed reaction. Camera on top, gameplay underneath, one static
two-line caption at the seam for most of the clip. Almost no cutting: the joke is
the face and the line, so the edit does not compete with them.

Borrowed: whole-sentence captions that hold long enough to read, placed on the
seam, with no hook and no punch-in.

**Preset:** `story`.

### lacy.himself — `Df4Z56nJmDu`

A 14.6-second GTA VI map reaction using the same template as wixxey: camera and
gameplay stacked, a static caption at the join. The difference is the line
length and the slightly larger type, which keeps a longer sentence readable.

Borrowed: the same static seam treatment, with a caption size that survives two
lines.

**Preset:** `story`.

### jirolfs — `Df5MaMLMqfz`

A 9.7-second square clip. No vertical split: the footage fills the square and a
static three-line caption sits in the lower third. The frame is a single visual
beats, so the caption is the edit.

Borrowed: a lower-third static block, sized for three lines, with no hook.

**Preset:** `meme`.

### \_shadowzin\_\_ — `Df6haSQzqSh`

A 19-second clip with camera on top and gameplay below. It combines two caption
layers: a static sentence at the seam (the story) and one very large word at a
time over the camera (the beat). There are no hard cuts; the motion comes from
the words changing.

Borrowed: the two-layer idea. `story-karaoke` keeps the readable sentence at the
seam and puts the big word over the face, where the reference puts it.

**Preset:** `story-karaoke`.

## The YouTube Short — `DDBh-a5HNAk`

**"Streamers Who Showed Up In GTA 6 😳🔥"** — 36.4 seconds, 1080×1920, 30 fps,
15 hard cuts (24.8/min, average shot 2.3 s). The scanner calls the pacing `hype`.
Six and a half million views at the time of analysis, so the structure is worth
reading closely.

### What the edit does

- **One word at a time, dead centre or slightly low.** Uppercase, heavy
  condensed sans (Impact-like), white fill, thick black outline. The word is the
  unit of rhythm, not the sentence.
- **Colour is a beat marker, not decoration.** Most words are white. A few are
  coloured for one beat: red for `STREAMERS`, purple for `GTA 6`, orange for
  `10,000`. The colour marks the noun the viewer should remember.
- **No permanent caption box.** The word appears on the cut, punches in slightly,
  and is replaced by the next word. There is no two-line paragraph to read.
- **The picture cuts on the word.** Trailer footage and streamer reaction shots
  swap every 2–3 seconds, so the caption and the visual hit together.
- **One square/PIP variant** breaks the full-frame rhythm and keeps the feed from
  looking like one long gameplay capture.

### What the voiceover does

The audio is a **narrated list**, not a reaction compilation with subtitles over
it. The creator speaks in third-person present tense, then drops to the
streamer's original audio for the punchline:

> "These streamers showed up in GTA 6 without you even noticing. First one is
> IShowSpeed, who accidentally found his family's business in the second
> trailer."
>
> — cut to IShowSpeed: "Oh my god, I'm in a game."

Then the pattern repeats: "Then there's Queso, who discovered his in-game
character, but he wasn't very happy." — cut to Queso's reaction. The last beat is
Queso again, tagged in a GTA 6 picture "like 10,000 times".

The narrator does four jobs in 36 seconds:

1. **Sets the premise** in one sentence and promises a list.
2. **Names each streamer** so a viewer who does not know them can follow.
3. **Explains the specific coincidence** in plain language.
4. **Gets out of the way** and lets the original reaction land.

It is calm and factual, not shouted. The energy comes from the cuts and the
streamer audio, not from the narrator overselling. No voice is cloned and no line
is put in a streamer's mouth.

### What clipper borrows

**Preset:** `karaoke` — one uppercase word at a time, centred, `colour_emphasis`
on the stressed word only. This is the one built-in style that deliberately
colours a caption word, because the reference uses colour as part of its rhythm.

What clipper does **not** copy:

- the exact font file or the reference's title cards;
- the trailer footage, the streamer clips or the watermark;
- a synthetic impersonation of the narrator or the streamers.

`--voice on` remains the safe version of the narration: a short, factual intro
built from the clip's own transcript and chat, spoken by a generic TTS voice,
with the stream audio ducked underneath. It is off by default because the live
path is capture-only and most clips do not need it.

## Cross-cutting rules

These held across every reference, and they are why the styles are built the way
they are:

- **Vertical framing is not optional.** Every reference is 1080×1920 (or square
  for one). The gameplay is cropped to fill, not letterboxed with black bars.
- **Captions live at a seam or a corner, never in the middle of the action.**
  The seam between camera and gameplay is the natural home for a sentence.
- **White is the default.** Colour is rationed: one word, one beat, one reason.
  The built-in styles keep subtitles white except `karaoke`.
- **A cut or a punch-in is a punctuation mark.** It lands on a stressed word or
  a new idea, not on a timer.
- **The hook is about the clip.** A gold title can open the clip (fxy), a white
  title can sit on top (ViceDetail), or there can be no hook at all (wixxey,
  lacy, jirolfs). None of them use a template that could apply to any stream.

## Reproducing the measurements

```bash
cd ~/clipper
python3 -m clipper styles --scan demo/references
python3 -m clipper styles
```

The scan writes one `ref-*.json` style per measurable reference and a
`.clipper/styles/report.json` with the cut counts. The hand-built presets above
are separate from those scanned styles: a scan copies pacing, while `story`,
`karaoke` and the rest also encode caption placement and emphasis, which a cut
detector cannot see.

To try a look on a real capture:

```bash
cd ~/clipper
python3 -m clipper style --last 3 --style story
python3 -m clipper style --last 3 --style karaoke
python3 -m clipper run -i https://twitch.tv/xqc -c twitch:xqc --layout duo
```

`--layout duo` is the camera-top / gameplay-below frame used by wixxey, lacy and
shadowzin. The older `split` layout keeps a dedicated caption band; `duo` gives
the gameplay the extra height and lets the caption sit over the join.