#!/usr/bin/env python3
"""
Generate dictation practice audio via Google Cloud Text-to-Speech (Chirp 3: HD)
for the Meridian Freight mock deposition transcript, at calibrated per-voice
speakingRate values, then trim leading/trailing silence with ffmpeg.

API key is read from the GOOGLE_TTS_API_KEY environment variable (see .env.local,
which is gitignored / untracked -- never hardcoded here).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_BIN = "ffmpeg"

BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_PATH = BASE_DIR / "transcript.json"
RAW_DIR = BASE_DIR / "output" / "raw"
TRIMMED_DIR = BASE_DIR / "output" / "trimmed"
DIAGNOSTIC_DIR = BASE_DIR / "output" / "diagnostic"

TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

VOICES = {
    "attorney": "en-US-Chirp3-HD-Achird",
    "witness": "en-US-Chirp3-HD-Achernar",
}

# target wpm -> speakingRate, per voice (calibrated against each voice's
# native rate at speakingRate=1.0: Achird ~184.89 wpm, Achernar ~180.86 wpm)
SPEAKING_RATE = {
    "attorney": {60: 0.32, 80: 0.43, 100: 0.54, 120: 0.65, 140: 0.76,
                 160: 0.87, 180: 0.97, 200: 1.08, 225: 1.22},
    "witness": {60: 0.33, 80: 0.44, 100: 0.55, 120: 0.66, 140: 0.77,
                160: 0.88, 180: 1.00, 200: 1.11, 225: 1.24},
}

# Below this speakingRate, individual words still sound stretched/deliberate
# even without outright glitching. A first pass at 0.65 avoided warping but
# on closer listening the words themselves weren't fully natural; 0.75 is
# confirmed by ear as where they stop sounding stretched. Target wpm whose
# table rate falls below this floor get the floor rate + calibrated pause
# chunking instead of further rate reduction, so the voice itself never
# stretches unnaturally.
NATURAL_RATE_FLOOR = 0.75

SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05:"
    "detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05:"
    "detection=peak,"
    "areverse"
)

# A more conservative trim used only for isolated per-word clips in
# synthesize_paced_per_word: requires quieter (-50dB) and longer (0.08s)
# silence before trimming, so a word's own trailing consonant is never
# mistaken for silence and cut. The general SILENCE_FILTER above is left
# untouched since it's already confirmed working well elsewhere.
WORD_SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08:"
    "detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08:"
    "detection=peak,"
    "areverse"
)


def load_transcript():
    with open(TRANSCRIPT_PATH) as f:
        return json.load(f)


def get_api_key():
    key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not key:
        sys.exit(
            "GOOGLE_TTS_API_KEY is not set. Export it or source .env.local first, e.g.:\n"
            "  export GOOGLE_TTS_API_KEY=your_key_here"
        )
    return key


def synthesize(text: str, voice_name: str, speaking_rate: float, api_key: str, ssml: bool = False) -> bytes:
    payload = {
        "input": {"ssml": text} if ssml else {"text": text},
        "voice": {"languageCode": "en-US", "name": voice_name},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": speaking_rate},
    }
    resp = requests.post(
        TTS_ENDPOINT,
        params={"key": api_key},
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TTS API error {resp.status_code}: {resp.text[:500]}")
    audio_b64 = resp.json()["audioContent"]
    import base64
    return base64.b64decode(audio_b64)


def get_duration_seconds(path: Path) -> float:
    result = subprocess.run([FFMPEG_BIN, "-i", str(path)], capture_output=True, text=True)
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError(f"Could not read duration for {path}")


def split_into_n_chunks(words: list, num_chunks: int) -> list:
    """Even, contiguous split into exactly num_chunks groups."""
    n = len(words)
    num_chunks = max(1, min(num_chunks, n))
    chunks, start = [], 0
    for i in range(num_chunks):
        end = max(start + 1, round((i + 1) * n / num_chunks))
        chunks.append(words[start:end])
        start = end
    if start < n:
        chunks[-1].extend(words[start:])
    return [c for c in chunks if c]


def generate_silence(duration_sec: float, path: Path):
    duration_sec = max(0.0, duration_sec)
    cmd = [
        FFMPEG_BIN, "-y", "-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono",
        "-t", f"{duration_sec:.3f}", "-codec:a", "libmp3lame", "-qscale:a", "2",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg silence generation failed: {result.stderr[-500:]}")


def concat_audio(paths: list, output_path: Path):
    cmd = [FFMPEG_BIN, "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    n = len(paths)
    filter_str = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    cmd += ["-filter_complex", filter_str, "-map", "[out]",
            "-codec:a", "libmp3lame", "-qscale:a", "2", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-800:]}")


def synthesize_paced(entry: dict, target_wpm: int, voice_name: str, api_key: str,
                      tmp_dir: Path) -> bytes:
    """For target wpm whose table rate is below NATURAL_RATE_FLOOR: hold
    speakingRate at the floor (voice never stretches unnaturally). If a
    continuous natural-pace reading is already close enough to the target
    duration, use it as-is. Otherwise split into as few chunks as needed
    (synthesized separately, then stitched with silence gaps of our own
    exact duration) to add the remaining slowdown. Chunking itself inflates
    spoken duration -- an isolated short phrase is read more deliberately
    than the same words mid-sentence, ~0.5-0.8s per chunk boundary on top of
    any inserted silence -- so more chunks than necessary overshoots the
    target; the chunk count below is sized to the actual gap needed, not a
    fixed word-per-chunk rate. (SSML <break> was tried first but Chirp3 HD's
    handling of it was unreliable -- duration didn't even track the
    requested break time consistently.)"""
    text = entry["text"]
    words = text.split()
    n = len(words)
    target_duration = n / target_wpm * 60

    baseline_raw = synthesize(text, voice_name, NATURAL_RATE_FLOOR, api_key)
    braw_path = tmp_dir / "baseline_raw.mp3"
    braw_path.write_bytes(baseline_raw)
    btrim_path = tmp_dir / "baseline_trim.mp3"
    trim_silence(braw_path, btrim_path)
    baseline_duration = get_duration_seconds(btrim_path)

    target_added = target_duration - baseline_duration
    # Tight on purpose: lines that skip this correction entirely (because
    # they're "close enough") rely on raw, uncorrected TTS variance for their
    # pace, which is what caused noticeable line-to-line pace swings near the
    # natural-floor boundary (e.g. 100wpm). Routing nearly every line through
    # the same calibrated small-pause treatment keeps pacing consistent.
    tolerance_abs = max(0.05 * target_duration, 0.15)
    if target_added <= tolerance_abs or n <= 1:
        return btrim_path.read_bytes()

    # Per-word chunking was tried (pause after every single word) and
    # rejected by ear: synthesizing each word in total isolation loses the
    # natural co-articulation between neighboring words, so longer lines
    # (more chunk boundaries) came out sounding dragged and glitchy even
    # though shorter lines were fine. Fewer, larger chunks (~2 words each,
    # sized to the actual padding needed) avoid that while still hitting
    # the target wpm consistently -- and consistent wpm across lines is the
    # actual goal for dictation practice, not conversational naturalism
    # (nobody speaks at 60wpm naturally; that's the point of the drill).
    assumed_cost_per_boundary = 0.8  # inflation + intended gap, empirically observed
    num_gaps = max(1, round(target_added / assumed_cost_per_boundary))
    num_chunks = max(2, min(num_gaps + 1, max(2, (n + 1) // 2), n))
    chunks = split_into_n_chunks(words, num_chunks)
    if len(chunks) < 2:
        return btrim_path.read_bytes()

    chunk_paths = []
    total_speech = 0.0
    for i, chunk in enumerate(chunks):
        chunk_text = " ".join(chunk)
        raw_bytes = synthesize(chunk_text, voice_name, NATURAL_RATE_FLOOR, api_key)
        raw_path = tmp_dir / f"chunk_raw_{i}.mp3"
        raw_path.write_bytes(raw_bytes)
        trimmed_path = tmp_dir / f"chunk_trim_{i}.mp3"
        trim_silence(raw_path, trimmed_path)
        chunk_paths.append(trimmed_path)
        total_speech += get_duration_seconds(trimmed_path)

    num_gaps_actual = len(chunks) - 1
    # a 0.00s silence clip round-trips as an invalid/unreadable mp3 for concat
    gap_duration = max(0.05, (target_duration - total_speech) / num_gaps_actual)

    # Refine the gap: chunk speech durations are now fixed (no more TTS
    # calls needed), so re-measuring and adjusting the silence length is
    # cheap (ffmpeg only) and removes residual error from concat/mp3-frame
    # rounding that the one-shot algebra doesn't account for.
    out_path = tmp_dir / "paced_out.mp3"
    silence_path = tmp_dir / "gap_silence.mp3"
    best_path, best_error = None, float("inf")
    tol = max(0.08 * target_duration, 0.2)
    for _ in range(4):
        generate_silence(gap_duration, silence_path)
        sequence = []
        for i, cp in enumerate(chunk_paths):
            sequence.append(cp)
            if i < len(chunk_paths) - 1:
                sequence.append(silence_path)
        concat_audio(sequence, out_path)
        final_duration = get_duration_seconds(out_path)
        error = abs(target_duration - final_duration)
        if error < best_error:
            best_error = error
            best_path = tmp_dir / "paced_best.mp3"
            best_path.write_bytes(out_path.read_bytes())
        if error <= tol:
            break
        gap_duration = max(0.05, gap_duration + (target_duration - final_duration) / num_gaps_actual)

    # Safety net: if chunk-inflation alone overshot badly and the plain
    # continuous reading is actually closer to target, prefer that instead.
    if (best_error > abs(baseline_duration - target_duration)
            and best_error > target_duration * 0.25):
        return btrim_path.read_bytes()
    return best_path.read_bytes()


PER_WORD_RATE = 1.0  # each voice's own native rate, held FIXED -- confirmed
# by ear as the only setting that sounds fully natural. Escalating the rate
# for higher-wpm targets (tried up to 1.15-1.4) was rejected outright: even
# a modest boost made words sound rushed and still clipped at the edges.
# Natural word pronunciation is never traded away here -- only the gap
# between words changes to hit different target speeds, even if that means
# some higher-wpm lines run a bit slower than their exact label.

SHORT_WORD_RATE = 0.92  # short/common words ("a", "the", "is", "your") are
# genuinely brief even at pure rate 1.0 -- that's just phonetics, not
# anything we're doing -- but next to longer words that brevity read as
# "rushed" (confirmed by ear). A small deliberate slowdown ONLY for short
# words gives them more equal weight; anything longer stays untouched.
SHORT_WORD_MAX_LETTERS = 4


def word_rate_for(word: str) -> float:
    letters = "".join(ch for ch in word if ch.isalnum())
    return SHORT_WORD_RATE if len(letters) <= SHORT_WORD_MAX_LETTERS else PER_WORD_RATE


def strip_word_for_synthesis(word: str) -> str:
    """Drop attached punctuation (commas, periods, etc.) before sending an
    isolated word to TTS. When synthesized alone, a word like "crew," gets
    its own model-added pause/intonation drop for the comma -- redundant
    and inconsistent with our own controlled gap, and confirmed by ear as
    part of why some words (especially comma-heavy witness lines) read as
    more "cut off" than others. Keep an apostrophe inside a word (e.g.
    "don't") since that's part of the word itself, not trailing punctuation."""
    stripped = word.strip(".,?!;:\"()[]")
    return stripped if stripped else word


def synthesize_paced_per_word(entry: dict, target_wpm: int, voice_name: str, api_key: str,
                               tmp_dir: Path) -> bytes:
    """For target wpm <=120: a pause after every word, confirmed as the
    actual goal (not the ~2-word chunking synthesize_paced falls back to).
    Every word is synthesized alone at the fixed, natural PER_WORD_RATE --
    never adjusted -- and one EVEN gap size is computed for every boundary
    to hit the target overall duration ("balanced breaks between each
    word"). If the words alone already take as long as (or longer than) the
    target, the gap floors at a small minimum and the line simply runs
    somewhat slower than its label; that's the accepted tradeoff for never
    distorting the words themselves."""
    text = entry["text"]
    words = text.split()
    n = len(words)
    target_duration = n / target_wpm * 60

    if n <= 1:
        return synthesize(text, voice_name, NATURAL_RATE_FLOOR, api_key)

    word_paths = []
    for i, w in enumerate(words):
        audio = synthesize(w, voice_name, word_rate_for(w), api_key)
        rp = tmp_dir / f"pw_raw_{i}.mp3"
        rp.write_bytes(audio)
        tp = tmp_dir / f"pw_trim_{i}.mp3"
        trim_silence_word(rp, tp)
        fp = tmp_dir / f"pw_fade_{i}.mp3"
        fade_word_edges(tp, fp)
        word_paths.append(fp)

    total_word_speech = sum(get_duration_seconds(p) for p in word_paths)
    num_gaps = n - 1
    # A gap this small (the old 0.05s floor) is long enough to register as an
    # interruption but too short to read as a deliberate pause -- confirmed
    # by ear as sounding "staccato"/jittery rather than natural, specifically
    # on lines where the words alone already eat most of the time budget
    # (100/120wpm). Raising the floor accepts more wpm undershoot on those
    # lines in exchange for a gap that actually reads as a real pause.
    MIN_GAP = 0.15
    gap_duration = max(MIN_GAP, (target_duration - total_word_speech) / num_gaps)

    out_path = tmp_dir / "pw_out.mp3"
    silence_path = tmp_dir / "pw_gap_silence.mp3"
    best_path, best_error = None, float("inf")
    tol = max(0.08 * target_duration, 0.2)
    for _ in range(4):
        generate_silence(gap_duration, silence_path)
        sequence = []
        for i, wp in enumerate(word_paths):
            sequence.append(wp)
            if i < len(word_paths) - 1:
                sequence.append(silence_path)
        concat_audio(sequence, out_path)
        final_duration = get_duration_seconds(out_path)
        error = abs(target_duration - final_duration)
        if error < best_error:
            best_error = error
            best_path = tmp_dir / "pw_best.mp3"
            best_path.write_bytes(out_path.read_bytes())
        if error <= tol:
            break
        gap_duration = max(MIN_GAP, gap_duration + (target_duration - final_duration) / num_gaps)

    # No fallback to plain continuous speech here -- per-word pausing with a
    # floored gap (even if that means running a bit slower than the exact
    # label) is always preferred over silently dropping the per-word
    # structure, since natural, undistorted words are the non-negotiable
    # requirement and wpm precision is the secondary one.
    return best_path.read_bytes()


_WHISPER_MODEL = None


def get_whisper_model():
    """Loaded once per process (lazy import -- only synthesize_paced_aligned
    needs it). base.en on CPU is plenty accurate for word boundaries in
    short, clearly-spoken dictation lines and loads in a couple seconds."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def align_words(audio_path: Path) -> list:
    """Word-level timestamps for a continuously-spoken clip, via local
    alignment (Chirp3 HD's own SSML <mark> timepoints return empty -- ruled
    out on day 1). Returns [(word_text, start, end), ...] in order."""
    model = get_whisper_model()
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language="en")
    words = []
    for seg in segments:
        for w in seg.words:
            words.append((w.word.strip(), w.start, w.end))
    return words


def to_wav(src: Path, dst: Path):
    """Decode any input (mp3 from the API, etc.) to PCM WAV. Everything in
    the aligned/chunked pipeline below stays in WAV until the single final
    export -- repeatedly re-encoding short clips to lossy MP3 at every
    intermediate step (extract, then concat) was confirmed by ear as the
    source of words "cutting in and out multiple times," a classic artifact
    of chaining lossy transcodes on very short audio."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-ar", "24000", "-ac", "1", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg wav decode failed: {result.stderr[-800:]}")


def generate_silence_wav(duration_sec: float, path: Path):
    duration_sec = max(0.0, duration_sec)
    cmd = [
        FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{duration_sec:.3f}", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg silence generation failed: {result.stderr[-500:]}")


def concat_wav(paths: list, output_path: Path):
    cmd = [FFMPEG_BIN, "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    n = len(paths)
    filter_str = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    cmd += ["-filter_complex", filter_str, "-map", "[out]", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-800:]}")


def wav_to_mp3(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-codec:a", "libmp3lame", "-qscale:a", "2", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mp3 encode failed: {result.stderr[-800:]}")


def extract_segment(src: Path, dst: Path, lo: float, hi: float,
                     fade_in: float = 0.015, fade_out: float = 0.045):
    """Slice one chunk out of a continuous natural WAV recording at [lo, hi]
    and apply a fade to remove the cut-edge click. fade_out is deliberately
    longer than a "just remove the click" fade and uses a gentle sine curve:
    even with generous padding, an aligned end-time can still land a hair
    inside real voiced sound, and a near-hard fade there is audible as an
    abrupt stop ("like a phone call dropping out"). A slower, curved taper
    turns that same slightly-early cut into something that reads as the word
    naturally trailing off instead of being chopped. Output stays WAV."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, hi - lo)
    fade_out = min(fade_out, duration / 2)
    fade_in = min(fade_in, duration / 2)
    fade_out_start = max(0.0, duration - fade_out)
    af = (f"atrim=start={lo:.3f}:end={hi:.3f},asetpts=PTS-STARTPTS,"
          f"afade=t=in:d={fade_in:.3f}:curve=hsin,"
          f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}:curve=hsin")
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-af", af, str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg segment extraction failed: {result.stderr[-800:]}")


def synthesize_paced_aligned(entry: dict, target_wpm: int, voice_name: str, api_key: str,
                              tmp_dir: Path, chunk_size: int = 2) -> bytes:
    """Day-1's shipped method (2-word chunks, held at a natural rate, silence
    added between chunks) avoided isolated-word synthesis entirely and never
    had a "cutting off" problem -- but its chunk padding was sized ad hoc per
    chunk, which read as uneven pausing, and per-word chunking (today's whole
    detour) kept re-splicing/re-encoding single words in isolation, which is
    what actually caused the cutting/glitching.

    This keeps Day 1's core idea -- chunks of a few words, never a single
    word alone, so co-articulation inside each chunk is always intact -- but
    builds them the way we learned to today: synthesize the FULL line once,
    continuously, at native rate (fully natural), find real word boundaries
    via local alignment, group them into chunk_size-word chunks, slice those
    chunks out of the ONE continuous recording, and splice in one EVEN,
    controlled gap between every chunk. Halving (or better) the number of
    splice points relative to per-word chunking, plus staying in WAV until
    the single final MP3 export, is what should actually fix the cutting."""
    text = entry["text"]
    words = text.split()
    n = len(words)
    target_duration = n / target_wpm * 60

    if n <= 1:
        return synthesize(text, voice_name, NATURAL_RATE_FLOOR, api_key)

    raw_mp3 = tmp_dir / "pa_raw.mp3"
    raw_mp3.write_bytes(synthesize(text, voice_name, 1.0, api_key))
    raw_wav = tmp_dir / "pa_raw.wav"
    to_wav(raw_mp3, raw_wav)

    aligned = align_words(raw_wav)
    if len(aligned) != n:
        # Alignment didn't cleanly match the transcript word-for-word (rare
        # for these short, clear lines) -- don't guess at a mapping, fall
        # back to the isolated-word method instead.
        return synthesize_paced_per_word(entry, target_wpm, voice_name, api_key, tmp_dir)

    # Confirmed with silencedetect: fixed word-count chunking can group two
    # words that have a real natural pause between them (e.g. after a comma)
    # into the SAME chunk -- so that chunk's own audio contains an
    # uncontrolled silence in its middle, on top of our own controlled gaps
    # elsewhere. That's what an uneven mix of ~0.45s (our gap) and ~0.2s
    # (a hidden natural pause) silences in the same line sounds like: a
    # dropout. Always break the chunk at a natural pause, even if that
    # chunk ends up smaller than chunk_size.
    NATURAL_GAP_THRESHOLD = 0.12
    chunks = [[aligned[0]]]
    for i in range(1, n):
        natural_gap = aligned[i][1] - aligned[i - 1][2]
        if len(chunks[-1]) >= chunk_size or natural_gap >= NATURAL_GAP_THRESHOLD:
            chunks.append([aligned[i]])
        else:
            chunks[-1].append(aligned[i])

    raw_duration = get_duration_seconds(raw_wav)
    # Same reasoning as the per-word attempt: alignment end-times land a
    # touch early, so pad by a fixed amount regardless of the neighbor
    # (a little overlap between adjacent chunks in the source is inaudible
    # since each extracted chunk is only used once in the rebuilt sequence).
    PAD_BEFORE = 0.03
    PAD_AFTER = 0.09
    chunk_paths = []
    for i, grp in enumerate(chunks):
        start, end = grp[0][1], grp[-1][2]
        lo = max(0.0, start - PAD_BEFORE)
        hi = min(raw_duration, end + PAD_AFTER)
        cp = tmp_dir / f"pa_chunk_{i}.wav"
        extract_segment(raw_wav, cp, lo, hi)
        chunk_paths.append(cp)

    total_chunk_speech = sum(get_duration_seconds(p) for p in chunk_paths)
    num_gaps = len(chunk_paths) - 1
    if num_gaps == 0:
        wav_to_mp3(chunk_paths[0], tmp_dir / "pa_final.mp3")
        return (tmp_dir / "pa_final.mp3").read_bytes()

    MIN_GAP = 0.15  # confirmed by ear that anything smaller reads as
    # staccato/jittery rather than a real pause.
    gap_duration = max(MIN_GAP, (target_duration - total_chunk_speech) / num_gaps)

    out_path = tmp_dir / "pa_out.wav"
    silence_path = tmp_dir / "pa_gap_silence.wav"
    best_path, best_error = None, float("inf")
    tol = max(0.08 * target_duration, 0.2)
    for _ in range(4):
        generate_silence_wav(gap_duration, silence_path)
        sequence = []
        for i, cp in enumerate(chunk_paths):
            sequence.append(cp)
            if i < len(chunk_paths) - 1:
                sequence.append(silence_path)
        concat_wav(sequence, out_path)
        final_duration = get_duration_seconds(out_path)
        error = abs(target_duration - final_duration)
        if error < best_error:
            best_error = error
            best_path = tmp_dir / "pa_best.wav"
            best_path.write_bytes(out_path.read_bytes())
        if error <= tol:
            break
        gap_duration = max(MIN_GAP, gap_duration + (target_duration - final_duration) / num_gaps)

    final_mp3 = tmp_dir / "pa_final.mp3"
    wav_to_mp3(best_path, final_mp3)
    return final_mp3.read_bytes()


def trim_silence(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(src),
        "-af", SILENCE_FILTER,
        "-codec:a", "libmp3lame", "-qscale:a", "2",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {src.name}: {result.stderr[-800:]}")


def trim_silence_word(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(src),
        "-af", WORD_SILENCE_FILTER,
        "-codec:a", "libmp3lame", "-qscale:a", "2",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {src.name}: {result.stderr[-800:]}")


def fade_word_edges(src: Path, dst: Path, fade_in: float = 0.015, fade_out: float = 0.05):
    """A short fade at each edge of an isolated word clip before it's spliced
    next to a silence gap. Per-word chunking reported "distorted... last
    letters cut off" at smaller gap sizes (80-120wpm) even though the same
    trim was fine at 60wpm's larger gaps -- the hard digital edge at a splice
    point (whether from trimming slightly tight or just an un-crossfaded cut)
    is much more audible with little silence around it to soften it. A tiny
    fade removes that hard edge without perceptibly changing the word."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # fade-out via reverse/fade-in/reverse so we don't need to know duration
    af = f"afade=t=in:d={fade_in}," \
         f"areverse,afade=t=in:d={fade_out},areverse"
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(src),
        "-af", af,
        "-codec:a", "libmp3lame", "-qscale:a", "2",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg fade failed on {src.name}: {result.stderr[-800:]}")


def analyze_audio(path: Path) -> dict:
    """Objective checks: duration, peak/mean volume, clipping sample count."""
    cmd = [
        FFMPEG_BIN, "-i", str(path),
        "-af", "astats=metadata=1:reset=1,volumedetect",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr
    info = {"path": str(path)}
    for line in stderr.splitlines():
        line = line.strip()
        if "Duration:" in line and "duration" not in info:
            info["duration"] = line.split("Duration:")[1].split(",")[0].strip()
        if "max_volume:" in line:
            info["max_volume"] = line.split("max_volume:")[1].strip()
        if "mean_volume:" in line:
            info["mean_volume"] = line.split("mean_volume:")[1].strip()
        if "Peak level dB:" in line:
            info["peak_level_db"] = line.split("Peak level dB:")[1].strip()
    return info


def synthesize_rate_calibrated(entry: dict, target_wpm: int, voice_name: str, table_rate: float,
                                api_key: str, tmp_dir: Path) -> bytes:
    """For target wpm at/above NATURAL_RATE_FLOOR: the multiplier table (built
    from one continuous 150-word passage) doesn't hold precisely for short
    individual lines -- observed up to ~40% off target on some lines, because
    a given line's own natural pace can differ from the passage average.
    Refine with a proportional speakingRate correction, same idea as the
    low-speed calibration. If a line would need a rate below NATURAL_RATE_FLOOR
    to hit its target (common for lines near the 120-140wpm boundary), that's
    a warping risk -- fall back to the floor-rate + calibrated-pause method
    instead of either distorting the voice or accepting a large undershoot.

    (A rate clamp bounded to the table's canonical rate was tried to force
    line-to-line consistency, on the theory that our own per-line correction
    was the source of pace swings. Measurement disproved that: pinning EVERY
    line to the exact same rate still produced actual paces from 185-295wpm
    on a 225wpm target -- the model itself reads different content at very
    different natural paces at a fixed rate. Full per-line correction is
    necessary, not optional; clamping it only left the hard lines further
    from target. So this now runs more iterations and keeps a two-point
    secant estimate for the cases where simple proportional scaling doesn't
    converge in a couple of tries.)"""
    text = entry["text"]
    words = text.split()
    n = len(words)
    target_duration = n / target_wpm * 60
    tolerance = max(0.08 * target_duration, 0.2)

    rate = table_rate
    tried = []  # (rate, actual_duration) pairs, for secant fallback
    best_audio, best_error = None, float("inf")
    hit_floor = False
    for i in range(8):
        audio_bytes = synthesize(text, voice_name, rate, api_key)
        raw_path = tmp_dir / "rate_probe_raw.mp3"
        raw_path.write_bytes(audio_bytes)
        trimmed_path = tmp_dir / "rate_probe_trim.mp3"
        trim_silence(raw_path, trimmed_path)
        actual_duration = get_duration_seconds(trimmed_path)
        error = abs(target_duration - actual_duration)
        if error < best_error:
            best_audio, best_error = audio_bytes, error
        if error <= tolerance:
            break
        tried.append((rate, actual_duration))
        if len(tried) >= 2:
            # secant: fit a line through the last two (rate, duration) points
            # and solve for the rate that should hit target_duration exactly
            (r0, d0), (r1, d1) = tried[-2], tried[-1]
            if abs(d1 - d0) > 1e-6:
                new_rate = r1 + (target_duration - d1) * (r1 - r0) / (d1 - d0)
            else:
                new_rate = r1 * (actual_duration / target_duration)
        else:
            ratio = actual_duration / target_duration
            new_rate = rate * ratio
        if new_rate < NATURAL_RATE_FLOOR:
            hit_floor = True
            break
        rate = min(2.5, max(NATURAL_RATE_FLOOR, new_rate))

    if hit_floor and best_error > tolerance:
        paced_audio = synthesize_paced(entry, target_wpm, voice_name, api_key, tmp_dir)
        paced_path = tmp_dir / "paced_fallback.mp3"
        paced_path.write_bytes(paced_audio)
        paced_duration = get_duration_seconds(paced_path)
        if abs(target_duration - paced_duration) < best_error:
            return paced_audio

    return best_audio


def generate_line(entry: dict, wpm: int, api_key: str, out_dir: Path) -> Path:
    speaker = entry["speaker"]
    voice_name = VOICES[speaker]
    rate = SPEAKING_RATE[speaker][wpm]
    text = entry["text"]
    line_num = entry["line_num"]
    n = len(text.split())
    target_duration = n / wpm * 60

    fname = f"line{line_num:02d}_{speaker}_{wpm}wpm.mp3"
    raw_path = RAW_DIR / f"{wpm}wpm" / fname
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = BASE_DIR / "output" / "_tmp_probe"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def attempt():
        if rate < NATURAL_RATE_FLOOR:
            # Uniform per-word pausing for the whole <=120wpm bucket -- the
            # 2-word adaptive chunker's gaps are sized to the padding each
            # segment needs, which vary line to line and were confirmed by
            # ear to sound unnatural ("shouldn't be longer pauses in some
            # sections than others"). Every word gets the same fixed gap
            # here instead, even if that means 100/120wpm run slower than
            # their exact label -- natural words and even spacing take
            # priority over label accuracy for this whole speed range.
            if wpm <= 120:
                return synthesize_paced_per_word(entry, wpm, voice_name, api_key, tmp_dir)
            return synthesize_paced(entry, wpm, voice_name, api_key, tmp_dir)
        else:
            return synthesize_rate_calibrated(entry, wpm, voice_name, rate, api_key, tmp_dir)

    # Chirp3 HD has run-to-run jitter (confirmed earlier: identical input can
    # synthesize at noticeably different durations across separate calls).
    # Most attempts land close to target; an occasional unlucky run doesn't
    # converge well within the iteration budget. Re-running once and keeping
    # whichever attempt is closer catches those without doubling cost for
    # every line -- only lines that come out clearly off (>20%) pay for a
    # second attempt.
    audio_bytes = attempt()
    probe_path = tmp_dir / "attempt_probe.mp3"
    probe_path.write_bytes(audio_bytes)
    error_pct = abs(get_duration_seconds(probe_path) - target_duration) / target_duration
    if error_pct > 0.20:
        retry_bytes = attempt()
        retry_path = tmp_dir / "attempt_retry.mp3"
        retry_path.write_bytes(retry_bytes)
        retry_error_pct = abs(get_duration_seconds(retry_path) - target_duration) / target_duration
        if retry_error_pct < error_pct:
            audio_bytes = retry_bytes

    raw_path.write_bytes(audio_bytes)

    trimmed_path = out_dir / fname
    trim_silence(raw_path, trimmed_path)
    return trimmed_path


def cmd_diagnostic(args):
    api_key = get_api_key()
    entries = load_transcript()
    wpm = args.wpm
    picks = entries[: args.count]
    print(f"Diagnostic: generating {len(picks)} line(s) at {wpm} wpm (rate values below), "
          f"no client-side speed adjustment, raw Google TTS output only.\n")

    for entry in picks:
        speaker = entry["speaker"]
        rate = SPEAKING_RATE[speaker][wpm]
        print(f"  line{entry['line_num']:02d} [{speaker}] speakingRate={rate} -> {entry['text'][:60]!r}")
        raw_path = DIAGNOSTIC_DIR / f"line{entry['line_num']:02d}_{speaker}_{wpm}wpm_raw.mp3"
        trimmed_path = DIAGNOSTIC_DIR / f"line{entry['line_num']:02d}_{speaker}_{wpm}wpm_trimmed.mp3"
        audio_bytes = synthesize(entry["text"], VOICES[speaker], rate, api_key)
        raw_path.write_bytes(audio_bytes)
        trim_silence(raw_path, trimmed_path)

        raw_info = analyze_audio(raw_path)
        trimmed_info = analyze_audio(trimmed_path)
        print(f"    raw:     duration={raw_info.get('duration')} max_volume={raw_info.get('max_volume')} mean_volume={raw_info.get('mean_volume')}")
        print(f"    trimmed: duration={trimmed_info.get('duration')} max_volume={trimmed_info.get('max_volume')} mean_volume={trimmed_info.get('mean_volume')}")
        time.sleep(0.2)

    print(f"\nDiagnostic files written to: {DIAGNOSTIC_DIR}")
    print("These are objective level/clipping checks only -- perceived 'distortion' "
          "(warble, robotic artifacts at low speakingRate) is subjective and needs a human listen.")


def cmd_sweep(args):
    """Generate the same line across a range of speakingRate values so the
    user can pinpoint exactly where Chirp3 HD starts sounding warped."""
    api_key = get_api_key()
    entries = load_transcript()
    entry = entries[args.line - 1]
    speaker = entry["speaker"]
    voice_name = VOICES[speaker]
    rates = [float(r) for r in args.rates.split(",")]

    print(f"Sweep on line{entry['line_num']:02d} [{speaker}] voice={voice_name}: {entry['text']!r}\n")
    sweep_dir = DIAGNOSTIC_DIR / "sweep"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for rate in rates:
        rate_tag = f"{rate:.2f}".replace(".", "p")
        raw_path = sweep_dir / f"line{entry['line_num']:02d}_{speaker}_rate{rate_tag}_raw.mp3"
        trimmed_path = sweep_dir / f"line{entry['line_num']:02d}_{speaker}_rate{rate_tag}.mp3"
        audio_bytes = synthesize(entry["text"], voice_name, rate, api_key)
        raw_path.write_bytes(audio_bytes)
        trim_silence(raw_path, trimmed_path)
        info = analyze_audio(trimmed_path)
        print(f"  rate={rate:<5} duration={info.get('duration')} max_volume={info.get('max_volume')}  -> {trimmed_path.name}")
        paths.append(trimmed_path)
        time.sleep(0.2)

    print(f"\n{len(paths)} files written to: {sweep_dir}")


def cmd_batch(args):
    api_key = get_api_key()
    entries = load_transcript()
    wpm = args.wpm
    if wpm not in SPEAKING_RATE["attorney"]:
        sys.exit(f"wpm={wpm} has no calibrated speakingRate. Valid: {sorted(SPEAKING_RATE['attorney'])}")

    out_dir = TRIMMED_DIR / f"{wpm}wpm"
    out_dir.mkdir(parents=True, exist_ok=True)

    failures = []
    produced = []
    print(f"Generating {len(entries)} lines at {wpm} wpm "
          f"(attorney rate={SPEAKING_RATE['attorney'][wpm]}, witness rate={SPEAKING_RATE['witness'][wpm]})")

    for entry in entries:
        try:
            path = generate_line(entry, wpm, api_key, out_dir)
            produced.append(path)
            print(f"  OK   line{entry['line_num']:02d}_{entry['speaker']}_{wpm}wpm.mp3")
        except Exception as e:
            failures.append((entry["line_num"], entry["speaker"], str(e)))
            print(f"  FAIL line{entry['line_num']:02d}_{entry['speaker']}: {e}")
        time.sleep(0.15)

    print(f"\nDone: {len(produced)}/{len(entries)} succeeded.")
    if failures:
        print("Failures:")
        for num, speaker, err in failures:
            print(f"  line{num:02d} [{speaker}]: {err}")
        sys.exit(1)

    print(f"All {len(entries)} files written to: {out_dir}")

    print("\nQA pass: checking actual duration vs. target wpm for every file...")
    outliers = []
    for entry, path in zip(entries, produced):
        n = len(entry["text"].split())
        target = n / wpm * 60
        actual = get_duration_seconds(path)
        pct_err = abs(actual - target) / target * 100
        if pct_err > 15:
            outliers.append((entry["line_num"], entry["speaker"], target, actual, pct_err))
    if outliers:
        print(f"  {len(outliers)} line(s) more than 15% off target duration:")
        for num, speaker, target, actual, pct in outliers:
            print(f"    line{num:02d} [{speaker}]: target={target:.2f}s actual={actual:.2f}s ({pct:.0f}% off)")
    else:
        print("  All files within 15% of target duration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_diag = sub.add_parser("diagnostic", help="Generate a few lines directly at a given wpm to check raw TTS quality")
    p_diag.add_argument("--wpm", type=int, default=100)
    p_diag.add_argument("--count", type=int, default=2)
    p_diag.set_defaults(func=cmd_diagnostic)

    p_sweep = sub.add_parser("sweep", help="Generate one line across a range of speakingRate values")
    p_sweep.add_argument("--line", type=int, default=1, help="1-indexed line number from transcript.json")
    p_sweep.add_argument("--rates", type=str, default="1.0,0.9,0.85,0.8,0.75,0.7,0.65,0.6,0.54")
    p_sweep.set_defaults(func=cmd_sweep)

    p_batch = sub.add_parser("batch", help="Generate + trim the full 70-line transcript at one target wpm")
    p_batch.add_argument("--wpm", type=int, required=True)
    p_batch.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
