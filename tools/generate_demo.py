#!/usr/bin/env python3
"""Generate tiny synthetic preflight examples; never overwrite existing files."""

from pathlib import Path
import sys
import wave


def generate(destination: Path) -> None:
    # Require a new directory so running the demo never overwrites someone's assets.
    destination.mkdir(parents=True, exist_ok=False)
    examples = [
        ("SFX_UI_Click_01.wav", 48000, 2, 1, 0.1),
        ("nested/SFX_Footstep_Concrete_01.wav", 48000, 3, 2, 0.1),
        ("SFX_Explosion_03.wav", 44100, 2, 1, 0.1),
        ("footstep-final.wav", 48000, 2, 1, 0.1),
        ("SFX_UI_Tiny_01.wav", 48000, 2, 1, 0.01),
        ("SFX_UI_Empty_01.wav", 48000, 2, 1, 0),
        ("SFX_UI_LowDepth_01.wav", 48000, 1, 1, 0.1),
        ("SFX_Ambience_Surround_01.wav", 48000, 2, 6, 0.1),
    ]
    for name, rate, width, channels, duration in examples:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as audio:
            audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            # Synthetic silence; these files demonstrate metadata, not sound design.
            sample = b"\x80" if width == 1 else b"\x00" * width
            audio.writeframes(sample * channels * int(rate * duration))
    (destination / "SFX_UI_Broken_01.wav").write_bytes(b"not a WAV")
    (destination / "unsupported.mp3").write_bytes(b"placeholder, not encoded audio")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("demo_audio")
    try:
        generate(target)
    except OSError as error:
        raise SystemExit(f"Cannot create demo: {error}")
    print(f"Created 10 synthetic examples in {target}")
