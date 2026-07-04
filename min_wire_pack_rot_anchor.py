# min_wire_pack_rot_anchor.py — アンカー考慮版の最短配線・最密配置。
# 選択フットプリント（ロックされていないもの）を最密（コートヤード距離0）に詰めつつ、
# 配置と90度刻みの回転、さらに「ブロック全体をどこに置くか」を最適化する。
# アンカー（固定点）:
#   - 選択中でもロックされたフットプリントは動かさず、その位置を固定点として扱う
#   - 選択外のフットプリントのうち、選択部品とネットを共有するもののパッドも固定点になる
# これにより「ICの近くの正しい側にデカップリングコンデンサ群を寄せる」といった配置ができる。
# ブロックはアンカー部品のコートヤードと重ならない位置に置かれる。
import itertools
import math
import random
import time

import pcbnew

GAP_NM = 0            # 部品間ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)
TIME_BUDGET_S = 2.0   # 最適化に使う時間 [秒]。増やすほど良い解になりやすい
MAX_ROW_WIDTH_MM = 0  # 配置ブロックの最大幅 [mm]。0なら全体が正方形に近づくよう自動設定
DENSITY_WEIGHT = 1.0  # 密度の重み: 全体の高さ1nm増加を配線長何nm相当として罰するか

try:
    ANGLE_90 = pcbnew.ANGLE_90
except AttributeError:
    ANGLE_90 = pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T)

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

def measure_variants(fp):
    """フットプリントを実際に90度ずつ回して4方向の形状を実測し、元の向きに戻す。
    対称な向きは除外する。"""
    variants = []
    seen = set()
    anchor = fp.GetPosition()
    for k in range(4):
        bb = courtyard_bbox(fp)
        topleft = pcbnew.VECTOR2I(bb.GetLeft(), bb.GetTop())
        pads = []
        for pad in fp.Pads():
            net = pad.GetNetCode()
            if net <= 0:
                continue
            rel = pad.GetPosition() - topleft
            pads.append((net, rel.x, rel.y))
        pads.sort()
        key = (bb.GetWidth(), bb.GetHeight(), tuple(pads))
        if key not in seen:
            seen.add(key)
            variants.append({
                "k": k,
                "w": bb.GetWidth() + GAP_NM,
                "h": bb.GetHeight() + GAP_NM,
                "off": fp.GetPosition() - topleft,
                "pads": pads,
                "bb": bb,
            })
        fp.Rotate(anchor, ANGLE_90)  # 4回で元の向きに戻る
    return variants

class MinWirePackRotAnchor(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Min-Wire Pack Selected (rotate 90, anchor-aware)"
        self.category = "Placement"
        self.description = ("Pack selected footprints densely near their fixed anchors "
                            "(locked/unselected connected parts) for shortest ratsnest")
        self.show_toolbar_button = True

    def Run(self):
        board = pcbnew.GetBoard()
        selected = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        movable = [fp for fp in selected if not fp.IsLocked()]
        n = len(movable)
        if n == 0:
            return

        variants = []   # variants[i] = [{k, w, h, off, pads, bb}, ...]
        cur_pos = []    # 現在のBBox左上
        net_items = {}  # netcode -> このネットを持つ可動部品indexの集合
        for idx, fp in enumerate(movable):
            vs = measure_variants(fp)
            variants.append(vs)
            cur_pos.append((vs[0]["bb"].GetLeft(), vs[0]["bb"].GetTop()))
            for net, _, _ in vs[0]["pads"]:
                net_items.setdefault(net, set()).add(idx)
        movable_nets = set(net_items)

        # 固定アンカー: 可動部品以外（選択外 + ロック済み）のパッドのうち、
        # 可動部品とネットを共有するもの。所属フットプリントは障害物として避ける。
        fixed_boxes = {}  # net -> [min_x, max_x, min_y, max_y] (絶対座標)
        obstacles = []    # (left, top, right, bottom)
        movable_ids = {fp.m_Uuid.AsString() for fp in movable}
        for fp in board.GetFootprints():
            if fp.m_Uuid.AsString() in movable_ids:
                continue
            hit = False
            for pad in fp.Pads():
                net = pad.GetNetCode()
                if net not in movable_nets:
                    continue
                p = pad.GetPosition()
                b = fixed_boxes.get(net)
                if b is None:
                    fixed_boxes[net] = [p.x, p.x, p.y, p.y]
                else:
                    if p.x < b[0]: b[0] = p.x
                    elif p.x > b[1]: b[1] = p.x
                    if p.y < b[2]: b[2] = p.y
                    elif p.y > b[3]: b[3] = p.y
                hit = True
            if hit:
                ob = courtyard_bbox(fp)
                obstacles.append((ob.GetLeft(), ob.GetTop(), ob.GetRight(), ob.GetBottom()))

        if n < 2 and not fixed_boxes:
            return  # 動かす意味がない

        # 評価対象: 可動2部品以上にまたがるネット、または固定点を持つネット
        eval_nets = {net for net, idxs in net_items.items()
                     if len(idxs) >= 2 or net in fixed_boxes}
        for vs in variants:
            for v in vs:
                v["pads"] = [p for p in v["pads"] if p[0] in eval_nets]

        # 配置ブロックの最大幅
        if MAX_ROW_WIDTH_MM > 0:
            max_w = pcbnew.FromMM(MAX_ROW_WIDTH_MM)
        else:
            total_area = sum(vs[0]["w"] * vs[0]["h"] for vs in variants)
            max_w = int(math.sqrt(total_area) * 1.15)
        max_w = max(max_w, max(min(v["w"] for v in vs) for vs in variants))

        def pack(order, seed_boxes):
            """orderの順にスカイライン詰め。seed_boxes（ブロック座標系に直した固定点）を
            見ながら、各部品は 4方向×候補位置 のうちコスト最小の置き方を選ぶ。
            戻り値: (左上座標リスト, 採用方向リスト, 可動パッドのみのネットBBox,
                     高さ, ブロックの範囲(minx,miny,maxx,maxy))"""
            segments = [[0, max_w, 0]]
            boxes = {net: list(b) for net, b in seed_boxes.items()}  # 固定+可動（評価用）
            mboxes = {}                                              # 可動のみ（平行移動最適化用）
            pos = [None] * n
            ori = [0] * n
            height = 0
            ext = None  # ブロック範囲
            for i in order:
                best = None
                best_put = None
                for v in variants[i]:
                    w, h, pads = v["w"], v["h"], v["pads"]
                    if w > max_w:
                        continue
                    cands = set()
                    for s0, s1, _ in segments:
                        for x in (s0, s1 - w):
                            if 0 <= x and x + w <= max_w:
                                cands.add(x)
                    for x in cands:
                        y = 0
                        for s0, s1, sy in segments:
                            if s1 <= x or s0 >= x + w:
                                continue
                            if sy > y:
                                y = sy
                        tent = {}
                        for net, rx, ry in pads:
                            px, py = x + rx, y + ry
                            b = tent.get(net)
                            if b is None:
                                b0 = boxes.get(net)
                                if b0 is None:
                                    tent[net] = [px, px, py, py]
                                else:
                                    tent[net] = [min(b0[0], px), max(b0[1], px),
                                                 min(b0[2], py), max(b0[3], py)]
                            else:
                                if px < b[0]: b[0] = px
                                elif px > b[1]: b[1] = px
                                if py < b[2]: b[2] = py
                                elif py > b[3]: b[3] = py
                        wl = sum((b[1] - b[0]) + (b[3] - b[2]) for b in tent.values())
                        grow = y + h - height
                        if grow < 0:
                            grow = 0
                        score = (wl + int(DENSITY_WEIGHT * grow), y, x, v["k"])
                        if best is None or score < best:
                            best = score
                            best_put = (v, x, y)
                v, x, y = best_put
                w, h = v["w"], v["h"]
                for net, rx, ry in v["pads"]:
                    px, py = x + rx, y + ry
                    for target in (boxes, mboxes):
                        b = target.get(net)
                        if b is None:
                            target[net] = [px, px, py, py]
                        else:
                            if px < b[0]: b[0] = px
                            elif px > b[1]: b[1] = px
                            if py < b[2]: b[2] = py
                            elif py > b[3]: b[3] = py
                new_segs = []
                for s0, s1, sy in segments:
                    if s1 <= x or s0 >= x + w:
                        new_segs.append([s0, s1, sy])
                    else:
                        if s0 < x:
                            new_segs.append([s0, x, sy])
                        if s1 > x + w:
                            new_segs.append([x + w, s1, sy])
                new_segs.append([x, x + w, y + h])
                new_segs.sort()
                segments = new_segs
                pos[i] = (x, y)
                ori[i] = v["k"]
                if y + h > height:
                    height = y + h
                if ext is None:
                    ext = [x, y, x + w, y + h]
                else:
                    if x < ext[0]: ext[0] = x
                    if y < ext[1]: ext[1] = y
                    if x + w > ext[2]: ext[2] = x + w
                    if y + h > ext[3]: ext[3] = y + h
            return pos, ori, mboxes, height, ext

        def optimal_offset(mboxes):
            """固定点とのHPWL合計を最小にするブロックの平行移動量。
            各軸独立の凸区分線形関数なので、折れ点の中央値が最適。"""
            bps_x, bps_y = [], []
            for net, m in mboxes.items():
                f = fixed_boxes.get(net)
                if f is None:
                    continue
                a, b = f[0] - m[0], f[1] - m[1]
                bps_x += [min(a, b), max(a, b)]
                a, b = f[2] - m[2], f[3] - m[3]
                bps_y += [min(a, b), max(a, b)]
            if not bps_x:
                return None
            bps_x.sort()
            bps_y.sort()
            return (bps_x[(len(bps_x) - 1) // 2], bps_y[(len(bps_y) - 1) // 2])

        def total_cost(mboxes, t, height):
            tot = 0
            for net, m in mboxes.items():
                x0, x1 = m[0] + t[0], m[1] + t[0]
                y0, y1 = m[2] + t[1], m[3] + t[1]
                f = fixed_boxes.get(net)
                if f is not None:
                    if f[0] < x0: x0 = f[0]
                    if f[1] > x1: x1 = f[1]
                    if f[2] < y0: y0 = f[2]
                    if f[3] > y1: y1 = f[3]
                tot += (x1 - x0) + (y1 - y0)
            return tot + int(DENSITY_WEIGHT * height)

        sel_left = min(p[0] for p in cur_pos)
        sel_top = min(p[1] for p in cur_pos)

        def evaluate(order):
            """パック→最適平行移動を2回繰り返して評価。
            戻り値: (コスト, pos, ori, 平行移動量t, ext, mboxes, height)"""
            t = (sel_left, sel_top)  # 初期推定: 現在の選択範囲の左上
            result = None
            for _ in range(2):
                seed = {net: [f[0] - t[0], f[1] - t[0], f[2] - t[1], f[3] - t[1]]
                        for net, f in fixed_boxes.items()}
                pos, ori, mboxes, height, ext = pack(order, seed)
                t2 = optimal_offset(mboxes)
                if t2 is None:
                    t = (sel_left - ext[0], sel_top - ext[1])  # アンカー無し: 従来動作
                    result = (pos, ori, mboxes, height, ext)
                    break
                t = t2
                result = (pos, ori, mboxes, height, ext)
            pos, ori, mboxes, height, ext = result
            return (total_cost(mboxes, t, height), pos, ori, t, ext, mboxes, height)

        def cost(order):
            return evaluate(order)[0]

        t0 = time.time()
        if n <= 5:
            best_order = None
            best_cost = None
            for perm in itertools.permutations(range(n)):
                c = cost(perm)
                if best_cost is None or c < best_cost:
                    best_cost = c
                    best_order = list(perm)
        else:
            seeds = [
                sorted(range(n), key=lambda i: (cur_pos[i][1], cur_pos[i][0])),
                sorted(range(n), key=lambda i: -variants[i][0]["w"] * variants[i][0]["h"]),
            ]
            order = min(seeds, key=cost)
            best_order = list(order)
            best_cost = cur_cost = cost(order)
            t_start = max(best_cost * 0.05, 1.0)
            while True:
                elapsed = time.time() - t0
                if elapsed > TIME_BUDGET_S:
                    break
                temp = t_start * (0.001 ** (elapsed / TIME_BUDGET_S))
                a = random.randrange(n)
                b = random.randrange(n)
                if a == b:
                    continue
                if random.random() < 0.5:
                    order[a], order[b] = order[b], order[a]
                    undo = ("swap", a, b)
                else:
                    order.insert(b, order.pop(a))
                    undo = ("ins", a, b)
                c = cost(order)
                if c <= cur_cost or random.random() < math.exp((cur_cost - c) / temp):
                    cur_cost = c
                    if c < best_cost:
                        best_cost = c
                        best_order = list(order)
                else:
                    if undo[0] == "swap":
                        order[a], order[b] = order[b], order[a]
                    else:
                        order.insert(a, order.pop(b))

        _, pos, ori, t, ext, mboxes, height = evaluate(best_order)

        # アンカー部品のコートヤードと重なるなら、重ならない位置のうちコスト最小へ押し出す
        def block_rect(t):
            return (t[0] + ext[0], t[1] + ext[1], t[0] + ext[2], t[1] + ext[3])

        def overlaps_any(t):
            l, tp, r, btm = block_rect(t)
            for ol, ot, orr, ob in obstacles:
                if l < orr and ol < r and tp < ob and ot < btm:
                    return True
            return False

        if obstacles and overlaps_any(t):
            cands = []
            for ol, ot, orr, ob in obstacles:
                cands.append((ol - ext[2], t[1]))   # 障害物の左に
                cands.append((orr - ext[0], t[1]))  # 右に
                cands.append((t[0], ot - ext[3]))   # 上に
                cands.append((t[0], ob - ext[1]))   # 下に
            feas = [c for c in cands if not overlaps_any(c)]
            if feas:
                t = min(feas, key=lambda c: total_cost(mboxes, c, height))
            else:
                # 縦横同時にずらす候補も試す
                cands2 = []
                for ol, ot, orr, ob in obstacles:
                    for cx in (ol - ext[2], orr - ext[0]):
                        for cy in (ot - ext[3], ob - ext[1]):
                            cands2.append((cx, cy))
                feas = [c for c in cands2 if not overlaps_any(c)]
                if feas:
                    t = min(feas, key=lambda c: total_cost(mboxes, c, height))
                # それでも見つからなければ重なりを許容してそのまま置く

        # 配置を適用
        for i, fp in enumerate(movable):
            k = ori[i]
            v = next(v for v in variants[i] if v["k"] == k)
            for _ in range(k):
                fp.Rotate(fp.GetPosition(), ANGLE_90)  # 実測時と同方向に回す
            target = pcbnew.VECTOR2I(t[0] + pos[i][0], t[1] + pos[i][1]) + v["off"]
            fp.SetPosition(target)

        pcbnew.Refresh()

MinWirePackRotAnchor().register()
