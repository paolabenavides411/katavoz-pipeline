#!/usr/bin/env python3
"""
Assemble the 9 speed-calibrated Q&A practice tracks (attorney/witness,
Meridian Freight deposition transcript) into one continuous MP3 per target
WPM: practice_60wpm.mp3 ... practice_225wpm.mp3.

Line-level synthesis (fixed_rate_for / synthesize_paused /
synthesize_at_fixed_rate below) is entirely owned by this script now, for
every speed -- tts_generate.py's own per-line-tuned methods
(synthesize_paced, synthesize_paced_per_word, synthesize_rate_calibrated)
are not called here. Those all search for a DIFFERENT speakingRate on every
individual line to hit that line's own exact duration, which is what made
speakers sound inconsistent -- rushed on one line, dragging on the next, at
every speed, confirmed by ear. This script instead uses exactly ONE
speakingRate per (speaker, wpm) pair -- see fixed_rate_for -- applied
identically to every line: 1.0 (Google's own natural default, paced via
pauses only) at or below the voice's natural pace, or one fixed elevated
rate above it. Only tts_generate.py's low-level utilities (synthesize,
to_wav, concat_wav, generate_silence_wav, wav_to_mp3, get_duration_seconds)
are reused; tts_generate.py itself is not edited. NOT SSML: SSML <break>
pausing and <mark> timepoints were both tried on Chirp3 HD and abandoned
early (see tts_generate.py's own comments).

Active WPM (word count / active speech duration, i.e. excluding the
inserted Q&A gaps below) is reported per assembled track so the accuracy
tradeoff of fixed-rate synthesis is visible, not hidden.

Usage:
  cd tts_project && source .env.local && cd ..   # only needed if a line is missing
  python3 src/audio/generator.py --wpm all
  python3 src/audio/generator.py --wpm 60,120,225 --gap-ms 175
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent.parent
TTS_PROJECT_DIR = PROJECT_ROOT / "tts_project"
sys.path.insert(0, str(TTS_PROJECT_DIR))

import tts_generate as ttsgen  # noqa: E402 -- proven synthesis/trim/concat pipeline, reused not duplicated

# 250/275/300 added later, above ttsgen.SPEAKING_RATE's original 9-speed
# calibration table (60-225) -- kept as a separate literal here rather than
# editing that table, since tts_generate.py itself is never edited (its own
# per-line-tuned methods aren't used by this file at all; see fixed_rate_for
# for how these three are actually paced).
TARGET_WPMS = sorted(list(ttsgen.SPEAKING_RATE["attorney"]) + [250, 275, 300])
OUTPUT_DIR = SRC_DIR / "output"
TMP_DIR = SRC_DIR / "_tmp_assemble"

# Normalized, consistent inter-line Q&A silence -- covers the terminal-
# punctuation pause at every line boundary. Internal punctuation (commas,
# semicolons) is already paced by each line's own Chirp3 HD prosody, so
# nothing extra is added mid-line.
DEFAULT_GAP_MS = 175
GAP_MS_MIN, GAP_MS_MAX = 150, 200


def normalize_internal_gaps(wav_path: Path, tmp_dir: Path, max_gap: float = 0.9,
                             min_edge_margin: float = 0.12, tag: str = "") -> Path:
    """Cap any single internal silence run in a continuous recording to
    max_gap, by trimming ONLY the deep middle of that silence -- never the
    edges, so real speech starting or ending right at the boundary is never
    touched. Safe because only audio already verified silent (by actual
    signal level via ffmpeg's silencedetect, not a fuzzy timestamp) is ever
    removed, unlike the alignment-slicing this project rejected elsewhere
    for clipping into real speech.

    Fixes Chirp3 HD occasionally producing an oversized natural pause mid-
    recording (confirmed on a whole-line reading: the SAME line's natural
    period-pause measured ~0.5s on one take and 2.27s on another, pure
    synthesis jitter -- and confirmed again on an individual CHUNK: a user-
    reported "glitch" between two words turned out to be a 1.7s silence
    Chirp3 HD inserted mid-chunk, missed entirely because this function
    wasn't being called on chunk audio, only on whole-line baseline audio).
    Applies to ANY continuous recording, not just whole lines -- always pass
    a unique `tag` when calling this more than once into the same tmp_dir
    (e.g. once per chunk in a loop), since output filenames are keyed on it
    and would otherwise collide and overwrite each other."""
    result = subprocess.run(
        [ttsgen.FFMPEG_BIN, "-i", str(wav_path), "-af", "silencedetect=noise=-40dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True)
    starts, ends = [], []
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            starts.append(float(line.split("silence_start:")[1].strip()))
        elif "silence_end:" in line:
            ends.append(float(line.split("silence_end:")[1].strip().split("|")[0].strip()))
    regions = list(zip(starts, ends))
    oversized = [(s, e) for s, e in regions if (e - s) > max_gap]
    if not oversized:
        return wav_path

    total_dur = ttsgen.get_duration_seconds(wav_path)
    segments = []  # ("audio", lo, hi) or ("gap", duration)
    cursor = 0.0
    for s, e in oversized:
        keep_until = s + min_edge_margin
        resume_from = e - min_edge_margin
        if keep_until > cursor:
            segments.append(("audio", cursor, keep_until))
        segments.append(("gap", max_gap))
        cursor = resume_from
    if cursor < total_dur:
        segments.append(("audio", cursor, total_dur))

    pieces = []
    gap_cache = {}
    for i, seg in enumerate(segments):
        if seg[0] == "audio":
            _, lo, hi = seg
            if hi - lo < 0.01:
                continue
            piece = tmp_dir / f"ng_seg_{tag}_{i}.wav"
            ttsgen.extract_segment(wav_path, piece, lo, hi, fade_in=0.01, fade_out=0.01)
            pieces.append(piece)
        else:
            dur = round(seg[1], 3)
            if dur not in gap_cache:
                gp = tmp_dir / f"ng_gap_{tag}_{dur}.wav"
                ttsgen.generate_silence_wav(dur, gp)
                gap_cache[dur] = gp
            pieces.append(gap_cache[dur])

    out_path = tmp_dir / f"ng_normalized_{tag}.wav"
    ttsgen.concat_wav(pieces, out_path)
    return out_path


def synthesize_resilient(text: str, voice_name: str, rate: float, api_key: str) -> bytes:
    """ttsgen.synthesize, retrying on transient network failures (DNS
    hiccups, connection resets, 503s) instead of letting one bad moment
    kill an entire multi-hour batch run -- confirmed this connection drops
    for anywhere from seconds to a few minutes, repeatedly, then recovers
    on its own. 10 attempts with a 30s ceiling gives ~5 minutes of total
    margin before giving up, which has been enough to ride out every outage
    seen so far; 5 attempts at a lower ceiling wasn't."""
    last_err = None
    for attempt in range(10):
        try:
            return ttsgen.synthesize(text, voice_name, rate, api_key)
        except Exception as e:
            last_err = e
            if attempt < 9:
                time.sleep(min(30, 3 * (attempt + 1)))
    raise last_err


def wav_trim_silence(src: Path, dst: Path):
    """WAV-domain equivalent of tts_generate.py's trim_silence, which always
    forces mp3 output regardless of dst's extension -- kept local to this
    script so the whole chunk pipeline below can stay in WAV until the single
    final MP3 export (repeated mp3->mp3 re-encodes at every intermediate step
    is the documented cause of "cutting in and out" artifacts elsewhere in
    this codebase).

    Uses ttsgen.WORD_SILENCE_FILTER (quieter -50dB / longer 0.08s threshold),
    not the more aggressive general-purpose ttsgen.SILENCE_FILTER -- found
    2026-10-04 calibrating Charon/Aoede at 60-120wpm: a word landing at the
    very end of its own independently-synthesized chunk (e.g. "...but he
    was" / "...appeared to") was having its own quiet tail clipped by the
    -45dB/0.05s general filter, audibly cutting the word short. Every call
    site here trims either a whole-line reading or an individual chunk --
    exactly the "isolated word/fragment" case this stricter filter was
    already built for elsewhere (tts_generate.py's synthesize_paced_per_word)
    -- so there's no real-world case in this file where the laxer filter is
    actually needed; the only cost of the stricter one is a touch more
    leftover edge silence, never clipped speech."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ttsgen.FFMPEG_BIN, "-y", "-i", str(src), "-af", ttsgen.WORD_SILENCE_FILTER,
           "-c:a", "pcm_s16le", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg wav trim failed on {src.name}: {result.stderr[-800:]}")


# Each voice's own natural pace at speakingRate=1.0. tts_generate.py's own
# original calibration comment claimed "Achird ~184.89 wpm, Achernar ~180.86
# wpm" (attorney faster), but that was never actually reflected in real
# 200wpm output: measured across 12 production lines, attorney averaged only
# 187.6 actual wpm against a 200 target (undershooting) while witness
# averaged 210.0 (overshooting) -- confirmed audibly by the user as "witness
# sounds faster" on two separate listening rounds. Back-solving from that
# same data (actual_wpm / rate_used = implied native wpm) gives attorney
# ~173.4, witness ~189.9 -- the ORIGINAL claim had the relative speeds of
# the two voices backwards. Only affects fixed_rate_for's elevated branch
# (200/225wpm) in practice: every locked/approved speed at or below 180wpm
# either clamps to NATURAL_RATE_FLOOR regardless of this value, or (140-180)
# uses the flat-1.0 branch that doesn't read it at all.
NATIVE_WPM = {"attorney": 173.4, "witness": 189.9}

NATIVE_ENOUGH_WPM = 140  # empirically-confirmed boundary, not derived from a
# formula: at/above this, hold rate at 1.0 instead of tapering -- see
# fixed_rate_for's docstring.

# Two new voice identities calibrated 2026-10-04: Aoede (female,
# en-US-Chirp3-HD-Aoede) and Charon (male, en-US-Chirp3-HD-Charon), picked
# from Google's live voices:list API for a future single-narrator role
# (Literary/Jury-Charge-category dictations -- a real content gap found in
# NCRA/NVRA standards research; KATAVOZ currently only has two-voice Q&A
# content). NOT YET WIRED into VOICES/NATIVE_WPM/fixed_rate_for above,
# which stay role-keyed ("attorney"/"witness") for the existing swappable
# two-voice Q&A pipeline -- how these two new voices should actually plug
# into the codebase (single-narrator role vs. something else) is still an
# open decision, deliberately not made yet. This dict exists so the
# calibration work itself (all real, by-ear user-confirmed, same rigor as
# Achird/Achernar) survives regardless of which way that decision goes.
#
# Native pace at speakingRate=1.0, measured from real production-style
# lines (same methodology as Achird/Achernar): Aoede ~175.4 wpm, Charon
# ~172.1 wpm.
#
# Confirmed by ear (user, 2026-10-04) per speed -- NOT all derived from the
# same formula as Achird/Achernar, because Charon's own voice character
# doesn't follow it: a literal wpm/native ratio (or this project's usual
# "flat 1.0 at/above NATIVE_ENOUGH_WPM" rule) made Charon sound
# meaningfully faster/less settled than Aoede at the same wpm label despite
# their native paces being close (175.4 vs 172.1) -- re-confirms the
# standing lesson that per-voice by-ear confirmation is required even when
# the math looks similar to an already-approved voice, don't skip it.
# Charon's floor needed to drop from the usual 0.75 to 0.70 (not just at
# 140wpm but uniformly across the whole 60-120 floor-pinned bucket, since
# fixed_rate_for pins ONE floor value across that whole range) before his
# pacing matched Aoede's/Achird's cadence at the same speeds.
VOICE_PROFILES = {
    "aoede": {
        "voice_id": "en-US-Chirp3-HD-Aoede",
        "native_wpm": 175.4,
        "rate_floor": 0.75,  # same floor as Achird/Achernar
        "rate_by_wpm": {60: 0.75, 80: 0.75, 100: 0.75, 120: 0.75,
                        140: 1.000, 160: 1.000, 180: 1.026, 200: 1.140, 225: 1.150},
    },
    "charon": {
        "voice_id": "en-US-Chirp3-HD-Charon",
        "native_wpm": 172.1,
        "rate_floor": 0.70,  # confirmed LOWER than the usual 0.75 -- see note above
        "rate_by_wpm": {60: 0.70, 80: 0.70, 100: 0.70, 120: 0.70,
                        140: 0.906, 160: 1.000, 180: 1.046, 200: 1.150, 225: 1.150},
    },
}
# 160wpm and 200wpm for both voices, and Aoede's 140/160/180/225, were sent
# as part of a larger batch and never individually re-flagged -- treated as
# confirmed by the absence of a complaint, same as how approval worked for
# the rest of this file's locked speeds, but worth a quick explicit re-listen
# before treating them as equally hardened as the ones that got dedicated,
# multi-round by-ear iteration (140/180/225 via explicit "I APPROVE", and all
# of 60/80/100/120 for both voices via multiple rounds of specific feedback).

MIN_GAP = 0.15  # same floor used throughout tts_generate.py -- confirmed by
# ear there as the point below which a gap reads as staccato/jittery rather
# than a real pause.
TRANSITION_GAP = 0.03  # just enough to avoid a hard splice click between two
# independently-recorded chunks; not meant to register as a pause.

_NUMBER_TOKEN_RE = re.compile(r"^(\$?)(\d{1,3}(?:,\d{3})*|\d+)([.,;:!?]*)$")


def _count_under_1000(n: int) -> int:
    """Spoken word count for 0-999, e.g. 234 -> "two hundred thirty-four" -> 4."""
    if n == 0:
        return 0
    words = 0
    hundreds, rem = divmod(n, 100)
    if hundreds:
        words += 2  # digit word + "hundred"
    if rem:
        if rem < 20 or rem % 10 == 0:
            words += 1  # "thirteen" or "twenty" -- one word
        else:
            words += 2  # "twenty" + "three"
    return words


def count_number_words(n: int) -> int:
    """Spoken word count for a whole number, e.g. 18400 -> "eighteen thousand
    four hundred" -> 4. Used to correct target-duration word counts for any
    line containing a digit-form number or dollar amount -- naive
    text.split() counts "$18,400" as one word when it's actually spoken as
    several, which was silently mistargeting duration for any such line."""
    if n == 0:
        return 1  # "zero"
    billions, rem = divmod(n, 10**9)
    millions, rem = divmod(rem, 10**6)
    thousands, ones = divmod(rem, 10**3)
    words = 0
    if billions:
        words += _count_under_1000(billions) + 1
    if millions:
        words += _count_under_1000(millions) + 1
    if thousands:
        words += _count_under_1000(thousands) + 1
    words += _count_under_1000(ones)
    return words


def _count_under_100(n: int) -> int:
    if n == 0:
        return 0
    return 1 if (n < 20 or n % 10 == 0) else 2


def count_year_words(n: int) -> int:
    """Spoken word count for a bare 4-digit number read as two paired
    2-digit groups (calendar years, model years, and the like), e.g. 1951
    -> "nineteen fifty-one" -> 3, 2024 -> "twenty twenty-four" -> 3, 1900 ->
    "nineteen hundred" -> 2. This is the REAL spoken convention for such
    numbers -- confirmed both by NCRA's own grading-guidelines example
    (1951 misread as 1815 is graded as 3 errors, which only reconciles
    under paired reading, not standard place-value's 6) and empirically
    against this project's own already-generated audio (pooled mean
    duration error across 7 affected lines/37 samples: 12.2% under paired
    reading vs 16.5% under count_number_words' standard place-value --
    Chirp3 HD leans the same way real speech does). A last pair of 0 reads
    as "hundred" (1900 -> "nineteen hundred"), under 10 as "oh <digit>"
    (1905 -> "nineteen" + "oh five" -> 3)."""
    first, last = divmod(n, 100)
    if last == 0:
        return _count_under_100(first) + 1
    if last < 10:
        return _count_under_100(first) + 2
    return _count_under_100(first) + _count_under_100(last)


def spoken_word_count(text: str) -> int:
    """Word count for target-duration purposes: same as a plain whitespace
    split, except any digit-form number (optionally comma-grouped, optionally
    a dollar amount) is counted as however many words it's actually spoken
    as -- "1" is one word, "200" is two, "23" is two, "234" is four, "$1" is
    two ("one dollar"). A BARE 4-digit number in a plausible year range
    (1000-2999; no comma grouping, no dollar sign -- a year, model year, or
    a lower street-address number, never a quantity/money figure) uses
    count_year_words' paired reading instead of standard place-value; see
    its docstring for why, and for why the range is capped there rather than
    covering every possible 4-digit number -- confirmed against real
    generated audio that a HIGHER 4-digit address number (4417) does NOT
    follow paired reading the way 2019/2020/2021/2024/2210 measurably do.
    This does NOT change what text gets synthesized (callers still send the
    real text to TTS) -- it only fixes the duration target so lines with
    numbers stop getting flagged as pacing outliers against a target that
    was never achievable to begin with."""
    total = 0
    for token in text.split():
        m = _NUMBER_TOKEN_RE.match(token)
        if not m:
            total += 1
            continue
        dollar_sign, digits, _trailing_punct = m.groups()
        n = int(digits.replace(",", ""))
        if not dollar_sign and "," not in digits and len(digits) == 4 and 1000 <= n <= 2999:
            total += count_year_words(n)
        else:
            total += count_number_words(n) + (1 if dollar_sign else 0)
    return total


def fixed_rate_for(speaker: str, wpm: int) -> float:
    """ONE speakingRate for this (speaker, wpm) pair, used for EVERY line at
    that speed -- never searched or re-tuned per line. Per-line rate tuning
    (tts_generate.py's synthesize_rate_calibrated) is what was making
    speakers sound inconsistent -- "attorney rushes here, witness drags
    there" -- because it picks a DIFFERENT rate for every individual line to
    hit that line's own exact duration, so articulation speed itself
    wanders line to line even at a single wpm label. A single fixed value
    per speaker per speed fixes that regardless of what the value is.

    The value itself: proportional to how far wpm sits from this voice's
    own native pace at rate=1.0, clamped to [NATURAL_RATE_FLOOR, ~1.15].
    Pinning every sub-native speed to a flat 1.0 was tried and rejected by
    ear: it forced a continuous whole-line reading to run far shorter than
    target at 60-100wpm, so synthesize_paused's chunking won its own
    accuracy comparison almost every time, bringing back an audible "pause
    every 2 words" cadence -- confirmed directly against the ONE line the
    user held up as the gold standard (140wpm), which sounded right
    specifically because ITS continuous reading came close enough to target
    that chunking wasn't needed at all, its only pause being Chirp3 HD's own
    natural sentence-final slowdown. Letting rate drop toward the floor as
    wpm drops brings continuous reading close enough to target more often,
    the same way, at every sub-native speed -- chunking becomes the
    exception, not the default. Above native pace and at or below 225wpm,
    rate is capped well below the literal wpm/native ratio (2.5 was tried
    and confirmed to sound "sped up... unnatural... hard to follow" -- even
    the ORIGINAL tts_generate.py calibration history documented that boosts
    above ~1.15 read as rushed) -- 225wpm will not hit its exact label as a
    result, naturalness wins over label precision here same as everywhere
    else in this file.

    Above 225wpm (250/275/300, added later): the literal, uncapped
    wpm/native ratio is used instead of the 1.15 ceiling -- user-confirmed
    by ear as still acceptable up to rate~1.73 (300wpm/attorney), unlike
    the earlier-rejected 2.5. Measured per-line variance is real and
    noticeably larger up here than anywhere at/below 225wpm (one 8-line
    attorney spread at a single rate ran 225-353 actual wpm against a
    250 target), but it's the same kind of content-driven jitter this file
    has always had, just amplified -- handled the same way, via
    synthesize_paused's per-line outlier retry, not by capping rate
    further. This branch never touches wpm<=225's rate, so the 9 already-
    locked speeds are unaffected regardless of what this returns here."""
    ratio = wpm / NATIVE_WPM[speaker]
    if ratio >= 1.0:
        if wpm <= 225:
            return min(1.15, ratio)
        return ratio
    if wpm >= NATIVE_ENOUGH_WPM:
        # Close enough to native pace that a continuous rate=1.0 reading
        # already lands near target on its own -- confirmed by ear: 1.0
        # sounded right at 140wpm (the user's own gold-standard example),
        # while 120wpm at flat 1.0 still needed real chunking help and
        # sounded noticeably worse than its later, tapered-rate version.
        # No reason to taper rate down here just because wpm is
        # technically sub-native; that would only trade an already-working
        # sound for a needlessly slower one.
        return 1.0
    return max(ttsgen.NATURAL_RATE_FLOOR, ratio)


def synthesize_at_fixed_rate(entry: dict, rate: float, voice_name: str, api_key: str, tmp_dir: Path) -> bytes:
    """One TTS call for the whole line, at the ONE fixed rate for this
    (speaker, wpm) -- see fixed_rate_for. Individual lines will land a bit
    off their own precise duration target since content varies at a fixed
    rate, but the articulation pace itself never wavers, which is the
    actual thing that was reported as inconsistent. Caps any oversized
    internal pause Chirp3 HD's own prosody happens to produce (see
    normalize_internal_gaps) -- a no-op when there isn't one."""
    text = entry["text"]
    raw_mp3 = tmp_dir / "fr_raw.mp3"
    raw_mp3.write_bytes(synthesize_resilient(text, voice_name, rate, api_key))
    trimmed_wav = tmp_dir / "fr_trim.wav"
    wav_trim_silence(raw_mp3, trimmed_wav)
    normalized_wav = normalize_internal_gaps(trimmed_wav, tmp_dir, tag="fr")
    final_mp3 = tmp_dir / "fr_final.mp3"
    ttsgen.wav_to_mp3(normalized_wav, final_mp3)
    return final_mp3.read_bytes()


def synthesize_paused(entry: dict, wpm: int, rate: float, voice_name: str, api_key: str, tmp_dir: Path) -> bytes:
    """Line synthesis for wpm at or below the voice's natural pace. Every
    chunk is synthesized at the ONE fixed `rate` for this (speaker, wpm) --
    never adjusted per line or per chunk (see fixed_rate_for). Multi-syllable
    words simply take proportionally longer at a fixed rate, the same as
    real speech; the TTS engine already handles that correctly on its own,
    so there's no need to hand-weight anything by syllable count.

    Strongly prefers a plain CONTINUOUS whole-line reading over chunked
    pausing: the user held up one line as the gold standard for how this
    should sound everywhere, and it turned out to be exactly a continuous
    reading with zero artificial chunking, its only pause being Chirp3 HD's
    own natural sentence-final slowdown. Chunking is a fallback for when
    continuous reading genuinely can't reach the target, not the default --
    and even then, only as many real pauses as the remaining padding budget
    calls for are used (see pause_at below); the rest of the natural
    chunk boundaries get a plain, brief transition instead of a forced
    pause, so pacing doesn't collapse into "a pause every 2 words" once the
    line is long enough to need several chunk boundaries at all. Chunks are
    independently-synthesized complete recordings (never sliced out of a
    longer one -- slicing was tried twice and reliably clipped), with a
    retry for any chunk whose own timing is a clear outlier against the
    line's median (an isolated short fragment occasionally gets its own
    improvised pause from Chirp3 HD -- a retry usually avoids it, unrelated
    to rate)."""
    text = entry["text"]
    words = text.split()  # real chunking tokens -- unchanged, used for synthesis
    n = spoken_word_count(text)  # true spoken word count -- used for the target only
    target_duration = n / wpm * 60

    def synth_chunk(chunk_words, tag):
        chunk_text = " ".join(chunk_words)
        raw_bytes = synthesize_resilient(chunk_text, voice_name, rate, api_key)
        raw_mp3 = tmp_dir / f"sp_raw_{tag}.mp3"
        raw_mp3.write_bytes(raw_bytes)
        trimmed_wav = tmp_dir / f"sp_trim_{tag}.wav"
        wav_trim_silence(raw_mp3, trimmed_wav)
        try:
            dur = ttsgen.get_duration_seconds(trimmed_wav)
            if dur <= 0:
                raise ValueError("empty trim result")
        except Exception:
            # A very short/quiet single-word chunk (confirmed: "Correct," at
            # 60wpm) can get entirely silence-trimmed away, leaving no usable
            # audio. Fall back to the untrimmed recording rather than crash
            # or lose the word -- a touch of extra edge silence on one rare
            # chunk beats dropping the word.
            trimmed_wav = tmp_dir / f"sp_untrimmed_{tag}.wav"
            ttsgen.to_wav(raw_mp3, trimmed_wav)
            dur = ttsgen.get_duration_seconds(trimmed_wav)
        # Cap any oversized internal pause Chirp3 HD's own prosody happens to
        # produce WITHIN this chunk -- confirmed missing here specifically:
        # a user-reported "glitch" between two words in a multi-word chunk
        # turned out to be a 1.7s silence Chirp3 HD inserted mid-chunk, which
        # nothing caught because this was only being applied to the
        # whole-line baseline reading below, never to individual chunks.
        trimmed_wav = normalize_internal_gaps(trimmed_wav, tmp_dir, tag=f"chunk{tag}")
        dur = ttsgen.get_duration_seconds(trimmed_wav)
        return trimmed_wav, dur

    # A plain continuous whole-line reading first -- if it already reaches
    # the target with no chunking at all, use it as-is. Chunking
    # (independently-synthesized fragments) measurably runs slower per word
    # than one continuous take (isolation inflation), so it's the fallback,
    # never the default. synth_chunk already caps any oversized internal
    # pause Chirp3 HD's own prosody happens to produce (confirmed: the same
    # line's natural sentence-break pause measured ~0.5s on one take and
    # 2.27s on another, pure synthesis jitter) -- a no-op when there isn't
    # one, so no need to repeat it here.
    baseline_wav, baseline_dur = synth_chunk(words, "baseline")
    if baseline_dur >= target_duration:
        final_mp3 = tmp_dir / "sp_final.mp3"
        ttsgen.wav_to_mp3(baseline_wav, final_mp3)
        return final_mp3.read_bytes()

    # Size chunk grouping to roughly how much pause time is actually needed
    # (estimated from the continuous baseline just measured), instead of a
    # fixed 2-word cap regardless of how much slowdown a line needs. A fixed
    # small cap was confirmed by ear to sound like a metronomic "pause every
    # 2 words" specifically at 60-80wpm, where the padding needed is large
    # relative to how few natural punctuation breaks a short deposition line
    # usually has -- there's no way for punctuation alone to carry it, so a
    # rigid small cap forces a pause at nearly every boundary. Sizing chunks
    # to the actual padding budget means a line needing lots of slowdown
    # gets naturally more, evenly-sized pieces and one needing little gets
    # big pieces (often zero extra pauses beyond real punctuation).
    word_chunks = build_adaptive_chunks(words, target_duration - baseline_dur)
    chunk_wavs, per_word = [], []
    for ci, chunk in enumerate(word_chunks):
        wav_path, dur = synth_chunk(chunk, str(ci))
        chunk_wavs.append(wav_path)
        per_word.append(dur / len(chunk))

    if len(per_word) >= 3:
        med = sorted(per_word)[len(per_word) // 2]
        if med > 0:
            for ci, chunk in enumerate(word_chunks):
                if per_word[ci] > med * 1.8 or per_word[ci] < med / 1.8:
                    best_wav, best_pw = chunk_wavs[ci], per_word[ci]
                    for retry in range(2):
                        wav_path, dur = synth_chunk(chunk, f"{ci}r{retry}")
                        pw = dur / len(chunk)
                        if abs(pw - med) < abs(best_pw - med):
                            best_wav, best_pw = wav_path, pw
                    chunk_wavs[ci] = best_wav

    if len(chunk_wavs) < 2:
        final_mp3 = tmp_dir / "sp_final.mp3"
        ttsgen.wav_to_mp3(chunk_wavs[0], final_mp3)
        return final_mp3.read_bytes()

    total_speech = sum(ttsgen.get_duration_seconds(p) for p in chunk_wavs)
    num_gaps = len(chunk_wavs) - 1
    padding_needed = target_duration - total_speech

    # Chunk grouping was already sized to the padding budget above (real
    # punctuation breaks plus just enough subdivided pieces), so every
    # boundary here is a deliberate pause point, not an arbitrary word-count
    # cutoff -- no further subset selection needed. If the actual chunked
    # total (isolation-inflated, so usually a bit more than the baseline
    # estimate used for sizing) already covers the target, don't add any
    # real pause at all.
    pause_at = set(range(num_gaps)) if padding_needed > 0 else set()
    non_pause_time = (num_gaps - len(pause_at)) * TRANSITION_GAP
    gap_duration = max(MIN_GAP, (target_duration - total_speech - non_pause_time) / max(1, len(pause_at))) \
        if pause_at else TRANSITION_GAP

    gap_wav = tmp_dir / "sp_gap.wav"
    transition_wav = tmp_dir / "sp_transition.wav"
    ttsgen.generate_silence_wav(TRANSITION_GAP, transition_wav)
    out_wav = tmp_dir / "sp_out.wav"
    best_wav, best_error = None, float("inf")
    tol = max(0.08 * target_duration, 0.2)
    for _ in range(4):
        ttsgen.generate_silence_wav(gap_duration, gap_wav)
        sequence = []
        for i, cw in enumerate(chunk_wavs):
            sequence.append(cw)
            if i < len(chunk_wavs) - 1:
                sequence.append(gap_wav if i in pause_at else transition_wav)
        ttsgen.concat_wav(sequence, out_wav)
        final_duration = ttsgen.get_duration_seconds(out_wav)
        error = abs(target_duration - final_duration)
        if error < best_error:
            best_error = error
            best_wav = tmp_dir / "sp_best.wav"
            best_wav.write_bytes(out_wav.read_bytes())
        if error <= tol or not pause_at:
            break
        gap_duration = max(MIN_GAP, gap_duration + (target_duration - final_duration) / len(pause_at))

    # Isolation inflation can still push the chunked-and-padded result past
    # target even when the continuous baseline (already ruled out above for
    # being under target) would have been closer -- whichever actually ends
    # up nearer wins, rather than trusting the chunking path once entered.
    if abs(baseline_dur - target_duration) < best_error:
        best_wav = baseline_wav

    final_mp3 = tmp_dir / "sp_final.mp3"
    ttsgen.wav_to_mp3(best_wav, final_mp3)
    return final_mp3.read_bytes()


CLAUSE_BREAK_CHARS = set(",;:.!?")

# A trailing "." after one of these is a title/abbreviation, not a sentence
# end -- without this, "Ms." on its own splits off as a spurious one-word
# chunk (confirmed: pushed a 7-word line 14% off target and reintroduced the
# short-chunk choppiness this whole rewrite exists to avoid).
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "jr", "sr", "st", "mt", "vs", "etc",
                  "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}


def split_into_natural_chunks(words: list, max_chunk_words: int):
    """Break at real clause boundaries (comma, semicolon, colon, sentence-
    ending punctuation) wherever the transcript has one, so pauses land where
    a person would actually pause -- not at an arbitrary fixed word count.
    Falls back to max_chunk_words only for a stretch with no punctuation at
    all, so it still gets a breath somewhere. Callers needing PURE
    punctuation-only breaks (no fallback at all) pass len(words)+1 -- see
    build_adaptive_chunks below, the only caller now.

    Returns (chunks, is_punct_break): is_punct_break[i] says whether the
    boundary right after chunks[i] is a real clause break (True) or just the
    max_chunk_words fallback (False) -- callers use this to prefer pausing
    at real clause breaks over the fallback ones, which is what keeps
    pausing from collapsing into a flat, metronomic "every N words" when
    several boundaries are available (see synthesize_paused)."""
    chunks = []
    is_punct_break = []
    current = []
    for w in words:
        current.append(w)
        stripped = w.rstrip("\"')]")
        last_char = stripped[-1] if stripped else ""
        is_period_abbrev = last_char == "." and stripped[:-1].lower() in ABBREVIATIONS
        ends_clause = last_char in CLAUSE_BREAK_CHARS and not is_period_abbrev
        if ends_clause or len(current) >= max_chunk_words:
            chunks.append(current)
            is_punct_break.append(ends_clause)
            current = []
    if current:
        chunks.append(current)
    return chunks, is_punct_break[:len(chunks) - 1]


COMFORTABLE_GAP = 0.65  # target pause length when sizing chunks below --
# long enough to read as a deliberate break, short enough not to feel like
# dead air. (Separate from MIN_GAP, which is a hard floor on any actual
# inserted silence once chunk sizing is already decided.)


def build_adaptive_chunks(words: list, estimated_padding: float, min_piece: int = 2) -> list:
    """Size chunk grouping to roughly how much pause time is actually needed
    (estimated_padding, from a continuous baseline reading measured before
    this is called), instead of a fixed word-count cap regardless of how
    much slowdown a line needs. A fixed small cap (2 words) was confirmed by
    ear to sound like a metronomic "pause every 2 words," especially at
    60-80wpm: the padding needed there is large relative to how few natural
    punctuation breaks a short deposition line usually has, so punctuation
    alone can't carry it and a rigid small cap forces a pause at nearly
    every boundary regardless.

    Starts from real clause breaks only (commas/periods/etc -- those always
    stay, they're genuine pause points). If that alone would already give
    roughly enough break points for the padding budget (padding /
    COMFORTABLE_GAP), stop there. Otherwise subdivide the LONGEST
    unpunctuated stretches first, roughly in half each time, until there
    are enough pieces -- never shrinking a piece below min_piece words, so
    it still doesn't degenerate into per-word chunking."""
    natural_chunks, _ = split_into_natural_chunks(words, max_chunk_words=len(words) + 1)
    if estimated_padding <= 0:
        return natural_chunks

    desired_pauses = max(1, round(estimated_padding / COMFORTABLE_GAP))
    if len(natural_chunks) - 1 >= desired_pauses:
        return natural_chunks

    chunks = [list(c) for c in natural_chunks]
    extra_needed = desired_pauses - (len(natural_chunks) - 1)
    for _ in range(extra_needed):
        best_idx, best_len = -1, min_piece * 2 - 1
        for i, c in enumerate(chunks):
            if len(c) > best_len:
                best_idx, best_len = i, len(c)
        if best_idx == -1:
            break  # nothing left worth splitting further
        c = chunks[best_idx]
        mid = max(min_piece, min(len(c) - min_piece, len(c) // 2))
        chunks[best_idx:best_idx + 1] = [c[:mid], c[mid:]]
    return chunks


def generate_line_fixed(entry: dict, wpm: int, api_key: str, out_dir: Path) -> Path:
    """Line synthesis entirely owned by this script now, for every speed
    (tts_generate.py's own per-line rate-tuned methods -- synthesize_paced,
    synthesize_rate_calibrated, etc -- are no longer called here at all, for
    any wpm). See fixed_rate_for's docstring for why: hitting each line's
    exact duration by searching a different speakingRate per line is what
    was making speakers sound inconsistent ("attorney rushes here, witness
    drags there") at every speed, not just the slow ones -- confirmed by
    the user across both the sub-floor and above-floor tracks. ONE fixed
    rate per (speaker, wpm), computed once, used for every line at that
    speed: exactly 1.0 (paced via pauses, synthesize_paused) at or below
    the voice's natural pace, or one fixed elevated rate above it
    (synthesize_at_fixed_rate)."""
    speaker = entry["speaker"]
    voice_name = ttsgen.VOICES[speaker]
    rate = fixed_rate_for(speaker, wpm)
    fname = f"line{entry['line_num']:02d}_{speaker}_{wpm}wpm.mp3"

    tmp_dir = TMP_DIR / "_linegen"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    trimmed_path = out_dir / fname

    n_words = len(entry["text"].split())
    target_duration = n_words / wpm * 60

    def attempt():
        if rate > 1.0:
            return synthesize_at_fixed_rate(entry, rate, voice_name, api_key, tmp_dir)
        return synthesize_paused(entry, wpm, rate, voice_name, api_key, tmp_dir)

    audio_bytes = attempt()
    probe_path = tmp_dir / "attempt_probe.mp3"
    probe_path.write_bytes(audio_bytes)
    error_pct = abs(ttsgen.get_duration_seconds(probe_path) - target_duration) / target_duration
    if error_pct > 0.20:
        # Same jitter-retry convention used throughout this codebase for
        # this voice's confirmed run-to-run duration jitter -- NEVER changes
        # rate, just re-rolls the same fixed-rate synthesis and keeps
        # whichever attempt lands closer.
        retry_bytes = attempt()
        retry_path = tmp_dir / "attempt_retry.mp3"
        retry_path.write_bytes(retry_bytes)
        retry_error_pct = abs(ttsgen.get_duration_seconds(retry_path) - target_duration) / target_duration
        if retry_error_pct < error_pct:
            audio_bytes = retry_bytes

    trimmed_path.write_bytes(audio_bytes)
    return trimmed_path


def get_line_path(entry: dict, wpm: int, rebuild: bool = False) -> Path:
    """Reuse the already-generated, already-QA'd trimmed line file if present;
    otherwise (or if `rebuild` forces it) synthesize it via generate_line_fixed
    above. Avoids burning API calls / alignment time re-deriving audio that's
    already confirmed clean, unless explicitly told to rebuild."""
    out_dir = ttsgen.TRIMMED_DIR / f"{wpm}wpm"
    fname = f"line{entry['line_num']:02d}_{entry['speaker']}_{wpm}wpm.mp3"
    existing = out_dir / fname
    if existing.exists() and not rebuild:
        return existing
    api_key = ttsgen.get_api_key()
    print(f"    generating line{entry['line_num']:02d}_{entry['speaker']} ({wpm}wpm)...")
    return generate_line_fixed(entry, wpm, api_key, out_dir)


def assemble_track(wpm: int, gap_ms: int, entries: list, rebuild: bool = False) -> dict:
    if wpm not in TARGET_WPMS:
        raise ValueError(f"wpm={wpm} not calibrated. Valid: {TARGET_WPMS}")

    run_tmp = TMP_DIR / f"{wpm}wpm"
    run_tmp.mkdir(parents=True, exist_ok=True)
    gap_path = run_tmp / "gap.wav"
    ttsgen.generate_silence_wav(gap_ms / 1000, gap_path)

    wav_paths = []
    line_durations = []
    speaker_stats = {"attorney": {"words": 0, "seconds": 0.0}, "witness": {"words": 0, "seconds": 0.0}}
    total_words = 0

    for i, entry in enumerate(entries):
        line_mp3 = get_line_path(entry, wpm, rebuild=rebuild)
        line_wav = run_tmp / f"line{entry['line_num']:02d}.wav"
        ttsgen.to_wav(line_mp3, line_wav)
        duration = ttsgen.get_duration_seconds(line_wav)
        line_durations.append(duration)

        n_words = len(entry["text"].split())
        total_words += n_words
        speaker_stats[entry["speaker"]]["words"] += n_words
        speaker_stats[entry["speaker"]]["seconds"] += duration

        wav_paths.append(line_wav)
        if i < len(entries) - 1:
            wav_paths.append(gap_path)

    combined_wav = run_tmp / "combined.wav"
    ttsgen.concat_wav(wav_paths, combined_wav)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"practice_{wpm}wpm.mp3"
    ttsgen.wav_to_mp3(combined_wav, out_path)

    active_seconds = sum(line_durations)
    active_wpm = total_words / active_seconds * 60
    total_seconds = active_seconds + gap_ms / 1000 * (len(entries) - 1)

    return {
        "wpm": wpm,
        "out_path": out_path,
        "total_words": total_words,
        "active_seconds": active_seconds,
        "total_seconds": total_seconds,
        "active_wpm": active_wpm,
        "attorney_wpm": speaker_stats["attorney"]["words"] / speaker_stats["attorney"]["seconds"] * 60,
        "witness_wpm": speaker_stats["witness"]["words"] / speaker_stats["witness"]["seconds"] * 60,
    }


def parse_wpm_arg(raw: str) -> list:
    if raw == "all":
        return TARGET_WPMS
    out = []
    for tok in raw.split(","):
        wpm = int(tok.strip())
        if wpm not in TARGET_WPMS:
            sys.exit(f"wpm={wpm} not calibrated. Valid: {TARGET_WPMS}")
        out.append(wpm)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wpm", type=str, default="all", help="Comma-separated target speeds, or 'all' (default)")
    parser.add_argument("--gap-ms", type=int, default=DEFAULT_GAP_MS,
                         help=f"Inter-line Q&A silence gap in ms, clamped to [{GAP_MS_MIN}, {GAP_MS_MAX}] "
                              f"(default {DEFAULT_GAP_MS})")
    parser.add_argument("--rebuild-lines", action="store_true",
                         help="Regenerate every line for the selected --wpm speed(s) with the corrected "
                              "synthesis method (generate_line_fixed), even if a trimmed file already "
                              "exists. Needs GOOGLE_TTS_API_KEY.")
    args = parser.parse_args()

    gap_ms = max(GAP_MS_MIN, min(GAP_MS_MAX, args.gap_ms))
    wpms = parse_wpm_arg(args.wpm)
    entries = sorted(ttsgen.load_transcript(), key=lambda e: e["line_num"])

    print(f"Assembling {len(wpms)} practice track(s) from {len(entries)} lines, gap={gap_ms}ms"
          f"{' (rebuilding lines)' if args.rebuild_lines else ''}\n")

    results = []
    for wpm in wpms:
        print(f"[{wpm}wpm] assembling...")
        r = assemble_track(wpm, gap_ms, entries, rebuild=args.rebuild_lines)
        results.append(r)
        dev = abs(r["active_wpm"] - wpm) / wpm * 100
        print(f"  -> {r['out_path'].name}  "
              f"active_wpm={r['active_wpm']:.1f} (target {wpm}, {dev:.1f}% off)  "
              f"attorney={r['attorney_wpm']:.1f} witness={r['witness_wpm']:.1f}  "
              f"track={r['total_seconds']:.1f}s")

    print("\nSummary:")
    print(f"{'wpm':>5} {'active_wpm':>11} {'dev%':>6} {'attorney':>9} {'witness':>9} {'track_s':>8}  file")
    for r in results:
        dev = abs(r["active_wpm"] - r["wpm"]) / r["wpm"] * 100
        flag = "  <-- check" if dev > 5 else ""
        print(f"{r['wpm']:>5} {r['active_wpm']:>11.1f} {dev:>5.1f}% {r['attorney_wpm']:>9.1f} "
              f"{r['witness_wpm']:>9.1f} {r['total_seconds']:>8.1f}  {r['out_path'].name}{flag}")

    print(f"\nOutput dir: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
