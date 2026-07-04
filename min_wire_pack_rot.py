# min_wire_pack_rot.py — 選択フットプリントを最密（コートヤード枠線が距離0で密着）に詰めつつ、
# 配線（ラッツネスト）総延長が最短になるように配置と90度刻みの回転を最適化する。
# スカイライン法: 部品を1つずつ、既存の部品に密着する候補位置（左寄せ/右寄せ）×4方向のうち
# 配線長が最短になる置き方を選ぶ。置く順番は焼きなまし法（部品数が少なければ全列挙）で最適化。
# 選択外の部品・ネットは考慮しない。
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
    各方向について (k, w, h, off, pads) を返す。
      k: 現在の向きから +90度×k 回すことを意味する
      off: フットプリント原点 - コートヤードBBox左上
      pads: [(netcode, BBox左上からの相対x, 相対y), ...]
    形状もパッドも同一になる向き（対称部品）は除外する。"""
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

class MinWirePackRot(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Min-Wire Dense Pack Selected (rotate 90)"
        self.category = "Placement"
        self.description = "Pack selected footprints densely, optimizing position and 90-deg rotation for shortest ratsnest"
        self.show_toolbar_button = True

    def Run(self):
        board = pcbnew.GetBoard()
        fps = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        if len(fps) < 2:
            return

        n = len(fps)
        variants = []   # variants[i] = [{k, w, h, off, pads}, ...]
        cur_pos = []    # 現在のBBox左上（基準点合わせ用）
        net_items = {}  # netcode -> このネットを持つ部品indexの集合
        for idx, fp in enumerate(fps):
            vs = measure_variants(fp)
            variants.append(vs)
            cur_pos.append((vs[0]["bb"].GetLeft(), vs[0]["bb"].GetTop()))
            for net, _, _ in vs[0]["pads"]:
                net_items.setdefault(net, set()).add(idx)

        # 配置で長さが変わるのは、選択内の2部品以上にまたがるネットだけ
        shared = {net for net, idxs in net_items.items() if len(idxs) >= 2}
        for vs in variants:
            for v in vs:
                v["pads"] = [p for p in v["pads"] if p[0] in shared]

        # 配置ブロックの最大幅
        if MAX_ROW_WIDTH_MM > 0:
            max_w = pcbnew.FromMM(MAX_ROW_WIDTH_MM)
        else:
            total_area = sum(vs[0]["w"] * vs[0]["h"] for vs in variants)
            max_w = int(math.sqrt(total_area) * 1.15)
        # どの部品も最低1方向は収まる幅を保証
        max_w = max(max_w, max(min(v["w"] for v in vs) for vs in variants))

        def pack(order):
            """orderの順にスカイライン詰め。各部品は 4方向×候補位置 のうち
            (配線長 + 高さ増加ペナルティ) が最小の置き方を選ぶ。
            戻り値: (左上座標リスト, 採用方向リスト, 総コスト)"""
            segments = [[0, max_w, 0]]  # スカイライン: [x開始, x終了, y]
            boxes = {}                  # net -> [min_x, max_x, min_y, max_y] (配置済みパッド)
            pos = [None] * n
            ori = [0] * n
            height = 0
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
                        # このパッド群を加えたときの関係ネットのHPWL
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
                # 確定: ネットBBoxとスカイラインを更新
                for net, rx, ry in v["pads"]:
                    px, py = x + rx, y + ry
                    b = boxes.get(net)
                    if b is None:
                        boxes[net] = [px, px, py, py]
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
            total = sum((b[1] - b[0]) + (b[3] - b[2]) for b in boxes.values())
            total += int(DENSITY_WEIGHT * height)
            return pos, ori, total

        def cost(order):
            return pack(order)[2]

        t0 = time.time()
        if n <= 5:
            # 全列挙（回転は pack 内で貪欲に選ばれる）
            best_order = None
            best_cost = None
            for perm in itertools.permutations(range(n)):
                c = cost(perm)
                if best_cost is None or c < best_cost:
                    best_cost = c
                    best_order = list(perm)
        else:
            # 初期順の候補: 現在の並び(上→下、左→右) と 面積の大きい順
            seeds = [
                sorted(range(n), key=lambda i: (cur_pos[i][1], cur_pos[i][0])),
                sorted(range(n), key=lambda i: -variants[i][0]["w"] * variants[i][0]["h"]),
            ]
            order = min(seeds, key=cost)
            best_order = list(order)
            best_cost = cur_cost = cost(order)
            # 焼きなまし法（スワップ・挿入）で順番を最適化
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

        # 配置を適用（配置ブロックの左上を現在の選択範囲の左上に合わせる）
        pos, ori, _ = pack(best_order)
        min_px = min(p[0] for p in pos)
        min_py = min(p[1] for p in pos)
        origin_x = min(p[0] for p in cur_pos) - min_px
        origin_y = min(p[1] for p in cur_pos) - min_py
        for i, fp in enumerate(fps):
            k = ori[i]
            v = next(v for v in variants[i] if v["k"] == k)
            for _ in range(k):
                fp.Rotate(fp.GetPosition(), ANGLE_90)  # 実測時と同方向に回す
            target = pcbnew.VECTOR2I(origin_x + pos[i][0], origin_y + pos[i][1]) + v["off"]
            fp.SetPosition(target)

        pcbnew.Refresh()

MinWirePackRot().register()
