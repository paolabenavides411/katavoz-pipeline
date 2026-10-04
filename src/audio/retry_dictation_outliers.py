#!/usr/bin/env python3
"""Generalized outlier-retry pass for ANY dictation's audio (same convention
as the 250/275/300wpm and Luxe Nails outlier passes): for every line that
came out >15% off its target duration, re-roll the SAME fixed rate a few
more times and keep whichever attempt (existing file included) lands
closest -- the rate itself is never changed. Run this once after
gen_dictation_audio.py's first pass finishes for a given speed.

Usage:
  python3 src/audio/retry_dictation_outliers.py --transcript /path/to/transcript.json --key some_dictation_key 60 80 100
  python3 src/audio/retry_dictation_outliers.py --transcript /path/to/transcript.json --key some_dictation_key   # all TARGET_WPMS
"""
import argparse
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent.parent
TTS_PROJECT_DIR = PROJECT_ROOT / "tts_project"
sys.path.insert(0, str(TTS_PROJECT_DIR))
sys.path.insert(0, str(SRC_DIR))
import tts_generate as ttsgen  # noqa: E402
import generator as gen  # noqa: E402

AUDIO_BASE = PROJECT_ROOT / "dictation_player" / "audio"
EXTRA_ATTEMPTS = 3
OUTLIER_THRESHOLD = 0.15

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--transcript", type=str, required=True)
parser.add_argument("--key", type=str, required=True)
parser.add_argument("--swap-voices", action="store_true",
                     help="Must match whatever --swap-voices setting generated the existing files being "
                          "retried, so re-rolled attempts use the same voice/rate pairing as the originals.")
parser.add_argument("wpms", nargs="*", type=int, help="Speeds to check (default: all TARGET_WPMS)")
args = parser.parse_args()

if args.swap_voices:
    ttsgen.VOICES["attorney"], ttsgen.VOICES["witness"] = ttsgen.VOICES["witness"], ttsgen.VOICES["attorney"]
    gen.NATIVE_WPM["attorney"], gen.NATIVE_WPM["witness"] = gen.NATIVE_WPM["witness"], gen.NATIVE_WPM["attorney"]

entries = sorted(json.load(open(args.transcript)), key=lambda e: e["line_num"])
api_key = ttsgen.get_api_key()
out_base = AUDIO_BASE / args.key
tmp_dir = gen.TMP_DIR / f"_retry_{args.key}_outliers"
tmp_dir.mkdir(parents=True, exist_ok=True)

wpms = args.wpms or gen.TARGET_WPMS

for wpm in wpms:
    out_dir = out_base / f"{wpm}wpm"
    print(f"\n=== {wpm}wpm ===")
    improved, checked, still_outlier = 0, 0, 0
    for entry in entries:
        n_words = gen.spoken_word_count(entry["text"])
        target_duration = n_words / wpm * 60
        fname = f"line{entry['line_num']:02d}_{entry['speaker']}_{wpm}wpm.mp3"
        existing_path = out_dir / fname
        if not existing_path.exists():
            print(f"  MISSING: {fname}")
            continue
        existing_dur = ttsgen.get_duration_seconds(existing_path)
        existing_err = abs(existing_dur - target_duration) / target_duration
        if existing_err <= OUTLIER_THRESHOLD:
            continue
        checked += 1
        best_path, best_err = existing_path, existing_err
        best_bytes = existing_path.read_bytes()
        speaker = entry["speaker"]
        voice_name = ttsgen.VOICES[speaker]
        rate = gen.fixed_rate_for(speaker, wpm)
        for i in range(EXTRA_ATTEMPTS):
            if rate > 1.0:
                audio_bytes = gen.synthesize_at_fixed_rate(entry, rate, voice_name, api_key, tmp_dir)
            else:
                audio_bytes = gen.synthesize_paused(entry, wpm, rate, voice_name, api_key, tmp_dir)
            probe = tmp_dir / "probe.mp3"
            probe.write_bytes(audio_bytes)
            dur = ttsgen.get_duration_seconds(probe)
            err = abs(dur - target_duration) / target_duration
            if err < best_err:
                best_err = err
                best_bytes = audio_bytes
        if best_err < existing_err:
            existing_path.write_bytes(best_bytes)
            improved += 1
            print(f"  line{entry['line_num']:02d}_{speaker}: {existing_err*100:.1f}% -> {best_err*100:.1f}%"
                  f"{'  (still >15%)' if best_err > OUTLIER_THRESHOLD else ''}")
        else:
            print(f"  line{entry['line_num']:02d}_{speaker}: {existing_err*100:.1f}% (no improvement in {EXTRA_ATTEMPTS} tries)")
        if best_err > OUTLIER_THRESHOLD:
            still_outlier += 1
    print(f"{wpm}wpm: checked={checked} improved={improved} still_outlier={still_outlier}")

print("\ndone")
