#!/usr/bin/env python3
"""Netlist contract: assert intended net membership against a KiCad netlist.

Geometry-independent proof that a schematic is wired the way you intended. Because
locally-wired nets get auto-named (e.g. Net-(U2-BST)), each contract entry is
*anchored by a pin*: "the net that contains pin ANCHOR must contain exactly this set
of pins". Extra/missing pins are reported.

Export a netlist first:
  kicad-cli sch export netlist --output board.net board.kicad_sch

Usage:
  check_netlist.py board.net contract.json

contract.json example:
  {
    "U2.14": ["U2.14", "R2.2", "R3.1"],     # FB divider node
    "U2.1":  ["U2.1", "C11.1"],             # bootstrap
    "U2.6":  ["U2.6", "U2.19", "U2.20", "L1.1", "C11.2"]   # SW node
  }

Exits non-zero if any contract fails.
"""
import json
import re
import sys


def parse_nets(text):
    """Return list of (net_name, set_of 'REF.PIN')."""
    nets = []
    # split on net headers; each block lists nodes
    for block in re.split(r'\(net\s*\(code\s*"?\d+"?\)', text)[1:]:
        nm = re.search(r'\(name\s*"([^"]*)"\)', block)
        name = nm.group(1) if nm else "?"
        pins = {
            f"{r}.{p}"
            for r, p in re.findall(
                r'\(node\s*\(ref\s*"([^"]+)"\)\s*\(pin\s*"([^"]+)"\)', block
            )
        }
        nets.append((name, pins))
    return nets


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: check_netlist.py <netlist.net> <contract.json>")
    text = open(sys.argv[1], encoding="utf-8").read()
    contract = json.load(open(sys.argv[2], encoding="utf-8"))
    nets = parse_nets(text)

    ok = True
    for anchor, expected in contract.items():
        expected = set(expected) | {anchor}
        hit = [(name, pins) for name, pins in nets if anchor in pins]
        if not hit:
            print(f"FAIL  {anchor}: anchor pin not found in any net")
            ok = False
            continue
        name, pins = hit[0]
        missing = expected - pins
        extra = pins - expected
        if missing or extra:
            ok = False
            print(f"FAIL  {anchor} (net '{name}')")
            if missing:
                print(f"        missing: {sorted(missing)}")
            if extra:
                print(f"        extra:   {sorted(extra)}")
        else:
            print(f"PASS  {anchor} (net '{name}'): {sorted(pins)}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
