#!/usr/bin/env python3
"""
Generate per-speed dictation audio for ANY transcript, using the same
calibrated fixed-rate synthesis (fixed_rate_for / synthesize_paused /
synthesize_at_fixed_rate / jitter-retry) already proven out for Meridian
Freight and Luxe Nails & Spa. Generalized so a new dictation's audio can be
generated standalone -- on this machine or any other with the same repo,
Python deps, ffmpeg, and GOOGLE_TTS_API_KEY -- without needing a live
session to drive it turn-by-turn. The content/rule-citation work (choosing
the scenario, writing lines, verifying every citation against the real
style-guide-rules JSON) still has to happen with a model in the loop -- this
script is only the mechanical last mile.

Transcript JSON format (single-spaced text, NOT the double-spaced grading
copy in index.html -- see README below for the difference):
  [{"line_num": 1, "speaker": "attorney", "text": "..."}, ...]

Usage:
  cd tts_project && source .env.local && cd ..
  python3 src/audio/gen_dictation_audio.py --transcript /path/to/transcript.json --key some_dictation_key --wpm all
  python3 src/audio/gen_dictation_audio.py --transcript /path/to/transcript.json --key some_dictation_key --wpm 60,300

Output lands at dictation_player/audio/<key>/<wpm>wpm/lineNN_speaker_wpmwpm.mp3
-- <key> must match the `key` field of that dictation's entry in the
DICTATIONS array in dictation_player/index.html so audioPath() finds it.

--- README: single-spaced vs double-spaced text ---
The TEXT SYNTHESIZED here must be single-spaced (normal English spacing) --
double spaces don't change Chirp3 HD's prosody but do complicate word-count
math for nothing. The double-spaced convention (two spaces after a
sentence-ending period/semicolon) belongs ONLY in the TRANSCRIPT array
inside index.html, which is what the grading engine reads -- audio
generation and grading intentionally read from two differently-spaced
copies of the same words. Build this script's input JSON by taking the
index.html TRANSCRIPT entries and collapsing every run of 2+ spaces to one
before writing it out (see how /tmp/luxe_nails_tts.json was produced from
LUXE_NAILS_TRANSCRIPT for the exact one-liner).
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


def load_entries(transcript_path):
    with open(transcript_path) as f:
        return json.load(f)


def generate_line(entry, wpm, api_key, out_dir, tmp_dir):
    speaker = entry["speaker"]
    voice_name = ttsgen.VOICES[speaker]
    rate = gen.fixed_rate_for(speaker, wpm)
    fname = f"line{entry['line_num']:02d}_{speaker}_{wpm}wpm.mp3"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname

    n_words = gen.spoken_word_count(entry["text"])
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
    parser.add_argument("--transcript", type=str, required=True, help="Path to single-spaced transcript JSON")
    parser.add_argument("--key", type=str, required=True, help="Dictation key -- output goes to audio/<key>/")
    parser.add_argument("--wpm", type=str, default="all")
    parser.add_argument("--attorney-voice", type=str, default="achird", choices=sorted(gen.VOICE_PROFILES),
                         help="Which voice (any key in generator.py's VOICE_PROFILES) plays attorney for this "
                              "dictation. Default: achird, this project's original pairing.")
    parser.add_argument("--witness-voice", type=str, default="achernar", choices=sorted(gen.VOICE_PROFILES),
                         help="Which voice plays witness for this dictation. Default: achernar. Voices are a "
                              "freely mixable pool, not a fixed pair or a binary swap -- e.g. "
                              "--attorney-voice achird --witness-voice aoede, or "
                              "--attorney-voice achernar --witness-voice charon, are both valid, not just the "
                              "original achird/achernar pairing or a straight swap of it.")
    args = parser.parse_args()

    gen.set_voice_pair(args.attorney_voice, args.witness_voice)
    print(f"Voices: attorney={args.attorney_voice} ({ttsgen.VOICES['attorney']})  "
          f"witness={args.witness_voice} ({ttsgen.VOICES['witness']})\n")

    wpms = gen.TARGET_WPMS if args.wpm == "all" else [int(x) for x in args.wpm.split(",")]
    entries = sorted(load_entries(args.transcript), key=lambda e: e["line_num"])
    api_key = ttsgen.get_api_key()
    out_base = AUDIO_BASE / args.key
    tmp_dir = gen.TMP_DIR / f"_{args.key}_linegen"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating {len(entries)} lines x {len(wpms)} speeds for '{args.key}' -> {out_base}\n")
    for wpm in wpms:
        out_dir = out_base / f"{wpm}wpm"
        print(f"[{wpm}wpm]")
        outliers = []
        for entry in entries:
            path, err = generate_line(entry, wpm, api_key, out_dir, tmp_dir)
            flag = "  <-- check" if err > 0.15 else ""
            if err > 0.15:
                outliers.append((entry["line_num"], entry["speaker"], err))
            print(f"  line{entry['line_num']:02d}_{entry['speaker']}: {err*100:.1f}% off target{flag}")
        print(f"  -> {out_dir}  ({len(outliers)} outlier(s) >15%)\n")

    print("done")


if __name__ == "__main__":
    main()
