"""選択したパッドを長方形のベタ（銅箔ゾーン）で囲む

パッドを選択して実行すると設定ウィンドウが開く。
層・ネット・余白などを決めて「作成」を押すと、
選択パッド全体を囲む長方形ゾーンが1つ作られる。

- 層: 有効な銅箔層から複数選択できる（1つのゾーンが複数層にまたがる）
- ネット: 選択パッドに含まれるネットから選ぶ。1種類だけなら自動で選択済み
- 余白: パッド外形からベタの縁までの距離
- パッド接続（ベタ直結／サーマル）とクリアランスも指定できる
- 「作成後に塗りつぶす」を外した場合は、KiCad上で B キーで塗れる

フットプリントごと選択した場合は、その全パッドが対象になる。
選んだネットのパッドが層の中に無いと、その層のベタは孤立扱いで
塗りつぶしが空になる（その場合は警告を出す）。
"""
import pcbnew

try:
    import wx
except ImportError:
    wx = None

ICON = "🟩"                 # パレットのタイルに表示するアイコン
LABEL = "ベタで囲む"        # パレットのタイルに表示する短い名前

DEFAULT_MARGIN_MM = 1.0     # パッド外形→ベタ縁の余白（サーマルギャップより広くしておく）
DEFAULT_CLEARANCE_MM = 0.3  # 他ネットとのクリアランス
DEFAULT_MIN_W_MM = 0.2      # 最小幅（これより細い銅は塗られない）

PRESET = None  # パレットが実行直前に渡すパラメータ(Run()が消費、無ければダイアログ)

PANEL = [  # パレット埋め込みUIの定義(pack_launcher が参照。規約はREADME)
    {"type": "number", "key": "margin", "label": "余白", "unit": "mm",
     "default": DEFAULT_MARGIN_MM, "min": 0.0, "max": 10.0, "step": 0.1},
    {"type": "number", "key": "clearance", "label": "クリアランス", "unit": "mm",
     "default": DEFAULT_CLEARANCE_MM, "min": 0.0, "max": 10.0, "step": 0.1},
    {"type": "number", "key": "min_w", "label": "最小幅", "unit": "mm",
     "default": DEFAULT_MIN_W_MM, "min": 0.0, "max": 10.0, "step": 0.1},
    {"type": "choice", "key": "conn", "label": "パッド接続",
     "options": ["サーマルリリーフ", "ベタ直結"], "default": 0},
    {"type": "check", "key": "fill", "label": "作成後に塗りつぶす",
     "default": True},
    {"type": "run", "label": "作成"},
]


def collect_selected_pads(board):
    """選択中のパッドを返す。フットプリント選択なら、その全パッドを対象にする。"""
    pads = []
    try:  # GUIの選択はまずこれで拾う（ヘッドレスでは空になる）
        for item in pcbnew.GetCurrentSelection():
            if isinstance(item, pcbnew.PAD):
                pads.append(item)
            elif isinstance(item, pcbnew.FOOTPRINT):
                pads.extend(item.Pads())
    except Exception:
        pass
    if not pads:
        pads = [p for p in board.GetPads() if p.IsSelected()]
    if not pads:
        pads = [p for fp in board.GetFootprints() if fp.IsSelected()
                for p in fp.Pads()]
    return pads


def unique_netnames(pads):
    """選択パッドが属するネット名の一覧（名前順、無ネットは除外）。"""
    return sorted({p.GetNetname() for p in pads if p.GetNetname()})


def pads_bbox(pads):
    """パッド外形を合成したBBox。"""
    bb = None
    for p in pads:
        pbb = p.GetBoundingBox()
        if bb is None:
            bb = pcbnew.BOX2I(pbb.GetPosition(), pbb.GetSize())
        else:
            bb.Merge(pbb)
    return bb


def create_zone(board, pads, layers, netcode, margin_nm,
                clearance_nm, min_w_nm, thermal, fill):
    """パッド群を囲む長方形ゾーンを作って board に追加する。

    戻り値: (zone, 警告layer名リスト)。警告layer = 選んだネットのパッドが
    ゾーン範囲内に1つも無い層（塗りつぶすと孤立扱いで銅が消える）。
    """
    bb = pads_bbox(pads)
    bb.Inflate(margin_nm)

    zone = pcbnew.ZONE(board)
    ls = pcbnew.LSET()
    for layer in layers:
        ls.AddLayer(layer)
    zone.SetLayerSet(ls)
    zone.SetNetCode(netcode)

    outline = zone.Outline()
    outline.NewOutline()
    for x, y in [(bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
                 (bb.GetRight(), bb.GetBottom()), (bb.GetLeft(), bb.GetBottom())]:
        outline.Append(x, y)

    zone.SetMinThickness(min_w_nm)
    zone.SetLocalClearance(clearance_nm)
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL if thermal
                          else pcbnew.ZONE_CONNECTION_FULL)
    board.Add(zone)  # 塗りつぶしに失敗してもゾーン自体は残るよう、先に追加する

    # 選んだネットのパッドがその層の範囲内に無ければ、塗っても孤立して消える
    warn_layers = []
    for layer in layers:
        connected = any(p.GetNetCode() == netcode and p.IsOnLayer(layer)
                        and bb.Intersects(p.GetBoundingBox())
                        for p in board.GetPads())
        if not connected:
            warn_layers.append(board.GetLayerName(layer))

    if fill:
        try:
            zones = pcbnew.ZONES()
            zones.append(zone)
            pcbnew.ZONE_FILLER(board).Fill(zones)
        except Exception:
            pass  # 塗りは KiCad 上で B キーでやり直せる
    return zone, warn_layers


if wx is not None:

    class PadZoneDialog(wx.Dialog):
        """層・ネット・余白などを決める設定ウィンドウ。"""

        def __init__(self, parent, board, pads):
            super().__init__(parent, title="パッドをベタで囲む",
                             style=wx.DEFAULT_DIALOG_STYLE)
            self.board = board
            self.pads = pads

            root = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
            grid.AddGrowableCol(1)

            grid.Add(wx.StaticText(self, label="対象パッド"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(wx.StaticText(self, label="%d 個" % len(pads)), 0)

            # ネット: 選択パッドのネットだけを候補にする。1種類なら自動選択
            self.net_names = unique_netnames(pads) or ["(ネットなし)"]
            grid.Add(wx.StaticText(self, label="ネット"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            self.net_choice = wx.Choice(self, choices=self.net_names)
            self.net_choice.SetSelection(0)
            grid.Add(self.net_choice, 0, wx.EXPAND)

            # 層: 有効な銅箔層のチェックボックス。パッドが載っている層を初期ON
            grid.Add(wx.StaticText(self, label="層"), 0)
            layer_box = wx.BoxSizer(wx.VERTICAL)
            self.layer_checks = []
            for layer in board.GetEnabledLayers().CuStack():
                cb = wx.CheckBox(self, label=board.GetLayerName(layer))
                cb.SetValue(any(p.IsOnLayer(layer) for p in pads))
                self.layer_checks.append((layer, cb))
                layer_box.Add(cb, 0, wx.BOTTOM, 2)
            grid.Add(layer_box, 0)

            self.margin = self._mm_spin(grid, "余白 [mm]", DEFAULT_MARGIN_MM)
            self.clearance = self._mm_spin(grid, "クリアランス [mm]",
                                           DEFAULT_CLEARANCE_MM)
            self.min_w = self._mm_spin(grid, "最小幅 [mm]", DEFAULT_MIN_W_MM)

            grid.Add(wx.StaticText(self, label="パッド接続"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            self.conn_choice = wx.Choice(self, choices=["サーマルリリーフ", "ベタ直結"])
            self.conn_choice.SetSelection(0)
            grid.Add(self.conn_choice, 0, wx.EXPAND)

            grid.Add(wx.StaticText(self, label=""), 0)
            self.fill_check = wx.CheckBox(self, label="作成後に塗りつぶす")
            self.fill_check.SetValue(True)
            grid.Add(self.fill_check, 0)

            root.Add(grid, 1, wx.ALL | wx.EXPAND, 12)
            btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            self.FindWindow(wx.ID_OK).SetLabel("作成")
            root.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
            self.SetSizerAndFit(root)
            self.CentreOnParent()

        def _mm_spin(self, grid, label, default):
            grid.Add(wx.StaticText(self, label=label),
                     0, wx.ALIGN_CENTER_VERTICAL)
            spin = wx.SpinCtrlDouble(self, min=0.0, max=10.0,
                                     initial=default, inc=0.1)
            spin.SetDigits(2)
            grid.Add(spin, 0, wx.EXPAND)
            return spin

        def get_params(self):
            """create_zone() にそのまま渡せる dict を返す。"""
            name = self.net_names[self.net_choice.GetSelection()]
            net = self.board.FindNet(name)
            return {
                "layers": [l for l, cb in self.layer_checks if cb.GetValue()],
                "netcode": net.GetNetCode() if net else 0,
                "margin_nm": pcbnew.FromMM(self.margin.GetValue()),
                "clearance_nm": pcbnew.FromMM(self.clearance.GetValue()),
                "min_w_nm": pcbnew.FromMM(self.min_w.GetValue()),
                "thermal": self.conn_choice.GetSelection() == 0,
                "fill": self.fill_check.GetValue(),
            }


class PadZoneWrap(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Wrap Pads with Zone (rectangle)"
        self.category = "Zones"
        self.description = "Create a rectangular copper zone around selected pads"
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        if wx is None:
            return
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        pads = collect_selected_pads(board)
        if not pads:
            wx.MessageBox("先に囲みたいパッド（またはフットプリント）を選択してから実行してください。",
                          "パッドをベタで囲む", wx.OK | wx.ICON_INFORMATION, parent)
            return

        global PRESET
        preset, PRESET = PRESET, None
        nets = unique_netnames(pads)
        if preset and len(nets) <= 1:
            # パレットから: ネットが一意ならダイアログなしで作成。
            # 層は選択パッドが載っている銅箔層(ダイアログの初期値と同じ)
            net = board.FindNet(nets[0]) if nets else None
            params = {
                "layers": [l for l in board.GetEnabledLayers().CuStack()
                           if any(p.IsOnLayer(l) for p in pads)],
                "netcode": net.GetNetCode() if net else 0,
                "margin_nm": pcbnew.FromMM(
                    float(preset.get("margin", DEFAULT_MARGIN_MM))),
                "clearance_nm": pcbnew.FromMM(
                    float(preset.get("clearance", DEFAULT_CLEARANCE_MM))),
                "min_w_nm": pcbnew.FromMM(
                    float(preset.get("min_w", DEFAULT_MIN_W_MM))),
                "thermal": int(preset.get("conn", 0)) == 0,
                "fill": bool(preset.get("fill", True)),
            }
        else:
            # メニューから直接、または複数ネット選択(どのネットで囲むか
            # 選ぶ必要がある)のときは従来の設定ダイアログ
            dlg = PadZoneDialog(parent, board, pads)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                params = dlg.get_params()
            finally:
                dlg.Destroy()

        if not params["layers"]:
            wx.MessageBox("層が1つも選ばれていません。", "パッドをベタで囲む",
                          wx.OK | wx.ICON_WARNING, parent)
            return

        _, warn_layers = create_zone(board, pads, **params)
        pcbnew.Refresh()
        if warn_layers:
            wx.MessageBox(
                "次の層は、選んだネットのパッドが範囲内に無いため、塗りつぶすと孤立して空になります:\n"
                "  %s\nネットか層の選択を見直してください（ゾーン自体は作成済み）。"
                % ", ".join(warn_layers),
                "パッドをベタで囲む", wx.OK | wx.ICON_WARNING, parent)


PadZoneWrap().register()
