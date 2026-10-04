#!/usr/bin/env python3
"""
Regenerates the two-voice intro clips (dictation_player/audio/intro/
intro_attorney.mp3, intro_witness.mp3) with new orienting text -- explains
this is a two-voice testimony and lets the student hear both voices by ear
before practice starts. Does NOT touch the countdown clip or any of the
locked per-speed dictation audio.

Reuses tts_generate.py's own synthesize()/VOICES/trim approach for
consistency with the rest of the project's audio.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tts_generate import synthesize, get_api_key, VOICES, trim_silence, FFMPEG_BIN

OUT_DIR = Path(__file__).resolve().parent.parent / "dictation_player" / "audio" / "intro"

LINES = {
    "attorney": "This is a two-voice testimony. I am the questioning attorney.",
    "witness": "And I am the witness answering the questions. Listen carefully, and transcribe exactly what you hear.",
}

def main():
    api_key = get_api_key()
    tmp_dir = OUT_DIR / "_tmp_gen"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for speaker, text in LINES.items():
        print(f"[{speaker}] synthesizing: {text!r}")
        raw_bytes = synthesize(text, VOICES[speaker], 1.0, api_key)
        raw_path = tmp_dir / f"intro_{speaker}_raw.mp3"
        raw_path.write_bytes(raw_bytes)

        final_path = OUT_DIR / f"intro_{speaker}.mp3"
        trim_silence(raw_path, final_path)
        print(f"[{speaker}] wrote {final_path}")

    for f in tmp_dir.glob("*"):
        f.unlink()
    tmp_dir.rmdir()
    print("done")

if __name__ == "__main__":
    main()
