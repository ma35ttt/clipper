# Content and posting strategy

Written from the channel's own numbers: 7 Shorts, all uploaded on 18 Nov, 320
views in 48 hours, 0 subscribers.

## What the numbers actually say

| # | title | length | views |
| --- | --- | --- | --- |
| 1 | jasontheween asks "am i blind?" — chat: LOOK AT THE MAP | 0:27 | 0 |
| 2 | ohnepixel explains GTA 6 fashion using a BURGER 🍔💀 | 0:27 | 3 |
| 3 | xQc finds gold doubloons in GTA 6... then it gets HAUNTED 👻 | 0:27 | 3 |
| 4 | xQc reads the SUSSIEST photo in GTA 6 💀👀 | 0:39 | 2 |
| 5 | crazy gameplay by xQc | 0:33 | 3 |
| 6 | Can't believe GTA VI is live now!! | 0:40 | 1 |
| 7 | **xQc playing GTA VII!!** | 0:40 | **299** |

Three things are true at once:

1. **Seven uploads in one day is a dump, not a schedule.** Shorts reward a steady
   feed; a burst competes with itself and gives you no clean data.
2. **Six of seven did nothing.** That is normal for a channel with no audience.
   It is not a verdict on the clips.
3. **The only one that moved is the one that says something impossible.** "GTA VII"
   does not exist and GTA VI is not even out. That contradiction is the click, not
   the streamer's name — nobody on YouTube knows who these streamers are yet.

And the uncomfortable part: **that same video carries a "Potential limitation"
notice.** The format that earned reach is the format the platform is suspicious
of. That is the problem to solve, and it is why the restructure and the voiceover
exist.

Do not over-fit to one video. Treat #7 as a hypothesis — "a title that states
something impossible gets clicks" — and test it three more times before believing
it.

## 1. Lock two formats for 14 days

Random formats produce random data. Pick two and alternate them so the comparison
is fair:

- **A — cold open.** Payoff first, dip, rewind, payoff again (`--edit coldopen`).
- **B — narrated.** A short factual intro over the top (`--voice on`), straight
  order.

Run both for two weeks. At the end, keep the one with the better median 24-hour
views **and** better average view duration. Median, not average — one outlier will
lie to you.

## 2. Titles

The winning shape was: **a game people care about + a claim that cannot be true.**

- Good: `xQc finds a GTA 6 glitch that should not exist`
- Good: `This GTA 6 clip is from a game that isn't out yet`
- Good: `Chat told him to look at the map. He should have listened.`
- Bad: `crazy gameplay by xQc` — no claim, no curiosity, no keyword.
- Bad: `xQc reads the SUSSIEST photo in GTA 6` — a description, not a promise.

Rules:

- Under 60 characters so it is not truncated.
- Front-load the game name, because that is what the algorithm and the viewer
  both match on.
- One claim, and it must be true of the clip. A lie gets a click and then a swipe,
  which is worse than no click.
- Skip the streamer's name unless the channel is already known for him.

`python3 -m clipper pack` already writes the hook as the title and two alternates
per clip, so you only choose between three, not from scratch.

## 3. The first 1.5 seconds

Retention is decided before the payoff. Every clip must open on the most
interesting frame and the loudest syllable, not on the streamer alt-tabbing.

- `--edit coldopen` does this automatically.
- Keep the burned captions on. Most Shorts are watched muted.
- 25–40 seconds. The one that worked was 0:40; the 0:27 batch was not better.
- Cut the dead air. `--pre 8 --post 6` is punchier than `--pre 14 --post 10` for
  Shorts.

## 4. Schedule

Two posts a day, roughly 7–8 hours apart, at the same two times every day for two
weeks. For a Twitch audience that is US evening: about **17:00 and 22:00 US
Eastern**. Consistency matters more than the exact minute.

- Do not post seven in an hour.
- Do not delete the low performers; they cost nothing and deleting resets nothing.
- Cross-post the same file to TikTok and Reels with a different caption. Reels and
  TikTok often find an audience that Shorts ignores, and it is the same file.

## 5. Descriptions and hashtags

- Line 1: the same claim as the title, in a sentence.
- Line 2: credit — `from <streamer>'s stream`.
- Hashtags: `#shorts #twitch #clips` plus the game. Three to five, not fifteen.
- Do not put links in the description until the channel has an audience; they
  suppress reach on new accounts.

`clipper pack` writes all of this into one CSV.

## 6. The reused-content problem

The "Potential limitation" notice is not random, and an AI intro alone does not
clear it. YouTube's inauthentic-content policy is aimed at mass-produced,
near-identical uploads. A fixed template voiceover on every clip is *exactly* that
pattern, just with better audio.

What actually helps, in order of strength:

1. **Substantive transformation.** Restructuring the clip, cutting context,
   choosing what to keep. This is why `coldopen` matters more than the voiceover.
2. **Specific commentary.** A line that could only be about *this* clip. The
   voiceover is built from the clip's own words and chat for that reason; do not
   replace it with the same generic sentence every time.
3. **A visible editorial layer.** Your own framing, your own series, your own
   selection.
4. **A human voice.** Even 10 seconds of your own commentary over the top beats a
   synthetic one, legally and algorithmically. The AI voice is the automated
   fallback, not the ceiling.

What does not help: a longer intro, a logo sting, a watermark, or a title card.

Also: only clip streamers whose rules allow it, and expect a claim from a large
streamer's rights holder eventually. Keep the clips short, transformative and
credited.

## 7. Measure it

`clipper pack` writes `posting-queue.csv` next to the clips. Add three columns
after publishing:

- `views_24h`
- `avg_view_duration` (YouTube Studio → Content → the clip)
- `likes`

Log them at the same time each day. After 28 clips you will know:

- which format holds attention,
- which title shape gets the click,
- whether the cold open helps retention or hurts it.

Kill the losing format. Double the winning one. Do not add a third format until
the first two are decided.

## 8. What can be automated

| step | automated today | notes |
| --- | --- | --- |
| detect and cut the moment | yes | already running |
| restructure (cold open) | yes | `--edit coldopen` |
| captions | yes | streamer-only voice gate |
| voiceover | yes | `--voice on` |
| title, description, hashtags | yes | `clipper pack` |
| upload | **no, by design** | see below |
| schedule | partly | upload, then use the platform's own scheduler |
| analytics | partly | YouTube Analytics API with OAuth |

**Why upload is not automated.** Every platform allows it, but each needs its own
app registration and OAuth approval (YouTube Data API quota is 1,600 units per
upload, about six a day; TikTok's Content Posting API needs app review; Instagram
needs a business account and a hosted video URL). Browser automation of the upload
form is fragile, against the terms, and puts the account at risk for a task that
takes five minutes.

**The practical middle ground, which is what `pack` is:** the machine does
everything up to the last mile, you upload two clips in a batch and paste the
title. If you later want real auto-publishing, the honest build is a small OAuth
integration for one platform at a time, starting with YouTube.

## 9. The next 14 days

1. Run the overnight session. Keep `--edit coldopen` on; turn `--voice on` on for
   half the clips.
2. Each morning: `python3 -m clipper pack --out <clips folder>`, pick two clips
   with the strongest hook, publish at the two fixed times.
3. Three times this fortnight, use a "this cannot be true" title and see whether
   it repeats #7.
4. Log views at 24 hours.
5. On day 14, keep the better format. Then, and only then, add a third idea.

Volume is not the strategy. Two transformed clips a day, measured, beats twenty
reuploads.