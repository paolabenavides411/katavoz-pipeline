#!/usr/bin/env python3
"""
Generate the "Luxe Nails & Spa" second-dictation audio at every calibrated
speed, using the exact same per-line fixed-rate synthesis (fixed_rate_for /
synthesize_paused / synthesize_at_fixed_rate / the jitter-retry convention)
that generator.py uses for Meridian Freight -- just pointed at a different
transcript and a different output folder
(dictation_player/audio/luxe_nails/{wpm}wpm/), so the existing pipeline
files and Meridian Freight's own audio are never touched.

Usage:
  cd tts_project && source .env.local && cd ..
  python3 src/audio/gen_luxe_nails.py --wpm all
  python3 src/audio/gen_luxe_nails.py --wpm 60,300
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

TRANSCRIPT_PATH = Path("/tmp/luxe_nails_tts.json")
OUT_BASE = PROJECT_ROOT / "dictation_player" / "audio" / "luxe_nails"


def load_entries():
    with open(TRANSCRIPT_PATH) as f:
        return json.load(f)


def generate_line(entry, wpm, api_key, out_dir):
    speaker = entry["speaker"]
    voice_name = ttsgen.VOICES[speaker]
    rate = gen.fixed_rate_for(speaker, wpm)
    fname = f"line{entry['line_num']:02d}_{speaker}_{wpm}wpm.mp3"
    tmp_dir = gen.TMP_DIR / "_luxe_linegen"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname

    n_words = len(entry["text"].split())
    target_duration = n_words / wpm * 60

    def attempt():
        if rate > 1.0:
            return gen.synthesize_at_fixed_rate(entry, rate, voice_name, api_key, tmp_dir)
        return gen.synthesize_paused(entry, wpm, rate, voice_name, api_key, tmp_dir)

    audio_bytes = attempt()
    probe_path = tmp_dir / "probe.mp3"
    probe_path.write_bytes(audio_bytes)
    error_pct = abs(ttsgen.get_duration_seconds(probe_path) - target_duration) / target_duration
    if error_pct > 0.20:
        retry_bytes = attempt()
        retry_path = tmp_dir / "probe_retry.mp3"
        retry_path.write_bytes(retry_bytes)
        retry_error_pct = abs(ttsgen.get_duration_seconds(retry_path) - target_duration) / target_duration
        if retry_error_pct < error_pct:
            audio_bytes = retry_bytes
            error_pct = retry_error_pct

    out_path.write_bytes(audio_bytes)
    return out_path, error_pct


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wpm", type=str, default="all")
    args = parser.parse_args()

    wpms = gen.TARGET_WPMS if args.wpm == "all" else [int(x) for x in args.wpm.split(",")]
    entries = sorted(load_entries(), key=lambda e: e["line_num"])
    api_key = ttsgen.get_api_key()

    print(f"Generating {len(entries)} lines x {len(wpms)} speeds for luxe_nails\n")
    for wpm in wpms:
        out_dir = OUT_BASE / f"{wpm}wpm"
        print(f"[{wpm}wpm]")
        outliers = []
        for entry in entries:
            path, err = generate_line(entry, wpm, api_key, out_dir)
            flag = "  <-- check" if err > 0.15 else ""
            if err > 0.15:
                outliers.append((entry["line_num"], entry["speaker"], err))
            print(f"  line{entry['line_num']:02d}_{entry['speaker']}: {err*100:.1f}% off target{flag}")
        print(f"  -> {out_dir}  ({len(outliers)} outlier(s) >15%)\n")

    print("done")


if __name__ == "__main__":
    main()
