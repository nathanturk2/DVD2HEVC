"""Crop the transparent icon master and build a Windows multi-size ICO."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def build(source: Path, png: Path, ico: Path) -> None:
    image = Image.open(source).convert("RGBA")
    alpha = image.getchannel("A")
    bounds = alpha.point(lambda value: 255 if value >= 24 else 0).getbbox()
    if bounds is None:
        raise ValueError(f"Icon source has no visible pixels: {source}")
    subject = image.crop(bounds)

    # A five-percent safe area prevents Windows from visually clipping the
    # optical-disc outline while still keeping the symbol large at 16/32 px.
    master_size = 1024
    usable = int(master_size * 0.90)
    scale = min(usable / subject.width, usable / subject.height)
    fitted = subject.resize(
        (max(1, round(subject.width * scale)), max(1, round(subject.height * scale))),
        Image.Resampling.LANCZOS,
    )
    master = Image.new("RGBA", (master_size, master_size), (0, 0, 0, 0))
    master.alpha_composite(
        fitted,
        ((master_size - fitted.width) // 2, (master_size - fitted.height) // 2),
    )
    png.parent.mkdir(parents=True, exist_ok=True)
    master.save(png, optimize=True)
    master.save(ico, format="ICO", sizes=[(size, size) for size in ICON_SIZES])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("png", type=Path)
    parser.add_argument("ico", type=Path)
    args = parser.parse_args()
    build(args.source, args.png, args.ico)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
