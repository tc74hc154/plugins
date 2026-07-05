#!/usr/bin/env python3
"""Render a .kicad_sch to a PNG file and print the path.

Replaces the ad-hoc "export SVG -> Edge headless --screenshot -> System.Drawing
pixel crop" pipeline. Uses kicad-cli to export SVG, then cairosvg to rasterize.
Prints ONLY the output path on success so the caller can Read it directly.

Run with KiCad's bundled Python (it has cairosvg + PIL), e.g.:
  <KiCad>/bin/python.exe render_region.py board.kicad_sch --trim

Usage:
  render_region.py <sch> [--out PNG] [--dpi N] [--trim] [--region fx1 fy1 fx2 fy2]

Zooming:
  --trim            crop away the empty page margins (content bounding box). Best
                    default for a readable "show me the schematic" view.
  --region f...     crop to a fraction of the page: x1 y1 x2 y2 each in 0..1
                    (e.g. 0 0.45 0.5 1.0 = lower-left quadrant). Coordinate-system
                    safe.
NOTE: cropping by *.kicad_sch millimetres is intentionally NOT supported — KiCad's
SVG page coordinates are offset from the schematic's internal coordinates, so an
mm box taken from get_schematic_pin_locations does not line up with the render.
Use --trim, or pick a fraction from the full image.
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile


def content_bbox(img, pad=20):
    """Bounding box of non-background pixels (background = top-left corner colour)."""
    from PIL import Image, ImageChops
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg)
    bbox = diff.getbbox()
    if not bbox:
        return None
    x1, y1, x2, y2 = bbox
    return (max(0, x1 - pad), max(0, y1 - pad),
            min(img.width, x2 + pad), min(img.height, y2 + pad))


def find_kicad_cli():
    for c in (
        os.environ.get("KICAD_CLI"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\KiCad\10.0\bin\kicad-cli.exe"),
        r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
        "/usr/bin/kicad-cli",
    ):
        if c and os.path.exists(c):
            return c
    return "kicad-cli"  # hope it is on PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sch", help="path to .kicad_sch")
    ap.add_argument("--out", help="output PNG path (default: <sch>_render.png)")
    ap.add_argument("--dpi", type=float, default=200.0)
    ap.add_argument("--trim", action="store_true",
                    help="crop away empty page margins (content bounding box)")
    ap.add_argument(
        "--region", nargs=4, type=float, metavar=("FX1", "FY1", "FX2", "FY2"),
        help="crop to a fraction of the page, each value in 0..1",
    )
    a = ap.parse_args()

    sch = os.path.abspath(a.sch)
    if not os.path.exists(sch):
        sys.exit(f"no such schematic: {sch}")
    out = a.out or os.path.splitext(sch)[0] + "_render.png"

    try:
        import cairosvg  # noqa: F401
        from PIL import Image
    except ImportError as e:
        sys.exit(f"run me with KiCad's bundled python (needs cairosvg + PIL): {e}")

    with tempfile.TemporaryDirectory() as td:
        cli = find_kicad_cli()
        r = subprocess.run(
            [cli, "sch", "export", "svg", "--output", td, sch],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.exit(f"kicad-cli sch export svg failed: {r.stderr or r.stdout}")
        svgs = glob.glob(os.path.join(td, "*.svg"))
        if not svgs:
            sys.exit("kicad-cli produced no SVG")
        full = os.path.join(td, "full.png")
        cairosvg.svg2png(url=svgs[0], write_to=full, dpi=a.dpi)
        img = Image.open(full)
        if a.region:
            fx1, fy1, fx2, fy2 = a.region
            img = img.crop((
                int(fx1 * img.width), int(fy1 * img.height),
                int(fx2 * img.width), int(fy2 * img.height),
            ))
        elif a.trim:
            bbox = content_bbox(img)
            if bbox:
                img = img.crop(bbox)
        img.save(out)

    print(out)


if __name__ == "__main__":
    main()
