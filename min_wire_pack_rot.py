"""90度回転も使って、配線が最短になる並びで隙間ゼロに詰める

選択フットプリントを最密（コートヤード枠線が距離0で密着）に詰めつつ、
配線（ラッツネスト）総延長が最短になるように配置と90度刻みの回転を最適化する。

スカイライン法: 部品を1つずつ、既存の部品に密着する候補位置（左寄せ/右寄せ）×4方向のうち
配線長が最短になる置き方を選ぶ。置く順番は焼きなまし法（部品数が少なければ全列挙）で最適化。
選択外の部品・ネットは考慮しない。

実行中は進捗ダイアログ（経過バー・ラウンド数・改善回数・評価値）を表示し、
暫定ベストの配置を基板にも随時描画する。「中止」を押すと、
その時点のベスト配置のまま確定して終了する。"""
import itertools
import math
import random
import time

import pcbnew

try:
    import wx
except ImportError:
    wx = None

ICON = "🔄"           # パレットのタイルに表示するアイコン
LABEL = "配線最短で詰める"  # パレットのタイルに表示する短い名前

PANEL = [  # パレット埋め込みUIの定義(パラメータなし、実行ボタンのみ)
    {"type": "run", "label": "実行"},
]
GAP_NM = 0            # 部品間ギャップ [nm] 例: 0.1mm なら int(0.1 * 1e6)
TIME_BUDGET_S = 60.0  # 最適化に使う時間 [秒]。増やすほど良い解になりやすい
MAX_ROW_WIDTH_MM = 0  # 配置ブロックの最大幅 [mm]。0なら全体が正方形に近づくよう自動設定
DENSITY_WEIGHT = 1.0  # 密度の重み: 全体の高さ1nm増加を配線長何nm相当として罰するか
LIVE_UPDATE_S = 0.2   # 暫定ベスト解を画面に反映する最短間隔 [秒]。0なら最後だけ描画

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
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        board = pcbnew.GetBoard()
        fps = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        if len(fps) < 2:
            if wx is not None:
                wx.MessageBox("先に詰めたいフットプリントを2つ以上選択してください。",
                              "配線最短で詰める",
                              wx.OK | wx.ICON_INFORMATION,
                              wx.FindWindowByName("PcbFrame"))
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

        cur_k = [0] * n       # 各部品の現在の向き（実測基準からの90度単位）
        last_draw = [0.0]

        def apply_placement(order):
            """orderで詰めた結果を実際の基板に反映する。何度呼んでも整合する。"""
            pos, ori, _ = pack(order)
            min_px = min(p[0] for p in pos)
            min_py = min(p[1] for p in pos)
            origin_x = min(p[0] for p in cur_pos) - min_px
            origin_y = min(p[1] for p in cur_pos) - min_py
            for i, fp in enumerate(fps):
                k = ori[i]
                for _ in range((k - cur_k[i]) % 4):
                    fp.Rotate(fp.GetPosition(), ANGLE_90)
                cur_k[i] = k
                v = next(v for v in variants[i] if v["k"] == k)
                target = pcbnew.VECTOR2I(origin_x + pos[i][0], origin_y + pos[i][1]) + v["off"]
                fp.SetPosition(target)

        progress = [None]     # wx.ProgressDialog（初回tick時に遅延生成）
        cancelled = [False]
        last_tick = [time.time()]  # 開始直後0.1秒はダイアログを出さない
        improves = [0]        # ベスト解を更新した回数（進捗表示用）

        def tick(frac, msg):
            """進捗ダイアログを更新しGUIイベントを回す。「中止」が押されたらFalse。
            ダイアログは初回呼び出し時に作る（瞬時に終わる場合は出さない）。
            Update()がイベントを処理するので「応答なし」防止も兼ねる。"""
            if wx is None or cancelled[0]:
                return not cancelled[0]
            now = time.time()
            if now - last_tick[0] < 0.1:
                return True
            last_tick[0] = now
            if progress[0] is None:
                progress[0] = wx.ProgressDialog(
                    "Min-Wire Dense Pack (rotate 90)", msg, maximum=1000,
                    parent=wx.FindWindowByName("PcbFrame"),
                    style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME,
                )
            keep = progress[0].Update(max(0, min(999, int(frac * 1000))), msg)[0]
            # ネイティブ実装のUpdate()はメインスレッドのイベントを回さないことがあるので、
            # 基板の再描画と「応答なし」防止のため明示的にyieldする(ダイアログは無効化しない)
            wx.SafeYield(progress[0])
            if not keep:
                cancelled[0] = True
            return keep

        def live_update(order):
            """暫定ベストを間引きつつ基板に描画し、進捗を目に見えるようにする。"""
            if LIVE_UPDATE_S <= 0:
                return
            now = time.time()
            if now - last_draw[0] < LIVE_UPDATE_S:
                return
            last_draw[0] = now
            apply_placement(order)
            pcbnew.Refresh()

        ABORT_NOTE = "「中止」を押すと、その時点のベスト配置で確定します"

        best_order = None
        t0 = time.time()
        try:
            if n <= 5:
                # 全列挙（回転は pack 内で貪欲に選ばれる）
                best_cost = None
                total = math.factorial(n)
                for done, perm in enumerate(itertools.permutations(range(n))):
                    if not tick(done / total,
                                "並び順を全列挙中 %d/%d\n%s" % (done, total, ABORT_NOTE)):
                        break
                    c = cost(perm)
                    if best_cost is None or c < best_cost:
                        best_cost = c
                        best_order = list(perm)
                        improves[0] += 1
                        live_update(best_order)
            else:
                # 初期順の候補: 現在の並び(上→下、左→右) と 面積の大きい順
                seeds = [
                    sorted(range(n), key=lambda i: (cur_pos[i][1], cur_pos[i][0])),
                    sorted(range(n), key=lambda i: -variants[i][0]["w"] * variants[i][0]["h"]),
                ]
                order = min(seeds, key=cost)
                best_order = list(order)
                best_cost = cost(order)

                def anneal_msg(round_no):
                    return ("焼きなまし ラウンド%d / 改善 %d回 / 評価値 %.2f mm\n%s"
                            % (round_no, improves[0],
                               pcbnew.ToMM(int(best_cost)), ABORT_NOTE))

                # 多スタート焼きなまし: ベスト解を揺らして再出発を繰り返し、局所解から脱出する
                round_len = max(TIME_BUDGET_S / 8.0, 3.0)  # 1ラウンドの長さ [秒]
                round_no = 0
                while not cancelled[0] and time.time() - t0 < TIME_BUDGET_S:
                    round_no += 1
                    order = list(best_order)
                    if round_no > 1:
                        # ベストからランダムに数箇所入れ替えて再出発（キック）
                        for _ in range(max(2, n // 4)):
                            a, b = random.sample(range(n), 2)
                            order[a], order[b] = order[b], order[a]
                    cur_cost = cost(order)
                    t_r0 = time.time()
                    budget = min(round_len, TIME_BUDGET_S - (t_r0 - t0))
                    if budget <= 0:
                        break
                    t_start = max(cur_cost * 0.05, 1.0)
                    while True:
                        elapsed = time.time() - t_r0
                        if elapsed > budget:
                            break
                        if not tick((time.time() - t0) / TIME_BUDGET_S,
                                    anneal_msg(round_no)):
                            break
                        temp = t_start * (0.001 ** (elapsed / budget))
                        a = random.randrange(n)
                        b = random.randrange(n)
                        if a == b:
                            continue
                        r = random.random()
                        if r < 0.4:
                            order[a], order[b] = order[b], order[a]
                            undo = ("swap", a, b)
                        elif r < 0.7:
                            order.insert(b, order.pop(a))
                            undo = ("ins", a, b)
                        else:
                            lo, hi = min(a, b), max(a, b)
                            order[lo:hi + 1] = reversed(order[lo:hi + 1])
                            undo = ("rev", lo, hi)
                        c = cost(order)
                        if c <= cur_cost or random.random() < math.exp((cur_cost - c) / temp):
                            cur_cost = c
                            if c < best_cost:
                                best_cost = c
                                best_order = list(order)
                                improves[0] += 1
                                live_update(best_order)
                        else:
                            if undo[0] == "swap":
                                order[undo[1]], order[undo[2]] = order[undo[2]], order[undo[1]]
                            elif undo[0] == "ins":
                                order.insert(undo[1], order.pop(undo[2]))
                            else:
                                lo, hi = undo[1], undo[2]
                                order[lo:hi + 1] = reversed(order[lo:hi + 1])
                # 山登り仕上げ: 全ペアのスワップ/挿入を改善が無くなるまで貪欲に適用
                improved = True
                while improved and not cancelled[0]:
                    improved = False
                    for a in range(n):
                        if cancelled[0]:
                            break
                        for b in range(n):
                            if a == b:
                                continue
                            if not tick(0.999,
                                        "山登り仕上げ中（全ペアの入れ替えを検証）/ 改善 %d回\n%s"
                                        % (improves[0], ABORT_NOTE)):
                                break
                            order = list(best_order)
                            order[a], order[b] = order[b], order[a]
                            c = cost(order)
                            if c < best_cost:
                                best_cost = c
                                best_order = order
                                improved = True
                                improves[0] += 1
                                live_update(best_order)
                                continue
                            order = list(best_order)
                            order.insert(b, order.pop(a))
                            c = cost(order)
                            if c < best_cost:
                                best_cost = c
                                best_order = order
                                improved = True
                                improves[0] += 1
                                live_update(best_order)
        finally:
            # 例外・中止のどちらでも進捗ダイアログを確実に閉じる
            if progress[0] is not None:
                progress[0].Destroy()
                progress[0] = None

        # 配置を適用（配置ブロックの左上を現在の選択範囲の左上に合わせる）
        if best_order is None:
            return  # 開始直後に中止された場合は何も変えない
        apply_placement(best_order)
        pcbnew.Refresh()

MinWirePackRot().register()
