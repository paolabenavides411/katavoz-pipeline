# Meridian Freight Dictation Tool — Progress Notes

Saved from a Claude Code session on 2026-09-17. To resume work tomorrow,
point the new session at this folder (it started in a temporary scratch
workspace that no longer exists, so this is the only copy).

## What's here

- `tts_project/` — the audio generation pipeline
  - `tts_generate.py` — the script (Google Cloud TTS, Chirp3 HD voices)
  - `transcript.json` — the 70-line Meridian Freight deposition transcript
  - `.env.local` — your Google Cloud TTS API key (untracked, don't commit)
  - `output/trimmed/{60,80,100,120,140,160,180,200,225}wpm/` — final audio,
    70 files each, 630 total. Diagnostic/test files from calibration were
    NOT copied (they're throwaway).
- `dictation_player/` — the web app
  - `index.html` — the player (single file, dark theme, brass/gold accents)
  - `images/` — courtroom photos (achird.png = attorney glow, achernar.png
    = witness glow, neutral.png = idle)
  - `audio/` — same 630 files as above, copied alongside the player
  - `durations.json` — exact per-file durations, embedded in index.html too,
    used for the global timeline/seek bar

## Current state of the audio

- Voices: `en-US-Chirp3-HD-Achird` (attorney), `en-US-Chirp3-HD-Achernar` (witness)
- Below speakingRate 0.75, words sound stretched/unnatural (confirmed by ear).
  60/80/100/120wpm hold the voice at that floor and add silence between
  2-word-average chunks (sized to the padding actually needed) to hit the
  target pace. 140-225wpm use direct speakingRate with a per-line secant
  correction (the multiplier table alone was off by up to 40% on short lines).
- Final accuracy: median ~+1.3% off target wpm, only ~1.7% of files flagged
  as outliers. Speaker balance confirmed even (no systematic attorney/witness
  bias). Per-word pausing (pause after every single word) was tried and
  rejected — it sounds glitchy/dragged on longer lines because synthesizing
  each word in isolation loses natural co-articulation.
- To regenerate: `cd tts_project && source .env.local && python3 tts_generate.py batch --wpm <speed>`

## Player features built so far

- 9 speed buttons, real distinct recordings per speed (no playback-rate tricks)
- Global timeline scrubber (shows position across all 70 lines, not per-line;
  click anywhere to jump to that point, including mid-line)
- Transcript toggle with click-to-jump on any line; highlight now only
  scrolls the panel every 4 lines (not every line) so the current line stays
  readable instead of constantly re-centering
- Notes section at the very bottom: auto-extracted proper nouns (names,
  companies, places) for spelling reference — NOT overlaid on the courtroom
  image (that was tried and explicitly rejected)
- Compare-your-transcript panel: paste text or upload a PDF (client-side
  extraction via pdf.js), diffs word-and-punctuation level against the
  correct transcript (red strikethrough = wrong/extra, bold gold = missing)
- "Expand View": white-background/black-text overlay, correct transcript
  left / your transcript right, each half the screen. Has a "Sync Scroll"
  toggle (proportional scroll sync between the two columns)
- "Grade" button: NCRA-style scoring per the rules you provided (verbatim
  errors omission/addition/substitution/Q&A = 1.0 each; formatting errors
  punctuation/capitalization/hyphenation = 0.5 each; 95% pass threshold).
  Shows a score card + breakdown table + a "show raw JSON" toggle matching
  your exact schema. Verified against a hand-built test case with all
  error types.

## Known open items / things mentioned but not yet built

- A separate "introductory portion introducing the speakers with highlight"
  (you mentioned this is coming as its own feature, not part of the current
  image/notes work)

## URGENT — reported at end of session 2026-09-17, NOT yet fixed

Reported right as we were signing off, only checked at 60wpm (other speeds
not checked yet). Start tomorrow's session here, in this order:

1. **"The script is read wrong at 60wpm."** Ambiguous as reported — need to
   clarify with the user first thing: does this mean (a) the spoken audio
   doesn't match the transcript text/word content, (b) a line plays out of
   order or under the wrong speaker, or (c) something else entirely? Don't
   guess — ask, then verify against the actual line-to-file mapping in
   `output/trimmed/60wpm/`.

2. **Pacing/spacing sounds wrong again — "pauses every 2 words still."**
   IMPORTANT CONTEXT: earlier in this same session the user explicitly said
   the 2-word-adaptive-chunk samples "sound better already" compared to
   per-word chunking, after per-word caused dragged/glitchy audio on longer
   lines (confirmed by their own ear). That adaptive approach is what
   actually shipped for 60/80/100/120wpm. Now they're saying it still
   sounds like every-2-words and implying we'd agreed on something else.
   This needs to be reconciled with the user directly before changing
   anything — don't just flip back to per-word without re-confirming, since
   that was already tried and rejected for sounding glitchy on longer lines.
   Re-listen together, re-clarify what "agreed" outcome they mean, and only
   then decide whether to adjust chunk size, or explain/re-demonstrate why
   2-word chunking was the better-sounding tradeoff.

3. **Compare-your-transcript readability — NOT fixed yet, easy fix.** The
   inline `#compareResult` panel (shown right after clicking "Compare",
   without needing "Expand View") is still dark-themed. Only the "Expand
   View" overlay got the white-background/black-text treatment. User wants
   white bg / black text for the comparison-to-master view for readability
   in general, not just inside Expand View. Fix: restyle `#compareResult`
   (and probably `#gradeResult` for consistency) to light theme, or clarify
   whether they want the *entire* compare panel light-themed by default.

4. **"The scrolling feature still doesn't work."** Also vague — could refer
   to: the transcript-panel 4-line-batch scroll fix (item 4 from the prior
   round), the sync-scroll toggle in Expand View (item 5), or something new.
   Both of those WERE tested and confirmed working via automated traces
   before signing off (see git/session history), so either the fix doesn't
   hold up under real manual use, or this is a different scrolling issue
   than what was tested. Ask which specific scrolling behavior is broken and
   reproduce it manually (not just via automated scroll-position traces)
   before assuming either previous fix is intact.

**Do not re-run large audio regeneration batches tomorrow until #1 and #2
are clarified with the user** — regenerating audio is expensive/slow, and
both open questions could change what actually needs regenerating.

## Day 2 (2026-09-17, later session) — what changed and what didn't

This entire second session was spent trying to fix a "words sound cut
off / choppy" complaint at 60-120wpm. It is UNRESOLVED. Read this before
touching audio again, so the same dead ends aren't repeated.

### The player itself was NOT changed, except one small thing

Every feature listed above under "Player features built so far" (compare
panel, PDF upload, grading, Expand View, sync scroll, notes section, etc.)
is exactly as it was at the end of Day 1. None of it was touched today.
Concretely, in the compare panel: a word you got wrong or added that
wasn't spoken shows with a **red strikethrough**; a word you missed
entirely shows in **bold gold** (e.g. if the master line is "state your
full name" and your transcript says "state name", the missing "your full"
would render bold gold). That logic lives in `runComparison()` in
`dictation_player/index.html` (~line 1205 on) and was not modified.

The one real code change in `index.html`: `audioPath()` now appends a
`?v2` query string to every audio file URL (`AUDIO_CACHE_BUST` constant,
near the top of the script block). Browsers — especially loading a page
over `file://` — will keep serving an old cached copy of an mp3 after
you've overwritten it on disk with the same filename, so a fix could look
like it "didn't work" simply because the player was still playing the
stale cached file. Bump `AUDIO_CACHE_BUST` (e.g. to `v3`) any time audio
files are regenerated with the same filenames, so the browser is forced
to re-fetch them.

### The audio files actually in the player were NOT changed

`dictation_player/audio/` (all 9 speeds, 630 files) is still exactly the
batch from the end of Day 1 (every file timestamped from that original
run). Nothing from today's experiments was ever copied in. If you want to
double check, file timestamps under `dictation_player/audio/*/` will all
match; anything newer would mean something got copied in after this note
was written.

### What was tried today for the cutting-off/pacing complaint, and why none of it shipped

1. **Isolated single-word synthesis with a fixed natural rate + one even
   gap between every word** (`synthesize_paced_per_word` in
   `tts_generate.py`, still in the file, used for 60/80/100/120wpm).
   Went through several rounds: short/common words (≤4 letters) got a
   small deliberate slowdown (`SHORT_WORD_RATE`) since they read as
   "rushed" next to longer words; the minimum gap floor was raised from
   0.05s to 0.15s because shorter gaps read as "staccato." 120wpm was
   confirmed sounding good under these settings at one point; 100wpm was
   never fully resolved ("better but not fixed"); a full-batch regen of
   60/80wpm was run and is now considered STALE (predates later fixes).
   A later attempt at stripping trailing punctuation before synthesizing
   a word alone (to test a theory about the witness voice sounding more
   cut off than the attorney voice) was explicitly rejected by ear and
   was reverted — that code path is back to how it was when 120wpm was
   last confirmed good.

2. **A completely different method**: synthesize the whole line
   continuously and naturally in one take, use local word-level alignment
   (`faster-whisper`, a new pip dependency) to find real word boundaries,
   slice per-word or per-2-word-chunk clips out of that ONE natural
   recording, and splice in controlled even gaps
   (`synthesize_paced_aligned` in `tts_generate.py`). This fixed timing
   accuracy dramatically (within ~1% of target vs. up to 50% off before)
   and, along the way, surfaced and fixed two real, confirmed bugs:
   - Word-boundary padding was bounded by the neighboring word's own
     timestamp, which left most words with zero extra padding (since most
     word-to-word boundaries in fluent speech have no natural gap) —
     right where the cutting-off was happening.
   - The whole intermediate pipeline was round-tripping through lossy MP3
     encoding at every step (each word/chunk extracted as its own MP3,
     concatenated, etc.) instead of staying in uncompressed WAV until one
     final export — a known source of "cutting in and out" glitching on
     short clips, confirmed by ear.
   - A 2-word chunk could accidentally swallow a real natural pause (e.g.
     the comma after "crew,") into its own middle, creating an
     uncontrolled silence buried inside what was supposed to be one
     continuous sound — confirmed objectively with `ffmpeg silencedetect`
     (a line's gaps should all measure the same; they didn't, until the
     chunking was fixed to always break at a natural pause instead of a
     fixed word count).

   Despite fixing all three of those, the user reported **the samples
   still sounded cut off**, and — critically — reported the same
   complaint on the untouched, original Day 1 audio pulled directly from
   `dictation_player/audio/`. That means whatever is causing "cutting
   off" was not fully explained or fixed by anything above, and may not
   even be specific to today's new code.

### Where things stand

- No audio was shipped or changed in the player today. It's safe.
- The "cutting off" complaint was never conclusively resolved, and the
  last data point (it's also reported on the original, untouched Day 1
  audio) undermines the working theories from earlier in the session.
  Don't assume any of the fixes above are "the answer" — treat this as
  still an open, not-understood problem.
- Next session: before writing more code, get very concrete with the
  user about ONE specific line/word/speed where "cutting off" happens,
  and listen to (or waveform-inspect) that exact file together, rather
  than generating new batches of samples on a hypothesis and asking them
  to re-report. The back-and-forth of "here's a fix, does this sound
  better?" repeated many times today without converging, and became
  frustrating for the user.
- `faster-whisper` was pip-installed into whatever Python environment
  `tts_generate.py` runs in, as a new dependency for the alignment-based
  approach. It's not required for the 140-225wpm path or for anything
  currently shipped in the player.
