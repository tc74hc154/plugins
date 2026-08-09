"""今の並びのまま、隙間ゼロに詰める（整列のみ・配線は見ない）

選択フットプリントを、今の並び（行・列の構造と順序）を保ったまま
コートヤード枠線（中心線）基準でギャップ0に詰める。
隣り合うコートヤードの枠線がちょうど一致する（距離0で重なる）。

すでにコートヤード同士が接している（距離0または重なり、許容1µm）
フットプリント群は1つのブロックとして扱い、内部の相対位置を保った
まま全体を動かす。一度詰めた結果に再実行しても配置は変わらない。

実行時に3x3の整列ボタンで揃え方を選ぶ:
縦に並ぶ行同士にはボタンの横位置(左/中央/右)、
横に並ぶ行内の部品にはボタンの縦位置(上/中央/下)が効く。
中央ボタンで上下左右対称になる。

最短配線などは考慮しない、単純な「隙間詰め」ツール。
部品を意図した並びに置いてから実行すると、その並びのままギャップ0になる。"""
import pcbnew
import wx

ICON = "🧱"  # パレットのカードに表示するアイコン
GAP_NM = 0   # ブロック間ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)
TOUCH_TOL_NM = 1000  # コートヤードが「接している」とみなす距離の許容 [nm]

ALIGN_X = {"left": 0.0, "center": 0.5, "right": 1.0}   # 行同士の横揃え
ALIGN_Y = {"top": 0.0, "middle": 0.5, "bottom": 1.0}   # 行内の縦揃え
LAST_ALIGN = ["center", "middle"]  # セッション内で最後に選んだボタンを覚える

def courtyard_bbox(fp):
    """コートヤード枠線の中心線基準BBoxを返す。
    図形のBBoxは線幅ぶん外側に膨らむので、線幅/2だけ内側に縮めてから合成する。
    コートヤードが無ければテキスト抜きの実体BBoxにフォールバック。"""
    layer = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
    bb = None
    for item in fp.GraphicalItems():
        if item.GetLayer() != layer:
            continue
        ibb = item.GetBoundingBox()
        try:
            half_w = item.GetWidth() // 2
            if half_w > 0:
                ibb.Inflate(-half_w)
        except AttributeError:
            pass
        if bb is None:
            bb = ibb
        else:
            bb.Merge(ibb)
    if bb is not None:
        return bb
    # 図形が無い場合: キャッシュ済みコートヤードポリゴン
    try:
        cy = fp.GetCourtyard(layer)
        if cy.OutlineCount() > 0:
            return cy.BBox()
    except Exception:
        pass
    try:
        return fp.GetBoundingBox(False)          # v8以降: テキスト除外
    except TypeError:
        return fp.GetBoundingBox(False, False)   # 旧シグネチャ

def cluster_rects(rects, tol=TOUCH_TOL_NM):
    """接触/重なり(許容tol)している矩形同士をunion-findでまとめ、
    インデックスのグループのリストを返す。rects: [(left, top, right, bottom)]"""
    n = len(rects)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        li, ti, ri, bi = rects[i]
        for j in range(i + 1, n):
            lj, tj, rj, bj = rects[j]
            if li - tol <= rj and lj - tol <= ri and ti - tol <= bj and tj - tol <= bi:
                parent[find(i)] = find(j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())

def pack_targets(blocks, align_x="left", align_y="top"):
    """今の行・列構造と順序を保ったまま詰めたときの、
    各ブロックBBox左上の目標座標のリストを返す。
    blocks: [{left, top, bottom, w, h}] (w/hはギャップ込み)
    align_x: 幅の違う行同士をどう揃えるか (left/center/right)
    align_y: 高さの違う行内ブロックをどう揃えるか (top/middle/bottom)"""
    fx = ALIGN_X[align_x]
    fy = ALIGN_Y[align_y]
    order = sorted(range(len(blocks)), key=lambda i: blocks[i]["top"])

    # 現在のY範囲が重なるもの同士を同じ行とみなしてグループ化
    rows = []
    row = [order[0]]
    row_bottom = blocks[order[0]]["bottom"]
    for i in order[1:]:
        if blocks[i]["top"] < row_bottom:  # 現在の行と縦に重なっている
            row.append(i)
            row_bottom = max(row_bottom, blocks[i]["bottom"])
        else:
            rows.append(row)
            row = [i]
            row_bottom = blocks[i]["bottom"]
    rows.append(row)

    # 全体の左上を基準点にして、行・列の順序を保ったまま詰める
    origin_x = min(b["left"] for b in blocks)
    origin_y = blocks[order[0]]["top"]
    total_w = max(sum(blocks[i]["w"] for i in row) - GAP_NM for row in rows)
    targets = [None] * len(blocks)
    y = 0
    for row in rows:
        row.sort(key=lambda i: blocks[i]["left"])  # 行内は左から順に
        row_w = sum(blocks[i]["w"] for i in row) - GAP_NM
        row_h = max(blocks[i]["h"] for i in row)
        x = int((total_w - row_w) * fx)  # 幅の違う行はfxで横に寄せる
        for i in row:
            dy = int((row_h - blocks[i]["h"]) * fy)  # 低いブロックはfyで縦に寄せる
            targets[i] = (origin_x + x, origin_y + y + dy)
            x += blocks[i]["w"]
        y += row_h
    return targets

def pack_selected(board, align_x="left", align_y="top"):
    """選択フットプリントをブロック化して詰める。動かした個数を返す。"""
    fps = [fp for fp in board.GetFootprints() if fp.IsSelected()]
    if not fps:
        return 0

    rects = []
    for fp in fps:
        bb = courtyard_bbox(fp)
        rects.append((bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()))

    # 接触しているコートヤード同士は1ブロック（剛体）として扱う
    blocks = []
    for group in cluster_rects(rects):
        left = min(rects[i][0] for i in group)
        top = min(rects[i][1] for i in group)
        right = max(rects[i][2] for i in group)
        bottom = max(rects[i][3] for i in group)
        members = [(fps[i], fps[i].GetPosition() - pcbnew.VECTOR2I(left, top))
                   for i in group]
        blocks.append({
            "left": left, "top": top, "bottom": bottom,
            "w": right - left + GAP_NM, "h": bottom - top + GAP_NM,
            "members": members,
        })

    moved = 0
    for block, (tx, ty) in zip(blocks, pack_targets(blocks, align_x, align_y)):
        for fp, off in block["members"]:
            target = pcbnew.VECTOR2I(tx, ty) + off
            if fp.GetPosition() != target:
                fp.SetPosition(target)
                moved += 1
    return moved

class AlignPackDialog(wx.Dialog):
    """3x3の整列ボタン。押した位置で揃え方が決まり、即実行される。"""
    XS = ("left", "center", "right")
    YS = ("top", "middle", "bottom")
    LABELS = (("↖", "↑", "↗"),
              ("←", "●", "→"),
              ("↙", "↓", "↘"))
    JP_X = {"left": "左揃え", "center": "中央", "right": "右揃え"}
    JP_Y = {"top": "上揃え", "middle": "中央", "bottom": "下揃え"}

    def __init__(self, parent, last):
        super().__init__(parent, title="整列して詰める")
        self.choice = None
        outer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(
            self, label="縦の並びには横位置、横の並びには縦位置が効きます\n"
                        "（中央 ● で上下左右対称）")
        outer.Add(note, 0, wx.ALL, 8)
        grid = wx.GridSizer(3, 3, 4, 4)
        focus_btn = None
        for r, ay in enumerate(self.YS):
            for c, ax in enumerate(self.XS):
                btn = wx.Button(self, label=self.LABELS[r][c],
                                size=wx.Size(48, 48))
                btn.SetToolTip(f"行同士: {self.JP_X[ax]} / 行内: {self.JP_Y[ay]}")
                btn.Bind(wx.EVT_BUTTON,
                         lambda e, a=(ax, ay): self._pick(a))
                if [ax, ay] == list(last):
                    focus_btn = btn
                grid.Add(btn, 0, wx.EXPAND)
        outer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, 8)
        self.SetSizerAndFit(outer)
        if focus_btn is not None:
            focus_btn.SetFocus()

    def _pick(self, align):
        self.choice = align
        self.EndModal(wx.ID_OK)

class DensePack(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Dense Pack Selected (zero gap)"
        self.category = "Placement"
        self.description = "Pack selected footprints so courtyard outlines coincide, keeping current arrangement"
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        board = pcbnew.GetBoard()
        if not any(fp.IsSelected() for fp in board.GetFootprints()):
            return
        parent = wx.FindWindowByName("PcbFrame")
        dlg = AlignPackDialog(parent, LAST_ALIGN)
        try:
            if dlg.ShowModal() != wx.ID_OK or dlg.choice is None:
                return  # ×/Escは何もしない
            align_x, align_y = dlg.choice
        finally:
            dlg.Destroy()
        LAST_ALIGN[:] = [align_x, align_y]
        pack_selected(board, align_x, align_y)
        pcbnew.Refresh()

DensePack().register()
