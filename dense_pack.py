"""今の並びのまま、隙間ゼロに詰める（整列のみ・配線は見ない）

選択フットプリントを、今の並び（行・列の構造と順序）を保ったまま
コートヤード枠線（中心線）基準でギャップ0に詰める。
隣り合うコートヤードの枠線がちょうど一致する（距離0で重なる）。

最短配線などは考慮しない、単純な「隙間詰め」ツール。
部品を意図した並びに置いてから実行すると、その並びのままギャップ0になる。"""
import pcbnew

ICON = "🧱"  # パレットのカードに表示するアイコン
GAP_NM = 0   # ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)

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

class DensePack(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Dense Pack Selected (zero gap)"
        self.category = "Placement"
        self.description = "Pack selected footprints so courtyard outlines coincide, keeping current arrangement"
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        board = pcbnew.GetBoard()
        fps = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        if not fps:
            return

        items = []
        for fp in fps:
            bb = courtyard_bbox(fp)
            # アンカー(fp原点)とBBox左上のオフセットを記録しておく
            off = fp.GetPosition() - pcbnew.VECTOR2I(bb.GetLeft(), bb.GetTop())
            items.append({
                "fp": fp,
                "w": bb.GetWidth() + GAP_NM,
                "h": bb.GetHeight() + GAP_NM,
                "off": off,
                "top": bb.GetTop(),
                "bottom": bb.GetBottom(),
                "left": bb.GetLeft(),
            })

        # 現在のY範囲が重なるもの同士を同じ行とみなしてグループ化
        items.sort(key=lambda it: it["top"])
        rows = []
        row = [items[0]]
        row_bottom = items[0]["bottom"]
        for it in items[1:]:
            if it["top"] < row_bottom:  # 現在の行と縦に重なっている
                row.append(it)
                row_bottom = max(row_bottom, it["bottom"])
            else:
                rows.append(row)
                row = [it]
                row_bottom = it["bottom"]
        rows.append(row)

        # 全体の左上を基準点にして、行・列の順序を保ったまま詰める
        origin_x = min(it["left"] for it in items)
        origin_y = items[0]["top"]
        y = 0
        for row in rows:
            row.sort(key=lambda it: it["left"])  # 行内は左から順に
            x = 0
            row_h = max(it["h"] for it in row)
            for it in row:
                target = pcbnew.VECTOR2I(origin_x + x, origin_y + y) + it["off"]
                it["fp"].SetPosition(target)
                x += it["w"]
            y += row_h

        pcbnew.Refresh()

DensePack().register()
