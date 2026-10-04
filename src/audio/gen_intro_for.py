#!/usr/bin/env python3
"""
Generalized version of tts_project/gen_intro.py: generates the two-voice
intro clip for a SPECIFIC dictation key, optionally with swapped voices --
needed because the shared intro under audio/intro/ was generated with the
default voice mapping (Achird=attorney, Achernar=witness), which is WRONG
for any dictation (like greenmart_theft) that swaps which voice plays which
role. Output goes to audio/<key>/intro/, matching the same per-dictation
layout as the numbered lines and the shared audio/intro/ files that the
default-voice dictations already have copies of there too.

Usage:
  cd tts_project && source .env.local && cd ..
  python3 src/audio/gen_intro_for.py --key greenmart_theft --attorney-voice achernar --witness-voice achird
  python3 src/audio/gen_intro_for.py --key some_other_key
"""
import argparse
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent.parent
TTS_PROJECT_DIR = PROJECT_ROOT / "tts_project"
sys.path.insert(0, str(TTS_PROJECT_DIR))
sys.path.insert(0, str(SRC_DIR))

from tts_generate import synthesize, get_api_key, VOICES, trim_silence  # noqa: E402
import generator as gen  # noqa: E402

LINES = {
    "attorney": "This is a two-voice testimony. I am the questioning attorney.",
    "witness": "And I am the witness answering the questions. Listen carefully, and transcribe exactly what you hear.",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", type=str, required=True)
    parser.add_argument("--attorney-voice", type=str, default="achird", choices=sorted(gen.VOICE_PROFILES))
    parser.add_argument("--witness-voice", type=str, default="achernar", choices=sorted(gen.VOICE_PROFILES))
    args = parser.parse_args()

    gen.set_voice_pair(args.attorney_voice, args.witness_voice)
    print(f"Voices: attorney={args.attorney_voice} ({VOICES['attorney']})  "
          f"witness={args.witness_voice} ({VOICES['witness']})")

    out_dir = PROJECT_ROOT / "dictation_player" / "audio" / args.key / "intro"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "_tmp_gen"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    api_key = get_api_key()
    for speaker, text in LINES.items():
        print(f"[{speaker}] synthesizing: {text!r}")
        raw_bytes = synthesize(text, VOICES[speaker], 1.0, api_key)
        raw_path = tmp_dir / f"intro_{speaker}_raw.mp3"
        raw_path.write_bytes(raw_bytes)

        final_path = out_dir / f"intro_{speaker}.mp3"
        trim_silence(raw_path, final_path)
        print(f"[{speaker}] wrote {final_path}")

    for f in tmp_dir.glob("*"):
        f.unlink()
    tmp_dir.rmdir()
    print("done")


if __name__ == "__main__":
    main()
