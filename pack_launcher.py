# pack_launcher.py — PCBツールパレット(常駐型ランチャー)。
# ツールバーのボタン1つで、同じフォルダのプラグイン一覧パレットの表示/非表示を切り替える。
# パレットは非モーダルの常駐ウィンドウで、開いたままpcbnewを操作できる。
# 一覧はHTML/CSS/JS(WebView2)で描画し、カテゴリ別セクション+アイコン+検索付き。
# WebView2が使えない環境ではwx標準ウィジェットの一発ダイアログにフォールバックする。
#
# ツールの追加規約(コード修正不要):
#   - ActionPluginサブクラスを含む .py をこのフォルダに置く(+「プラグインを更新」)
#   - docstring 1行目=カードに出る短い日本語要約、空行、以降=選択時に展開される詳細
#   - モジュール変数 ICON = "🧱" (絵文字1つ) でカードのアイコンを指定(省略可)
#   - plugin.category がセクション見出しになる
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

    戻り値: [{"inst", "name", "summary", "detail", "icon", "category"}, ...] 名前順。
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
            tools.append({
                "inst": inst,
                "name": inst.name,
                "summary": summary or inst.name,
                "detail": detail,
                "icon": getattr(mod, "ICON", ""),
                "category": inst.category or "その他",
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
    payload = [{k: t[k] for k in ("name", "summary", "detail", "icon", "category")}
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
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1e2124; --panel: #26292d; --border: #3a3f45;
      --text: #e6e8ea; --muted: #9aa4af;
      --accent: #5b8def; --accent-bg: #253143;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Yu Gothic UI", "Meiryo", system-ui, sans-serif;
    font-size: 13px; display: flex; flex-direction: column;
  }
  header { display: flex; gap: 6px; padding: 10px 12px 6px; }
  #q {
    flex: 1; padding: 7px 10px; font: inherit;
    color: var(--text); background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; outline: none;
  }
  #q:focus { border-color: var(--accent); }
  #reload {
    font: inherit; font-size: 15px; width: 34px; cursor: pointer;
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--panel); color: var(--muted);
  }
  #reload:hover { color: var(--accent); border-color: var(--accent); }
  #list { flex: 1; overflow-y: auto; padding: 2px 12px 8px; }
  .cat {
    margin: 10px 2px 2px; color: var(--muted);
    font-size: 11px; font-weight: bold; letter-spacing: 0.08em;
  }
  .card {
    display: flex; gap: 10px; align-items: flex-start;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px; margin: 6px 0; cursor: pointer;
  }
  .card:hover { border-color: var(--accent); }
  .card.sel {
    border-color: var(--accent); background: var(--accent-bg);
    box-shadow: 0 0 0 1px var(--accent);
  }
  .card .icon { font-size: 22px; line-height: 1.2; flex: none; }
  .card .body { flex: 1; min-width: 0; }
  .card h3 { margin: 0; font-size: 14px; }
  .card .sub { margin-top: 3px; color: var(--muted); font-size: 11.5px; }
  .card .detail {
    margin: 8px 0 0; padding-top: 8px; border-top: 1px dashed var(--border);
    color: var(--muted); white-space: pre-wrap; line-height: 1.55;
  }
  .empty { color: var(--muted); text-align: center; padding: 24px 0; }
  body.running .card { opacity: 0.55; pointer-events: none; }
  footer {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; border-top: 1px solid var(--border);
  }
  #status { color: var(--muted); flex: 1; }
  #status.busy { color: var(--accent); font-weight: bold; }
  button.act {
    font: inherit; padding: 6px 18px; border-radius: 6px; cursor: pointer;
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
  }
  #runbtn { background: var(--accent); border-color: var(--accent); color: #fff; }
  body.running #runbtn { opacity: 0.55; pointer-events: none; }
</style>
</head>
<body>
<header>
  <input id="q" type="text" placeholder="ツール名・説明で絞り込み...">
  <button id="reload" title="一覧を再読み込み">&#x21bb;</button>
</header>
<div id="list"></div>
<footer>
  <span id="status">Enter: 実行 / ↑↓: 選択 / Esc: 隠す / ツールバーのボタンで表示切替</span>
  <button class="act" id="runbtn">実行</button>
  <button class="act" id="hidebtn">隠す</button>
</footer>
<script>
"use strict";
let TOOLS = __TOOLS_JSON__;
let sel = __INITIAL_INDEX__;
let view = [];
let running = false;
const IDLE_HINT = "Enter: 実行 / ↑↓: 選択 / Esc: 隠す / ツールバーのボタンで表示切替";
const CAT_JA = {"Placement": "配置", "Routing": "配線", "Zones": "ベタ", "Export": "出力"};
const CAT_ICON = {"Placement": "\\ud83d\\udccd", "Routing": "\\u2702\\ufe0f", "Zones": "\\ud83d\\udfe9"};
const list = document.getElementById("list");
const q = document.getElementById("q");
const status = document.getElementById("status");

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
function catLabel(c) { return CAT_JA[c] || c; }
function iconOf(t) { return t.icon || CAT_ICON[t.category] || "\\ud83e\\udde9"; }
function render() {
  const needle = q.value.trim().toLowerCase();
  view = TOOLS.map((t, i) => i).filter(i =>
    !needle ||
    TOOLS[i].name.toLowerCase().includes(needle) ||
    TOOLS[i].summary.toLowerCase().includes(needle) ||
    TOOLS[i].detail.toLowerCase().includes(needle));
  if (!view.includes(sel)) sel = view.length ? view[0] : -1;
  let html = "";
  let lastCat = null;
  for (const i of view) {
    const t = TOOLS[i];
    if (t.category !== lastCat) {
      lastCat = t.category;
      html += '<div class="cat">' + esc(catLabel(t.category)) + "</div>";
    }
    const isSel = i === sel;
    html += '<div class="card' + (isSel ? " sel" : "") + '" data-i="' + i + '">';
    html += '<span class="icon">' + iconOf(t) + "</span>";
    html += '<div class="body"><h3>' + esc(t.summary) + "</h3>";
    html += '<div class="sub">' + esc(t.name) + "</div>";
    if (isSel && t.detail) html += '<p class="detail">' + esc(t.detail) + "</p>";
    html += "</div></div>";
  }
  list.innerHTML = html || '<div class="empty">該当するツールがありません</div>';
}
function post(msg) { window.kicad.postMessage(JSON.stringify(msg)); }
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
  if (sel >= TOOLS.length) sel = TOOLS.length ? 0 : -1;
  render();
};
function runSel() {
  if (running || sel < 0) return;
  setRunning(true, TOOLS[sel].summary);
  post({cmd: "run", index: sel});
}
list.addEventListener("click", e => {
  const c = e.target.closest(".card");
  if (c) { sel = +c.dataset.i; render(); }
});
list.addEventListener("dblclick", e => {
  const c = e.target.closest(".card");
  if (c) { sel = +c.dataset.i; runSel(); }
});
q.addEventListener("input", render);
document.addEventListener("keydown", e => {
  if (e.key === "Enter") {
    runSel();
  } else if (e.key === "Escape") {
    post({cmd: "hide"});
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!view.length) return;
    let p = view.indexOf(sel) + (e.key === "ArrowDown" ? 1 : -1);
    p = Math.min(view.length - 1, Math.max(0, p));
    sel = view[p];
    render();
    const c = document.querySelector(".card.sel");
    if (c) c.scrollIntoView({block: "nearest"});
  }
});
document.getElementById("runbtn").onclick = runSel;
document.getElementById("hidebtn").onclick = () => post({cmd: "hide"});
document.getElementById("reload").onclick = () => post({cmd: "refresh"});
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
                size=(480, 640),
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
                if isinstance(idx, int):
                    self._running = True
                    # WebView2のコールバック内で直接実行しない(固まるため)
                    wx.CallAfter(self._run_tool, idx)
            elif cmd == "hide":
                wx.CallAfter(self.Hide)
            elif cmd == "refresh":
                wx.CallAfter(self.refresh_tools)

        def _run_tool(self, idx):
            global _last_tool_name
            try:
                if not (0 <= idx < len(self.tools)):
                    return
                tool = self.tools[idx]
                _last_tool_name = tool["name"]
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
