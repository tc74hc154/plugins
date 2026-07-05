---
name: kicad-schematic-wiring
description: >-
  Drive the kicad MCP server (mcp__kicad__* tools) to place, wire, and re-lay-out
  components in a KiCad .kicad_sch file: convert net-label connections into real
  wires, bus multiple pins, relocate parts (dividers, decoupling, inductors) next
  to their IC pins, and verify the result with ERC + netlist + a rendered image.
  Use whenever the user asks to wire nets, connect pins, add/relocate a divider or
  decoupling cap, "make the schematic real wires instead of labels", tidy/relayout
  a schematic, or make it more readable in KiCad. Encodes hard-won gotchas
  (mid-span junctions, 1.27mm grid, a file-corrupting bug when adding power
  symbols, stale coordinates after manual edits) so the work does not have to be
  re-learned.
---

# KiCad schematic wiring & relayout (via the kicad MCP server)

This skill is for editing an existing `.kicad_sch` through the **kicad MCP server**
(tools named `mcp__kicad__*`). It captures the workflow and the traps that cost the
most time, so a fresh agent can wire/relayout a schematic quickly and safely.

## Prerequisites (verify once)

- The `kicad` MCP server is registered and connected (`claude mcp get kicad`).
  It runs on KiCad's bundled Python, which already has `kicad-skip`, `cairosvg`,
  `sexpdata`, `kipy`.
- `kicad-cli` is available (KiCad 10). Used for ERC and netlist export.
- Open the project first: `mcp__kicad__open_project` with the `.kicad_pro` path.
  All `*_schematic*` tools then take the `.kicad_sch` path directly (file-based;
  KiCad GUI need not be running).

## Golden rules (each one cost real time to learn — do not relearn them)

1. **NEVER add power symbols with `add_schematic_component`.** Adding
   `power:+5V` / `power:GND` / `power:+12V` corrupts the file: the writer mangles
   the symbol's `Description` string (`"… with name \"+5V\""`) into an unterminated
   quote, and KiCad then refuses to load the schematic. `kicad-cli` and a plain
   S-expr paren check may still pass — KiCad's parser is stricter.
   - To add a rail connection: **MOVE an existing power symbol**
     (`move_schematic_component` with its `#PWR…` ref) into place, or use
     `add_schematic_net_label` with `labelType: global_label` (`+5V` global label ==
     `+5V` power net). If you already corrupted a file, repair by fixing that one
     `Description` line (close the string), then reload.

2. **A pin touching the MIDDLE of a wire does NOT connect** — KiCad needs a
   junction there. Only wire *endpoints* connect a pin automatically. So bus
   several stacked pins as a **segmented polyline whose vertices land on each pin**:
   `add_schematic_wire waypoints=[[x,y1],[x,y2],[x,y3],[x,y4]]` — now every pin is a
   segment endpoint. A single straight `[[x,y1],[x,y4]]` leaves the middle pins
   unconnected (ERC: "Pin not connected"). There is no add-junction tool; segment
   the wire instead. (Landing a *new wire's endpoint* on an existing wire mid-span
   does auto-insert a junction — useful for tapping a bus.)

3. **Everything on the 1.27 mm grid.** Component centers and wire endpoints must be
   multiples of 1.27. Off-grid (e.g. 126.99 vs 127.0) → ERC "off connection grid"
   warnings and silent non-connections. Device R/C pins sit at center ±3.81 (=3×1.27).

4. **After the user hand-edits in KiCad, every cached coordinate is stale.**
   Re-query before touching anything: `get_schematic_pin_locations` (per IC),
   `list_schematic_components` (positions), `list_schematic_labels`,
   `list_schematic_wires`. To move a power symbol you need its `#PWR…` ref — get it
   from `list_schematic_components` filtered by `libId` (e.g. `power:GND`) and match
   by position.

5. **Generated passives are "decorated".** Each is: the component **+ 2 stub wires +
   2 labels/power symbols**. To relocate cleanly: delete the stubs + net labels
   (both the component side AND the matching label on the IC pin), **move the
   component and its power symbols**, then draw fresh wires. Deleting first, then
   moving the bare parts, then wiring, is far less error-prone than stretching wires.

## Workflow: convert labels → real wires / relocate a group

1. **Snapshot / back up** the `.kicad_sch` (copy the file) before bulk edits.
2. **Map the state** (rule 4): pin locations + component/label/wire/power lists.
   Build the net→pins→positions picture once.
3. **Per net / per component group:**
   - Delete old decorations: the IC-pin stub wire + its net label, and the
     component's stub wires + net labels. Keep power symbols (you will MOVE them).
   - `move_schematic_component` the part(s) next to the target pin; `move` the
     part's `#PWR…` power symbols to their new top/bottom.
   - Wire it: `add_schematic_wire`. Signal pin → IC pin (L-shaped waypoints to route
     around obstacles). Bus stacked pins as a segmented polyline (rule 2). For a
     rail end, wire to the moved power symbol.
   - For a net that spans far (e.g. VCC from a top-left pin to a bottom divider),
     a **net label pair is fine and standard** — do not force a long crossing wire.
4. **Verify** (see below) and **render** to eyeball the layout.

## Verification (do all three — cheap and catches everything)

- **ERC baseline-diff.** Run `mcp__kicad__run_erc` (or `kicad-cli sch erc`) on the
  *unedited* file first and record the count. Only **new** violations are yours.
  Typical pre-existing noise in a WIP board: unconfigured `my_power` / `lib`
  symbol libraries, and `power_pin_not_driven` on standby rails. Your goal: end at
  the baseline count, no new violations.
- **Netlist contract.** `kicad-cli sch export netlist` then assert each intended net
  contains exactly the expected pins — geometry-independent proof of connectivity.
  Use `scripts/check_netlist.py`. Note: nets you wire *without* a label get
  auto-named like `Net-(U2-BST)` — that is correct, not a problem.
- **Visual.** Render a region to a PNG **file** and `Read` it. Do NOT use
  `mcp__kicad__get_schematic_view` — it returns base64 inline and blows the tool
  token limit. Use `scripts/render_region.py <sch> --trim` (crops the empty page
  margins) or `--region fx1 fy1 fx2 fy2` (a fraction of the page, e.g.
  `0 0.02 0.42 0.72` for the lower-left), which writes a PNG and prints its path;
  then `Read` that path. One command instead of the Edge-headless + System.Drawing
  pixel-crop pipeline. NB: do NOT crop by `.kicad_sch` millimetres — KiCad's SVG
  page coordinates are offset from the schematic's internal coordinates.

## ERC / symbol-quality notes

- Buck-IC symbol pin types that keep ERC clean: `SW`/`BST` = `passive`,
  `VCC` (LDO output) = `power_out`; `VIN`/`PGND`/`AGND` stay `power_in`. This
  avoids spurious "Input Power pin not driven".
- Board supply rails taken from a connector (`+12V`, `GND`) have no power-*output*
  driving them → add one `PWR_FLAG` per such rail to satisfy ERC.
- Tie true `NC` pins with `add_no_connect` (not to GND) to avoid pin-to-pin warnings.

## Scripts

- `scripts/relocate_group.py` — **the high-level "one call" op.** Places a template
  group (currently a resistor `divider`) next to an IC pin and wires it: strips the
  group's old net (BFS from the anchor pin, so neighbouring nets are untouched),
  places a grid-aligned vertical stack, wires the internal mid node + power-end
  global labels + an orthogonal tap route to the pin, auto-inserts junctions
  (WireManager does this), keeps the Reference/Value text with the part, and
  self-checks with an ERC baseline-diff. Reuses the KiCAD-MCP-Server primitives
  (`PinLocator`, `WireManager`) and works in kicad-skip / raw `.kicad_sch`
  coordinates. This is the piece that turns ~12 manual MCP ops per divider into one.
  Known limits: `divider` template only; fixed-offset placement (no obstacle
  avoidance yet — the ERC/visual gate catches a bad spot, then rerun with `--gap`).
  Key implementation notes (skip gotchas): delete elements with `element.delete()`
  (NOT `collection._elements.remove()` — that does not persist through
  `overwrite()`); `WireManager` methods want a `pathlib.Path`, not a `str`; move a
  symbol's field text by the same delta as its `at` or the labels get left behind.
- `scripts/render_region.py` — kicad-cli SVG → cairosvg PNG → `--trim` / page-
  fraction crop → saves PNG, prints path. Run with KiCad's bundled python.
- `scripts/check_netlist.py` — assert intended net membership against an exported
  netlist (the "netlist contract" gate).

## See also

- `references/gotchas.md` — the traps above with more detail and symptoms.
