#!/usr/bin/env python3
"""relocate_group: place a template component-group next to an IC pin and wire it.

Composes the KiCAD-MCP-Server's kicad-skip primitives (PinLocator + WireManager)
into the high-level "place near pin -> clean up old wiring -> real-wire ->
auto-junction -> verify" operation that was the slowest part of a manual relayout.

Implemented template: 'divider' (two series resistors, a middle tap routed to an IC
pin, and a power net at each end). Power ends are emitted as GLOBAL LABELS rather
than power symbols (both to dodge the add_schematic_component corruption bug and to
avoid the fragile power-symbol bookkeeping).

IMPORTANT coordinate note: this works entirely in kicad-skip / raw-.kicad_sch
coordinates (which PinLocator also uses). Those differ from the coordinates the
kicad MCP tools report/accept, so do not mix numbers from get_schematic_pin_locations
into here — always fetch positions through PinLocator.

Run with KiCad's bundled Python:
  <KiCad>/bin/python.exe relocate_group.py board.kicad_sch \
      --template divider --top R6 --bot R7 --top-net +12V --bot-net GND \
      --anchor U2.12 --side L

Set KICAD_MCP_PY if the server checkout is not at the default path.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

SERVER = os.environ.get("KICAD_MCP_PY", r"C:\MyItem\1_dev\KiCAD-MCP-Server\python")
sys.path.insert(0, SERVER)
from skip import Schematic                       # noqa: E402
from commands.pin_locator import PinLocator      # noqa: E402
from commands.wire_manager import WireManager    # noqa: E402

GRID = 1.27


def g(v):
    return round(v / GRID) * GRID


def _near(a, b, t=0.3):
    return abs(a[0] - b[0]) <= t and abs(a[1] - b[1]) <= t


def _find_kicad_cli():
    for c in (os.environ.get("KICAD_CLI"),
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\KiCad\10.0\bin\kicad-cli.exe")):
        if c and os.path.exists(c):
            return c
    return "kicad-cli"


def _erc_count(path):
    """Total ERC violations (errors+warnings) reported by kicad-cli, or -1 on failure."""
    r = subprocess.run([_find_kicad_cli(), "sch", "erc", "--exit-code-violations",
                        "--output", os.devnull, path],
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    import re
    m = re.search(r"Found (\d+) violation", out)
    return int(m.group(1)) if m else (0 if "No " in out else -1)


def relocate_divider(path, top_ref, bot_ref, top_net, bot_net,
                     anchor_ref, anchor_pin, side="L", gap=12.7):
    path = str(path)
    pl = PinLocator()
    anchor = pl.get_pin_location(path, anchor_ref, anchor_pin)
    if not anchor:
        raise SystemExit(f"anchor pin {anchor_ref}.{anchor_pin} not found")

    # current group pin positions (raw coords) — needed to strip the old wiring
    old_pins = []
    for ref in (top_ref, bot_ref):
        for pn in ("1", "2"):
            xy = pl.get_pin_location(path, ref, pn)
            if xy:
                old_pins.append(tuple(xy))
    gcx = sum(p[0] for p in old_pins) / len(old_pins)
    gcy = sum(p[1] for p in old_pins) / len(old_pins)

    # ---- 1) strip the group's old wiring, net-scoped so we do not touch neighbours:
    #        (a) wires touching a group pin (mid node, power stubs, tap's group side)
    #        (b) every wire electrically reachable from the anchor pin (the tap net,
    #            including any L-route jog segments that touch no pin)
    #        (c) labels at group pins, and the group's now-orphaned power symbols
    sch = Schematic(path)
    seeds = {(round(p[0], 2), round(p[1], 2)) for p in old_pins}
    seeds.add((round(anchor[0], 2), round(anchor[1], 2)))
    remaining = list(sch.wire)
    changed = True
    while changed:
        changed = False
        for w in list(remaining):
            eps = [tuple(xy.value) for xy in w.points]
            if any(_near(ep, sp) for ep in eps for sp in seeds):
                for ep in eps:
                    seeds.add((round(ep[0], 2), round(ep[1], 2)))
                w.delete()
                remaining.remove(w)
                changed = True
    for lbl in list(getattr(sch, "label", []) or []):
        if any(_near(lbl.at.value[:2], op) for op in old_pins):
            lbl.delete()
    for s in list(sch.symbol):
        if s.lib_id.value.startswith("power:"):
            sx, sy = s.at.value[0], s.at.value[1]
            if abs(sx - gcx) <= 9 and abs(sy - gcy) <= 18:
                s.delete()

    # ---- 2) place the two resistors as a vertical stack beside the anchor pin
    ax, ay = anchor
    gx = g(ax - gap) if side == "L" else g(ax + gap) if side == "R" else g(ax)
    gy_top = g(ay - 3.81)
    gy_bot = gy_top + 11.43          # leaves a 3.81 mid-node gap between the resistors
    targets = {top_ref: [gx, gy_top, 0], bot_ref: [gx, gy_bot, 0]}
    for s in sch.symbol:
        r = s.property.Reference.value.rstrip("_")
        if r not in targets:
            continue
        ox, oy = s.at.value[0], s.at.value[1]
        nx, ny, nrot = targets[r]
        dx, dy = nx - ox, ny - oy
        s.at.value = [nx, ny, nrot]
        for prop in s.property:            # keep Reference/Value/... text with the part
            try:
                pv = prop.at.value
                prop.at.value = [pv[0] + dx, pv[1] + dy] + list(pv[2:])
            except Exception:
                pass
    sch.overwrite()

    # ---- 3) wire it (WireManager auto-inserts junctions + snaps to grid)
    top_top = [gx, gy_top - 3.81]
    top_bot = [gx, gy_top + 3.81]
    bot_top = [gx, gy_bot - 3.81]
    bot_bot = [gx, gy_bot + 3.81]
    P = Path(path)                             # WireManager needs a Path, not str
    WireManager.add_polyline_wire(P, [top_bot, bot_top])                 # mid node
    WireManager.add_label(P, top_net, top_top, label_type="global_label")
    WireManager.add_label(P, bot_net, bot_bot, label_type="global_label")
    tap = [gx, g(gy_top + 5.08)]                                         # on the mid wire
    route = WireManager.create_orthogonal_path(tap, list(anchor), prefer_horizontal_first=True)
    WireManager.add_polyline_wire(P, route)                             # tap -> IC pin

    return dict(anchor=anchor, gx=gx, gy_top=gy_top, gy_bot=gy_bot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sch")
    ap.add_argument("--template", default="divider", choices=["divider"])
    ap.add_argument("--top", required=True, help="top resistor ref")
    ap.add_argument("--bot", required=True, help="bottom resistor ref")
    ap.add_argument("--top-net", required=True)
    ap.add_argument("--bot-net", required=True)
    ap.add_argument("--anchor", required=True, help="REF.PIN, e.g. U2.12")
    ap.add_argument("--side", default="L", choices=["L", "R"])
    ap.add_argument("--gap", type=float, default=12.7)
    a = ap.parse_args()

    base = _erc_count(a.sch)
    aref, apin = a.anchor.split(".")
    info = relocate_divider(a.sch, a.top, a.bot, a.top_net, a.bot_net,
                            aref, apin, side=a.side, gap=a.gap)
    after = _erc_count(a.sch)

    print(f"placed {a.top}/{a.bot} at x={info['gx']} y={info['gy_top']}..{info['gy_bot']}, "
          f"tap -> {a.anchor} {info['anchor']}")
    print(f"ERC: baseline={base}  after={after}  "
          f"({'OK: no new violations' if after <= base else 'REGRESSION: +%d' % (after - base)})")
    sys.exit(0 if after <= base else 1)


if __name__ == "__main__":
    main()
