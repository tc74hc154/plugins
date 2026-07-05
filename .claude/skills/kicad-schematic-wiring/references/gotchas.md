# KiCad MCP schematic-editing gotchas (symptoms → cause → fix)

Field notes from real sessions. Each of these silently wasted time.

## 1. Adding a power symbol corrupts the file
- **Symptom:** after `add_schematic_component` with `power:+5V`/`GND`/`+12V`, the
  next tool call fails with "Failed to load schematic"; `add_schematic_wire` fails
  with `'NoneType' object has no attribute 'start'`.
- **Cause:** the written `Description` property becomes
  `"Power symbol creates a global label with name \"` — an unterminated string that
  swallows following tokens. A bare paren-balance check still passes; KiCad does not.
- **Fix:** never add power symbols this way. MOVE an existing `#PWR…` symbol, or use
  `add_schematic_net_label labelType=global_label`. To repair a corrupted file, edit
  that one `Description` line to a valid closed string and reload.

## 2. Mid-span pins do not connect
- **Symptom:** ERC "Pin not connected" for the *middle* pins of a stack, even though
  a wire visually passes through them. Endpoint pins connect fine.
- **Cause:** KiCad only auto-connects a pin at a wire *endpoint* (or where a junction
  exists). A pin under a straight wire's middle is not connected.
- **Fix:** draw the bus as a segmented polyline whose vertices are the pins:
  `waypoints=[[x,y1],[x,y2],[x,y3],[x,y4]]`. To *tap* an existing wire, draw a new
  wire whose endpoint lands on it — that auto-inserts a junction.

## 3. Off-grid endpoints
- **Symptom:** ERC "Symbol pin or wire end off connection grid"; sometimes silent
  non-connection.
- **Cause:** a coordinate not a multiple of 1.27 (e.g. center 126.99 instead of
  127.0). Device R/C/L pins are at center ±3.81.
- **Fix:** snap all centers/endpoints to 1.27 multiples. `move_schematic_component`
  with `preserveWires=true` re-snaps connected wire ends when you fix a center.

## 4. Stale coordinates after a manual edit
- **Symptom:** deletes/moves target nothing or the wrong thing; wires land in空白.
- **Cause:** the user moved parts in the KiCad GUI between turns; your cached
  positions are wrong.
- **Fix:** re-query `get_schematic_pin_locations`, `list_schematic_components`,
  `list_schematic_labels`, `list_schematic_wires` before editing. Positions→refs for
  power symbols: `list_schematic_components filter libId=power:GND` (etc.).

## 5. The decoration structure
- A generated passive = component + 2 stub wires + 2 labels/power symbols. Both the
  component AND the matching IC-pin have a net label. Relocating "the component"
  alone leaves orphaned stubs/labels and a broken net.
- **Fix pattern:** delete stubs+labels on both sides → move component + its power
  symbols → add fresh wires. `move` with `preserveWires` stretches wires (ugly for a
  relocate); prefer delete-then-move-then-rewire for clean results.

## 6. Rendering returns base64 that is too large
- **Symptom:** `get_schematic_view` result "exceeds maximum allowed tokens".
- **Cause:** the tool embeds the PNG as inline base64 in the JSON result.
- **Fix:** render to a FILE and `Read` the path. `scripts/render_region.py` uses
  kicad-cli SVG + cairosvg (both present) and crops by **mm** bounding box — no
  Edge-headless, no pixel math, no `--force-device-scale-factor`, no System.Drawing.

## 7. ERC noise vs. real regressions
- A WIP board often carries pre-existing ERC violations: unconfigured `my_power` /
  `lib` symbol libraries, `power_pin_not_driven` on `+5VSB`/`+12VSB`, a
  `multiple_net_names` on tied ATX/PSON labels, an isolated status label.
- **Fix:** diff against a baseline ERC of the untouched file. Only *new* violations
  are yours. Do not chase the pre-existing ones unless asked.

## 8. Nets you wire without labels get auto-named
- After removing `BST`/`FB` labels and wiring pin-to-pin, the netlist shows
  `Net-(U2-BST)`, `Net-(U2-FB)`. That is correct and expected for local unlabeled
  nets — verify by membership (pins on the net), not by name.
