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
動かない側にもつながる配線は接続点だけ追従して伸びる。
伸びる区間は45度+軸方向のL字で引くので斜め線にはならない
(途中にT字接続がある線分は接続点で分割して接続を保つ)。
円弧は伸縮できないため追従せず、件数を警告する。
移動先の重なり(DRC)はチェックしない。遠回りが残ったら
track_shorten で引き直すと短くなる。

最短配線などは考慮しない、単純な「隙間詰め」ツール。
部品を意図した並びに置いてから実行すると、その並びのままギャップ0になる。"""
from bisect import bisect_left, bisect_right

import pcbnew
import wx

ICON = "🧱"  # パレットのカードに表示するアイコン
GAP_NM = 0   # ブロック間ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)
TOUCH_TOL_NM = 1000  # コートヤードが「接している」とみなす距離の許容 [nm]

WIRE_TOL_NM = 1000   # 配線の点一致/T字接触とみなす距離の許容 [nm]

ROW_OVERLAP_FRAC = 0.5  # 同じ行とみなすY重なりの下限(低い方の高さに対する割合)

ALIGN_X = {"left": 0.0, "center": 0.5, "right": 1.0}   # 行同士の横揃え
ALIGN_Y = {"top": 0.0, "middle": 0.5, "bottom": 1.0}   # 行内の縦揃え
LAST_ALIGN = ["center", "middle"]  # セッション内で最後に選んだボタンを覚える
LAST_WIRES = [True]                # 「配線も動かす」チェックの前回値

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
    stats = {"rigid": 0, "stretch": 0, "added": 0, "arc_skip": 0}
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

    for items in by_net.values():
        _plan_net_moves(items, moving_pads, static_pads, tol, ops, stats)
    return ops, stats

def _plan_net_moves(items, moving_pads, static_pads, tol, ops, stats):
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
        # 伸縮: 接続点だけ追従させる
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
            else:
                a, b = pts
                nodes = [(a, mark.get(canon[a], (0, 0)))]
                for cp in sorted(taps.get(i, ()),
                                 key=lambda c: _seg_param(a, b, c)):
                    nodes.append((cp, mark.get(cp, (0, 0))))
                nodes.append((b, mark.get(canon[b], (0, 0))))
                deltas = {d for _, d in nodes}
                if deltas == {(0, 0)}:
                    continue
                if len(deltas) == 1:
                    ops.append(("move", items[i], deltas.pop()))
                    stats["stretch"] += 1
                    continue
                # 節点ごとに追従量が違う → 接続点で分割しつつ端点を動かす。
                # 変形する区間は45度+軸のL字にして斜め線を作らない
                subs = []
                for (p1, d1), (p2, d2) in zip(nodes, nodes[1:]):
                    q1 = (p1[0] + d1[0], p1[1] + d1[1])
                    q2 = (p2[0] + d2[0], p2[1] + d2[1])
                    if q1 == q2:
                        continue
                    if d1 == d2:
                        subs.append((q1, q2))  # 平行移動区間は形そのまま
                    else:
                        opts = _octi_path(q1, q2, diag_at_start=(d1 != (0, 0)))
                        subs.extend(zip(opts, opts[1:]))
                if not subs:
                    continue
                first = True
                for q1, q2 in subs:
                    if first:
                        ops.append(("set", items[i], q1, q2))
                        stats["stretch"] += 1
                        first = False
                    else:
                        ops.append(("add", q1, q2, items[i].GetWidth(),
                                    layer, items[i].GetNetCode()))
                        stats["added"] += 1

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
            move_wires = dlg.cb_wires.GetValue()
        finally:
            dlg.Destroy()
        LAST_ALIGN[:] = [align_x, align_y]
        LAST_WIRES[0] = move_wires
        result = pack_selected(board, align_x, align_y, move_wires)
        if move_wires:
            board.BuildConnectivity()  # 配線を変えたのでラッツネストを更新
        pcbnew.Refresh()
        w = result["wire"]
        if w and w["arc_skip"]:
            wx.MessageBox(
                f"円弧を含む配線 {w['arc_skip']} 本は追従できませんでした。\n"
                "手で直すか、円弧を線分に置き換えてから再実行してください。",
                "整列して詰める", wx.OK | wx.ICON_WARNING, parent)

DensePack().register()
