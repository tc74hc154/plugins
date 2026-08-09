"""遠回りしている配線を45度配線で引き直して短くする

配線(トラック)を選択して実行するとその配線だけ、何も選択していなければ
全配線を対象に、遠回りしている配線を1本ずつ安全に引き直して短くする。

- ビア・分岐点(T字)・円弧との接続点は動かさず、その間の同一層の
  セグメント連鎖だけを引き直す(ビアの追加・削除はしない)
- 端子がパッドの場合は、元の取り付き点ではなく**パッド中心を目標**に
  引き直す(パッドtoパッドの最短化)。パッド内で閉じた冗長な尻尾は削除
- セグメントの「途中」にセンターライン上で乗っているT字タップ・ビア・
  パッド通過点も分岐点として認識する(そこで連鎖を分割する)
- 数十µmズレて重なっている端点合流は1つの接続点として吸着する
  (手配線の「ほぼ同じ点」。許容は JOIN_TOL_NM = 0.1mm)
- 分岐点にぶら下がる行き止まりのヒゲ(どこにも繋がらないスタブ)は、
  合流先の連鎖を引き直すときに一緒に削除する。パッド・ビアに繋がる枝や
  配線途中の宙ぶらりんの配線は消さない
- 経路は45度制約(水平/垂直/斜め)。障害物の角を結ぶ可視グラフ+A*で
  探索する連続空間方式なので、グリッドに依存しない
- 他ネットの配線・ビア・パッド・基板外形(Edge.Cuts)とはクリアランスを
  保つ(衝突判定はKiCad自身のエンジンを使用)
- 「部品の下も通る」を外すとコートヤードも障害物になる
- 今より厳密に短くなり、かつ無衝突のときだけ置き換える。全体を何周か
  繰り返し、改善が無くなった周で自動終了(1本の短縮が別の配線の
  障害物をどけるので、周回に意味がある)

円弧を含む配線・途中で幅が変わる配線・ロックされた配線と、センターライン
から外れた位置で同ネットの銅が接触している連鎖は触らない(引き直すと
切れるため)。ベタ(ゾーン)は障害物として見ないので、実行後に B キーで
塗り直すこと。
"""
import heapq
import math
import time
import traceback

import pcbnew

try:
    import wx
except ImportError:
    wx = None

ICON = "✂️"
DEFAULT_MAX_PASSES = 5
DEFAULT_CLEARANCE_MM = 0.2  # ネットクラスから取れないときの既定値
MIN_GAIN_NM = 1000          # これ(1µm)以上縮まないなら置き換えない
NODE_MARGIN_NM = 5000       # 障害物の角ノードに足す余白(5µm)
MAX_NODES = 800             # 1本あたりの探索ノード数上限(迂回の小さい角を優先)
SEARCH_TIME_S = 10.0        # 1本あたりの探索時間の上限[秒](切れたら暫定解を採用)
NODE_DEDUP_NM = 100000      # 近接ノードの間引きグリッド(0.1mm)
ON_LINE_TOL_NM = 10         # センターラインに「乗っている」とみなす距離
JOIN_TOL_NM = 100000        # 同ネット端点の吸着許容(0.1mm)。手配線の
                            # 「ほぼ同じ点」のズレを1つの接続点として扱う
STUB_MAX_MM = 3.0           # 吸収削除する行き止まりヒゲの最大長
                            # (これより長い宙ぶらりんは配線途中かもしれないので残す)
SQRT2_1 = math.sqrt(2.0) - 1.0

V = pcbnew.VECTOR2I


# ---------------------------------------------------------------- 幾何ユーティリティ

def octi(a, b):
    """45度制約下での2点間の理論最短長(octilinear距離)。"""
    dx = abs(b[0] - a[0])
    dy = abs(b[1] - a[1])
    if dx < dy:
        dx, dy = dy, dx
    return dx + SQRT2_1 * dy


def polyline_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pts, pts[1:]))


def bend_candidates(a, b):
    """a→bの45度制約・理論最短長の接続候補(中間点リスト)を列挙する。

    曲げ1回のL字2種に加え、斜め区間を中間に置くZ字(曲げ2回)を数本試す。
    45度制約では等長の最短経路が無数にあり、密集地帯ではL字2種が両方
    塞がっていてもZ字なら通ることがよくある。どれも長さは同じoctil距離。
    """
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if adx == 0 or ady == 0 or adx == ady:
        return [[]]  # 水平/垂直/45度の直線1本
    m = min(adx, ady)
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    cands = [[(a[0] + sx * m, a[1] + sy * m)],   # 斜め→軸
             [(b[0] - sx * m, b[1] - sy * m)]]   # 軸→斜め
    rest = (adx if adx > ady else ady) - m       # 軸方向だけで進む長さ
    for f in (0.5, 0.25, 0.75):                  # 軸→斜め→軸(Z字)
        r1 = int(rest * f)
        if r1 <= 0 or r1 >= rest:
            continue
        if adx > ady:
            p1 = (a[0] + sx * r1, a[1])
        else:
            p1 = (a[0], a[1] + sy * r1)
        cands.append([p1, (p1[0] + sx * m, p1[1] + sy * m)])
    return cands


def simplify(pts):
    """連続する同方向セグメントを1本にまとめる(重複点も除去)。"""
    out = [pts[0]]
    for p in pts[1:]:
        if p == out[-1]:
            continue
        if len(out) >= 2:
            ax, ay = out[-2]
            bx, by = out[-1]
            d1 = (bx - ax, by - ay)
            d2 = (p[0] - bx, p[1] - by)
            if d1[0] * d2[1] - d1[1] * d2[0] == 0 and \
               d1[0] * d2[0] + d1[1] * d2[1] > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def _pt(v):
    return (v.x, v.y)


def _uid(item):
    """SWIGはイテレーションごとに別プロキシを返すので、同一性はUUIDで見る。"""
    return item.m_Uuid.AsString()


def seg_length(t):
    a, b = t.GetStart(), t.GetEnd()
    return math.hypot(b.x - a.x, b.y - a.y)


# ---------------------------------------------------------------- 基板スナップショット

def snapshot(board):
    """基板アイテムをPythonリストに写して以降はこれだけを見る。

    GUI実行中に board.GetTracks() 等のSWIGコンテナ列挙が稀に
    'SwigPyObject not iterable' で壊れる(実測)ため、列挙は周の頭の
    ここ1回に集約し、失敗したら少し待ってやり直す。
    """
    last = None
    for _ in range(3):
        try:
            return {
                "tracks": list(board.GetTracks()),
                "pads": list(board.GetPads()),
                "footprints": list(board.GetFootprints()),
                "edges": [d for d in board.GetDrawings()
                          if d.GetLayer() == pcbnew.Edge_Cuts],
                "zones": list(board.Zones()),
            }
        except Exception as exc:
            last = exc
            time.sleep(0.05)
    raise last


# ---------------------------------------------------------------- 連鎖(コネクション)抽出

def build_chains(board, snap=None):
    """端子(パッド/ビア/分岐/自由端/円弧端)間の同一層セグメント連鎖を列挙する。

    KiCadはセグメントを分割せずにT字タップを作れるため、セグメントの途中に
    センターライン上で乗っている同ネットのトラック端点・ビア・パッド中心では
    連鎖を論理分割する。分岐点にぶら下がる行き止まりスタブは合流先の連鎖の
    extra_edges として吸収する(引き直すときに一緒に削除される)。
    """
    if snap is None:
        snap = snapshot(board)
    straight = []
    arc_ends = {}   # (net, layer) -> {pt}
    via_pts = {}    # net -> {pt}
    for t in snap["tracks"]:
        cls = t.GetClass()
        if cls == "PCB_TRACK":
            if _pt(t.GetStart()) != _pt(t.GetEnd()):
                straight.append(t)
        elif cls == "PCB_ARC":
            key = (t.GetNetCode(), t.GetLayer())
            arc_ends.setdefault(key, set()).update(
                [_pt(t.GetStart()), _pt(t.GetEnd())])
        elif cls == "PCB_VIA":
            via_pts.setdefault(t.GetNetCode(), set()).add(_pt(t.GetPosition()))

    pads_by_net = {}
    for p in snap["pads"]:
        pads_by_net.setdefault(p.GetNetCode(), []).append(p)

    groups = {}
    for t in straight:
        groups.setdefault((t.GetNetCode(), t.GetLayer()), []).append(t)

    chains = []
    for (net, layer), group in groups.items():
        chains.extend(_group_chains(net, layer, group,
                                    via_pts, arc_ends, pads_by_net))
    return chains


def _build_canon(points, linked=frozenset()):
    """近接する点(チェビシェフ距離 ≤ JOIN_TOL_NM)を代表点へ吸着する写像。

    ソートしてから代表点アンカー方式で束ねるので決定的で、クラスタ径は
    最大でも 2×JOIN_TOL_NM に収まる(トラック幅より十分小さいので、
    代表点に張り直しても銅の重なりで導通は保たれる)。

    linked は実セグメントで直接結ばれている点対の集合。実配線で繋がって
    いる2点は「ズレ」ではないので吸着しない(45度経路の曲げ端数の微小
    セグメントを潰すと、端点ズレ扱い→張り直し→また端数…の無限往復になる)。
    """
    canon = {}
    reps = {}
    cell = JOIN_TOL_NM
    for p in sorted(set(points)):
        cx, cy = p[0] // cell, p[1] // cell
        rep = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for r in reps.get((cx + dx, cy + dy), ()):
                    if frozenset((p, r)) in linked:
                        continue
                    if abs(p[0] - r[0]) <= cell and abs(p[1] - r[1]) <= cell:
                        rep = r
                        break
                if rep:
                    break
            if rep:
                break
        if rep is None:
            reps.setdefault((cx, cy), []).append(p)
            canon[p] = p
        else:
            canon[p] = rep
    return canon


def _group_chains(net, layer, group, via_pts, arc_ends, pads_by_net):
    """1つの(ネット, 層)グループの連鎖を列挙する。"""
    vset = via_pts.get(net, set())
    aset = arc_ends.get((net, layer), set())
    pads = pads_by_net.get(net, [])

    def on_pad(pt):
        for p in pads:
            bb = p.GetBoundingBox()
            if bb.GetLeft() <= pt[0] <= bb.GetRight() and \
               bb.GetTop() <= pt[1] <= bb.GetBottom() and \
               p.HitTest(V(pt[0], pt[1])):
                return True
        return False


    def attached(pt):
        """その点に配線以外の実体(ビア/円弧/パッド)が居るか。"""
        return pt in vset or pt in aset or on_pad(pt)

    # --- 端点の吸着: 数十µmズレて重なっている同ネットの端点・ビア・
    #     パッド中心を1つの接続点(代表点)に丸める。手配線の「ほぼ同じ点」の
    #     合流を分岐として正しく認識するため
    raw_pts = []
    for t in group:
        raw_pts.append(_pt(t.GetStart()))
        raw_pts.append(_pt(t.GetEnd()))
    raw_pts += list(vset) + list(aset)
    raw_pts += [_pt(p.GetPosition()) for p in pads if p.IsOnLayer(layer)]
    linked = {frozenset((_pt(t.GetStart()), _pt(t.GetEnd()))) for t in group}
    canon_map = _build_canon(raw_pts, linked)
    pad_geo = [(p, _pt(p.GetPosition()), p.GetBoundingBox())
               for p in pads if p.IsOnLayer(layer)]
    _memo = {}

    def canon(p):
        """近接吸着に加え、同ネットパッド上の点はパッド中心に寄せる。

        パッド上のどこで合流・接続していても「パッドの中心」という
        1つのノードに正規化する(パッドtoパッド接続の実現)。物理座標との
        ズレは接続長(chain["length"])に算入され、引き直しで中心に揃う。
        """
        if p in _memo:
            return _memo[p]
        q = canon_map.get(p, p)
        for pd, center, bb in pad_geo:
            if bb.GetLeft() <= q[0] <= bb.GetRight() and \
               bb.GetTop() <= q[1] <= bb.GetBottom() and \
               pd.HitTest(V(q[0], q[1])):
                q = center
                break
        _memo[p] = q
        return q

    vset = {canon(p) for p in vset}
    aset = {canon(p) for p in aset}

    # --- 論理分割: セグメント途中のセンターライン上に乗っている
    #     同ネットのトラック端点・ビア・パッド中心で切る
    cand = {canon(p) for p in raw_pts}

    track_edges = {}   # uid -> [(a, b), ...] そのトラックの全サブ辺
    group_nodes = {}   # uid -> (端点+分割点, ...)
    phys_at = {}       # (uid, 代表点) -> 物理端点(中心寄せ等でズレた場合)
    fillers = {}       # 代表点 -> [(track, 物理a, 物理b)] 潰れた実セグメント
    edges = []         # (track, a, b)
    for t in group:
        pa, pb = _pt(t.GetStart()), _pt(t.GetEnd())
        a, b = canon(pa), canon(pb)
        uid = _uid(t)
        if a != pa:
            phys_at[(uid, a)] = pa
        if b != pb:
            phys_at[(uid, b)] = pb
        if a == b:  # 吸着で潰れたセグメントはグラフに載せず、橋渡し候補に回す
            track_edges[uid] = []
            group_nodes[uid] = (a,)
            fillers.setdefault(a, []).append((t, pa, pb))
            continue
        abx, aby = b[0] - a[0], b[1] - a[1]
        len2 = abx * abx + aby * aby
        seg_l = math.sqrt(len2)
        splits = []
        for p in cand:
            if p == a or p == b:
                continue
            apx, apy = p[0] - a[0], p[1] - a[1]
            dot = apx * abx + apy * aby
            if dot <= 0 or dot >= len2:
                continue
            if abs(apx * aby - apy * abx) > seg_l * ON_LINE_TOL_NM:
                continue
            splits.append((dot, p))
        splits.sort()
        seq = [a] + [p for _, p in splits] + [b]
        uid = _uid(t)
        track_edges[uid] = [(u, v) for u, v in zip(seq, seq[1:]) if u != v]
        group_nodes[uid] = tuple(seq)
        for u, v in track_edges[uid]:
            edges.append((t, u, v))

    def build_graph(edge_list):
        adj = {}
        for i, (_, u, v) in enumerate(edge_list):
            adj.setdefault(u, []).append(i)
            adj.setdefault(v, []).append(i)
        terms = {pt for pt in adj if len(adj[pt]) != 2 or attached(pt)}
        return adj, terms

    def walk_all(edge_list, adj, terms):
        visited = set()
        out = []
        for start in terms:
            for ei in adj[start]:
                if ei in visited:
                    continue
                cur_pt, cur = start, ei
                eidx = []
                nodes = [start]
                while True:
                    visited.add(cur)
                    eidx.append(cur)
                    _, u, v = edge_list[cur]
                    other = v if u == cur_pt else u
                    nodes.append(other)
                    if other in terms:
                        break
                    nxt = [k for k in adj[other] if k != cur]
                    if len(nxt) != 1 or nxt[0] in visited:
                        break  # 念のため(データ異常時の安全弁)
                    cur_pt, cur = other, nxt[0]
                out.append({"start": start, "end": nodes[-1],
                            "eidx": eidx, "nodes": nodes})
        return out

    def freeend(pt, adj):
        return len(adj[pt]) == 1 and not attached(pt)

    # --- 行き止まりスタブ(自由端↔分岐点、どこにも繋がらないヒゲ)を
    #     グラフから外して収集する。パッド/ビアに繋がる枝は端子扱いなので
    #     ここには掛からない(未配線の宙ぶらりんも消えない)
    all_stubs = []  # (分岐点, [edgeタプル])
    cur_edges = edges
    for _ in range(5):
        adj, terms = build_graph(cur_edges)
        raw = walk_all(cur_edges, adj, terms)
        stub_idx = set()
        for c in raw:
            s, e = c["start"], c["end"]
            if freeend(s, adj) and len(adj[e]) >= 3 and not attached(e):
                j = e
            elif freeend(e, adj) and len(adj[s]) >= 3 and not attached(s):
                j = s
            else:
                continue
            es = [cur_edges[i] for i in c["eidx"]]
            if any(t.IsLocked() for t, _, _ in es):
                continue
            length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                         for _, a, b in es)
            if length > pcbnew.FromMM(STUB_MAX_MM):
                continue  # 長い宙ぶらりんは配線途中の可能性があるので残す
            all_stubs.append((j, es))
            stub_idx.update(c["eidx"])
        if not stub_idx:
            break
        cur_edges = [e for i, e in enumerate(cur_edges) if i not in stub_idx]
    # ループが上限で打ち切られた場合に raw が古い cur_edges を指さないよう作り直す
    adj, terms = build_graph(cur_edges)
    raw = walk_all(cur_edges, adj, terms)

    chains = []
    interior = {}  # 内部ノード -> chains内インデックス
    claimed_fillers = set()
    for c in raw:
        es = [cur_edges[i] for i in c["eidx"]]
        widths = {t.GetWidth() for t, _, _ in es}
        tracks = {}
        for t, _, _ in es:
            tracks[_uid(t)] = t
        for p in c["nodes"][1:-1]:
            interior[p] = len(chains)
        if len(widths) > 1:
            skip_reason = "幅が途中で変わるため対象外"
        elif any(t.IsLocked() for t, _, _ in es):
            skip_reason = "ロックされているため対象外"
        elif c["start"] == c["end"]:
            skip_reason = "閉ループのため対象外"
        else:
            skip_reason = None
        # 物理端点が代表点(パッド中心等)からズレている分は接続長に算入する。
        # ただしそのズレを潰れた実セグメント(旧経路の断片)が橋渡し済みなら、
        # ギャップ扱いにしない(等長の張り直しを繰り返す空回りの防止)。
        # 橋渡し片は連鎖に併合し、引き直されるときに一緒に消える
        gs = phys_at.get((_uid(es[0][0]), c["start"]))
        ge = phys_at.get((_uid(es[-1][0]), c["end"]))
        gap = 0.0
        extra0 = []
        for node, phys in ((c["start"], gs), (c["end"], ge)):
            if phys is None:
                continue
            bridge = None
            for f in fillers.get(node, ()):
                ft, fa, fb = f
                if _uid(ft) in claimed_fillers:
                    continue
                if (fa == node and fb == phys) or (fb == node and fa == phys):
                    bridge = f
                    break
            if bridge is not None:
                claimed_fillers.add(_uid(bridge[0]))
                extra0.append(bridge)
            else:
                gap += octi(phys, node)
        for t, _, _ in extra0:
            tracks[_uid(t)] = t
        chains.append({
            "net": net, "layer": layer,
            "start": c["start"], "end": c["end"],
            "edges": es, "extra_edges": extra0, "tracks": tracks,
            "track_edges": track_edges, "group_nodes": group_nodes,
            "length": sum(math.hypot(b[0] - a[0], b[1] - a[1])
                          for _, a, b in es) + gap,
            "retargeted": gap > 0,  # 端点を中心へ揃え直す必要がある
            "width": max(widths),
            "skip": skip_reason is not None,
            "skip_reason": skip_reason,
        })

    # スタブは、外したことで分岐が解消して連鎖の内部に埋まった場合のみ吸収
    # (まだ分岐点のままなら、そのまま基板に残す)
    for j, es in all_stubs:
        if j in interior:
            ch = chains[interior[j]]
            ch["extra_edges"].extend(es)
            for t, _, _ in es:
                ch["tracks"][_uid(t)] = t
    return chains


def chain_key(chain):
    """周回をまたいで同じ連鎖を指せるキー(端子は動かないので安定)。"""
    return (chain["net"], chain["layer"]) + tuple(
        sorted([chain["start"], chain["end"]]))


def _lonely_fragment(snap, chain, t, own):
    """連鎖以外のどの同ネット銅にも触れていない短い断片なら True。

    そのような断片は連鎖と一緒に消しても何も切断されない
    (配線編集で残った微小なゴミ)。
    """
    net, layer = chain["net"], chain["layer"]
    if t.IsLocked() or seg_length(t) > pcbnew.FromMM(STUB_MAX_MM):
        return False
    shape = t.GetEffectiveShape(layer)
    bb = t.GetBoundingBox()
    uid = _uid(t)
    for o in snap["tracks"]:
        if _uid(o) == uid or _uid(o) in own or o.GetNetCode() != net \
           or not o.IsOnLayer(layer):
            continue
        if o.GetBoundingBox().Intersects(bb) and \
           o.GetEffectiveShape(layer).Collide(shape, 0):
            return False
    for p in snap["pads"]:
        if p.GetNetCode() != net or not p.IsOnLayer(layer):
            continue
        if p.GetBoundingBox().Intersects(bb) and \
           p.GetEffectiveShape(layer).Collide(shape, 0):
            return False
    return True


def midspan_contact(snap, chain):
    """連鎖に同ネットの銅がグラフ外で接触していたら、その説明文字列を返す。

    センターライン上のT字タップ/ビア/パッド通過は連鎖抽出時に分割済みで
    端子になっている。ここで掛かるのはズレた位置の接触・平行の重なりなど
    グラフに乗らない接触だけで、引き直すと切れるため連鎖ごと触らない。
    例外として、連鎖にしか触れていない短い断片(ゴミ)は extra_edges に
    移して一緒に削除する。問題が無ければ None。
    """
    net, layer = chain["net"], chain["layer"]
    own = set(chain["tracks"])
    terms = (chain["start"], chain["end"])
    gn = chain["group_nodes"]

    shapes = []
    for t, a, b in chain["edges"] + chain["extra_edges"]:
        w = t.GetWidth()
        half = w // 2 + 10
        shapes.append((pcbnew.SHAPE_SEGMENT(V(*a), V(*b), w),
                       (min(a[0], b[0]) - half, min(a[1], b[1]) - half,
                        max(a[0], b[0]) + half, max(a[1], b[1]) + half)))

    def touches(shape, bb):
        l, t_, r, b_ = (bb.GetLeft(), bb.GetTop(),
                        bb.GetRight(), bb.GetBottom())
        for s, (sl, st, sr, sb) in shapes:
            if sl > r or sr < l or st > b_ or sb < t_:
                continue
            if s.Collide(shape, 0):
                return True
        return False

    offenders = []
    for t in snap["tracks"]:
        uid = _uid(t)
        if t.GetNetCode() != net or not t.IsOnLayer(layer) or uid in own:
            continue
        if t.GetClass() == "PCB_VIA":
            if _pt(t.GetPosition()) in terms:
                continue
        else:
            pts = gn.get(uid) or (_pt(t.GetStart()), _pt(t.GetEnd()))
            if any(p in terms for p in pts):
                continue
        if touches(t.GetEffectiveShape(layer), t.GetBoundingBox()):
            if t.GetClass() == "PCB_VIA":
                pos = t.GetPosition()
                return ("同ネットのビア (%.2f, %.2f) が端子以外の位置でズレて接触"
                        "(自動分割できないため安全のためスキップ)"
                        % (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)))
            offenders.append(t)
    for t in offenders:
        if _lonely_fragment(snap, chain, t, own):
            # 連鎖にしか触れていないゴミ断片 → 引き直しと一緒に削除する
            uid = _uid(t)
            chain["tracks"][uid] = t
            for a, b in chain["track_edges"].get(
                    uid, [(_pt(t.GetStart()), _pt(t.GetEnd()))]):
                chain["extra_edges"].append((t, a, b))
            continue
        a, b = t.GetStart(), t.GetEnd()
        return ("同ネットの配線 (%.2f, %.2f)-(%.2f, %.2f) が端子以外の"
                "位置でズレて接触(自動分割できないため安全のためスキップ)"
                % (pcbnew.ToMM(a.x), pcbnew.ToMM(a.y),
                   pcbnew.ToMM(b.x), pcbnew.ToMM(b.y)))
    for p in snap["pads"]:
        if p.GetNetCode() != net or not p.IsOnLayer(layer):
            continue
        if p.HitTest(V(*terms[0])) or p.HitTest(V(*terms[1])):
            continue
        if touches(p.GetEffectiveShape(layer), p.GetBoundingBox()):
            try:
                ref = "%s-%s" % (p.GetParentFootprint().GetReference(),
                                 p.GetNumber())
            except Exception:
                ref = "?"
            pos = p.GetPosition()
            return ("同ネットのパッド %s (%.2f, %.2f) が端子以外の位置で接触"
                    "(中心がセンターライン上に無く自動分割できないためスキップ)"
                    % (ref, pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)))
    return None


# ---------------------------------------------------------------- 障害物と経路探索

def _courtyard_layers(layer):
    if layer == pcbnew.F_Cu:
        return [pcbnew.F_CrtYd]
    if layer == pcbnew.B_Cu:
        return [pcbnew.B_CrtYd]
    return [pcbnew.F_CrtYd, pcbnew.B_CrtYd]


def collect_obstacles(snap, chain, clearance, avoid_courtyards, region,
                      planned_obs=()):
    """(shape, bboxタプル) のリスト。同ネットは障害物にしない(接触しても短絡しない)。

    planned_obs はこの周でまだ基板に反映していない計画済み新経路
    (net, layer, shape, bboxタプル)。他ネットの計画とも衝突しないようにする。
    """
    net, layer = chain["net"], chain["layer"]
    rl, rt, rr, rb = region
    obs = []
    for pnet, player, shape, bb in planned_obs:
        if pnet == net or player != layer:
            continue
        if bb[2] < rl or bb[0] > rr or bb[3] < rt or bb[1] > rb:
            continue
        obs.append((shape, bb))

    def add(shape, bb):
        t_ = (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())
        if t_[2] < rl or t_[0] > rr or t_[3] < rt or t_[1] > rb:
            return
        obs.append((shape, t_))

    for t in snap["tracks"]:
        if t.GetNetCode() == net or not t.IsOnLayer(layer):
            continue
        add(t.GetEffectiveShape(layer), t.GetBoundingBox())
    for p in snap["pads"]:
        if p.GetNetCode() != net and p.IsOnLayer(layer):
            add(p.GetEffectiveShape(layer), p.GetBoundingBox())
    if avoid_courtyards:
        for fp in snap["footprints"]:
            for cl in _courtyard_layers(layer):
                poly = fp.GetCourtyard(cl)
                if poly.OutlineCount():
                    add(poly, poly.BBox())
    for d in snap["edges"]:
        add(d.GetEffectiveShape(), d.GetBoundingBox())
    # 配線禁止のルールエリア(キープアウト)は避ける。銅ベタは障害物にしない
    # (ベタは塗り直し時に配線を避けて充填されるので、横切ってもDRC違反にならない)
    for z in snap["zones"]:
        try:
            if not (z.GetIsRuleArea() and z.GetDoNotAllowTracks()
                    and z.IsOnLayer(layer)):
                continue
            add(z.Outline(), z.GetBoundingBox())
        except Exception:
            continue
    return obs


def seg_ok(a, b, obstacles, width, clearance):
    if a == b:
        return True
    half = width // 2 + clearance
    sl = min(a[0], b[0]) - half
    sr = max(a[0], b[0]) + half
    st = min(a[1], b[1]) - half
    sb = max(a[1], b[1]) + half
    shape = None
    for oshape, (l, t, r, btm) in obstacles:
        if sl > r or sr < l or st > btm or sb < t:
            continue
        if shape is None:  # 生成は必要になった時だけ
            shape = pcbnew.SHAPE_SEGMENT(V(a[0], a[1]), V(b[0], b[1]), width)
        if oshape.Collide(shape, clearance):
            return False
    return True


def find_path(s, e, corner_nodes, obstacles, width, clearance, budget,
              deadline=None):
    """可視グラフ上のA*。(長さ<budgetの45度経路 or None, 時間切れか) を返す。

    時間切れでも、その時点でゴールに到達済みなら暫定解(検証済み・budget未満)
    を返す。
    """
    pts = [s, e] + corner_nodes
    n = len(pts)
    h = [octi(p, e) for p in pts]
    edge_cache = {}

    def connect(i, j):
        key = (i, j)
        if key not in edge_cache:
            a, b = pts[i], pts[j]
            res = None
            for bends in bend_candidates(a, b):
                way = [a] + bends + [b]
                if all(seg_ok(way[k], way[k + 1], obstacles, width, clearance)
                       for k in range(len(way) - 1)):
                    res = bends
                    break
            edge_cache[key] = res
        return edge_cache[key]

    g = {0: 0.0}
    parent = {}
    pq = [(h[0], 0)]
    closed = set()
    timed_out = False
    pops = 0
    while pq:
        _, u = heapq.heappop(pq)
        if u == 1:
            break
        pops += 1
        if deadline is not None and pops % 32 == 0 \
           and time.time() > deadline:
            timed_out = True
            break
        if u in closed:
            continue
        closed.add(u)
        gu = g[u]
        for v in range(n):
            if v == u or v in closed:
                continue
            duv = octi(pts[u], pts[v])
            if gu + duv + h[v] >= budget:
                continue
            if v in g and g[v] <= gu + duv:
                continue
            if connect(u, v) is None:
                continue
            g[v] = gu + duv
            parent[v] = u
            heapq.heappush(pq, (g[v] + h[v], v))
    if 1 not in parent:
        return None, timed_out
    seq = [1]
    while seq[-1] != 0:
        seq.append(parent[seq[-1]])
    seq.reverse()
    out = [pts[0]]
    for i, j in zip(seq, seq[1:]):
        out.extend(connect(i, j))
        out.append(pts[j])
    return simplify(out), timed_out


def reduce_bends(path, obstacles, width, clearance):
    """長さを増やさずに曲げ回数を減らす。

    A*は等長の最短経路をどれでも返し得るので、部分区間を「曲げ1回以下の
    等長接続」で張り直せるなら張り直す(無駄なジグザグを除去)。
    """
    path = list(path)
    changed = True
    while changed and len(path) > 3:
        changed = False
        n = len(path)
        for i in range(n - 2):
            if changed:
                break
            for j in range(n - 1, i + 1, -1):
                if j - i - 1 < 1:
                    continue  # 内部頂点なし=曲げは減らない
                sub = path[i:j + 1]
                if octi(path[i], path[j]) > polyline_len(sub) + 1:
                    continue  # 張り直すと長くなる区間
                for bends in bend_candidates(path[i], path[j]):
                    if len(bends) >= j - i - 1:
                        continue
                    way = [path[i]] + bends + [path[j]]
                    if all(seg_ok(way[k], way[k + 1], obstacles,
                                  width, clearance)
                           for k in range(len(way) - 1)):
                        path = path[:i + 1] + bends + path[j:]
                        changed = True
                        break
                if changed:
                    break
    return simplify(path)


def shorten_chain(snap, chain, clearance, avoid_courtyards, planned_obs=()):
    """(新経路の点列 or None, 不成立理由 or None) を返す。

    吸収するヒゲ(extra_edges)がある連鎖は、経路長が同じでも引き直しを
    許す(ヒゲの削除だけでも銅は厳密に減るので、単調改善は保たれる)。
    端子がパッドの場合は、元の取り付き点ではなくパッド中心を目標に引く。
    """
    s, e = chain["start"], chain["end"]
    contact = midspan_contact(snap, chain)  # ゴミ断片の吸収もここで行われる
    if contact:
        return None, contact
    if chain["extra_edges"] or chain["retargeted"]:
        # ヒゲ/ゴミ削除、または端点の中心揃えが利得なので、同長まで許容
        budget = chain["length"] + 1
    else:
        budget = chain["length"] - MIN_GAIN_NM
    lower = octi(s, e)
    if lower <= 0:
        return None, "長さがほぼゼロ"
    if lower >= budget:
        return None, ("すでに45度制約下の最短です(迂回率 %.4f)"
                      % (chain["length"] / lower))

    # 改善経路は octi(s,p)+octi(p,e) <= 現在長 の領域から出ない
    slack = int((chain["length"] - lower) / 2) + clearance + chain["width"] \
        + pcbnew.FromMM(1)
    region = (min(s[0], e[0]) - slack, min(s[1], e[1]) - slack,
              max(s[0], e[0]) + slack, max(s[1], e[1]) + slack)
    obstacles = collect_obstacles(snap, chain, clearance,
                                  avoid_courtyards, region, planned_obs)

    # --- 遅延障害物方式: まず障害物なしで最短を引き、その経路に実際に
    #     当たった障害物だけを探索対象に加えて引き直す、を収束まで繰り返す。
    #     ・部分集合で経路が無ければ全障害物でも無い(打ち切りは健全)
    #     ・毎回1件以上加わるので必ず停止する
    #     ・採用経路は毎回「全障害物」に対して検証するので安全
    #     ノードが「実際に邪魔な障害物の角」だけになり、密集地帯でも
    #     ノード上限に達しにくい
    infl = clearance + chain["width"] // 2 + NODE_MARGIN_NM
    width = chain["width"]
    half = width // 2 + clearance
    deadline = time.time() + SEARCH_TIME_S

    def corners_from(obs_list):
        corners = []
        for _, (l, t, r, b) in obs_list:
            for c in ((l - infl, t - infl), (r + infl, t - infl),
                      (r + infl, b + infl), (l - infl, b + infl)):
                d = octi(s, c) + octi(c, e)
                if d < budget:
                    corners.append((d, c))
        corners.sort(key=lambda x: x[0])
        # 近接ノードを間引いてから上限を適用(パッド列は角が重なりがち)
        nodes = []
        seen = set()
        cut = False
        for _, c in corners:
            key = (c[0] // NODE_DEDUP_NM, c[1] // NODE_DEDUP_NM)
            if key in seen:
                continue
            if len(nodes) >= MAX_NODES:
                cut = True
                break
            seen.add(key)
            nodes.append(c)
        return nodes, cut

    active = []          # 経路の邪魔をした障害物だけ
    active_idx = set()
    truncated = False
    timed_out = False
    path = None
    for _ in range(len(obstacles) + 1):
        nodes, truncated = corners_from(active)
        path, timed_out = find_path(s, e, nodes, active, width,
                                    clearance, budget, deadline=deadline)
        if path is None:
            break
        # 全障害物に対して検証し、当たったものを探索対象に足す
        seg_shapes = []
        for a, b in zip(path, path[1:]):
            seg_shapes.append(
                (pcbnew.SHAPE_SEGMENT(V(*a), V(*b), width),
                 (min(a[0], b[0]) - half, min(a[1], b[1]) - half,
                  max(a[0], b[0]) + half, max(a[1], b[1]) + half)))
        violators = []
        for i, (oshape, (l, t, r, b)) in enumerate(obstacles):
            if i in active_idx:
                continue
            for sshape, (sl, st, sr, sb) in seg_shapes:
                if sl > r or sr < l or st > b or sb < t:
                    continue
                if oshape.Collide(sshape, clearance):
                    violators.append(i)
                    break
        if not violators:
            break  # 全障害物クリア → この経路で確定
        for i in violators:
            active_idx.add(i)
            active.append(obstacles[i])
        path = None
        if time.time() > deadline:
            timed_out = True
            break

    if path is None or polyline_len(path) >= budget:
        why = ("クリアランスを保ちながら今より短くできる経路が"
               "見つかりません(障害物 %d 件、うち %d 件が実際に邪魔)"
               % (len(obstacles), len(active)))
        if timed_out:
            why += ("。探索が時間切れ(%.0f秒)になりました"
                    "(ファイル先頭の SEARCH_TIME_S で延長可)" % SEARCH_TIME_S)
        if truncated:
            why += ("。探索ノードが上限(%d)に達したため、"
                    "見落としの可能性があります" % MAX_NODES)
        return None, why
    # 曲げ削減の張り直しは全障害物に対して検証する
    reduced = reduce_bends(path, obstacles, width, clearance)
    # 曲げ削減は等長のはずだが、単調改善の不変条件は予算で再確認しておく
    return (reduced if polyline_len(reduced) < budget else path), None


def _add_seg(board, a, b, width, layer, net):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(V(a[0], a[1]))
    t.SetEnd(V(b[0], b[1]))
    t.SetWidth(width)
    t.SetLayer(layer)
    t.SetNetCode(net)
    board.Add(t)


def replace_chain(board, chain, pts):
    """連鎖(+吸収スタブ)を消して新経路を張る。

    論理分割で元トラックの一部だけが連鎖に入っている場合は、
    残り部分を同じ形のトラックとして復元する(銅の形は変わらない)。
    """
    consumed = {}
    for t, a, b in chain["edges"] + chain["extra_edges"]:
        consumed.setdefault(_uid(t), set()).add((a, b))
    for uid, track in chain["tracks"].items():
        keep = [e for e in chain["track_edges"][uid]
                if e not in consumed.get(uid, ())]
        w = track.GetWidth()
        track.ClearSelected()  # 選択中アイテムのRemoveはクラッシュの前科あり(罠#1)
        board.Remove(track)
        for a, b in keep:
            _add_seg(board, a, b, w, chain["layer"], chain["net"])
    for a, b in zip(pts, pts[1:]):
        _add_seg(board, a, b, chain["width"], chain["layer"], chain["net"])


# ---------------------------------------------------------------- 全体ループ

def _chain_desc(board, chain):
    """理由表示用の連鎖の見出し(ネット名・層・端点座標)。"""
    net = ""
    for t in chain["tracks"].values():
        net = t.GetNetname()
        break
    s, e = chain["start"], chain["end"]
    return "%s %s (%.1f, %.1f)→(%.1f, %.1f)" % (
        net, board.GetLayerName(chain["layer"]),
        pcbnew.ToMM(s[0]), pcbnew.ToMM(s[1]),
        pcbnew.ToMM(e[0]), pcbnew.ToMM(e[1]))


def shorten_board(board, clearance, avoid_courtyards,
                  max_passes=DEFAULT_MAX_PASSES, target_keys=None, tick=None):
    """迂回率の悪い順に1本ずつ引き直し、改善が無くなるまで周回する。

    tick(pass_no, idx, total, replaced, gain_nm) が False を返したら中止
    (それまでの置換は確定)。戻り値: replaced/gain_nm/passes/aborted。

    GUIクラッシュ対策として、探索中は基板に触らず「計画」だけを作り
    (計画済みの新経路同士も衝突チェックする)、周の最後に一括で反映する。
    反映中はGUIイベントを回さない。
    """
    replaced = 0
    gain = 0.0
    passes = 0
    aborted = False
    errors = 0
    first_error = None   # 1件目のtracebackは診断用に残す
    all_reasons = []     # 選択モード時: 1周目に短縮できなかった理由(表示用)
    for pass_no in range(1, max_passes + 1):
        passes = pass_no
        # 選択モードの1周目だけ、不成立の理由をユーザ向けに集める
        reasons = all_reasons if (target_keys is not None
                                  and pass_no == 1) else None
        snap = snapshot(board)  # SWIGコンテナの列挙は周の頭の1回に集約
        work = []
        for c in build_chains(board, snap):
            if target_keys is not None and chain_key(c) not in target_keys:
                continue
            if c["skip"]:
                if reasons is not None:
                    reasons.append(_chain_desc(board, c) + ": "
                                   + c["skip_reason"])
                continue
            lower = octi(c["start"], c["end"])
            if lower <= 0:
                continue
            if not c["extra_edges"] and not c["retargeted"] \
               and c["length"] - lower <= MIN_GAIN_NM:
                if reasons is not None:
                    reasons.append(_chain_desc(board, c)
                                   + ": すでに45度制約下の最短です(迂回率 %.4f)"
                                   % (c["length"] / lower))
                continue  # ほぼ最短で、消すヒゲも中心揃えの必要も無い
            work.append((c["length"] / lower, c))
        work.sort(key=lambda x: -x[0])

        improved = 0
        used = set()      # この周で計画済みの元トラックuid(共有する連鎖は次周へ)
        planned = []      # (chain, 新経路)
        planned_obs = []  # 計画済み新経路の形状(後続の連鎖の障害物に足す)
        for idx, (_, c) in enumerate(work):
            if tick and not tick(pass_no, idx, len(work), replaced, gain):
                aborted = True
                break
            if any(uid in used for uid in c["tracks"]):
                continue
            try:
                pts, why = shorten_chain(snap, c, clearance, avoid_courtyards,
                                         planned_obs)
            except Exception:
                errors += 1  # 1本の失敗で全体を止めない(件数は最後に報告)
                if first_error is None:
                    first_error = traceback.format_exc()
                continue
            if pts is None:
                if reasons is not None and why:
                    reasons.append(_chain_desc(board, c) + ": " + why)
                continue
            extra_len = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                            for _, a, b in c["extra_edges"])
            gain += c["length"] + extra_len - polyline_len(pts)
            planned.append((c, pts))
            used.update(c["tracks"])
            w = c["width"]
            half = w // 2
            for a, b in zip(pts, pts[1:]):
                bb = (min(a[0], b[0]) - half, min(a[1], b[1]) - half,
                      max(a[0], b[0]) + half, max(a[1], b[1]) + half)
                planned_obs.append((c["net"], c["layer"],
                                    pcbnew.SHAPE_SEGMENT(V(*a), V(*b), w), bb))
            improved += 1
            replaced += 1

        # 一括反映(中止時もここまでの計画は検証済みなので反映する)
        for c, pts in planned:
            replace_chain(board, c, pts)
        if planned:
            try:
                board.BuildConnectivity()  # 接続情報の生ポインタを作り直す
            except Exception:
                pass
        if aborted or improved == 0:
            break
    return {"replaced": replaced, "gain_nm": gain, "passes": passes,
            "aborted": aborted, "errors": errors, "first_error": first_error,
            "reasons": all_reasons}


def collect_selected_tracks(board):
    sel = []
    try:  # GUIの選択はまずこれで拾う(ヘッドレスでは空)
        for item in pcbnew.GetCurrentSelection():
            if isinstance(item, pcbnew.PCB_TRACK) and \
               item.GetClass() == "PCB_TRACK":
                sel.append(item)
    except Exception:
        pass
    if not sel:
        sel = [t for t in board.GetTracks()
               if t.GetClass() == "PCB_TRACK" and t.IsSelected()]
    return sel


def anything_selected(board):
    """トラック以外も含め、何かが選択されているか(全基板誤爆の防止用)。"""
    try:
        for _ in pcbnew.GetCurrentSelection():
            return True
    except Exception:
        pass
    return (any(t.IsSelected() for t in board.GetTracks())
            or any(fp.IsSelected() for fp in board.GetFootprints()))


def default_clearance(board):
    try:
        nc = board.GetAllNetClasses()["Default"]
        c = nc.GetClearance()
        if c > 0:
            return c
    except Exception:
        pass
    return pcbnew.FromMM(DEFAULT_CLEARANCE_MM)


def selection_target_keys(board, sel_tracks):
    """選択トラックを含む連鎖のキー集合(選択なしなら None = 全対象)。"""
    if not sel_tracks:
        return None
    sel_ids = {_uid(t) for t in sel_tracks}
    return {chain_key(c) for c in build_chains(board)
            if any(uid in sel_ids for uid in c["tracks"])}


# ---------------------------------------------------------------- UI

if wx is not None:

    class TrackShortenDialog(wx.Dialog):
        def __init__(self, parent, board, n_selected):
            super().__init__(parent, title="配線を最短化",
                             style=wx.DEFAULT_DIALOG_STYLE)
            root = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
            grid.AddGrowableCol(1)

            target = ("選択中の配線 %d 本から辿れる連鎖のみ" % n_selected
                      if n_selected else "全配線")
            grid.Add(wx.StaticText(self, label="対象"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(wx.StaticText(self, label=target), 0)

            grid.Add(wx.StaticText(self, label="クリアランス [mm]"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            self.clearance = wx.SpinCtrlDouble(
                self, min=0.05, max=5.0,
                initial=pcbnew.ToMM(default_clearance(board)), inc=0.05)
            self.clearance.SetDigits(2)
            grid.Add(self.clearance, 0, wx.EXPAND)

            grid.Add(wx.StaticText(self, label="最大周回数"),
                     0, wx.ALIGN_CENTER_VERTICAL)
            self.passes = wx.SpinCtrl(self, min=1, max=20,
                                      initial=DEFAULT_MAX_PASSES)
            grid.Add(self.passes, 0, wx.EXPAND)

            grid.Add(wx.StaticText(self, label=""), 0)
            self.under_parts = wx.CheckBox(self, label="部品の下も通る")
            self.under_parts.SetValue(True)
            grid.Add(self.under_parts, 0)

            root.Add(grid, 1, wx.ALL | wx.EXPAND, 12)
            btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            self.FindWindow(wx.ID_OK).SetLabel("実行")
            root.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
            self.SetSizerAndFit(root)
            self.CentreOnParent()

        def get_params(self):
            return {
                "clearance": pcbnew.FromMM(self.clearance.GetValue()),
                "avoid_courtyards": not self.under_parts.GetValue(),
                "max_passes": self.passes.GetValue(),
            }


class TrackShorten(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Shorten Tracks (45deg reroute)"
        self.category = "Routing"
        self.description = "Reroute detoured tracks with shortest 45-degree paths"
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        if wx is None:
            return
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")

        sel = collect_selected_tracks(board)
        if not sel and anything_selected(board):
            # ビアや部品だけ選択された状態で全配線を触るのは危険なので止める
            wx.MessageBox("選択の中に対象にできる配線(直線トラック)がありません。\n"
                          "全配線を対象にする場合は、選択を解除してから実行してください。",
                          "配線を最短化", wx.OK | wx.ICON_INFORMATION, parent)
            return
        target_keys = selection_target_keys(board, sel)
        if sel and not target_keys:
            wx.MessageBox("選択された配線から対象の連鎖を特定できませんでした。",
                          "配線を最短化", wx.OK | wx.ICON_INFORMATION, parent)
            return

        dlg = TrackShortenDialog(parent, board, len(sel))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            params = dlg.get_params()
        finally:
            dlg.Destroy()

        # 削除対象がGUIの選択に残ったまま Remove するとクラッシュし得るため、
        # 実行前に選択を解除して画面に反映しておく(罠#1対策)
        for t in board.GetTracks():
            t.ClearSelected()
        for fp in board.GetFootprints():
            fp.ClearSelected()
        pcbnew.Refresh()

        progress = [None]  # wx.ProgressDialog(初回tick時に遅延生成)
        cancelled = [False]
        last = [time.time()]

        def tick(pass_no, idx, total, replaced, gain_nm):
            if cancelled[0]:
                return False
            now = time.time()
            if now - last[0] < 0.1:
                return True
            last[0] = now
            msg = ("周回 %d — %d/%d 本を検討中"
                   "(ここまで %d 本を短縮 / -%.2f mm)\n"
                   "「中止」を押すと、ここまでの結果で確定します"
                   % (pass_no, idx + 1, total, replaced,
                      pcbnew.ToMM(int(gain_nm))))
            if progress[0] is None:
                progress[0] = wx.ProgressDialog(
                    "配線を最短化", msg, maximum=1000, parent=parent,
                    style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME)
            frac = (idx + 1) / max(1, total)
            keep = progress[0].Update(max(0, min(999, int(frac * 1000))), msg)[0]
            # ネイティブ実装のUpdate()はイベントを回さないことがある(応答なし防止)
            wx.SafeYield(progress[0])
            if not keep:
                cancelled[0] = True
            return keep

        try:
            stats = shorten_board(board, params["clearance"],
                                  params["avoid_courtyards"],
                                  params["max_passes"],
                                  target_keys=target_keys, tick=tick)
        finally:
            if progress[0] is not None:
                progress[0].Destroy()

        pcbnew.Refresh()
        err_note = ""
        if stats.get("errors"):
            err_note = ("\n※ %d 本は内部エラーでスキップしました。"
                        "続くようならKiCadを再起動してから再実行してください。"
                        % stats["errors"])
            if stats.get("first_error"):
                err_note += "\n--- 1件目のエラー ---\n" + stats["first_error"]
        if stats["replaced"]:
            note = "(中止までの結果)" if stats["aborted"] else ""
            wx.MessageBox(
                "%d 本を引き直し、合計 %.2f mm 短縮しました(%d 周)%s%s"
                % (stats["replaced"], pcbnew.ToMM(int(stats["gain_nm"])),
                   stats["passes"], note, err_note),
                "配線を最短化", wx.OK | wx.ICON_INFORMATION, parent)
        else:
            msg = "短縮できる配線はありませんでした。"
            reasons = stats.get("reasons") or []
            if reasons:
                msg += "\n\n理由:\n" + "\n".join("・" + r for r in reasons[:8])
                if len(reasons) > 8:
                    msg += "\n…ほか %d 件" % (len(reasons) - 8)
            wx.MessageBox(msg + err_note,
                          "配線を最短化", wx.OK | wx.ICON_INFORMATION, parent)


TrackShorten().register()
