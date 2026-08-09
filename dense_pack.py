"""今の並びのまま、隙間ゼロに詰める（整列のみ・配線は見ない）

選択フットプリントを、今の並び（行・列の構造と順序）を保ったまま
コートヤード枠線（中心線）基準でギャップ0に詰める。
隣り合うコートヤードの枠線がちょうど一致する（距離0で重なる）。

すでにコートヤード同士が接している（距離0または重なり、許容1µm）
フットプリント群は1つのブロックとして扱い、内部の相対位置を保った
まま全体を動かす。一度詰めた結果に再実行しても配置は変わらない。

実行時に3x3のボタンで詰め方を選ぶ。ボタンの各軸は独立で、
「壁に寄せる」か「中央に揃える」かを軸ごとに決める:
角ボタン(↖↗↙↘)は両軸とも壁=その角へ「横に寄せる→縦に落とす」を
繰り返すテトリス的な重力詰め。辺ボタン(←→↑↓)はその方向に詰めつつ、
直交軸は中央揃え。中央ボタン●は両軸とも中央=上下左右対称。

「パッドにつながる配線も一緒に動かす」をONにすると、移動する
パッドに乗っている配線が接続を保ったまま追従する:
つながる先がすべて同じ量だけ動く配線は形を保ったまま平行移動、
動かない側にもつながる配線は、分岐・ビア・タップのない一続きの
区間(run)ごとに古い形を捨てて端点間を45度+軸方向で引き直す
(うねった多セグメント配線も1本にまとまる)。経路候補(L字2種+
Z字3種)は移動後の盤面に当たり判定して、他ネットの銅に
クリアランス未満で近づかない候補を選ぶ。全候補が塞がっていれば
track_shorten の遅延障害物A*で迂回路を探索し、それでも無理な区間
だけ既定のL字で引いて件数を警告する(T字接続は接続点で分割)。
仕上げに、要素(パッド/ビア/他の配線/ゾーン)につながらない端を
持つ線分を、触ったグループの範囲で連鎖的に削除する。
さらに「仕上げに配線を短縮する」ONなら track_shorten を盤面全体に
1回かける(塞いでいる側の配線も動かせるので残った重なりも解消する)。
円弧は伸縮できないため追従せず、件数を警告する。

最短配線などは考慮しない、単純な「隙間詰め」ツール。
部品を意図した並びに置いてから実行すると、その並びのままギャップ0になる。"""
import time
from bisect import bisect_left, bisect_right

import pcbnew
import wx

ICON = "🧱"  # パレットのタイルに表示するアイコン
LABEL = "隙間ゼロに詰める"  # パレットのタイルに表示する短い名前
GAP_NM = 0   # ブロック間ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)
TOUCH_TOL_NM = 1000  # コートヤードが「接している」とみなす距離の許容 [nm]

WIRE_TOL_NM = 1000   # 配線の点一致/T字接触とみなす距離の許容 [nm]

ROW_OVERLAP_FRAC = 0.5  # 同じ行とみなすY重なりの下限(低い方の高さに対する割合)

ALIGN_X = {"left": 0.0, "center": 0.5, "right": 1.0}   # 行同士の横揃え
ALIGN_Y = {"top": 0.0, "middle": 0.5, "bottom": 1.0}   # 行内の縦揃え
LAST_ALIGN = ["center", "middle"]  # セッション内で最後に選んだボタンを覚える
LAST_WIRES = [True]                # 「配線も動かす」チェックの前回値
LAST_SHORTEN = [True]              # 「仕上げに短縮」チェックの前回値

PRESET = None  # パレットが実行直前に渡すパラメータ(Run()が消費、無ければダイアログ)


def PANEL():
    """パレット埋め込みUIの定義(pack_launcher が参照。規約はREADME)。"""
    return [
        {"type": "dirgrid", "key": "align", "last": list(LAST_ALIGN)},
        {"type": "check", "key": "wires",
         "label": "パッドにつながる配線も動かす", "default": LAST_WIRES[0]},
        {"type": "check", "key": "shorten",
         "label": "仕上げに配線を短縮", "default": LAST_SHORTEN[0]},
    ]

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

    # Y範囲の過半が重なるもの同士を同じ行とみなしてグループ化。
    # わずかな重なり(縦長ブロックの下端が次の段にかかっている等)で
    # 別の段まで1行に連鎖合流しないよう、重なり量が
    # 「行とブロックの低い方の高さ×ROW_OVERLAP_FRAC」以上のときだけ合流する
    rows = []
    row = [order[0]]
    row_top = blocks[order[0]]["top"]
    row_bottom = blocks[order[0]]["bottom"]
    for i in order[1:]:
        b = blocks[i]
        overlap = row_bottom - b["top"]
        limit = ROW_OVERLAP_FRAC * min(row_bottom - row_top, b["bottom"] - b["top"])
        if overlap >= limit and overlap > 0:
            row.append(i)
            row_bottom = max(row_bottom, b["bottom"])
        else:
            rows.append(row)
            row = [i]
            row_top = b["top"]
            row_bottom = b["bottom"]
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

def gravity_targets(blocks, gx, gy):
    """ブロックをボタン方向の壁に向けて滑らせて詰める(テトリス的コンパクション)。
    gx: -1=左へ, 0=横は動かさない, +1=右へ
    gy: -1=上へ, 0=縦は動かさない, +1=下へ
    横に寄せる→縦に落とすの順で、動かなくなるまで繰り返す。
    滑る方向にしか動かないので、ブロック同士の前後関係は崩れない。
    返り値: 各ブロックの目標BBox左上のリスト。blocks: [{left, top, w, h}]"""
    n = len(blocks)
    pos = [(b["left"], b["top"]) for b in blocks]
    w = [b["w"] for b in blocks]
    h = [b["h"] for b in blocks]
    left_wall = min(p[0] for p in pos)
    top_wall = min(p[1] for p in pos)
    right_wall = max(pos[i][0] + w[i] for i in range(n))
    bottom_wall = max(pos[i][1] + h[i] for i in range(n))

    def ovl(a0, a1, b0, b1):
        return min(a1, b1) - max(a0, b0) > 0

    moved = True
    rounds = 0
    while moved and rounds <= n + 2:
        moved = False
        rounds += 1
        if gx:
            for i in sorted(range(n), key=lambda k: pos[k][0], reverse=(gx > 0)):
                x, y = pos[i]
                if gx < 0:
                    t = left_wall
                    for j in range(n):
                        if j != i and pos[j][0] <= x \
                           and ovl(y, y + h[i], pos[j][1], pos[j][1] + h[j]):
                            t = max(t, pos[j][0] + w[j])
                    if t < x:
                        pos[i] = (t, y)
                        moved = True
                else:
                    t = right_wall - w[i]
                    for j in range(n):
                        if j != i and pos[j][0] >= x \
                           and ovl(y, y + h[i], pos[j][1], pos[j][1] + h[j]):
                            t = min(t, pos[j][0] - w[i])
                    if t > x:
                        pos[i] = (t, y)
                        moved = True
        if gy:
            for i in sorted(range(n), key=lambda k: pos[k][1], reverse=(gy > 0)):
                x, y = pos[i]
                if gy < 0:
                    t = top_wall
                    for j in range(n):
                        if j != i and pos[j][1] <= y \
                           and ovl(x, x + w[i], pos[j][0], pos[j][0] + w[j]):
                            t = max(t, pos[j][1] + h[j])
                    if t < y:
                        pos[i] = (x, t)
                        moved = True
                else:
                    t = bottom_wall - h[i]
                    for j in range(n):
                        if j != i and pos[j][1] >= y \
                           and ovl(x, x + w[i], pos[j][0], pos[j][0] + w[j]):
                            t = min(t, pos[j][1] - h[i])
                    if t > y:
                        pos[i] = (x, t)
                        moved = True
    return pos

def _canon_map(points, tol=WIRE_TOL_NM):
    """近接する点(チェビシェフ距離≤tol)を代表点に写す辞書。決定的。"""
    cells = {}
    out = {}
    for p in sorted(set(points)):
        cx, cy = p[0] // tol, p[1] // tol
        rep = None
        for nx in (cx - 1, cx, cx + 1):
            for ny in (cy - 1, cy, cy + 1):
                for r in cells.get((nx, ny), ()):
                    if abs(r[0] - p[0]) <= tol and abs(r[1] - p[1]) <= tol:
                        rep = r
                        break
                if rep:
                    break
            if rep:
                break
        if rep is None:
            cells.setdefault((cx, cy), []).append(p)
            rep = p
        out[p] = rep
    return out

def _pt_seg_d2(p, a, b):
    """点pと線分abの距離の2乗。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = a[0] + t * dx, a[1] + t * dy
    return (p[0] - cx) ** 2 + (p[1] - cy) ** 2

def _seg_param(a, b, p):
    """線分ab上での点pの位置(0..1)。タップ点のソート用。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    if d2 == 0:
        return 0.0
    return ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2

def _octi_path(q1, q2, diag_at_start):
    """q1→q2を45度線+軸方向線のL字でつなぐ点列。
    軸方向または45度ぴったりなら1本のまま。
    diag_at_start=True なら斜め区間をq1側に置く(動いた端にジョグを寄せる)。"""
    dx, dy = q2[0] - q1[0], q2[1] - q1[1]
    adx, ady = abs(dx), abs(dy)
    if adx == 0 or ady == 0 or adx == ady:
        return [q1, q2]
    m = min(adx, ady)
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    if diag_at_start:
        corner = (q1[0] + sx * m, q1[1] + sy * m)
    else:
        corner = (q2[0] - sx * m, q2[1] - sy * m)
    return [q1, corner, q2]

def _segs_intersect(a1, a2, b1, b2):
    """線分a1a2とb1b2が交差するか(接触含む)。整数座標の外積判定。"""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    def on_seg(o, p, q):
        return (min(o[0], p[0]) <= q[0] <= max(o[0], p[0])
                and min(o[1], p[1]) <= q[1] <= max(o[1], p[1]))

    d1 = cross(b1, b2, a1)
    d2 = cross(b1, b2, a2)
    d3 = cross(a1, a2, b1)
    d4 = cross(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0) or d1 == 0 or d2 == 0) \
       and ((d3 > 0) != (d4 > 0) or d3 == 0 or d4 == 0):
        if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
            return True
        for d, seg, p in ((d1, (b1, b2), a1), (d2, (b1, b2), a2),
                          (d3, (a1, a2), b1), (d4, (a1, a2), b2)):
            if d == 0 and on_seg(seg[0], seg[1], p):
                return True
    return False

def _seg_seg_d2(a1, a2, b1, b2):
    """線分同士の距離の2乗。"""
    if _segs_intersect(a1, a2, b1, b2):
        return 0
    return min(_pt_seg_d2(a1, b1, b2), _pt_seg_d2(a2, b1, b2),
               _pt_seg_d2(b1, a1, a2), _pt_seg_d2(b2, a1, a2))

def _seg_rect_d2(a, b, left, top, right, bottom):
    """線分abと矩形の距離の2乗。"""
    if left <= a[0] <= right and top <= a[1] <= bottom:
        return 0
    if left <= b[0] <= right and top <= b[1] <= bottom:
        return 0
    lt, rt = (left, top), (right, top)
    lb, rb = (left, bottom), (right, bottom)
    return min(_seg_seg_d2(a, b, lt, rt), _seg_seg_d2(a, b, rt, rb),
               _seg_seg_d2(a, b, rb, lb), _seg_seg_d2(a, b, lb, lt))

def _flex_candidates(q1, q2, diag_at_start):
    """q1→q2の45度+軸の経路候補(線分リスト)を優先順に返す。
    L字2種(斜めを動いた端側/固定端側)+Z字3種(斜めを中間25/50/75%に)。"""
    outs = []
    for ds in (diag_at_start, not diag_at_start):
        pts = _octi_path(q1, q2, ds)
        segs = [s for s in zip(pts, pts[1:]) if s[0] != s[1]]
        if segs not in outs:
            outs.append(segs)
    dx, dy = q2[0] - q1[0], q2[1] - q1[1]
    adx, ady = abs(dx), abs(dy)
    if adx and ady and adx != ady:
        m = min(adx, ady)
        sx = 1 if dx > 0 else -1
        sy = 1 if dy > 0 else -1
        for t in (0.5, 0.25, 0.75):
            if adx > ady:
                x1 = q1[0] + sx * int((adx - m) * t)
                p1 = (x1, q1[1])
                p2 = (x1 + sx * m, q2[1])
            else:
                y1 = q1[1] + sy * int((ady - m) * t)
                p1 = (q1[0], y1)
                p2 = (q2[0], y1 + sy * m)
            pts = [q1, p1, p2, q2]
            segs = [s for s in zip(pts, pts[1:]) if s[0] != s[1]]
            if segs not in outs:
                outs.append(segs)
    return outs

def _pad_hits(pads, pt, layer):
    """pt(x,y)が乗っているパッドのレコードを返す。layer=Noneは層を見ない(ビア用)。"""
    v = pcbnew.VECTOR2I(pt[0], pt[1])
    out = []
    for rec in pads:
        bb = rec[1]
        if not (bb.GetLeft() <= pt[0] <= bb.GetRight()
                and bb.GetTop() <= pt[1] <= bb.GetBottom()):
            continue
        if layer is not None and not rec[0].IsOnLayer(layer):
            continue
        if rec[0].HitTest(v):
            out.append(rec)
    return out

def plan_wire_moves(board, fp_deltas, tol=WIRE_TOL_NM):
    """フットプリントの移動に合わせた配線の追従を、移動前の形状から計画する。

    fp_deltas: [(fp, (dx, dy))]
    返り値: (ops, stats)
      ops: ("move", item, (dx,dy)) 平行移動 /
           ("set", seg, (ax,ay), (bx,by)) 既存線分の端点変更 /
           ("add", (ax,ay), (bx,by), width, layer, net) 分割で増える線分
    追従点(移動するパッドに乗る点)がすべて同じ移動量で、動かないパッドに
    触れていない連結群は丸ごと平行移動。それ以外は接続点だけ追従して伸縮し、
    変形する線分の途中にT字接続があれば接続点で分割して接続を保つ。
    """
    stats = {"rigid": 0, "stretch": 0, "added": 0, "arc_skip": 0,
             "overlap": 0, "deleted": 0}
    moving = {}
    for fp, d in fp_deltas:
        if d != (0, 0):
            moving[fp.m_Uuid.AsString()] = d

    ops = []
    if not moving:
        return ops, stats

    moving_pads = []   # (pad, bbox, delta)
    nets = set()
    for fp, d in fp_deltas:
        if d == (0, 0):
            continue
        for p in fp.Pads():
            if p.GetNetCode() > 0:
                moving_pads.append((p, p.GetBoundingBox(), d))
                nets.add(p.GetNetCode())
    static_pads = []   # (pad, bbox) 動かない側のパッド(対象ネットのみ)
    for fp in board.GetFootprints():
        if fp.m_Uuid.AsString() in moving:
            continue
        for p in fp.Pads():
            if p.GetNetCode() in nets:
                static_pads.append((p, p.GetBoundingBox()))

    by_net = {}
    for t in list(board.GetTracks()):
        if t.GetNetCode() in nets:
            by_net.setdefault(t.GetNetCode(), []).append(t)

    pending = []   # 引き直す区間: (item, extras, layer, width, net, specs)
    touched = set()   # 伸縮処理の対象になったアイテムのUUID(掃除の範囲)
    for items in by_net.values():
        _plan_net_moves(items, moving_pads, static_pads, tol, ops, stats,
                        pending, touched)
    if pending:
        _resolve_pending(board, fp_deltas, pending, ops, stats)
    _prune_dangling(board, fp_deltas, ops, stats, touched, tol)
    return ops, stats

def _net_clearance(board, netcode, cache):
    """ネットクラスの銅クリアランス。SWIGで安全に引けるのは
    GetNetClassName→GetAllNetClasses経由のみ(罠#20)。"""
    if netcode in cache:
        return cache[netcode]
    clr = int(0.2e6)
    try:
        name = board.FindNet(netcode).GetNetClassName()
        clr = board.GetAllNetClasses()[name].GetClearance()
    except Exception:
        try:
            clr = board.GetAllNetClasses()["Default"].GetClearance()
        except Exception:
            pass
    cache[netcode] = clr
    return clr

SEARCH_TIME_S = 3.0   # 伸縮1区間あたりのA*探索の時間予算 [秒]

def _search_route(board, q1, q2, layer, width, net, obstacles, clr_cache):
    """固定候補が全滅した伸縮区間の経路を、track_shorten の
    遅延障害物A*で探す。見つかれば線分リスト、無理なら None。

    障害物なしで最短を引き、経路に実際に当たった障害物だけを探索対象に
    加えて引き直す、を収束まで繰り返す(track_shorten と同じ方式)。
    """
    try:
        import track_shorten as ts
    except Exception:
        return None
    V = pcbnew.VECTOR2I
    own_clr = _net_clearance(board, net, clr_cache)
    full = []   # track_shorten形式: (shape, bboxタプル, クリアランス)
    for ob in obstacles:
        if ob[0] == "seg":
            _, ol, oa, obp, ow, onet = ob
            if onet == net or ol != layer:
                continue
            h = ow // 2
            full.append((pcbnew.SHAPE_SEGMENT(V(*oa), V(*obp), ow),
                         (min(oa[0], obp[0]) - h, min(oa[1], obp[1]) - h,
                          max(oa[0], obp[0]) + h, max(oa[1], obp[1]) + h),
                         max(own_clr, _net_clearance(board, onet, clr_cache))))
        elif ob[0] == "via":
            _, op_, ow, onet = ob
            if onet == net:
                continue
            h = ow // 2
            full.append((pcbnew.SHAPE_SEGMENT(V(*op_), V(*op_), ow),
                         (op_[0] - h, op_[1] - h, op_[0] + h, op_[1] + h),
                         max(own_clr, _net_clearance(board, onet, clr_cache))))
        elif ob[0] == "hole":
            _, hp, dia, onet, hclr = ob
            if onet == net:
                continue
            h = dia // 2
            full.append((pcbnew.SHAPE_SEGMENT(V(*hp), V(*hp), dia),
                         (hp[0] - h, hp[1] - h, hp[0] + h, hp[1] + h), hclr))
        else:
            _, pad, pd, onet = ob
            if onet == net or not pad.IsOnLayer(layer):
                continue
            bb = pad.GetBoundingBox()
            l, t = bb.GetLeft() + pd[0], bb.GetTop() + pd[1]
            r, btm = bb.GetRight() + pd[0], bb.GetBottom() + pd[1]
            full.append((pcbnew.SHAPE_RECT(V(l, t), r - l, btm - t),
                         (l, t, r, btm),
                         max(own_clr, _net_clearance(board, onet, clr_cache))))

    budget = ts.octi(q1, q2) * 2 + pcbnew.FromMM(20)
    deadline = time.time() + SEARCH_TIME_S
    half = width // 2
    active = []
    active_idx = set()
    path = None
    for _ in range(len(full) + 1):
        nodes, _cut = ts.corner_candidates(active, width, q1, q2, budget)
        path, _timed_out = ts.find_path(q1, q2, nodes, active, width,
                                        budget, deadline=deadline)
        if path is None:
            return None
        seg_shapes = []
        for a, b in zip(path, path[1:]):
            seg_shapes.append((pcbnew.SHAPE_SEGMENT(V(*a), V(*b), width),
                               (min(a[0], b[0]) - half, min(a[1], b[1]) - half,
                                max(a[0], b[0]) + half, max(a[1], b[1]) + half)))
        violators = []
        for i, (osh, (l, t, r, btm), oc) in enumerate(full):
            if i in active_idx:
                continue
            for ssh, (sl, st, sr, sb) in seg_shapes:
                if sl > r + oc or sr < l - oc or st > btm + oc or sb < t - oc:
                    continue
                if osh.Collide(ssh, oc):
                    violators.append(i)
                    break
        if not violators:
            break
        for i in violators:
            active_idx.add(i)
            active.append(full[i])
        path = None
        if time.time() > deadline:
            return None
    if path is None:
        return None
    path = ts.reduce_bends(path, full, width)
    path = ts.dedust_path(path, full, width)
    return [s for s in zip(path, path[1:]) if s[0] != s[1]]

def _via_width(v):
    """ビアの径。KiCad10のPCB_VIA::GetWidth()はレイヤ引数が必要(引数なしはassert)。"""
    try:
        return v.GetWidth(pcbnew.F_Cu)
    except TypeError:
        return v.GetWidth()

def _resolve_pending(board, fp_deltas, pending, ops, stats):
    """変形する区間の経路を、移動後の盤面に対する当たり判定つきで決める。
    候補(L字2種+Z字3種)を順に試し、他ネットの銅にクリアランス未満で
    近づかない最初の候補を採る。全滅なら既定のL字にして overlap を数える。"""
    moved = {}
    for op in ops:
        if op[0] == "move":
            moved[_uid(op[1])] = op[2]
    pend_uids = set()
    for item, extras, _, _, _, _ in pending:
        pend_uids.add(_uid(item))
        for it in extras:
            pend_uids.add(_uid(it))

    hole_clr = 0
    try:  # 「穴-銅」のホールクリアランス(銅ルールとは別の独立した制約)
        hole_clr = board.GetDesignSettings().m_HoleClearance
    except Exception:
        pass

    obstacles = []   # ("seg", layer, a, b, width, net) / ("via", p, w, net)
    #                  / ("pad", pad, d, net) / ("hole", p, 径, net, クリアランス)
    for t in list(board.GetTracks()):
        uid = _uid(t)
        if uid in pend_uids:
            continue   # 引き直す本人の旧形状は障害物にしない
        d = moved.get(uid, (0, 0))
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            pos = (p.x + d[0], p.y + d[1])
            obstacles.append(("via", pos, _via_width(t), t.GetNetCode()))
            if hole_clr > 0:
                try:
                    dia = t.GetDrillValue()  # 未設定でも既定値を解決する
                except Exception:
                    dia = 0
                if dia > 0:
                    obstacles.append(("hole", pos, dia,
                                      t.GetNetCode(), hole_clr))
        else:
            s, e = t.GetStart(), t.GetEnd()
            obstacles.append(("seg", t.GetLayer(),
                              (s.x + d[0], s.y + d[1]), (e.x + d[0], e.y + d[1]),
                              t.GetWidth(), t.GetNetCode()))
    fp_delta_map = {fp.m_Uuid.AsString(): dv for fp, dv in fp_deltas}
    for fp in board.GetFootprints():
        d = fp_delta_map.get(fp.m_Uuid.AsString(), (0, 0))
        for p in fp.Pads():
            obstacles.append(("pad", p, d, p.GetNetCode()))
            if hole_clr > 0:
                try:
                    dia = max(p.GetDrillSize().x, p.GetDrillSize().y)
                except Exception:
                    dia = 0
                if dia > 0:
                    pos = p.GetPosition()
                    obstacles.append(("hole",
                                      (pos.x + d[0], pos.y + d[1]),
                                      dia, p.GetNetCode(), hole_clr))

    clr_cache = {}

    def seg_clear(a, b, layer, width, net):
        own_clr = _net_clearance(board, net, clr_cache)
        for ob in obstacles:
            if ob[0] == "seg":
                _, ol, oa, obp, ow, onet = ob
                if onet == net or ol != layer:
                    continue
                need = max(own_clr, _net_clearance(board, onet, clr_cache)) \
                    + width // 2 + ow // 2
                if _seg_seg_d2(a, b, oa, obp) < need * need:
                    return False
            elif ob[0] == "via":
                _, op_, ow, onet = ob
                if onet == net:
                    continue
                need = max(own_clr, _net_clearance(board, onet, clr_cache)) \
                    + width // 2 + ow // 2
                if _pt_seg_d2(op_, a, b) < need * need:
                    return False
            elif ob[0] == "hole":
                _, hp, dia, onet, hclr = ob
                if onet == net:
                    continue  # 自ネットのビア/パッドには接続してよい
                need = hclr + width // 2 + dia // 2
                if _pt_seg_d2(hp, a, b) < need * need:
                    return False
            else:
                _, pad, pd, onet = ob
                if onet == net or not pad.IsOnLayer(layer):
                    continue
                bb = pad.GetBoundingBox()
                need = max(own_clr, _net_clearance(board, onet, clr_cache)) \
                    + width // 2
                if _seg_rect_d2(a, b, bb.GetLeft() + pd[0], bb.GetTop() + pd[1],
                                bb.GetRight() + pd[0],
                                bb.GetBottom() + pd[1]) < need * need:
                    return False
        return True

    for item, extras, layer, width, net, specs in pending:
        subs = []
        for spec in specs:
            if spec[0] == "fix":
                subs.append((spec[1], spec[2]))
                continue
            _, q1, q2, diag_start = spec
            if q1 == q2:
                continue
            chosen = None
            for cand in _flex_candidates(q1, q2, diag_start):
                if all(seg_clear(a, b, layer, width, net) for a, b in cand):
                    chosen = cand
                    break
            if chosen is None:
                # 固定候補が全滅 → 遅延障害物A*で迂回路を探す
                chosen = _search_route(board, q1, q2, layer, width, net,
                                       obstacles, clr_cache)
            if not chosen:
                stats["overlap"] += 1
                pts = _octi_path(q1, q2, diag_start)
                chosen = [s for s in zip(pts, pts[1:]) if s[0] != s[1]]
            subs.extend(chosen)
        if not subs:
            # 引き直し先が完全に潰れた(移動先が接続点と一致) →
            # 古い形を残すと無意味な配線になるので削除する
            for it in [item] + extras:
                ops.append(("del", it))
                stats["deleted"] += 1
            continue
        first = True
        for a, b in subs:
            if first:
                ops.append(("set", item, a, b))
                stats["stretch"] += 1
                first = False
            else:
                ops.append(("add", a, b, width, layer, net))
                stats["added"] += 1
        for it in extras:
            ops.append(("del", it))   # runの残りの線分は新経路に置き換え済み
            stats["deleted"] += 1
        for a, b in subs:
            obstacles.append(("seg", layer, a, b, width, net))

def _uid(item):
    """SWIGプロキシはid()で同一性判定できない(罠#17)のでUUIDで比較する。"""
    return item.m_Uuid.AsString()

def _plan_runs(g, run_segs, items, infos, canon, taps, mark, static_hit,
               ops, stats, pending, touched):
    """タップなし線分を、分岐・ビア・パッド・タップ・円弧のない
    「一続きの区間(run)」にまとめて処理する。
    両端が同じ量動くrunは形を保って平行移動、
    違う量動くrunは古い形を捨てて端点間を引き直す(後段で経路選択)。"""
    via_cps = set()
    bound_cps = set()   # run境界になる点(タップ点・タップ付き線分/円弧の端点)
    deg = {}
    for i in g:
        kind, _, pts = infos[i]
        if kind == "via":
            via_cps.add(canon[pts[0]])
            continue
        for cp in taps.get(i, ()):
            bound_cps.add(cp)
        if kind == "arc" or taps.get(i):
            for p in pts:
                bound_cps.add(canon[p])
        for p in pts:
            cp = canon[p]
            deg[cp] = deg.get(cp, 0) + 1

    def terminal(cp):
        return (deg.get(cp, 0) != 2 or cp in mark or cp in static_hit
                or cp in via_cps or cp in bound_cps)

    adj = {}
    for i in run_segs:
        for p in infos[i][2]:
            adj.setdefault(canon[p], []).append(i)

    visited = set()
    for i in run_segs:
        if i in visited:
            continue
        ca, cb = canon[infos[i][2][0]], canon[infos[i][2][1]]
        if terminal(ca):
            start, cur = ca, cb
        elif terminal(cb):
            start, cur = cb, ca
        else:
            continue   # 閉路の内部 → アンカーが無いので触らない
        chain = [i]
        visited.add(i)
        cur_seg = i
        layer = infos[i][1]
        width = items[i].GetWidth()
        while not terminal(cur):
            nxt = [j for j in adj.get(cur, ()) if j != cur_seg]
            if len(nxt) != 1 or nxt[0] in visited:
                break
            j = nxt[0]
            if infos[j][1] != layer or items[j].GetWidth() != width:
                break   # 層や幅が変わる点はrun境界として扱う
            cur_seg = j
            visited.add(j)
            chain.append(j)
            e1, e2 = canon[infos[j][2][0]], canon[infos[j][2][1]]
            cur = e2 if cur == e1 else e1

        d1 = mark.get(start, (0, 0))
        d2 = mark.get(cur, (0, 0))
        if d1 == d2:
            if d1 != (0, 0):
                # run全体が同じ量だけ動く → 形を保って平行移動
                for k in chain:
                    ops.append(("move", items[k], d1))
                    touched.add(_uid(items[k]))
                stats["stretch"] += len(chain)
            continue
        # 両端の追従量が違う → runの古い形は捨てて端点間を引き直す
        q1 = (start[0] + d1[0], start[1] + d1[1])
        q2 = (cur[0] + d2[0], cur[1] + d2[1])
        pending.append((items[chain[0]], [items[k] for k in chain[1:]],
                        layer, width, items[chain[0]].GetNetCode(),
                        [("flex", q1, q2, d1 != (0, 0))]))
        for k in chain:
            touched.add(_uid(items[k]))

def _plan_net_moves(items, moving_pads, static_pads, tol, ops, stats,
                    pending, touched):
    infos = []    # (kind, layer, pts)
    all_pts = []
    for t in items:
        cls = t.GetClass()
        if cls == "PCB_VIA":
            pos = t.GetPosition()
            infos.append(("via", None, [(pos.x, pos.y)]))
        else:
            kind = "arc" if cls == "PCB_ARC" else "seg"
            s, e = t.GetStart(), t.GetEnd()
            infos.append((kind, t.GetLayer(), [(s.x, s.y), (e.x, e.y)]))
        all_pts.extend(infos[-1][2])
    canon = _canon_map(all_pts, tol)

    owners = {}   # 代表点 -> [item index]
    for i, (_, _, pts) in enumerate(infos):
        for p in pts:
            owners.setdefault(canon[p], []).append(i)

    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for idxs in owners.values():
        for j in idxs[1:]:
            parent[find(idxs[0])] = find(j)

    # 線分の途中に乗っている他アイテムの端点(T字接続)を拾う
    cps_sorted = sorted(owners)
    xs = [c[0] for c in cps_sorted]
    taps = {}   # item index -> [代表点]
    for i, (kind, _, pts) in enumerate(infos):
        if kind != "seg":
            continue
        a, b = pts
        ca, cb = canon[a], canon[b]
        ylo, yhi = min(a[1], b[1]) - tol, max(a[1], b[1]) + tol
        lo = bisect_left(xs, min(a[0], b[0]) - tol)
        hi = bisect_right(xs, max(a[0], b[0]) + tol)
        for cp in cps_sorted[lo:hi]:
            if cp == ca or cp == cb or not (ylo <= cp[1] <= yhi):
                continue
            if _pt_seg_d2(cp, a, b) <= tol * tol:
                taps.setdefault(i, []).append(cp)
                parent[find(i)] = find(owners[cp][0])

    # 追従する点のマーク付けと、動かないパッドへの接触の記録
    mark = {}         # 代表点 -> delta
    static_hit = set()
    for i, (kind, layer, pts) in enumerate(infos):
        for p in pts:
            cp = canon[p]
            hits = _pad_hits(moving_pads, p, layer)
            if hits and cp not in mark:
                mark[cp] = hits[0][2]
            if _pad_hits(static_pads, p, layer):
                static_hit.add(cp)

    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)

    for g in groups.values():
        cps = {canon[p] for i in g for p in infos[i][2]}
        seeds = {mark[cp] for cp in cps if cp in mark}
        if not seeds:
            continue
        if len(seeds) == 1 and not (cps & static_hit):
            # 連結群全体が同じ量だけ動く → 形を保ったまま平行移動
            d = seeds.pop()
            for i in g:
                ops.append(("move", items[i], d))
            stats["rigid"] += len(g)
            continue
        # 伸縮: ビアは追従移動、タップ付き線分は接続点で分割、
        # タップなし線分は「一続きの区間(run)」にまとめて区間単位で追従させる。
        # グループ内の線分はすべて掃除(_prune_dangling)の対象に含める
        for i in g:
            if infos[i][0] == "seg":
                touched.add(_uid(items[i]))
        run_segs = []
        for i in g:
            kind, layer, pts = infos[i]
            if kind == "via":
                d = mark.get(canon[pts[0]], (0, 0))
                if d != (0, 0):
                    ops.append(("move", items[i], d))
                    stats["stretch"] += 1
            elif kind == "arc":
                if any(mark.get(canon[p], (0, 0)) != (0, 0) for p in pts):
                    stats["arc_skip"] += 1
            elif taps.get(i):
                a, b = pts
                nodes = [(a, mark.get(canon[a], (0, 0)))]
                for cp in sorted(taps[i], key=lambda c: _seg_param(a, b, c)):
                    nodes.append((cp, mark.get(cp, (0, 0))))
                nodes.append((b, mark.get(canon[b], (0, 0))))
                deltas = {d for _, d in nodes}
                if deltas == {(0, 0)}:
                    continue
                if len(deltas) == 1:
                    ops.append(("move", items[i], deltas.pop()))
                    stats["stretch"] += 1
                    touched.add(_uid(items[i]))
                    continue
                # 節点ごとに追従量が違う → 接続点で分割しつつ端点を動かす
                specs = []
                for (p1, d1), (p2, d2) in zip(nodes, nodes[1:]):
                    q1 = (p1[0] + d1[0], p1[1] + d1[1])
                    q2 = (p2[0] + d2[0], p2[1] + d2[1])
                    if q1 == q2:
                        continue
                    if d1 == d2:
                        specs.append(("fix", q1, q2))   # 平行移動区間は形そのまま
                    else:
                        specs.append(("flex", q1, q2, d1 != (0, 0)))
                if specs:
                    pending.append((items[i], [], layer, items[i].GetWidth(),
                                    items[i].GetNetCode(), specs))
                    touched.add(_uid(items[i]))
            else:
                run_segs.append(i)
        _plan_runs(g, run_segs, items, infos, canon, taps, mark, static_hit,
                   ops, stats, pending, touched)

def _prune_dangling(board, fp_deltas, ops, stats, touched, tol=WIRE_TOL_NM):
    """要素(パッド/ビア/他の配線/ゾーン)につながらない端を持つ線分を、
    今回伸縮の対象にしたアイテムと追加予定の線分の範囲で連鎖的に削除する。
    ゾーンはBBox内なら接続扱い(塗り連結はヘッドレスで判定できないため保守的に残す)。
    判定は計画済みopsを適用した後の最終形状で行う。"""
    if not touched and not any(op[0] == "add" for op in ops):
        return
    eff = {}
    for op in ops:
        if op[0] in ("move", "set", "del"):
            eff.setdefault(_uid(op[1]), []).append(op)

    segs = []   # 最終形状のビュー
    vias = []
    for t in list(board.GetTracks()):
        uid = _uid(t)
        d = (0, 0)
        coords = None
        dead = False
        for op in eff.get(uid, ()):
            if op[0] == "del":
                dead = True
            elif op[0] == "move":
                d = op[2]
            elif op[0] == "set":
                coords = (op[2], op[3])
        if dead:
            continue
        cls = t.GetClass()
        if cls == "PCB_VIA":
            p = t.GetPosition()
            vias.append((t.GetNetCode(), (p.x + d[0], p.y + d[1]), _via_width(t)))
            continue
        if coords is None:
            s, e = t.GetStart(), t.GetEnd()
            coords = ((s.x + d[0], s.y + d[1]), (e.x + d[0], e.y + d[1]))
        segs.append({"net": t.GetNetCode(), "layer": t.GetLayer(),
                     "a": coords[0], "b": coords[1],
                     "src": ("item", t),
                     # 円弧は消さない(接続源としてのみ使う)
                     "scope": cls == "PCB_TRACK" and uid in touched,
                     "dead": False})
    for idx, op in enumerate(ops):
        if op[0] == "add":
            _, a, b, width, layer, net = op
            segs.append({"net": net, "layer": layer, "a": a, "b": b,
                         "src": ("add", idx), "scope": True, "dead": False})

    nets = {s["net"] for s in segs if s["scope"]}
    if not nets:
        return
    fp_delta_map = {_uid(fp): dv for fp, dv in fp_deltas}
    pads = []
    for fp in board.GetFootprints():
        d = fp_delta_map.get(_uid(fp), (0, 0))
        for p in fp.Pads():
            if p.GetNetCode() in nets:
                pads.append((p, d))
    zones = []
    try:
        for z in board.Zones():
            if z.GetIsRuleArea() or z.GetNetCode() not in nets:
                continue
            bb = z.GetBoundingBox()
            zones.append((z.GetNetCode(), z.GetLayerSet(),
                          (bb.GetLeft(), bb.GetTop(),
                           bb.GetRight(), bb.GetBottom())))
    except Exception:
        pass

    V = pcbnew.VECTOR2I
    t2 = tol * tol

    def connected(s, pt):
        for o in segs:
            if o is s or o["dead"] or o["net"] != s["net"] \
               or o["layer"] != s["layer"]:
                continue
            if _pt_seg_d2(pt, o["a"], o["b"]) <= t2:
                return True
        for vnet, vp, vw in vias:
            if vnet != s["net"]:
                continue
            r = vw // 2 + tol
            if (pt[0] - vp[0]) ** 2 + (pt[1] - vp[1]) ** 2 <= r * r:
                return True
        for p, d in pads:
            if p.GetNetCode() != s["net"] or not p.IsOnLayer(s["layer"]):
                continue
            # HitTestは移動前の形状で動くので点を逆シフトして判定
            if p.HitTest(V(pt[0] - d[0], pt[1] - d[1])):
                return True
        for znet, zlayers, (zl, zt, zr, zb) in zones:
            if znet != s["net"]:
                continue
            try:
                if not zlayers.Contains(s["layer"]):
                    continue
            except Exception:
                pass
            if zl - tol <= pt[0] <= zr + tol and zt - tol <= pt[1] <= zb + tol:
                return True
        return False

    changed = True
    while changed:
        changed = False
        for s in segs:
            if s["dead"] or not s["scope"]:
                continue
            if not connected(s, s["a"]) or not connected(s, s["b"]):
                s["dead"] = True
                changed = True

    drop_adds = set()
    for s in segs:
        if s["dead"]:
            if s["src"][0] == "add":
                drop_adds.add(s["src"][1])
            else:
                ops.append(("del", s["src"][1]))
            stats["deleted"] += 1
    if drop_adds:
        ops[:] = [op for i, op in enumerate(ops)
                  if not (op[0] == "add" and i in drop_adds)]

def apply_wire_ops(board, ops):
    V = pcbnew.VECTOR2I
    for op in ops:
        if op[0] == "move":
            _, item, d = op
            item.ClearSelected()  # 選択中アイテムの変更はクラッシュの前科あり(罠#1)
            item.Move(V(d[0], d[1]))
        elif op[0] == "set":
            _, item, a, b = op
            item.ClearSelected()
            item.SetStart(V(a[0], a[1]))
            item.SetEnd(V(b[0], b[1]))
        elif op[0] == "del":
            _, item = op
            item.ClearSelected()
            board.Remove(item)
        else:  # add
            _, a, b, width, layer, net = op
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(V(a[0], a[1]))
            t.SetEnd(V(b[0], b[1]))
            t.SetWidth(width)
            t.SetLayer(layer)
            t.SetNetCode(net)
            board.Add(t)

def pack_selected(board, align_x="left", align_y="top", move_wires=False):
    """選択フットプリントをブロック化して詰める。
    move_wires=True ならパッドにつながる配線も接続を保ったまま追従させる。
    返り値: {"moved": 動かした部品数, "wire": 配線statsまたはNone}"""
    fps = [fp for fp in board.GetFootprints() if fp.IsSelected()]
    if not fps:
        return {"moved": 0, "wire": None}

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

    # 角ボタン(両軸とも壁)はテトリス的な重力詰め。
    # 辺・中央ボタンは行ベースの詰めで、壁でない軸は中央揃えになる
    if align_x != "center" and align_y != "middle":
        gx = -1 if align_x == "left" else 1
        gy = -1 if align_y == "top" else 1
        targets = gravity_targets(blocks, gx, gy)
    else:
        targets = pack_targets(blocks, align_x, align_y)

    # 各部品の目標位置と移動量を先に確定する(計画は移動前の形状で行う)
    fp_moves = []   # (fp, target, delta)
    for block, (tx, ty) in zip(blocks, targets):
        for fp, off in block["members"]:
            target = pcbnew.VECTOR2I(tx, ty) + off
            pos = fp.GetPosition()
            fp_moves.append((fp, target, (target.x - pos.x, target.y - pos.y)))

    wire_stats = None
    wire_ops = []
    if move_wires:
        wire_ops, wire_stats = plan_wire_moves(
            board, [(fp, d) for fp, _, d in fp_moves])

    moved = 0
    for fp, target, d in fp_moves:
        if d != (0, 0):
            fp.SetPosition(target)
            moved += 1
    apply_wire_ops(board, wire_ops)
    return {"moved": moved, "wire": wire_stats}

class AlignPackDialog(wx.Dialog):
    """3x3の整列ボタン。押した位置で揃え方が決まり、即実行される。"""
    XS = ("left", "center", "right")
    YS = ("top", "middle", "bottom")
    LABELS = (("↖", "↑", "↗"),
              ("←", "●", "→"),
              ("↙", "↓", "↘"))
    JP_X = {"left": "左に寄せる", "center": "左右中央", "right": "右に寄せる"}
    JP_Y = {"top": "上に詰める", "middle": "上下中央", "bottom": "下に詰める"}

    def __init__(self, parent, last):
        super().__init__(parent, title="整列して詰める")
        self.choice = None
        outer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(
            self, label="軸ごとに「壁に寄せる/中央に揃える」を選びます\n"
                        "（角=重力詰め 例:↙は左に寄せて下に落とす、"
                        "辺=詰め+直交軸は中央、● は上下左右対称）")
        outer.Add(note, 0, wx.ALL, 8)
        grid = wx.GridSizer(3, 3, 4, 4)
        focus_btn = None
        for r, ay in enumerate(self.YS):
            for c, ax in enumerate(self.XS):
                btn = wx.Button(self, label=self.LABELS[r][c],
                                size=wx.Size(48, 48))
                if ax == "center" and ay == "middle":
                    btn.SetToolTip("上下左右対称")
                else:
                    btn.SetToolTip(f"{self.JP_X[ax]} / {self.JP_Y[ay]}")
                btn.Bind(wx.EVT_BUTTON,
                         lambda e, a=(ax, ay): self._pick(a))
                if [ax, ay] == list(last):
                    focus_btn = btn
                grid.Add(btn, 0, wx.EXPAND)
        outer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, 8)
        self.cb_wires = wx.CheckBox(
            self, label="パッドにつながる配線も一緒に動かす")
        self.cb_wires.SetValue(LAST_WIRES[0])
        outer.Add(self.cb_wires, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.cb_shorten = wx.CheckBox(
            self, label="仕上げに配線を短縮する (track_shorten 全体1回)")
        self.cb_shorten.SetValue(LAST_SHORTEN[0])
        outer.Add(self.cb_shorten, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
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
        global PRESET
        preset, PRESET = PRESET, None
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        if not any(fp.IsSelected() for fp in board.GetFootprints()):
            wx.MessageBox("先に詰めたいフットプリントを選択してください。",
                          "整列して詰める", wx.OK | wx.ICON_INFORMATION, parent)
            return
        if preset:  # パレットから: パラメータ指定済みなのでダイアログを出さない
            align_x, align_y = preset.get("align") or LAST_ALIGN
            move_wires = bool(preset.get("wires", LAST_WIRES[0]))
            do_shorten = bool(preset.get("shorten", LAST_SHORTEN[0]))
        else:  # メニューから直接: 従来の3x3ダイアログ
            dlg = AlignPackDialog(parent, LAST_ALIGN)
            try:
                if dlg.ShowModal() != wx.ID_OK or dlg.choice is None:
                    return  # ×/Escは何もしない
                align_x, align_y = dlg.choice
                move_wires = dlg.cb_wires.GetValue()
                do_shorten = dlg.cb_shorten.GetValue()
            finally:
                dlg.Destroy()
        LAST_ALIGN[:] = [align_x, align_y]
        LAST_WIRES[0] = move_wires
        LAST_SHORTEN[0] = do_shorten
        result = pack_selected(board, align_x, align_y, move_wires)
        if move_wires:
            board.BuildConnectivity()  # 配線を変えたのでラッツネストを更新
        shorten_note = ""
        if move_wires and do_shorten:
            # 整列だけでは他ネットの配線が邪魔で通せない区間が残ることが
            # ある。塞いでいる側も動かせる track_shorten を全体に1回かけて
            # 仕上げる(手動で短縮ボタンを押すのと同じ)
            try:
                import track_shorten as ts
                dlgp = wx.ProgressDialog(
                    "整列して詰める", "仕上げの短縮を実行中...",
                    maximum=1000, parent=parent,
                    style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE)

                def tick(pass_no, idx, total, replaced, gain):
                    frac = ((pass_no - 1) + idx / max(1, total)) \
                        / ts.DEFAULT_MAX_PASSES
                    cont, _ = dlgp.Update(
                        min(999, int(frac * 1000)),
                        "仕上げの短縮 %d周目 %d/%d (置換 %d)"
                        % (pass_no, idx, total, replaced))
                    wx.SafeYield(dlgp)  # ネイティブ実装はメインループを回さない(罠#11)
                    return cont
                try:
                    sres = ts.shorten_board(board, ts.default_clearance(board),
                                            False, tick=tick)
                finally:
                    dlgp.Destroy()
                board.BuildConnectivity()
                if sres["replaced"]:
                    shorten_note = ("\n(仕上げの短縮で %d 本を引き直し済み)"
                                    % sres["replaced"])
            except Exception:
                shorten_note = "\n(仕上げの短縮は実行できませんでした)"
        pcbnew.Refresh()
        w = result["wire"]
        warns = []
        if w and w["arc_skip"]:
            warns.append(f"円弧を含む配線 {w['arc_skip']} 本は追従できませんでした。\n"
                         "手で直すか、円弧を線分に置き換えてから再実行してください。")
        if w and w["overlap"]:
            warns.append(f"他の配線との重なりを避けられなかった区間が "
                         f"{w['overlap']} 箇所ありました。"
                         + (shorten_note or
                            "\ntrack_shorten で引き直すか手で調整してください。"))
        if warns:
            wx.MessageBox("\n\n".join(warns), "整列して詰める",
                          wx.OK | wx.ICON_WARNING, parent)

DensePack().register()
