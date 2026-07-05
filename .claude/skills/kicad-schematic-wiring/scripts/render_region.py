#!/usr/bin/env python3
"""Render a .kicad_sch (or an mm-region of it) to a PNG file and print the path.

Replaces the ad-hoc "export SVG -> Edge headless --screenshot -> System.Drawing
pixel crop" pipeline. Uses kicad-cli to export SVG, then cairosvg to rasterize and
PIL to crop by a millimetre bounding box. Prints ONLY the output path on success so
the caller can Read it directly.

Run with KiCad's bundled Python (it has cairosvg + PIL), e.g.:
  <KiCad>/bin/python.exe render_region.py board.kicad_sch --region 0 95 135 178

Usage:
  render_region.py <sch> [--out PNG] [--dpi N] [--region X1 Y1 X2 Y2]

--region is in schematic millimetres (top-left x1 y1, bottom-right x2 y2).
Omit it to render the whole sheet.
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile


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
    ap.add_argument(
        "--region", nargs=4, type=float, metavar=("X1", "Y1", "X2", "Y2"),
        help="crop box in schematic mm",
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
            ppm = a.dpi / 25.4  # px per mm
            x1, y1, x2, y2 = a.region
            box = (
                max(0, int(round(x1 * ppm))),
                max(0, int(round(y1 * ppm))),
                min(img.width, int(round(x2 * ppm))),
                min(img.height, int(round(y2 * ppm))),
            )
            img = img.crop(box)
        img.save(out)

    print(out)


if __name__ == "__main__":
    main()
