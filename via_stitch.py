"""選択したベタ（ゾーン）にビアを敷き詰める（ビアステッチング）

ベタを選択して実行すると、ルールに適合する範囲でできるだけ多くの
スルーホールビアをベタの中に打つ。ビアのネットはベタのネットになる。

- 配置は千鳥（六角格子）。間隔 0 = ルール上の最小間隔
  （ビア径と「ドリル径 + 穴間の最小距離」の大きい方）で最も密になる
- 全銅箔層の他ネットの配線・パッド・ビアとのクリアランス、穴同士の
  最小距離、基板端クリアランス、ビア禁止ルールエリアを避ける
- パッドの境界線とレジスト開口の境界線には「境界マージン」より近づかない。
  同ネットのパッドは「同ネットのパッド上も可」でビア・イン・パッドを許可
  でき、その場合も境界からマージン以上内側に完全に収まる位置にだけ置く。
  マージン 0 = 自動（2×マスク拡張 + マスク最小幅、下限 0.1mm）
- 他ネットのベタは避けない（塗り直せばビアを避けて充填されるため）。
  「作成後にベタを塗り直す」を付けたまま実行すれば整合が取れる
- ベタが未塗りつぶし・塗りが古い場合も、実行時に対象ベタを塗り直して
  から配置するので、実際に銅がある場所にだけビアが載る

複数のベタを選択した場合は順に処理する（先に置いたビアも避ける）。
"""
import math

import pcbnew

try:
    import wx
except ImportError:
    wx = None

import track_shorten as _ts

ICON = "🪡"                    # パレットのタイルに表示するアイコン
LABEL = "ビアを敷き詰める"      # パレットのタイルに表示する短い名前

PRESET = None  # パレットが実行直前に渡すパラメータ(Run()が消費、無ければダイアログ)

# 既定値は JLCPCB 2層エコノミーで追加費用なしの最高密度 (capabilities 2026-08確認):
# ドリル0.15mmは常に有料、0.2/0.25mmは「ビア径0.45mm以上」なら無料。
# ビア穴間の最小距離は0.2mm。最小ピッチ = max(ビア径, ドリル+穴間) なので
# 0.45/0.25 でピッチ0.45mmが最密 (0.3/0.4 だと0.5mm)。
JLC_VIA_MM = 0.45
JLC_DRILL_MM = 0.25
JLC_VIA_H2H_MM = 0.2


def _via_defaults():
    """ビア径/ドリルの既定値 [mm] = JLCPCB 2層エコノミーの最密。"""
    return JLC_VIA_MM, JLC_DRILL_MM


def PANEL():  # パレット埋め込みUIの定義(pack_launcher が参照。規約はREADME)
    via, drill = _via_defaults()
    return [
        {"type": "number", "key": "via", "label": "ビア径", "unit": "mm",
         "default": via, "min": 0.2, "max": 3.0, "step": 0.05},
        {"type": "number", "key": "drill", "label": "ドリル", "unit": "mm",
         "default": drill, "min": 0.1, "max": 2.0, "step": 0.05},
        {"type": "number", "key": "pitch", "label": "間隔(0=最小)", "unit": "mm",
         "default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1},
        {"type": "number", "key": "margin", "label": "境界から(0=自動)",
         "unit": "mm", "default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05},
        {"type": "check", "key": "in_pad", "label": "同ネットのパッド上も可",
         "default": True},
        {"type": "check", "key": "refill", "label": "作成後にベタを塗り直す",
         "default": True},
        {"type": "run", "label": "敷き詰める"},
    ]


def collect_selected_zones(board):
    """選択中のベタ（ルールエリアは除く）。"""
    zones = []
    try:  # GUIの選択はまずこれで拾う（ヘッドレスでは空になる）
        for item in pcbnew.GetCurrentSelection():
            if isinstance(item, pcbnew.ZONE) and not item.GetIsRuleArea():
                zones.append(item)
    except Exception:
        pass
    if not zones:
        zones = [z for z in board.Zones()
                 if z.IsSelected() and not z.GetIsRuleArea()]
    seen, out = set(), []
    for z in zones:
        u = z.m_Uuid.AsString()
        if u not in seen:
            seen.add(u)
            out.append(z)
    return out


def min_pitch(board, via_nm, drill_nm):
    """新しいビア同士に許される最小間隔。

    新ビアは全て同ネットなので銅クリアランスは掛からない。効くのは
    穴間の最小距離と、ビア（銅）同士を重ねない、の2つ。
    穴間は基板設定とJLCPCBのビア穴間0.2mmの厳しい方を使う
    （基板設定が0や未設定でも製造限界は割らない）。
    """
    h2h = 0
    try:
        h2h = max(board.GetDesignSettings().m_HoleToHoleMin, 0)
    except Exception:
        pass
    h2h = max(h2h, pcbnew.FromMM(JLC_VIA_H2H_MM))
    return max(via_nm, drill_nm + h2h) + pcbnew.FromMM(0.01)


def _auto_margin(board):
    """パッド/レジスト開口の境界からビアを離す距離の自動値。

    ビアと相手の開口が両方ともマスク拡張ぶん銅より広がっても、間に
    製造可能な幅のレジストが残る距離 = 2×マスク拡張 + マスク最小幅。
    基板設定が0のときは下限 0.1mm。
    """
    exp = minw = 0
    try:
        bds = board.GetDesignSettings()
        exp = max(getattr(bds, "m_SolderMaskExpansion", 0), 0)
        minw = max(getattr(bds, "m_SolderMaskMinWidth", 0), 0)
    except Exception:
        pass
    return max(2 * exp + minw, pcbnew.FromMM(0.1))


def _pad_mask_expansion(pad):
    """パッドのレジスト開口の広がり（基板既定→パッド個別の解決済み）。

    マスク定義パッドでは負値（開口が銅より狭い）になる。
    開口が無い（マスク層に居ない）パッドは 0 = 銅の境界だけ見る。
    """
    for layer in (pcbnew.F_Mask, pcbnew.B_Mask):
        try:
            if pad.IsOnLayer(layer):
                return pad.GetSolderMaskExpansion(layer)
        except Exception:
            pass
    return 0


def _pad_inside_polys(pad, layers, deflate):
    """ビア・イン・パッドが許される領域 = パッド銅形状を deflate 縮めた層別
    ポリゴン群。ビアはパッドが居る全銅箔層で収まる必要がある。
    どこかの層で収まらない（縮めたら消える）パッドは None = 不可。"""
    polys = []
    for layer in layers:
        if not pad.IsOnLayer(layer):
            continue
        try:
            poly = pcbnew.SHAPE_POLY_SET(
                pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE))
        except Exception:
            return None
        poly.Deflate(deflate, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                     pcbnew.FromMM(0.01))
        if not poly.OutlineCount():
            return None
        poly.BuildBBoxCaches()
        polys.append(poly)
    return polys or None


def _refill(board, zones):
    try:
        zs = pcbnew.ZONES()
        for z in zones:
            zs.append(z)
        pcbnew.ZONE_FILLER(board).Fill(zs)
        return True
    except Exception:
        return False


class _HoleIndex:
    """穴の空間グリッド索引。候補ごとの穴距離チェックをO(近傍)にする。

    (置いたビアの数だけ穴が増えるので、線形走査だと大きいベタで
    O(候補数×ビア数) になり数十秒固まる。)
    """

    CELL = pcbnew.FromMM(4)

    def __init__(self, drill_r, h2h):
        self.grid = {}
        self.big = []            # 検査距離がセルを超える大穴だけ線形走査
        self.reach = self.CELL - drill_r - h2h  # gridに入れられる最大穴半径
        self.drill_r = drill_r
        self.h2h = h2h

    def add(self, x, y, r):
        if r > self.reach:
            self.big.append((x, y, r))
        else:
            self.grid.setdefault((x // self.CELL, y // self.CELL),
                                 []).append((x, y, r))

    def blocked(self, x, y):
        """(x,y)に新しい穴を開けると近すぎる既存穴があるか。"""
        need = self.drill_r + self.h2h
        cx, cy = x // self.CELL, y // self.CELL
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for hx, hy, hr in self.grid.get((cx + dx, cy + dy), ()):
                    if math.hypot(hx - x, hy - y) < need + hr:
                        return True
        for hx, hy, hr in self.big:
            if math.hypot(hx - x, hy - y) < need + hr:
                return True
        return False


def _collect_obstacles(snap, net, via_r, drill_r, region, margin, in_pad):
    """(shape, bboxタプル, 実効クリアランス, 許可領域) のリストと穴索引と
    ビア・イン・パッド許可領域のリストを作る。

    ビアは貫通なので、どの銅箔層の障害物も1本のリストにまとめてよい
    (どれか1層でも衝突すれば置けない)。
    実効クリアランス = max(銅クリアランス, ドリル半径+穴銅間クリアランス-ビア半径)。
    中心が円のビアでは「銅の環がclr以内」も「穴がhole_clr以内」も
    中心距離の条件になるので、1回のCollideにまとめられる。

    パッドは境界(銅とレジスト開口)から margin 以上離す。同ネットのパッドは
    in_pad が真なら「境界より margin 以上内側」も許可領域として返し、その
    パッドの障害物には許可領域を添える(衝突しても内側なら置ける)。
    """
    board = snap["board"]
    bds = board.GetDesignSettings()
    hole_clr = max(getattr(bds, "m_HoleClearance", 0), 0)
    h2h = max(getattr(bds, "m_HoleToHoleMin", 0), 0)
    rl, rt, rr, rb = region

    layers = [int(l) for l in board.GetEnabledLayers().CuStack()]
    obstacles = []
    pad_regions = []
    holes = _HoleIndex(drill_r, h2h)  # 全ネット共通(穴間距離はネットを見ない)

    def add(shape, bb, clr, allow=None):
        t = (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())
        if t[2] < rl - clr or t[0] > rr + clr or \
           t[3] < rt - clr or t[1] > rb + clr:
            return
        obstacles.append((shape, t, clr, allow))

    def eff(item, layer):
        clr = max(_ts._net_clearance(snap, net),
                  _ts._item_clearance(snap, item, layer))
        return max(clr, drill_r + hole_clr - via_r)

    def add_hole(pos, dia, same_net):
        """穴は「穴間の最小距離」の索引に入れ、他ネット(NPTH含む)の穴は
        「相手の穴 vs 新ビアの銅」の穴-銅クリアランス障害物としても追加する。
        (NPTHは銅形状が無いので、これが無いと取付穴のそばに置けてしまう)"""
        if dia <= 0:
            return
        holes.add(pos.x, pos.y, dia // 2)
        if same_net or hole_clr <= 0:
            return
        r = dia // 2 + hole_clr
        if pos.x + r < rl or pos.x - r > rr or \
           pos.y + r < rt or pos.y - r > rb:
            return
        obstacles.append((pcbnew.SHAPE_SEGMENT(pos, pos, dia),
                          (pos.x - dia // 2, pos.y - dia // 2,
                           pos.x + dia // 2, pos.y + dia // 2),
                          hole_clr, None))

    for t in snap["tracks"]:
        same = t.GetNetCode() == net
        if t.GetClass() == "PCB_VIA":
            add_hole(t.GetPosition(), t.GetDrillValue(), same)
        if same:
            continue  # 同ネットの配線・ビアの銅は重なってよい
        for layer in layers:
            if t.IsOnLayer(layer):
                add(t.GetEffectiveShape(layer), t.GetBoundingBox(),
                    eff(t, layer))
    for p in snap["pads"]:
        d = p.GetDrillSize()
        same = p.GetNetCode() == net
        add_hole(p.GetPosition(), max(d.x, d.y), same)
        if not any(p.IsOnLayer(layer) for layer in layers):
            continue  # 銅の無いパッド(NPTH)は穴の登録だけ
        exp = _pad_mask_expansion(p)
        # 外側: 銅境界とレジスト開口境界の両方から margin 以上離す
        out_clr = margin + max(exp, 0)
        allow = None
        if same and in_pad:
            pb = p.GetBoundingBox()
            if not (pb.GetRight() < rl or pb.GetLeft() > rr or
                    pb.GetBottom() < rt or pb.GetTop() > rb):
                # 内側: 両境界より margin 以上内側に完全に収まる領域
                allow = _pad_inside_polys(
                    p, layers, via_r + margin + max(0, -exp))
                if allow:
                    pad_regions.append(allow)
        for layer in layers:
            if p.IsOnLayer(layer):
                add(p.GetEffectiveShape(layer), p.GetBoundingBox(),
                    out_clr if same else max(eff(p, layer), out_clr), allow)
    edge_clr = max(_ts._net_clearance(snap, net),
                   getattr(bds, "m_CopperEdgeClearance", 0))
    hole_edge = max(getattr(bds, "m_HoleToEdgeClearance", 0), 0)
    edge_clr = max(edge_clr, drill_r + hole_edge - via_r)
    for d in snap["edges"]:
        add(d.GetEffectiveShape(), d.GetBoundingBox(), edge_clr)
    for z in snap["zones"]:
        try:
            if not (z.GetIsRuleArea() and z.GetDoNotAllowVias()):
                continue
            if any(z.IsOnLayer(layer) for layer in layers):
                add(z.Outline(), z.GetBoundingBox(), 0)
        except Exception:
            continue
    # 他ネットのベタ充填は障害物にしない(塗り直しでビアを避けて充填される)
    return obstacles, holes, pad_regions


def _fit_areas(zone, via_r):
    """ビアが完全に銅に載る領域(層ごと)= 塗り形状をビア半径ぶん縮めた領域。

    塗りポリゴンはそのまま最終的な銅形状(KiCad 6以降は縁取り線なし)。
    """
    areas = []
    for layer in zone.GetLayerSet().CuStack():
        fill = zone.GetFilledPolysList(layer)
        if not fill.OutlineCount():
            continue
        poly = pcbnew.SHAPE_POLY_SET(fill)
        poly.Deflate(via_r, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                     pcbnew.FromMM(0.01))
        if poly.OutlineCount():
            areas.append(poly)
    return areas


def _place_pass(board, zone, net, via_nm, drill_nm, pitch, margin, in_pad):
    """塗り直し→格子状に置けるだけ置く、を1周。追加した数を返す。"""
    via_r = via_nm // 2
    drill_r = drill_nm // 2
    _refill(board, [zone])  # 古い塗りのままだと銅の無い場所に置いてしまう
    areas = _fit_areas(zone, via_r)
    if not areas:
        return 0

    bb = areas[0].BBox()
    for poly in areas[1:]:
        bb.Merge(poly.BBox())
        poly.BuildBBoxCaches()  # Contains の高速化
    areas[0].BuildBBoxCaches()
    region = (bb.GetLeft() - via_r, bb.GetTop() - via_r,
              bb.GetRight() + via_r, bb.GetBottom() + via_r)
    snap = _ts.snapshot(board)
    obstacles, holes, pad_regions = _collect_obstacles(
        snap, net, via_r, drill_r, region, margin, in_pad)
    outline = pcbnew.SHAPE_POLY_SET(zone.Outline())
    outline.BuildBBoxCaches()

    def in_pad_region(pt):
        """同ネットパッドの許可領域(そのパッドの全銅箔層で内側)にいるか。"""
        return any(all(q.Contains(pt, -1, 0, True) for q in polys)
                   for polys in pad_regions)

    # 千鳥(六角格子)。格子より約15%多く入る。範囲の中央に揃える。
    # xの基準は全行で共通にする(行ごとにセンタリングすると半ピッチの
    # ずれが崩れ、隣接行間の距離がピッチ未満になってしまう)
    row_h = max(int(round(pitch * math.sqrt(3) / 2)), 1)
    w, h = bb.GetWidth(), bb.GetHeight()
    ny = h // row_h + 1
    y0 = bb.GetTop() + (h - (ny - 1) * row_h) // 2
    x_base = bb.GetLeft() + (w - (w // pitch) * pitch) // 2
    right = bb.GetRight()
    added = 0
    def try_place(x, y):
        pt = pcbnew.VECTOR2I(int(x), int(y))
        if not any(a.Contains(pt, -1, 0, True) for a in areas):
            # ベタの塗りの外でも、選択ベタの輪郭内かつ同ネットパッドの
            # 内側(ビア・イン・パッド)なら置ける(パッド経由で導通する)
            if not (pad_regions and outline.Contains(pt, -1, 0, True)
                    and in_pad_region(pt)):
                return False
        if holes.blocked(x, y):  # 穴間の最小距離(重なりは常に不可)
            return False
        circle = pcbnew.SHAPE_CIRCLE(pt, via_r)
        for shape, (l, t, r, b), clr, allow in obstacles:
            reach = via_r + clr
            if r < x - reach or l > x + reach or \
               b < y - reach or t > y + reach:
                continue
            if shape.Collide(circle, clr):
                # そのパッド自身の許可領域の内側なら衝突扱いにしない
                if allow is not None and \
                   all(q.Contains(pt, -1, 0, True) for q in allow):
                    continue
                return False
        v = pcbnew.PCB_VIA(board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(pt)
        v.SetWidth(via_nm)
        v.SetDrill(drill_nm)
        v.SetNetCode(net)
        board.Add(v)
        holes.add(x, y, drill_r)
        return True

    for iy in range(ny):
        y = y0 + iy * row_h
        x = x_base + (pitch // 2 if iy % 2 else 0)
        while x <= right:
            if try_place(x, y):
                added += 1
            x += pitch
    return added


def stitch_zone(board, zone, via_nm, drill_nm, pitch_nm=0,
                margin_nm=0, in_pad=True):
    """ゾーンにビアを敷き詰めて board に追加する。

    打ったビアが孤立島を導通させて塗り面積が広がることがあるので、
    追加が無くなるまで塗り直し→配置を繰り返す(通常1〜2周)。
    margin_nm 0 = 自動(_auto_margin)。in_pad = 同ネットパッド上を許可。

    戻り値: {"added": 追加したビア数, "pitch": 使った間隔[nm],
             "margin": 使った境界マージン[nm], "note": 警告文}
    """
    net = zone.GetNetCode()
    if net <= 0:
        return {"added": 0, "pitch": 0, "margin": 0,
                "note": "ネットの無いベタにはビアを打てません。"}

    pitch = max(int(pitch_nm), min_pitch(board, via_nm, drill_nm))
    margin = int(margin_nm) if margin_nm > 0 else _auto_margin(board)
    total = 0
    for _ in range(5):
        added = _place_pass(board, zone, net, via_nm, drill_nm, pitch,
                            margin, in_pad)
        total += added
        if not added:
            break
    note = ""
    if total == 0:
        note = "ベタの塗りつぶしが空か、ビアを置ける場所がありません。"
    return {"added": total, "pitch": pitch, "margin": margin, "note": note}


def stitch_zones(board, zones, via_nm, drill_nm, pitch_nm=0, refill=True,
                 margin_nm=0, in_pad=True):
    """複数ゾーンを順に処理。戻り値: (合計追加数, 警告文リスト)。"""
    total, notes = 0, []
    for zone in zones:
        r = stitch_zone(board, zone, via_nm, drill_nm, pitch_nm,
                        margin_nm, in_pad)
        total += r["added"]
        if r["note"]:
            notes.append(r["note"])
    if refill and total:
        # 他ネットのベタから新しいビアのクリアランスを彫り直す
        _refill(board, list(board.Zones()))
    return total, notes


if wx is not None:

    class ViaStitchDialog(wx.Dialog):
        """メニューから直接起動したときの設定ウィンドウ。"""

        def __init__(self, parent):
            super().__init__(parent, title="ベタにビアを敷き詰める",
                             style=wx.DEFAULT_DIALOG_STYLE)
            via, drill = _via_defaults()
            root = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
            grid.AddGrowableCol(1)
            self.via = self._mm_spin(grid, "ビア径 [mm]", via, 0.2, 3.0)
            self.drill = self._mm_spin(grid, "ドリル [mm]", drill, 0.1, 2.0)
            self.pitch = self._mm_spin(grid, "間隔 [mm] (0=最小)", 0.0, 0.0, 20.0)
            self.margin = self._mm_spin(grid, "境界から [mm] (0=自動)",
                                        0.0, 0.0, 5.0)
            grid.Add(wx.StaticText(self, label=""), 0)
            self.in_pad_check = wx.CheckBox(self, label="同ネットのパッド上も可")
            self.in_pad_check.SetValue(True)
            grid.Add(self.in_pad_check, 0)
            grid.Add(wx.StaticText(self, label=""), 0)
            self.refill_check = wx.CheckBox(self, label="作成後にベタを塗り直す")
            self.refill_check.SetValue(True)
            grid.Add(self.refill_check, 0)
            root.Add(grid, 1, wx.ALL | wx.EXPAND, 12)
            btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            self.FindWindow(wx.ID_OK).SetLabel("敷き詰める")
            root.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
            self.SetSizerAndFit(root)
            self.CentreOnParent()

        def _mm_spin(self, grid, label, default, lo, hi):
            grid.Add(wx.StaticText(self, label=label),
                     0, wx.ALIGN_CENTER_VERTICAL)
            spin = wx.SpinCtrlDouble(self, min=lo, max=hi,
                                     initial=default, inc=0.05)
            spin.SetDigits(2)
            grid.Add(spin, 0, wx.EXPAND)
            return spin

        def get_params(self):
            return {
                "via": self.via.GetValue(),
                "drill": self.drill.GetValue(),
                "pitch": self.pitch.GetValue(),
                "margin": self.margin.GetValue(),
                "in_pad": self.in_pad_check.GetValue(),
                "refill": self.refill_check.GetValue(),
            }


class ViaStitch(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Stitch Vias in Zone"
        self.category = "Zones"
        self.description = "Fill selected zones with as many vias as rules allow"
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        if wx is None:
            return
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        zones = collect_selected_zones(board)
        if not zones:
            wx.MessageBox("先にビアを敷き詰めたいベタ（ゾーン）を選択してから実行してください。",
                          "ベタにビアを敷き詰める",
                          wx.OK | wx.ICON_INFORMATION, parent)
            return

        global PRESET
        preset, PRESET = PRESET, None
        if preset:
            params = preset
        else:
            dlg = ViaStitchDialog(parent)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                params = dlg.get_params()
            finally:
                dlg.Destroy()

        dvia, ddrill = _via_defaults()
        via_nm = pcbnew.FromMM(float(params.get("via", dvia)))
        drill_nm = pcbnew.FromMM(float(params.get("drill", ddrill)))
        if drill_nm >= via_nm:
            wx.MessageBox("ドリル径はビア径より小さくしてください。",
                          "ベタにビアを敷き詰める",
                          wx.OK | wx.ICON_WARNING, parent)
            return
        margin_nm = pcbnew.FromMM(float(params.get("margin", 0)))
        total, notes = stitch_zones(
            board, zones, via_nm, drill_nm,
            pitch_nm=pcbnew.FromMM(float(params.get("pitch", 0))),
            refill=bool(params.get("refill", True)),
            margin_nm=margin_nm, in_pad=bool(params.get("in_pad", True)))
        pcbnew.Refresh()
        msg = "追加したビア: %d 個" % total
        if margin_nm <= 0:  # 自動で決めた値はユーザーに見せる
            msg += "\n境界マージン: %.2fmm (自動)" % \
                pcbnew.ToMM(_auto_margin(board))
        if notes:
            msg += "\n" + "\n".join(sorted(set(notes)))
        wx.MessageBox(msg, "ベタにビアを敷き詰める",
                      wx.OK | wx.ICON_INFORMATION, parent)


ViaStitch().register()
