"""選択部品を矢印の方向へ、隣の部品にコートヤードが接するまで寄せる

選択フットプリントを、押した矢印（←→↑↓）の方向へまっすぐ動かし、
その先にある選択外の部品のコートヤード枠線（中心線）と
ちょうど接する（距離0）位置で止める。

- 複数選択したときは、選択部品どうしの相対位置を保ったまま
  1つの塊として動く（選択部品どうしは障害物にならない）。
- 進む先に「進行方向と直交する軸で重なりのある」選択外の部品だけを
  障害物とみなす。角どうしが一致しているだけの部品や、すでに
  進行方向で重なっている（追い越し済みの）部品は無視する。
- 部品の裏表（F.CrtYd / B.CrtYd）が違う相手は障害物にしない。
- すでに接している（距離 1µm 以下）なら動かない。
  進む先に部品が無いときも動かない（メッセージを出す）。
- 「既存の配線をクリアランス分よける」ONで、選択部品のパッド（銅）が
  他ネットの配線・ビアにルール上のクリアランス未満まで近づく手前でも
  止まる。クリアランスは相手ごとに「自分のネットクラス値/ローカル値と
  相手の値の大きい方」。パッドは実形状でなくBBox近似で、同じ銅箔層に
  乗っている相手だけを見る（穴クリアランス・ベタは見ない）。
- 「パッドにつながる配線も動かす」ONで、dense_pack と同じ方法で
  移動するパッドに乗っている配線が接続を保ったまま追従する
  （円弧は追従できず、通せなかった区間があれば件数を警告する）。
  追従する配線は「よける」対象からも外れる。

矢印を1回押すごとに1回寄るので、続けて押すと次の部品まで進む。
「隙間ゼロに詰める」が並び全体を詰めるのに対し、こちらは
選んだ部品だけを狙った隣へ寄せる手動配置の補助ツール。"""
import pcbnew
import wx

ICON = "🧲"
LABEL = "隣に寄せる"

TOUCH_TOL_NM = 1000  # 「接している」とみなす距離の許容 [nm]（dense_pack と同じ）
GAP_NM = 0           # 止まる位置のコートヤード間隔 [nm]

LAST_DIR = ["right"]   # 前回押した矢印
LAST_TRACKS = [True]   # 「既存の配線をよける」チェックの前回値
LAST_WIRES = [True]    # 「配線も動かす」チェックの前回値

PRESET = None  # パレットが実行直前に渡すパラメータ(Run()が消費、無ければダイアログ)

DIRS = ("left", "right", "up", "down")
JP_DIR = {"left": "左の部品に寄せる", "right": "右の部品に寄せる",
          "up": "上の部品に寄せる", "down": "下の部品に寄せる"}


def PANEL():
    """パレット埋め込みUIの定義(pack_launcher が参照。規約はREADME)。"""
    return [
        {"type": "arrows", "key": "dir", "last": LAST_DIR[0]},
        {"type": "check", "key": "tracks",
         "label": "既存の配線をクリアランス分よける", "default": LAST_TRACKS[0]},
        {"type": "check", "key": "wires",
         "label": "パッドにつながる配線も動かす", "default": LAST_WIRES[0]},
    ]


def _axis_view(rect, direction):
    """(進行方向の先頭座標, 進行方向の後端座標, 直交軸の下限, 直交軸の上限)
    に並べ替える。座標系は KiCad（右+X, 下+Y）。"""
    left, top, right, bottom = rect
    if direction == "right":
        return right, left, top, bottom
    if direction == "left":
        return left, right, top, bottom
    if direction == "down":
        return bottom, top, left, right
    return top, bottom, left, right  # up


def slide_gap(moving, obstacles, direction, tol=TOUCH_TOL_NM,
              block_overlap=False):
    """moving の矩形群を direction へ平行移動したとき、最初に obstacles の
    どれかとコートヤードが接するまでの距離 [nm] を返す。
    進む先に何も無ければ None。すでに接していれば 0。

    矩形は (left, top, right, bottom)。obstacles の各要素は
    (rect, same_side) で、same_side が False のものは無視する。
    直交軸で tol より大きく重なっていて、先端が自分の先端より
    先（tol の余裕つき）にある相手だけを障害物とみなす。
    block_overlap=True なら、進行方向で今すでに重なっている相手は
    「通り過ぎる」ではなく 0（動けない）として扱う（銅の当たり判定用。
    相手が完全に後ろにあるときだけ無視）。"""
    sign = 1 if direction in ("right", "down") else -1
    best = None
    for m in moving:
        m_lead, _, m_lo, m_hi = _axis_view(m, direction)
        for rect, same_side in obstacles:
            if not same_side:
                continue
            o_lead, o_tail, o_lo, o_hi = _axis_view(rect, direction)
            if not (m_lo + tol < o_hi and o_lo + tol < m_hi):
                continue  # 直交軸で重なっていない(角一致だけも含む)
            gap = (o_tail - m_lead) * sign  # 自分の先端→相手の後端
            if gap < -tol:
                # 進行方向ですでに追い越している/重なっている
                if not (block_overlap and (o_lead - m_lead) * sign > tol):
                    continue
                gap = 0
            gap = max(0, gap)
            if best is None or gap < best:
                best = gap
    if best is not None and best <= tol:
        return 0
    return best


def _uv(pt, direction):
    """点を (進行方向座標u, 直交座標v) に変換する。u は進行方向が正。"""
    x, y = pt
    if direction == "right":
        return x, y
    if direction == "left":
        return -x, y
    if direction == "down":
        return y, x
    return -y, x  # up


def rect_seg_gap(rect, a, b, direction, tol=TOUCH_TOL_NM):
    """矩形 rect を direction へ平行移動したとき、線分 a-b（中心線、幅0）に
    最初に触れるまでの距離 [nm]。線分が進路の帯（直交軸の範囲）に tol より
    大きく掛かっていなければ None。先端より後ろにあれば None（今重なって
    いても進めば離れる）、先端をまたいでいれば 0（動けない）、
    接触済み(tol以内)も 0。
    配線幅やクリアランスは rect 側を膨らませて渡す。"""
    u_lead, _, v_lo, v_hi = _axis_view(rect, direction)
    au, av = _uv(a, direction)
    bu, bv = _uv(b, direction)
    if av == bv:  # 進行方向に平行な線分: 帯の内側(端に触れるだけは無視)か
        if not (v_lo + tol < av < v_hi - tol):
            return None
        min_u, max_u = min(au, bu), max(au, bu)
    else:
        # 直交軸で tol より大きく掛かっているか(端に触れるだけは無視)
        if min(max(av, bv), v_hi) - max(min(av, bv), v_lo) <= tol:
            return None
        # v が [v_lo, v_hi] に入るパラメータ範囲で線分をクリップ
        t0 = (v_lo - av) / (bv - av)
        t1 = (v_hi - av) / (bv - av)
        if t0 > t1:
            t0, t1 = t1, t0
        t0, t1 = max(0.0, t0), min(1.0, t1)
        if t0 >= t1:
            return None
        u0 = au + (bu - au) * t0
        u1 = au + (bu - au) * t1
        min_u, max_u = min(u0, u1), max(u0, u1)
    if max_u < u_lead - tol:
        return None  # 完全に後ろ
    gap = min_u - u_lead
    return 0 if gap <= tol else int(gap)


def _clearance_of(board, item, layer, cache):
    """アイテムの解決済みクリアランス [nm]（track_shorten と同じ考え方:
    ネットクラス値・ローカル上書き・GetOwnClearance の最大）。"""
    net = item.GetNetCode()
    if net in cache:
        c = cache[net]
    else:
        c = int(0.2e6)
        try:
            name = board.FindNet(net).GetNetClassName()
            c = board.GetAllNetClasses()[name].GetClearance()
        except Exception:
            try:
                c = board.GetAllNetClasses()["Default"].GetClearance()
            except Exception:
                pass
        cache[net] = c
    for getter in (lambda: item.GetOwnClearance(layer), item.GetLocalClearance):
        try:
            v = getter()
            if v and v > c:
                c = v
        except Exception:
            pass
    return c


def copper_gap(board, moving_fps, direction, skip_ids=()):
    """moving_fps のパッド（銅）が、他ネットの配線・ビアにクリアランス未満で
    近づく手前までの距離 [nm] を返す。進路に無ければ None。
    skip_ids のトラック(m_Uuid.AsString())は無視する（追従して一緒に動く配線）。"""
    cu_layers = list(board.GetEnabledLayers().CuStack())
    tracks = []
    for t in board.GetTracks():
        if t.m_Uuid.AsString() in skip_ids:
            continue
        tracks.append(t)
    clr_cache = {}
    best = None

    def consider(g):
        nonlocal best
        if g is not None and (best is None or g < best):
            best = g

    for fp in moving_fps:
        for pad in fp.Pads():
            pnet = pad.GetNetCode()
            pbb = pad.GetBoundingBox()
            prect = (pbb.GetLeft(), pbb.GetTop(), pbb.GetRight(), pbb.GetBottom())
            for t in tracks:
                tnet = t.GetNetCode()
                if tnet == pnet and pnet != 0:
                    continue  # 同ネットは接触してよい
                cls = t.GetClass()
                if cls == "PCB_VIA":
                    layers = [l for l in cu_layers
                              if pad.IsOnLayer(l) and t.IsOnLayer(l)]
                    if not layers:
                        continue
                    layer = layers[0]
                else:
                    layer = t.GetLayer()
                    if not pad.IsOnLayer(layer):
                        continue
                clr = max(_clearance_of(board, pad, layer, clr_cache),
                          _clearance_of(board, t, layer, clr_cache))
                if cls == "PCB_VIA":
                    # KiCad10 の PCB_VIA::GetWidth() はレイヤ引数が必要(引数なしは assert)
                    try:
                        half_w = t.GetWidth(layer) // 2
                    except TypeError:
                        half_w = t.GetWidth() // 2
                else:
                    half_w = t.GetWidth() // 2
                grow = clr + half_w
                r = (prect[0] - grow, prect[1] - grow,
                     prect[2] + grow, prect[3] + grow)
                if cls == "PCB_TRACK":
                    s, e = t.GetStart(), t.GetEnd()
                    consider(rect_seg_gap(r, (s.x, s.y), (e.x, e.y), direction))
                elif cls == "PCB_VIA":
                    p = t.GetPosition()
                    vr = (p.x - half_w, p.y - half_w, p.x + half_w, p.y + half_w)
                    r2 = (prect[0] - clr, prect[1] - clr,
                          prect[2] + clr, prect[3] + clr)
                    consider(slide_gap([r2], [(vr, True)], direction,
                                       block_overlap=True))
                else:  # PCB_ARC など: BBox で保守的に
                    bb = t.GetBoundingBox()
                    ar = (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())
                    r2 = (prect[0] - clr, prect[1] - clr,
                          prect[2] + clr, prect[3] + clr)
                    consider(slide_gap([r2], [(ar, True)], direction,
                                       block_overlap=True))
    return best


def _attached_track_ids(board, fps):
    """fps のパッドに端点が乗っている（=追従して動く）配線の m_Uuid 集合。
    直接つながる配線だけ（その先の連鎖は dense_pack 側で伸縮される）。"""
    ids = set()
    pads = [p for fp in fps for p in fp.Pads()]
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            continue
        for pt in (t.GetStart(), t.GetEnd()):
            if any(p.GetNetCode() == t.GetNetCode() and p.HitTest(pt)
                   for p in pads):
                ids.add(t.m_Uuid.AsString())
                break
    return ids


def nudge_selected(board, direction, move_wires=False, avoid_tracks=False):
    """選択フットプリントを direction へ、隣に接するまで動かす。
    avoid_tracks=True なら他ネットの配線・ビアにクリアランス未満で
    近づく手前でも止まる。
    返り値: {"moved": 動かした部品数, "gap": 移動量nm または None,
             "wire": stats, "by": "courtyard"|"copper"|None}"""
    import dense_pack  # コートヤードBBox と配線追従を共用

    sel = [fp for fp in board.GetFootprints() if fp.IsSelected()]
    if not sel:
        return {"moved": 0, "gap": None, "wire": None, "by": None}
    others = [fp for fp in board.GetFootprints() if not fp.IsSelected()]

    def rect_of(fp):
        bb = dense_pack.courtyard_bbox(fp)
        return (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())

    # 表裏が違えば障害物にならない。選択群の中に表裏が混在していれば
    # 部品ごとに相手の同一面だけを見る
    total = None
    for fp in sel:
        flipped = fp.IsFlipped()
        obstacles = [(rect_of(o), o.IsFlipped() == flipped) for o in others]
        g = slide_gap([rect_of(fp)], obstacles, direction)
        if g is not None and (total is None or g < total):
            total = g
    by = "courtyard" if total is not None else None
    if avoid_tracks:
        skip = _attached_track_ids(board, sel) if move_wires else set()
        cg = copper_gap(board, sel, direction, skip)
        if cg is not None and (total is None or cg < total):
            total, by = cg, "copper"
    if total is None:
        return {"moved": 0, "gap": None, "wire": None, "by": None}
    dist = max(0, total - GAP_NM)
    if dist <= 0:
        return {"moved": 0, "gap": 0, "wire": None, "by": by}

    dx = dy = 0
    if direction == "right":
        dx = dist
    elif direction == "left":
        dx = -dist
    elif direction == "down":
        dy = dist
    else:
        dy = -dist

    wire_ops, wire_stats = [], None
    if move_wires:
        wire_ops, wire_stats = dense_pack.plan_wire_moves(
            board, [(fp, (dx, dy)) for fp in sel])
    for fp in sel:
        pos = fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(pos.x + dx, pos.y + dy))
    dense_pack.apply_wire_ops(board, wire_ops)
    return {"moved": len(sel), "gap": dist, "wire": wire_stats, "by": by}


class NudgeDialog(wx.Dialog):
    """メニューから直接実行したときの矢印ダイアログ。押した矢印で即実行。"""
    LAYOUT = ((None, "up", None),
              ("left", None, "right"),
              (None, "down", None))
    ARROWS = {"left": "←", "right": "→", "up": "↑", "down": "↓"}

    def __init__(self, parent, last):
        super().__init__(parent, title="隣に寄せる")
        self.choice = None
        outer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(
            self, label="選択部品を矢印の方向へ、隣の部品の\n"
                        "コートヤードに接するまで動かします")
        outer.Add(note, 0, wx.ALL, 8)
        grid = wx.GridSizer(3, 3, 4, 4)
        focus_btn = None
        for row in self.LAYOUT:
            for d in row:
                if d is None:
                    grid.Add(wx.Size(48, 48))
                    continue
                btn = wx.Button(self, label=self.ARROWS[d], size=wx.Size(48, 48))
                btn.SetToolTip(JP_DIR[d])
                btn.Bind(wx.EVT_BUTTON, lambda e, d=d: self._pick(d))
                if d == last:
                    focus_btn = btn
                grid.Add(btn, 0, wx.EXPAND)
        outer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, 8)
        self.cb_tracks = wx.CheckBox(
            self, label="既存の配線・ビアをクリアランス分よける")
        self.cb_tracks.SetValue(LAST_TRACKS[0])
        outer.Add(self.cb_tracks, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.cb_wires = wx.CheckBox(self, label="パッドにつながる配線も一緒に動かす")
        self.cb_wires.SetValue(LAST_WIRES[0])
        outer.Add(self.cb_wires, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizerAndFit(outer)
        if focus_btn is not None:
            focus_btn.SetFocus()

    def _pick(self, d):
        self.choice = d
        self.EndModal(wx.ID_OK)


class NudgeToTouch(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Nudge Selected to Touch Neighbor"
        self.category = "Placement"
        self.description = ("Move selected footprints in an arrow direction until "
                            "their courtyard touches the next footprint")
        self.show_toolbar_button = False  # ツールバーには pack_launcher だけを出す

    def Run(self):
        global PRESET
        preset, PRESET = PRESET, None
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        title = "隣に寄せる"
        if not any(fp.IsSelected() for fp in board.GetFootprints()):
            wx.MessageBox("先に寄せたいフットプリントを選択してください。",
                          title, wx.OK | wx.ICON_INFORMATION, parent)
            return
        if preset:  # パレットから: パラメータ指定済みなのでダイアログを出さない
            direction = preset.get("dir") or LAST_DIR[0]
            avoid_tracks = bool(preset.get("tracks", LAST_TRACKS[0]))
            move_wires = bool(preset.get("wires", LAST_WIRES[0]))
        else:  # メニューから直接: 矢印ダイアログ
            dlg = NudgeDialog(parent, LAST_DIR[0])
            try:
                if dlg.ShowModal() != wx.ID_OK or dlg.choice is None:
                    return
                direction = dlg.choice
                avoid_tracks = dlg.cb_tracks.GetValue()
                move_wires = dlg.cb_wires.GetValue()
            finally:
                dlg.Destroy()
        if direction not in DIRS:
            direction = LAST_DIR[0]
        LAST_DIR[0] = direction
        LAST_TRACKS[0] = avoid_tracks
        LAST_WIRES[0] = move_wires

        result = nudge_selected(board, direction, move_wires, avoid_tracks)
        if result["moved"] and move_wires:
            board.BuildConnectivity()  # 配線を変えたのでラッツネストを更新
        pcbnew.Refresh()

        if result["gap"] is None:
            wx.MessageBox("この方向には寄せられる部品がありません。",
                          title, wx.OK | wx.ICON_INFORMATION, parent)
            return
        if result["gap"] == 0 and result["by"] == "copper":
            wx.MessageBox("既存の配線・ビアとのクリアランスがもう無いので、"
                          "この方向には動かせません。\n"
                          "（「既存の配線をよける」を外せばコートヤードまで寄ります）",
                          title, wx.OK | wx.ICON_INFORMATION, parent)
            return
        w = result["wire"]
        warns = []
        if w and w["arc_skip"]:
            warns.append(f"円弧を含む配線 {w['arc_skip']} 本は追従できませんでした。")
        if w and w["overlap"]:
            warns.append(f"他の配線との重なりを避けられなかった区間が "
                         f"{w['overlap']} 箇所ありました。\n"
                         "track_shorten で引き直すか手で調整してください。")
        if warns:
            wx.MessageBox("\n\n".join(warns), title, wx.OK | wx.ICON_WARNING, parent)


NudgeToTouch().register()
