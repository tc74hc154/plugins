# pack_launcher.py — PCBツールパレット(常駐型ランチャー)。
# ツールバーのボタン1つで、同じフォルダのプラグイン一覧パレットの表示/非表示を切り替える。
# パレットは非モーダルの常駐ウィンドウで、開いたままpcbnewを操作できる。
# 一覧はHTML/CSS/JS(WebView2)で描画。1枚の操作パネルにツールごとのカードを並べ、
# カード内に埋め込んだコントロール(3x3方向ボタン・チェック・数値など)を
# 操作してボタンを押すと、設定ダイアログなしで即実行される。
# 説明文は各カードの?ボタン(オーバーレイ)で見る。
# WebView2が使えない環境ではwx標準ウィジェットの一発ダイアログにフォールバックする。
#
# ツールの追加規約(コード修正不要。詳細はREADME):
#   - ActionPluginサブクラスを含む .py をこのフォルダに置く(+「プラグインを更新」)
#   - モジュール変数 LABEL = "短い名前" がカードの表示名(省略時はdocstring 1行目)
#   - docstring 1行目=短い日本語要約(ホバーのツールチップ)、空行、以降=詳細
#     (要約+詳細はカードの?ボタンで表示される)
#   - モジュール変数 ICON = "🧱" (絵文字1つ) でカードのアイコンを指定(省略可)
#   - plugin.category がセクション見出しになる
#   - モジュール変数 PANEL(リスト、または動的既定値を返す関数)でカードに
#     コントロールを埋め込める: {"type": "run"|"dirgrid"|"check"|"number"|"choice",
#     "key", "label", "default", ...}。実行時は入力値の dict がモジュール変数
#     PRESET に入ってから Run() が呼ばれる(Run側で消費し、無ければ従来ダイアログ)。
#     PANEL 省略時は「実行」ボタンだけのカードになる
"""PCBプラグインをカテゴリ別の常駐パレットから選んで実行するランチャー。"""
import importlib
import json
import os
import traceback

import pcbnew

try:
    import wx
except ImportError:
    wx = None

try:
    import wx.html2 as wxhtml2
except ImportError:
    wxhtml2 = None

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SELF_MODULE = os.path.splitext(os.path.basename(__file__))[0]
_PALETTE_NAME = "PackToolsPalette"
_ICON_FILE = os.path.join(_PLUGIN_DIR, "pack_tools_icon.png")
_last_tool_name = None  # 同一セッション内で前回実行したツールを覚えておく


def _split_doc(desc):
    """説明文を (1行目=短い要約, 残り=詳細) に分ける。"""
    lines = desc.strip().splitlines()
    summary = lines[0].strip() if lines else ""
    detail = "\n".join(lines[1:]).strip()
    return summary, detail


def discover_tools():
    """同じフォルダの .py ファイルから ActionPlugin サブクラスを集める。

    戻り値: [{"inst", "name", "label", "summary", "detail", "icon", "category"}, ...] 名前順。
    このランチャー自身と、_ で始まるファイルは除外する。
    """
    tools = []
    for fname in sorted(os.listdir(_PLUGIN_DIR)):
        if not fname.endswith(".py"):
            continue
        mod_name = fname[:-3]
        if mod_name == _SELF_MODULE or mod_name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue  # 壊れたモジュールは一覧から外すだけで、他は動かす
        for obj in vars(mod).values():
            if not (isinstance(obj, type) and issubclass(obj, pcbnew.ActionPlugin)):
                continue
            if obj.__module__ != mod.__name__:
                continue  # import されただけのクラスを再掲しない
            try:
                inst = obj()
                inst.defaults()
            except Exception:
                continue
            desc = (mod.__doc__ or "").strip() or (inst.description or "")
            summary, detail = _split_doc(desc)
            panel = getattr(mod, "PANEL", None)
            try:
                if callable(panel):
                    panel = panel()  # 動的な既定値(前回値・基板のルール等)を反映
                json.dumps(panel)    # JSON化できない定義は捨てて既定に落とす
            except Exception:
                panel = None
            if not (isinstance(panel, list) and panel):
                panel = [{"type": "run", "label": "実行"}]
            tools.append({
                "inst": inst,
                "mod": mod,
                "name": inst.name,
                "label": getattr(mod, "LABEL", ""),
                "summary": summary or inst.name,
                "detail": detail,
                "icon": getattr(mod, "ICON", ""),
                "category": inst.category or "その他",
                "panel": panel,
            })
    tools.sort(key=lambda t: (t["category"], t["name"]))
    return tools


def run_tool(tool):
    """ツールを実行する。可能ならKiCad本体のメニューイベント経由で。

    inst.Run() を直接呼ぶと、KiCadがメニュー実行時に行う「実行前後の
    基板スナップショット→Undoエントリ作成」(RunActionPlugin)を素通りして
    しまい、プラグインが行った削除/追加がUndo履歴に載らない。その状態で
    Ctrl+Z すると過去のUndoエントリが削除済みアイテムを参照して
    「Incomplete undo/redo operation: some items not found」になる。
    そこで「ツール→外部プラグイン」のメニュー項目を探して本物の
    コマンドイベントを同期発火し、KiCad純正のUndoラッパーに乗せる。
    メニューが見つからないときだけ直接Run()にフォールバックする(Undo不可)。
    """
    frame = wx.FindWindowByName("PcbFrame")
    menubar = frame.GetMenuBar() if frame is not None else None
    if menubar is not None:

        def find_id(menu):
            for item in menu.GetMenuItems():
                sub = item.GetSubMenu()
                if sub is not None:
                    found = find_id(sub)
                    if found is not None:
                        return found
                elif item.GetItemLabelText() == tool["name"]:
                    return item.GetId()
            return None

        for i in range(menubar.GetMenuCount()):
            mid = find_id(menubar.GetMenu(i))
            if mid is not None:
                evt = wx.CommandEvent(wx.EVT_MENU.typeId, mid)
                frame.GetEventHandler().ProcessEvent(evt)  # 同期実行
                return
    tool["inst"].Run()


def _tools_json(tools):
    payload = [{k: t[k] for k in ("name", "label", "summary", "detail", "icon",
                                  "category", "panel")}
               for t in tools]
    # </script> でHTMLが割れないよう、JSON中の < をエスケープしておく
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg: #f5f6f8; --panel: #ffffff; --border: #d8dce1;
    --text: #1a1d21; --muted: #5a6472;
    --accent: #2f6fed; --accent-bg: #eef4ff;
    --shadow: rgba(15, 23, 42, 0.10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1e2124; --panel: #26292d; --border: #3a3f45;
      --text: #e6e8ea; --muted: #9aa4af;
      --accent: #5b8def; --accent-bg: #253143;
      --shadow: rgba(0, 0, 0, 0.4);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Yu Gothic UI", "Meiryo", system-ui, sans-serif;
    font-size: 13px; display: flex; flex-direction: column;
  }
  header { display: flex; gap: 6px; padding: 10px 12px 4px; }
  #q {
    flex: 1; padding: 6px 10px; font: inherit;
    color: var(--text); background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; outline: none;
  }
  #q:focus { border-color: var(--accent); }
  .hbtn {
    font: inherit; font-size: 15px; width: 32px; cursor: pointer;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--panel); color: var(--muted);
  }
  .hbtn:hover { color: var(--accent); border-color: var(--accent); }
  #list { flex: 1; overflow-y: auto; padding: 2px 12px 10px; }
  .cat {
    margin: 12px 2px 4px; color: var(--muted);
    font-size: 11px; font-weight: bold; letter-spacing: 0.08em;
  }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; margin: 6px 0;
  }
  .card:hover { border-color: var(--accent); box-shadow: 0 2px 8px var(--shadow); }
  .chead { display: flex; align-items: center; gap: 8px; }
  .chead .icon { font-size: 22px; line-height: 1.15; }
  .clabel { flex: 1; font-weight: 600; font-size: 13px; }
  .help {
    width: 20px; height: 20px; line-height: 18px; font-size: 11px;
    text-align: center; cursor: pointer; padding: 0;
    border: 1px solid var(--border); border-radius: 50%;
    background: var(--panel); color: var(--muted);
  }
  .help:hover { color: var(--accent); border-color: var(--accent); }
  .runbtn {
    font: inherit; font-size: 12.5px; padding: 4px 16px; cursor: pointer;
    border: 1px solid var(--accent); border-radius: 7px;
    background: var(--accent); color: #fff;
  }
  .runbtn:hover { filter: brightness(1.1); }
  .ctrls {
    display: flex; flex-wrap: wrap; gap: 8px 18px;
    margin-top: 8px; align-items: flex-start;
  }
  .dirgrid {
    display: grid; grid-template-columns: repeat(3, 32px); gap: 4px;
  }
  .dbtn {
    width: 32px; height: 32px; font: inherit; font-size: 14px; padding: 0;
    cursor: pointer; border: 1px solid var(--border); border-radius: 7px;
    background: var(--bg); color: var(--text);
  }
  .dbtn:hover { border-color: var(--accent); background: var(--accent-bg); }
  .dbtn.last { border-color: var(--accent); color: var(--accent); }
  .opts { display: flex; flex-direction: column; gap: 5px; }
  .fld {
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--text); cursor: default;
  }
  .fld input[type="checkbox"] { accent-color: var(--accent); margin: 0; }
  .fld input[type="number"] {
    width: 62px; padding: 2px 5px; font: inherit; font-size: 12px;
    color: var(--text); background: var(--bg);
    border: 1px solid var(--border); border-radius: 5px;
  }
  .fld select {
    font: inherit; font-size: 12px; padding: 2px 4px;
    color: var(--text); background: var(--bg);
    border: 1px solid var(--border); border-radius: 5px;
  }
  .fld input:focus, .fld select:focus { outline: none; border-color: var(--accent); }
  .empty { color: var(--muted); text-align: center; padding: 24px 0; }
  body.running .card { opacity: 0.55; pointer-events: none; }
  footer {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px; border-top: 1px solid var(--border);
  }
  #status {
    color: var(--muted); flex: 1; font-size: 11.5px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #status.busy { color: var(--accent); font-weight: bold; }
  #ov {
    position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35);
    display: none; align-items: center; justify-content: center; padding: 18px;
  }
  #ov.show { display: flex; }
  #hp {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: 0 8px 30px var(--shadow);
    max-width: 430px; width: 100%; max-height: 88%; overflow-y: auto;
    padding: 16px 18px;
  }
  #hp .head { display: flex; gap: 10px; align-items: center; }
  #hp .icon { font-size: 26px; }
  #hp h3 { margin: 0; font-size: 14px; }
  #hp .name { color: var(--muted); font-size: 11px; margin-top: 2px; }
  #hp .detail {
    margin: 10px 0 0; padding-top: 10px; border-top: 1px dashed var(--border);
    color: var(--muted); white-space: pre-wrap; line-height: 1.6; font-size: 12px;
  }
</style>
</head>
<body>
<header>
  <input id="q" type="text" placeholder="絞り込み...">
  <button id="reload" class="hbtn" title="一覧を再読み込み">&#x21bb;</button>
</header>
<div id="list"></div>
<footer>
  <span id="status"></span>
  <button class="hbtn" id="hidebtn" title="隠す (Esc)">&#x2715;</button>
</footer>
<div id="ov"><div id="hp"></div></div>
<script>
"use strict";
let TOOLS = __TOOLS_JSON__;
let running = false;
const vals = {};  // ツール名 -> {key: 現在値} (再描画・再検出をまたいで保持)
const IDLE_HINT = "ボタンで実行 / ?で説明 / Esc: 隠す";
const CAT_JA = {"Placement": "配置", "Routing": "配線", "Zones": "ベタ", "Export": "出力"};
const CAT_ICON = {"Placement": "\\ud83d\\udccd", "Routing": "\\u2702\\ufe0f", "Zones": "\\ud83d\\udfe9"};
const DIR_LABELS = [["\\u2196", "\\u2191", "\\u2197"],
                    ["\\u2190", "\\u25cf", "\\u2192"],
                    ["\\u2199", "\\u2193", "\\u2198"]];
const DIR_X = ["left", "center", "right"];
const DIR_Y = ["top", "middle", "bottom"];
const JP_X = {left: "左に寄せる", center: "左右中央", right: "右に寄せる"};
const JP_Y = {top: "上に詰める", middle: "上下中央", bottom: "下に詰める"};
const list = document.getElementById("list");
const q = document.getElementById("q");
const status = document.getElementById("status");
const ov = document.getElementById("ov");
const hp = document.getElementById("hp");

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
function escAttr(s) { return esc(s).replace(/"/g, "&quot;"); }
function catLabel(c) { return CAT_JA[c] || c; }
function iconOf(t) { return t.icon || CAT_ICON[t.category] || "\\ud83e\\udde9"; }
function labelOf(t) { return t.label || t.summary; }
function toolVals(t) { return vals[t.name] || (vals[t.name] = {}); }
function valOf(t, c) {
  const v = toolVals(t);
  return c.key in v ? v[c.key] : c.default;
}
function ctrlHtml(i, t, c) {
  if (c.type === "dirgrid") {
    const cur = toolVals(t)[c.key] || c.last || [];
    let h = '<div class="dirgrid">';
    for (let r = 0; r < 3; r++) {
      for (let col = 0; col < 3; col++) {
        const ax = DIR_X[col], ay = DIR_Y[r];
        const tip = (ax === "center" && ay === "middle")
          ? "上下左右対称" : JP_X[ax] + " / " + JP_Y[ay];
        h += '<button class="dbtn' +
          ((cur[0] === ax && cur[1] === ay) ? " last" : "") +
          '" data-r="' + i + '" data-ax="' + ax + '" data-ay="' + ay +
          '" title="' + escAttr(tip) + '">' + DIR_LABELS[r][col] + '</button>';
      }
    }
    return h + '</div>';
  }
  if (c.type === "check") {
    return '<label class="fld"><input type="checkbox" data-i="' + i +
      '" data-key="' + escAttr(c.key) + '"' + (valOf(t, c) ? " checked" : "") +
      '> ' + esc(c.label || c.key) + '</label>';
  }
  if (c.type === "number") {
    return '<label class="fld">' + esc(c.label || c.key) +
      ' <input type="number" data-i="' + i + '" data-key="' + escAttr(c.key) +
      '" value="' + valOf(t, c) + '"' +
      (c.min != null ? ' min="' + c.min + '"' : '') +
      (c.max != null ? ' max="' + c.max + '"' : '') +
      (c.step != null ? ' step="' + c.step + '"' : '') + '>' +
      (c.unit ? ' ' + esc(c.unit) : '') + '</label>';
  }
  if (c.type === "choice") {
    let h = '<label class="fld">' + esc(c.label || c.key) +
      ' <select data-i="' + i + '" data-key="' + escAttr(c.key) + '">';
    const cur = valOf(t, c);
    (c.options || []).forEach(function (o, k) {
      h += '<option value="' + k + '"' + (k === cur ? " selected" : "") + '>' +
        esc(o) + '</option>';
    });
    return h + '</select></label>';
  }
  return "";
}
function render() {
  const needle = q.value.trim().toLowerCase();
  let html = "";
  let lastCat = null;
  TOOLS.forEach(function (t, i) {
    if (needle && !(t.name + " " + t.label + " " + t.summary + " " + t.detail)
        .toLowerCase().includes(needle)) return;
    if (t.category !== lastCat) {
      lastCat = t.category;
      html += '<div class="cat">' + esc(catLabel(t.category)) + '</div>';
    }
    const panel = t.panel || [];
    const runs = panel.filter(function (c) { return c.type === "run"; });
    const grids = panel.filter(function (c) { return c.type === "dirgrid"; });
    const opts = panel.filter(function (c) {
      return c.type === "check" || c.type === "number" || c.type === "choice";
    });
    html += '<div class="card" data-i="' + i + '">' +
      '<div class="chead" title="' + escAttr(t.summary) + '">' +
      '<span class="icon">' + iconOf(t) + '</span>' +
      '<span class="clabel">' + esc(labelOf(t)) + '</span>' +
      '<button class="help" data-h="' + i + '" title="説明を表示">?</button>';
    runs.forEach(function (c) {
      html += '<button class="runbtn" data-r="' + i + '">' +
        esc(c.label || "実行") + '</button>';
    });
    html += '</div>';
    if (grids.length || opts.length) {
      html += '<div class="ctrls">';
      grids.forEach(function (c) { html += ctrlHtml(i, t, c); });
      if (opts.length) {
        html += '<div class="opts">';
        opts.forEach(function (c) { html += ctrlHtml(i, t, c); });
        html += '</div>';
      }
      html += '</div>';
    }
    html += '</div>';
  });
  list.innerHTML = html || '<div class="empty">該当するツールがありません</div>';
}
function post(msg) {
  if (window.kicad) window.kicad.postMessage(JSON.stringify(msg));
  else console.log("post:", JSON.stringify(msg));
}
function setRunning(on, name) {
  running = on;
  document.body.classList.toggle("running", on);
  if (on) {
    status.textContent = "実行中: " + (name || "") + " …";
    status.className = "busy";
  } else {
    status.textContent = IDLE_HINT;
    status.className = "";
  }
}
window.setRunning = setRunning;
window.setTools = function (tools) {  // Python側から一覧を差し替える
  TOOLS = tools;
  render();
};
function gather(i) {
  const t = TOOLS[i];
  const params = {};
  (t.panel || []).forEach(function (c) {
    if (!c.key || c.type === "dirgrid" || c.type === "run") return;
    const v = valOf(t, c);
    params[c.key] = (typeof v === "number" && isNaN(v)) ? c.default : v;
  });
  return params;
}
function runTool(i, extra) {
  if (running || !(i >= 0)) return;
  const t = TOOLS[i];
  const params = Object.assign(gather(i), extra || {});
  render();  // dirgridの前回位置マークなどを反映
  setRunning(true, labelOf(t));
  post({cmd: "run", index: i, params: params});
}
function openHelp(i) {
  if (!(i >= 0)) return;
  const t = TOOLS[i];
  hp.innerHTML = '<div class="head"><span class="icon">' + iconOf(t) +
    '</span><div><h3>' + esc(labelOf(t)) + '</h3>' +
    '<div class="name">' + esc(t.name) + '</div></div></div>' +
    '<p class="detail">' +
    esc(t.summary + (t.detail ? "\\n\\n" + t.detail : "")) + '</p>';
  ov.classList.add("show");
}
function closeHelp() { ov.classList.remove("show"); }
list.addEventListener("click", function (e) {
  const h = e.target.closest(".help");
  if (h) { openHelp(+h.dataset.h); return; }
  const r = e.target.closest("[data-r]");
  if (!r) return;
  const i = +r.dataset.r;
  let extra = null;
  if (r.dataset.ax) {  // 3x3ボタン: 押した向きをパラメータに載せる
    const dg = (TOOLS[i].panel || []).find(function (c) {
      return c.type === "dirgrid";
    });
    if (dg) {
      extra = {};
      extra[dg.key] = [r.dataset.ax, r.dataset.ay];
      toolVals(TOOLS[i])[dg.key] = extra[dg.key];
    }
  }
  runTool(i, extra);
});
list.addEventListener("change", function (e) {
  const el = e.target;
  if (!el.dataset || el.dataset.i === undefined || !el.dataset.key) return;
  const t = TOOLS[+el.dataset.i];
  let v;
  if (el.type === "checkbox") v = el.checked;
  else if (el.tagName === "SELECT") v = +el.value;
  else v = parseFloat(el.value);
  if (typeof v === "number" && isNaN(v)) return;  // 入力途中は保存しない
  toolVals(t)[el.dataset.key] = v;
});
ov.addEventListener("click", function (e) { if (e.target === ov) closeHelp(); });
q.addEventListener("input", render);
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") {
    if (ov.classList.contains("show")) closeHelp();
    else post({cmd: "hide"});
  }
});
document.getElementById("hidebtn").onclick = function () { post({cmd: "hide"}); };
document.getElementById("reload").onclick = function () { post({cmd: "refresh"}); };
status.textContent = IDLE_HINT;
q.focus();
render();
</script>
</body>
</html>
"""


def _build_html(tools, initial):
    return (_HTML_TEMPLATE
            .replace("__TOOLS_JSON__", _tools_json(tools))
            .replace("__INITIAL_INDEX__", str(initial)))


if wx is not None and wxhtml2 is not None:

    class PaletteFrame(wx.Frame):
        """常駐型のツールパレット。閉じる操作では破棄せず隠すだけ(トグルで再表示)。

        JS側は window.kicad.postMessage(JSON文字列) でPython側へ通知する:
          {cmd:"run", index:N} → ツールNを実行(実行中は多重実行をロック)
          {cmd:"hide"} → 隠す / {cmd:"refresh"} → 一覧を再検出して差し替え
        """

        def __init__(self, parent):
            # FLOAT_ON_PARENT は親必須。親が見つからない場合は通常フレームにする
            style = wx.DEFAULT_FRAME_STYLE
            if parent is not None:
                style |= wx.FRAME_FLOAT_ON_PARENT
            super().__init__(
                parent,
                title="PCBツール パレット",
                size=(460, 640),
                style=style,
            )
            self.SetName(_PALETTE_NAME)
            self.tools = discover_tools()
            self._running = False
            self._page_loaded = False

            backend = wxhtml2.WebViewBackendDefault
            if wxhtml2.WebView.IsBackendAvailable(wxhtml2.WebViewBackendEdge):
                backend = wxhtml2.WebViewBackendEdge
            self.webview = wxhtml2.WebView.New(self, backend=backend)
            if not self.webview:
                raise RuntimeError("WebView backend unavailable")
            # ハンドラ登録はSetPageより前に行う(登録後に読み込んだページにのみ注入される)
            if not self.webview.AddScriptMessageHandler("kicad"):
                raise RuntimeError("AddScriptMessageHandler failed")
            self.Bind(wxhtml2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED,
                      self._on_msg, self.webview)
            self.Bind(wxhtml2.EVT_WEBVIEW_LOADED, self._on_loaded, self.webview)
            self.Bind(wx.EVT_CLOSE, self._on_close)

            sizer = wx.BoxSizer(wx.VERTICAL)
            sizer.Add(self.webview, 1, wx.EXPAND)
            self.SetSizer(sizer)

            initial = 0
            for i, t in enumerate(self.tools):
                if t["name"] == _last_tool_name:
                    initial = i
                    break
            self.webview.SetPage(_build_html(self.tools, initial), "")

        def _on_close(self, evt):
            # ×やEscでは常駐のまま隠す。KiCad終了などの強制クローズだけ実際に破棄する
            if evt.CanVeto():
                evt.Veto()
                self.Hide()
            else:
                self.Destroy()

        def _on_loaded(self, evt):
            if self._page_loaded:
                return
            self._page_loaded = True
            self.webview.SetFocus()

        def refresh_tools(self):
            """一覧を再検出してJS側に差し替えを送る(検索文字列などは保持される)。"""
            self.tools = discover_tools()
            if self._page_loaded:
                self.webview.RunScript(
                    "window.setTools(%s)" % _tools_json(self.tools))

        def _on_msg(self, evt):
            try:
                msg = json.loads(evt.GetString())
            except ValueError:
                return
            cmd = msg.get("cmd")
            if cmd == "run":
                # WebView2は別プロセス描画のため、メインスレッドが忙しくても
                # クリックが届き続ける。多重実行はここでロックする
                if self._running:
                    return
                idx = msg.get("index")
                params = msg.get("params")
                if isinstance(idx, int):
                    self._running = True
                    # WebView2のコールバック内で直接実行しない(固まるため)
                    wx.CallAfter(self._run_tool, idx,
                                 params if isinstance(params, dict) else None)
            elif cmd == "hide":
                wx.CallAfter(self.Hide)
            elif cmd == "refresh":
                wx.CallAfter(self.refresh_tools)

        def _run_tool(self, idx, params=None):
            global _last_tool_name
            mod = None
            try:
                if not (0 <= idx < len(self.tools)):
                    return
                tool = self.tools[idx]
                _last_tool_name = tool["name"]
                # パネルの入力値をモジュール変数 PRESET 経由でツールに渡す。
                # PRESET があるツールは Run() が設定ダイアログを出さずに使う
                mod = tool.get("mod")
                if mod is not None:
                    try:
                        mod.PRESET = params or None
                    except Exception:
                        mod = None
                try:
                    run_tool(tool)  # メニューイベント経由(Ctrl+Zを効かせる)
                except Exception:
                    wx.MessageBox(
                        traceback.format_exc(),
                        "%s の実行でエラー" % tool["name"],
                        wx.OK | wx.ICON_ERROR,
                        self,
                    )
            finally:
                if mod is not None:
                    try:
                        mod.PRESET = None  # 未消費でも次回に持ち越さない
                    except Exception:
                        pass
                self._running = False
                try:
                    self.webview.RunScript("window.setRunning(false)")
                except Exception:
                    pass


if wx is not None:

    class LauncherDialog(wx.Dialog):
        """wx標準ウィジェット版(WebView2が使えない環境向けの一発モーダル)。"""

        def __init__(self, parent, tools, initial):
            super().__init__(
                parent,
                title="配置ツール",
                size=(640, 440),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            )
            self.tools = tools
            self.selected = None

            self.listbox = wx.ListBox(self, choices=[t["name"] for t in tools])
            self.detail = wx.TextCtrl(
                self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_BESTWRAP
            )
            run_btn = wx.Button(self, wx.ID_OK, "実行")
            close_btn = wx.Button(self, wx.ID_CANCEL, "閉じる")
            run_btn.SetDefault()

            body = wx.BoxSizer(wx.HORIZONTAL)
            body.Add(self.listbox, 2, wx.EXPAND | wx.ALL, 8)
            body.Add(self.detail, 3, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, 8)
            btns = wx.BoxSizer(wx.HORIZONTAL)
            btns.AddStretchSpacer()
            btns.Add(run_btn, 0, wx.RIGHT, 8)
            btns.Add(close_btn, 0)
            root = wx.BoxSizer(wx.VERTICAL)
            root.Add(body, 1, wx.EXPAND)
            root.Add(btns, 0, wx.EXPAND | wx.ALL, 8)
            self.SetSizer(root)

            self.listbox.Bind(wx.EVT_LISTBOX, self._on_select)
            self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_run)
            run_btn.Bind(wx.EVT_BUTTON, self._on_run)

            if tools:
                self.listbox.SetSelection(initial)
                self._show_detail(initial)

        def _show_detail(self, idx):
            t = self.tools[idx]
            text = t["summary"]
            if t["detail"]:
                text += "\n\n" + t["detail"]
            self.detail.SetValue(text)

        def _on_select(self, event):
            idx = self.listbox.GetSelection()
            if idx != wx.NOT_FOUND:
                self._show_detail(idx)

        def _on_run(self, event):
            idx = self.listbox.GetSelection()
            if idx == wx.NOT_FOUND:
                return
            self.selected = idx
            self.EndModal(wx.ID_OK)


class PackToolLauncher(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "PCB Tools Palette (toggle)"
        self.category = "Placement"
        self.description = "PCBツールパレットの表示/非表示を切り替える"
        self.show_toolbar_button = True
        if os.path.isfile(_ICON_FILE):
            self.icon_file_name = _ICON_FILE
            self.dark_icon_file_name = _ICON_FILE

    def Run(self):
        global _last_tool_name
        if wx is None:
            return

        # 常駐パレットのトグル(存在確認はウィンドウ名だけを信頼する)
        frame = wx.FindWindowByName(_PALETTE_NAME)
        if frame is not None:
            if frame.IsShown():
                frame.Hide()
            else:
                frame.refresh_tools()
                frame.Show()
                frame.Raise()
            return

        if wxhtml2 is not None:
            try:
                frame = PaletteFrame(wx.FindWindowByName("PcbFrame"))
                frame.Show()
                frame.Raise()
                return
            except Exception:
                pass  # WebView2が無い等 → wx標準版へフォールバック

        # フォールバック: 従来の一発モーダルダイアログ
        tools = discover_tools()
        initial = 0
        for i, t in enumerate(tools):
            if t["name"] == _last_tool_name:
                initial = i
                break
        dlg = LauncherDialog(wx.FindWindowByName("PcbFrame"), tools, initial)
        idx = None
        if dlg.ShowModal() == wx.ID_OK:
            idx = dlg.selected
        dlg.Destroy()
        if idx is None or not (0 <= idx < len(tools)):
            return
        tool = tools[idx]
        _last_tool_name = tool["name"]
        try:
            run_tool(tool)  # メニューイベント経由(Ctrl+Zを効かせる)
        except Exception:
            wx.MessageBox(
                traceback.format_exc(),
                "%s の実行でエラー" % tool["name"],
                wx.OK | wx.ICON_ERROR,
                wx.FindWindowByName("PcbFrame"),
            )


# 「プラグインを更新」でモジュールが再importされたとき、旧モジュールの
# ハンドラを抱えた古いパレットが残っていれば破棄する(Destroyはクローズイベントを経ない)
if wx is not None:
    try:
        _stale = wx.FindWindowByName(_PALETTE_NAME)
        if _stale is not None:
            _stale.Destroy()
    except Exception:
        pass

PackToolLauncher().register()
