"""
简单的文件夹搜索 Web 应用（仅使用 Python 标准库）

用法:
    python folder_search.py [ROOT] [-p PORT] [-h HOST]

参数:
    ROOT              要浏览的根目录。留空 = 脚本所在目录；也可用 "." 或 CWD 表示当前工作目录
    -p, --port PORT   端口（默认 5000）
    -h, --host HOST   监听地址（默认 127.0.0.1）
    --help            显示帮助

示例:
    python folder_search.py                     # 以脚本目录为根
    python folder_search.py D:\\Downloads
    python folder_search.py . -p 8080
    python folder_search.py "C:\\My Docs" -h 0.0.0.0 -p 8000
"""
import os
import re
import sys
import html
import json
import argparse
import mimetypes
import posixpath
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 运行期由 CLI 参数决定；下面给出默认值
ROOT_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 5000

# 访问历史（内存中保存，进程重启会清空）
# 元素为 (rel_path_posix, display_name)，最新的在前
import threading
HISTORY_MAX = 20
_history = []  # type: list[tuple[str, str]]
_history_lock = threading.Lock()


def push_history(rel_path: str):
    """把一次目录访问加入历史。rel_path 为相对 ROOT_DIR 的 posix 路径，空串代表根目录。"""
    rel_path = rel_path.strip("/\\")
    display = rel_path if rel_path else "（根目录）"
    with _history_lock:
        # 去重
        for i, (r, _) in enumerate(_history):
            if r == rel_path:
                _history.pop(i)
                break
        _history.insert(0, (rel_path, display))
        del _history[HISTORY_MAX:]


def get_history():
    with _history_lock:
        return list(_history)


def clear_history():
    with _history_lock:
        _history.clear()


# ---------- 根目录管理 ----------
ROOTS_FILE = Path(__file__).resolve().parent / ".fs_roots.json"
_roots_lock = threading.Lock()
# 每个请求线程的当前根目录（如果 cookie 里有）
_ctx = threading.local()


def load_roots():
    """从磁盘加载根目录历史。返回 [str, ...]（绝对路径字符串，最近使用在前）。"""
    try:
        with open(ROOTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str) and x]
    except (OSError, ValueError):
        pass
    return []


def save_roots(roots):
    try:
        with open(ROOTS_FILE, "w", encoding="utf-8") as f:
            json.dump(roots, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def add_root(path_str: str):
    """把一个目录加入根目录历史，最近使用在前，最多保留 20 条。"""
    if not path_str:
        return
    with _roots_lock:
        roots = load_roots()
        # 去重（忽略大小写在 Windows 上）
        norm = os.path.normcase(os.path.normpath(path_str))
        roots = [r for r in roots if os.path.normcase(os.path.normpath(r)) != norm]
        roots.insert(0, path_str)
        del roots[20:]
        save_roots(roots)


def remove_root(path_str: str):
    with _roots_lock:
        roots = load_roots()
        norm = os.path.normcase(os.path.normpath(path_str))
        roots = [r for r in roots if os.path.normcase(os.path.normpath(r)) != norm]
        save_roots(roots)


def get_roots():
    with _roots_lock:
        return load_roots()


def current_root() -> Path:
    """返回本次请求应使用的根目录。优先线程本地，否则全局 ROOT_DIR。"""
    r = getattr(_ctx, "root", None)
    if r is not None:
        return r
    return ROOT_DIR

BASE_CSS = """
* { box-sizing: border-box; }
body {
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    max-width: 1200px; margin: 30px auto; padding: 0 20px;
    background: #f5f5f7; color: #333;
}
h1 { color: #1d1d1f; word-break: break-all; }
.info { color: #666; margin-bottom: 15px; font-size: 14px; }
.search-box { display: flex; gap: 10px; margin-bottom: 20px; }
input[type="text"] {
    flex: 1; padding: 10px 14px; font-size: 16px;
    border: 1px solid #ccc; border-radius: 8px; outline: none;
}
input[type="text"]:focus { border-color: #0071e3; }
button {
    padding: 10px 20px; font-size: 16px; background: #0071e3;
    color: #fff; border: none; border-radius: 8px; cursor: pointer;
}
button:hover { background: #0077ed; }
ul.results, ul.items { list-style: none; padding: 0; }
ul.results li, ul.items li {
    background: #fff; padding: 10px 16px; margin-bottom: 6px;
    border-radius: 8px; display: flex; align-items: center;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
ul.results li a, ul.items li a {
    color: #0071e3; text-decoration: none; flex: 1;
}
ul.results li a:hover, ul.items li a:hover { text-decoration: underline; }
.icon { margin-right: 10px; font-size: 18px; }
.size { color: #999; font-size: 12px; }
.empty { color: #999; text-align: center; padding: 40px; }
.back { display: inline-block; margin-bottom: 15px; color: #0071e3; text-decoration: none; font-size: 14px; }
.breadcrumb { margin-bottom: 20px; font-size: 14px; }
.breadcrumb a { color: #0071e3; text-decoration: none; }

/* 两栏布局：左侧主内容 / 右侧工具栏 */
.layout { display: flex; gap: 20px; align-items: flex-start; }
.main { flex: 1; min-width: 0; }
.sidebar {
    width: 300px; flex-shrink: 0;
    background: #fff; padding: 18px; border-radius: 12px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    position: sticky; top: 20px;
}
.sidebar h2 { margin: 0 0 12px; font-size: 16px; color: #1d1d1f; }
.sidebar h2.mt { margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; }
.sidebar .hint { color: #888; font-size: 12px; margin-bottom: 10px; word-break: break-all; }
.sidebar form { display: flex; flex-direction: column; gap: 10px; }
.sidebar input[type="text"] {
    width: 100%; padding: 8px 12px; font-size: 14px;
}
.sidebar button { width: 100%; padding: 8px 12px; font-size: 14px; }

/* 历史记录 */
.history { list-style: none; padding: 0; margin: 0; }
.history li {
    padding: 6px 8px; border-radius: 6px; font-size: 13px;
    display: flex; align-items: center; gap: 6px;
}
.history li:hover { background: #f0f0f5; }
.history li a {
    color: #0071e3; text-decoration: none; flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.history li a:hover { text-decoration: underline; }
.history .idx { color: #bbb; font-size: 11px; width: 20px; text-align: right; }
.history-header {
    display: flex; align-items: center; justify-content: space-between;
    margin: 20px 0 8px; padding-top: 16px; border-top: 1px solid #eee;
}
.history-header h2 { margin: 0; font-size: 16px; color: #1d1d1f; }
.history-header a.clear {
    color: #888; font-size: 12px; text-decoration: none;
}
.history-header a.clear:hover { color: #d33; text-decoration: underline; }
.history-empty { color: #bbb; font-size: 12px; padding: 6px 8px; }

/* 返回时的高亮效果 */
@keyframes fs-flash {
    0%   { background: #fff3b0; }
    100% { background: #fff; }
}
ul.results li.fs-active, ul.items li.fs-active {
    animation: fs-flash 1.6s ease-out;
}
.msg {
    padding: 10px 14px; border-radius: 8px; margin-bottom: 15px; font-size: 14px;
}
.msg.ok { background: #e5f6ea; color: #1d7a3c; border: 1px solid #a7d8b6; }
.msg.err { background: #fdecea; color: #a12525; border: 1px solid #f2b8b3; }
@media (max-width: 800px) {
    .layout { flex-direction: column; }
    .sidebar { width: 100%; position: static; }
}
"""

# 滚动位置记忆脚本 + 返回时定位到上次点击的目录/文件
SCROLL_JS = """
<script>
(function () {
    var PATH_KEY = 'fs_scroll_' + location.pathname + location.search;
    var CLICK_KEY = 'fs_clicked_' + location.pathname + location.search;

    try { if ('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch (e) {}

    // ---- 记录点击 ----
    // 每一个列表项都带 data-fs-key（相对路径），点击时把它保存为"上次点击项"
    document.addEventListener('click', function (e) {
        var li = e.target && (e.target.closest ? e.target.closest('li[data-fs-key]') : null);
        if (li) {
            try { sessionStorage.setItem(CLICK_KEY, li.getAttribute('data-fs-key')); } catch (err) {}
        }
        // 提前保存滚动位置
        savePos();
    }, true);

    // ---- 记录滚动位置（备用） ----
    function savePos() {
        try {
            var y = window.scrollY || window.pageYOffset ||
                    document.documentElement.scrollTop || 0;
            sessionStorage.setItem(PATH_KEY, String(y));
        } catch (e) {}
    }
    var t = null;
    window.addEventListener('scroll', function () {
        if (t) return;
        t = setTimeout(function () { t = null; savePos(); }, 100);
    }, { passive: true });
    window.addEventListener('beforeunload', savePos);
    window.addEventListener('pagehide', savePos);

    // ---- 恢复：优先定位到上次点击项，其次用滚动像素 ----
    function restore() {
        var key = null;
        var y = null;
        try {
            key = sessionStorage.getItem(CLICK_KEY);
            y = sessionStorage.getItem(PATH_KEY);
        } catch (e) {}

        if (key) {
            var sel = 'li[data-fs-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key.replace(/"/g,'\\\\"')) + '"]';
            var li = document.querySelector(sel);
            if (li) {
                li.scrollIntoView({ block: 'center' });
                li.classList.add('fs-active');
                // 一次性使用后清掉，防止刷新时反复触发
                try { sessionStorage.removeItem(CLICK_KEY); } catch (e) {}
                return;
            }
        }
        if (y !== null) {
            window.scrollTo(0, parseInt(y, 10) || 0);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restore);
    } else {
        restore();
    }
    // 兜底：load 之后再来一次
    window.addEventListener('load', function () { setTimeout(restore, 30); });
})();
</script>
"""


def format_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def safe_resolve(rel_path: str):
    """把相对路径解析为绝对路径，确保不越过当前根目录。返回 Path 或 None。"""
    rel_path = rel_path.strip("/\\")
    root = current_root()
    if not rel_path:
        return root
    try:
        target = (root / rel_path).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
        return None


# 合法的文件夹名（跨平台安全）：不含 \ / : * ? " < > | 且不以点或空格结尾，长度 1-100
_INVALID_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def validate_folder_name(name: str):
    """返回 (ok, message)。"""
    name = (name or "").strip()
    if not name:
        return False, "文件夹名不能为空"
    if len(name) > 100:
        return False, "文件夹名过长（最多 100 字符）"
    if name in (".", ".."):
        return False, "非法的文件夹名"
    if _INVALID_NAME_RE.search(name):
        return False, '文件夹名不能包含 \\ / : * ? " < > |'
    if name.endswith((" ", ".")):
        return False, "文件夹名不能以空格或点结尾"
    return True, name


def render_sidebar(current_rel: str, back_q: str = "") -> str:
    """右侧工具栏：根目录切换 + 创建文件夹 + 最近访问历史。current_rel 为空串代表根目录。"""
    display = current_rel if current_rel else "（根目录）"
    q_suffix = f"?q={quote(back_q)}" if back_q else ""
    cur_root_str = str(current_root())

    # 根目录快速切换（最近使用的前 5 条，不含当前）
    roots = get_roots()
    quick = []
    for r in roots:
        if os.path.normcase(os.path.normpath(r)) != os.path.normcase(os.path.normpath(cur_root_str)):
            quick.append(r)
        if len(quick) >= 5:
            break

    if quick:
        rows = []
        for r in quick:
            rows.append(
                f'<li><a href="/roots/use?path={quote(r)}" title="{html.escape(r)}">📁 {html.escape(r)}</a></li>'
            )
        quick_html = '<ul class="history">' + "".join(rows) + "</ul>"
    else:
        quick_html = '<div class="history-empty">暂无其他根目录</div>'

    # 访问历史
    hist = get_history()
    if hist:
        rows = []
        for i, (rel, disp) in enumerate(hist, 1):
            if rel:
                href = f"/browse/{quote(rel)}{q_suffix}"
            else:
                href = f"/{q_suffix}"
            rows.append(
                f'<li><span class="idx">{i}</span>'
                f'<a href="{href}" title="{html.escape(disp)}">📂 {html.escape(disp)}</a></li>'
            )
        hist_html = '<ul class="history">' + "".join(rows) + "</ul>"
    else:
        hist_html = '<div class="history-empty">暂无访问记录</div>'

    return f"""
<aside class="sidebar">
    <h2>📌 当前根目录</h2>
    <div class="hint"><code>{html.escape(cur_root_str)}</code></div>
    <a class="back" href="/roots" style="margin:0;">🗂️ 管理根目录</a>

    <div class="history-header">
        <h2>🔀 快速切换</h2>
    </div>
    {quick_html}

    <h2 class="mt">🛠️ 新建文件夹</h2>
    <div class="hint">位置：<br><code>{html.escape(display)}</code></div>
    <form method="post" action="/mkdir">
        <input type="hidden" name="parent" value="{html.escape(current_rel)}">
        <input type="hidden" name="q" value="{html.escape(back_q)}">
        <input type="text" name="name" placeholder="新文件夹名称" required maxlength="100">
        <button type="submit">➕ 创建文件夹</button>
    </form>

    <div class="history-header">
        <h2>🕘 最近访问</h2>
        <a class="clear" href="/history/clear?back={quote(('/browse/' + quote(current_rel) + q_suffix) if current_rel else ('/' + q_suffix))}">清空</a>
    </div>
    {hist_html}
</aside>
"""


def render_msg(msg: str, kind: str) -> str:
    if not msg:
        return ""
    cls = "ok" if kind == "ok" else "err"
    return f'<div class="msg {cls}">{html.escape(msg)}</div>'


def render_roots(msg: str = "", msg_kind: str = "") -> str:
    cur_root = str(current_root())
    roots = get_roots()
    rows = []
    for r in roots:
        is_cur = os.path.normcase(os.path.normpath(r)) == os.path.normcase(os.path.normpath(cur_root))
        badge = ' <span style="color:#1d7a3c;font-size:12px;">✓ 当前</span>' if is_cur else ""
        actions = []
        if not is_cur:
            actions.append(f'<a href="/roots/use?path={quote(r)}">切换到此</a>')
        actions.append(f'<a href="/roots/remove?path={quote(r)}" style="color:#a12525;" onclick="return confirm(\'从历史中移除该目录？\')">移除</a>')
        rows.append(
            f'<li style="justify-content:space-between;gap:12px;">'
            f'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;">📁 <code>{html.escape(r)}</code>{badge}</span>'
            f'<span style="font-size:13px;display:flex;gap:10px;">{" · ".join(actions)}</span>'
            f'</li>'
        )
    list_html = (
        '<ul class="items">' + "".join(rows) + "</ul>"
        if rows
        else '<div class="empty">还没有历史根目录</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>根目录管理</title>
<style>{BASE_CSS}</style></head><body>
<a class="back" href="/">← 返回</a>
<h1>🗂️ 根目录管理</h1>
<div class="info">当前根目录: <code>{html.escape(cur_root)}</code></div>
<div class="layout">
    <div class="main">
        {render_msg(msg, msg_kind)}
        <h2 style="font-size:16px;margin:10px 0;">添加新的根目录</h2>
        <form class="search-box" method="post" action="/roots/add">
            <input type="text" name="path" placeholder="输入绝对路径，如 D:\\Downloads 或 /home/user/docs" required>
            <button type="submit">添加并切换</button>
        </form>
        <h2 style="font-size:16px;margin:20px 0 10px;">历史根目录</h2>
        {list_html}
    </div>
</div>
{SCROLL_JS}
</body></html>"""


def render_index(query: str, msg: str = "", msg_kind: str = "") -> str:
    query_l = query.lower()
    root = current_root()
    folders = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir(follow_symlinks=False):
                if not query or query_l in entry.name.lower():
                    folders.append(entry.name)
    except PermissionError:
        pass
    folders.sort(key=str.lower)

    items_html = ""
    if folders:
        rows = []
        for name in folders:
            # 浏览链接带上 q，便于返回时保留搜索
            q_param = f"?q={quote(query)}" if query else ""
            rows.append(
                f'<li data-fs-key="{html.escape(name)}"><span class="icon">📂</span>'
                f'<a href="/browse/{quote(name)}{q_param}">{html.escape(name)}</a></li>'
            )
        items_html = '<ul class="results">' + "".join(rows) + "</ul>"
    else:
        items_html = '<div class="empty">没有找到匹配的文件夹</div>'

    if query:
        info = f'关键字 "<b>{html.escape(query)}</b>" 匹配到 {len(folders)} 个文件夹'
    else:
        info = f"共 {len(folders)} 个文件夹"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>文件夹搜索</title>
<style>{BASE_CSS}</style></head><body>
<h1>📁 文件夹搜索</h1>
<div class="info">根目录: <code>{html.escape(str(root))}</code> · <a href="/roots">切换</a></div>
<div class="layout">
    <div class="main">
        {render_msg(msg, msg_kind)}
        <form class="search-box" method="get" action="/">
            <input type="text" name="q" value="{html.escape(query)}" placeholder="输入关键字搜索文件夹（留空显示全部）..." autofocus>
            <button type="submit">搜索</button>
        </form>
        <div class="info">{info}</div>
        {items_html}
    </div>
    {render_sidebar("", back_q=query)}
</div>
{SCROLL_JS}
</body></html>"""


def render_browse(target: Path, back_q: str = "", sq: str = "", msg: str = "", msg_kind: str = "") -> str:
    root = current_root()
    sq_l = sq.lower()
    entries = []
    try:
        for entry in os.scandir(target):
            if sq and sq_l not in entry.name.lower():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            size_str = ""
            if not is_dir:
                try:
                    size_str = format_size(entry.stat().st_size)
                except OSError:
                    size_str = "-"
            entries.append((entry.name, is_dir, size_str))
    except PermissionError:
        return "<h1>403 无权访问</h1>"

    entries.sort(key=lambda x: (not x[1], x[0].lower()))

    rel = target.relative_to(root)
    rel_parts = rel.parts if str(rel) != "." else ()

    q_suffix = f"?q={quote(back_q)}" if back_q else ""

    # 面包屑
    crumbs = []
    acc = []
    for part in rel_parts:
        acc.append(part)
        crumbs.append(
            f'/ <a href="/browse/{quote("/".join(acc))}{q_suffix}">{html.escape(part)}</a>'
        )
    crumbs_html = " ".join(crumbs)

    # 上级
    if len(rel_parts) > 1:
        parent_url = "/browse/" + quote("/".join(rel_parts[:-1])) + q_suffix
        parent_link = f'<a class="back" href="{parent_url}">⬆ 上一级</a>'
    elif len(rel_parts) == 1:
        # 返回首页时保留搜索
        parent_link = f'<a class="back" href="/{q_suffix}">⬆ 上一级</a>'
    else:
        parent_link = ""

    rows = []
    rel_str = rel.as_posix() if str(rel) != "." else ""
    for name, is_dir, size_str in entries:
        rel_child = (rel_str + "/" + name) if rel_str else name
        url = quote(rel_child)
        if is_dir:
            rows.append(
                f'<li data-fs-key="{html.escape(rel_child)}"><span class="icon">📁</span>'
                f'<a href="/browse/{url}{q_suffix}">{html.escape(name)}</a></li>'
            )
        else:
            rows.append(
                f'<li data-fs-key="{html.escape(rel_child)}"><span class="icon">📄</span>'
                f'<a href="/file/{url}" target="_blank">{html.escape(name)}</a>'
                f'<span class="size">{size_str}</span></li>'
            )
    items_html = (
        '<ul class="items">' + "".join(rows) + "</ul>"
        if rows
        else ('<div class="empty">没有匹配的项</div>' if sq else '<div class="empty">此文件夹为空</div>')
    )

    current = target.name or str(root)
    back_home = f'<a class="back" href="/{q_suffix}">← 返回搜索</a>'

    # 搜索框：GET 到当前 URL，用 sq 过滤当前目录；同时保留 q（返回时的外层搜索）
    action_path = "/browse/" + quote(rel_str) if rel_str else "/"
    q_hidden = f'<input type="hidden" name="q" value="{html.escape(back_q)}">' if (rel_str and back_q) else ""
    if not rel_str and back_q:
        # 首页搜索时用 q（这里其实用不到这个分支）
        q_hidden = ""

    if sq:
        cnt_info = f'关键字 "<b>{html.escape(sq)}</b>" 匹配到 {len(entries)} 项'
    else:
        cnt_info = f"共 {len(entries)} 项"

    search_box = f"""
    <form class="search-box" method="get" action="{action_path}">
        {q_hidden}
        <input type="text" name="sq" value="{html.escape(sq)}" placeholder="在当前目录中搜索..." autofocus>
        <button type="submit">搜索</button>
    </form>
    <div class="info">{cnt_info}</div>
    """

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>{html.escape(current)}</title>
<style>{BASE_CSS}</style></head><body>
{back_home}
<div class="breadcrumb"><a href="/{q_suffix}">🏠 根目录</a> {crumbs_html}</div>
<h1>📂 {html.escape(current)}</h1>
<div class="layout">
    <div class="main">
        {render_msg(msg, msg_kind)}
        {parent_link}
        {search_box}
        {items_html}
    </div>
    {render_sidebar(rel_str, back_q=back_q)}
</div>
{SCROLL_JS}
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 每请求的根目录 ----------
    def _parse_cookie(self):
        raw = self.headers.get("Cookie", "") or ""
        result = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = unquote(v.strip())
        return result

    def _apply_root_from_cookie(self):
        cookies = self._parse_cookie()
        path_str = cookies.get("fs_root", "")
        if not path_str:
            _ctx.root = None
            return
        try:
            p = Path(path_str).resolve()
        except OSError:
            _ctx.root = None
            return
        if p.is_dir():
            _ctx.root = p
        else:
            _ctx.root = None

    def _set_root_cookie(self, path_str: str):
        # 设置 cookie；path_str 为空表示清除
        if path_str:
            v = quote(path_str)
            self.send_header("Set-Cookie", f"fs_root={v}; Path=/; Max-Age=31536000; SameSite=Lax")
        else:
            self.send_header("Set-Cookie", "fs_root=; Path=/; Max-Age=0; SameSite=Lax")

    def _send_html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, msg: str, status: int = 404):
        data = msg.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path):
        try:
            size = path.stat().st_size
            ctype, _ = mimetypes.guess_type(str(path))
            if not ctype:
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self._send_text("500 读取文件失败", 500)

    def do_GET(self):
        self._apply_root_from_cookie()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        q = (query.get("q", [""])[0] or "").strip()
        sq = (query.get("sq", [""])[0] or "").strip()
        msg = (query.get("msg", [""])[0] or "").strip()
        kind = (query.get("kind", [""])[0] or "").strip()

        # ---- 根目录管理 ----
        if path == "/roots":
            self._send_html(render_roots(msg, kind))
            return

        if path == "/roots/use":
            new_root = (query.get("path", [""])[0] or "").strip()
            try:
                p = Path(new_root).resolve()
            except OSError:
                self._redirect_with_cookie("/roots?msg=" + quote("路径无效") + "&kind=err", "")
                return
            if not p.is_dir():
                self._redirect_with_cookie("/roots?msg=" + quote(f"目录不存在: {new_root}") + "&kind=err", "")
                return
            add_root(str(p))
            clear_history()
            self._redirect_with_cookie("/?msg=" + quote(f"已切换到 {p}") + "&kind=ok", str(p))
            return

        if path == "/roots/remove":
            target = (query.get("path", [""])[0] or "").strip()
            if target:
                remove_root(target)
            self._redirect("/roots?msg=" + quote("已移除") + "&kind=ok")
            return

        if path == "/" or path == "":
            # 根目录也记入历史（用空串表示）
            push_history("")
            self._send_html(render_index(q, msg, kind))
            return

        if path == "/history/clear":
            clear_history()
            back = (query.get("back", ["/"])[0] or "/")
            if not back.startswith("/"):
                back = "/"
            self._redirect(back)
            return

        if path.startswith("/browse/") or path == "/browse":
            sub = path[len("/browse/"):] if path.startswith("/browse/") else ""
            target = safe_resolve(sub)
            if target is None or not target.exists():
                self._send_text("404 未找到", 404)
                return
            if target.is_file():
                self._send_file(target)
                return
            # 记录目录访问历史
            rel = target.relative_to(current_root()).as_posix()
            push_history("" if rel == "." else rel)
            self._send_html(render_browse(target, back_q=q, sq=sq, msg=msg, msg_kind=kind))
            return

        if path.startswith("/file/"):
            sub = path[len("/file/"):]
            target = safe_resolve(sub)
            if target is None or not target.is_file():
                self._send_text("404 未找到", 404)
                return
            self._send_file(target)
            return

        self._send_text("404 未找到", 404)

    def do_POST(self):
        self._apply_root_from_cookie()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = parse_qs(raw, keep_blank_values=True)

        if path == "/roots/add":
            new_root = (form.get("path", [""])[0] or "").strip()
            if not new_root:
                self._redirect("/roots?msg=" + quote("路径不能为空") + "&kind=err")
                return
            try:
                p = Path(new_root).expanduser().resolve()
            except OSError:
                self._redirect("/roots?msg=" + quote("路径无效") + "&kind=err")
                return
            if not p.is_dir():
                self._redirect("/roots?msg=" + quote(f"目录不存在: {new_root}") + "&kind=err")
                return
            add_root(str(p))
            clear_history()
            self._redirect_with_cookie("/?msg=" + quote(f"已切换到 {p}") + "&kind=ok", str(p))
            return

        if path == "/mkdir":
            parent_rel = (form.get("parent", [""])[0] or "").strip()
            name = (form.get("name", [""])[0] or "").strip()
            back_q = (form.get("q", [""])[0] or "").strip()

            parent = safe_resolve(parent_rel)
            if parent is None or not parent.is_dir():
                self._redirect_with_msg("/", "父目录不存在", "err", back_q)
                return

            ok, result = validate_folder_name(name)
            if not ok:
                self._redirect_to_parent(parent_rel, result, "err", back_q)
                return

            new_dir = parent / result
            try:
                new_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                self._redirect_to_parent(parent_rel, f'"{result}" 已存在', "err", back_q)
                return
            except OSError as e:
                self._redirect_to_parent(parent_rel, f"创建失败: {e}", "err", back_q)
                return

            self._redirect_to_parent(parent_rel, f'已创建 "{result}"', "ok", back_q)
            return

        self._send_text("404 未找到", 404)

    def _redirect_to_parent(self, parent_rel: str, msg: str, kind: str, back_q: str):
        parent_rel = parent_rel.strip("/\\")
        params = f"msg={quote(msg)}&kind={quote(kind)}"
        if parent_rel:
            url = f"/browse/{quote(parent_rel)}?{params}"
            if back_q:
                url += f"&q={quote(back_q)}"
        else:
            url = f"/?{params}"
            if back_q:
                url += f"&q={quote(back_q)}"
        self._redirect(url)

    def _redirect_with_msg(self, base: str, msg: str, kind: str, back_q: str = ""):
        params = f"msg={quote(msg)}&kind={quote(kind)}"
        sep = "&" if "?" in base else "?"
        url = base + sep + params
        if back_q:
            url += f"&q={quote(back_q)}"
        self._redirect(url)

    def _redirect(self, url: str):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_with_cookie(self, url: str, root_path: str):
        self.send_response(303)
        self.send_header("Location", url)
        self._set_root_cookie(root_path)
        self.send_header("Content-Length", "0")
        self.end_headers()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="folder_search",
        description="简单的文件夹搜索 Web 应用（仅使用 Python 标准库）",
        add_help=False,
    )
    p.add_argument("root", nargs="?", default=None,
                   help="要浏览的根目录，留空使用脚本所在目录；可用 '.' 表示当前工作目录")
    p.add_argument("-p", "--port", type=int, default=5000, help="端口，默认 5000")
    p.add_argument("-H", "--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    p.add_argument("--help", action="help", help="显示帮助")
    return p.parse_args(argv)


def resolve_root(arg):
    """根据 CLI 参数解析根目录。arg 可能是 None / '.' / 绝对路径 / 相对路径。"""
    if arg is None or arg == "":
        return Path(__file__).resolve().parent
    if arg in (".", "./", ".\\"):
        return Path.cwd().resolve()
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    return p


def main():
    global ROOT_DIR, HOST, PORT
    args = parse_args()

    root = resolve_root(args.root)
    if not root.exists():
        print(f"错误：目录不存在 -> {root}", file=sys.stderr)
        sys.exit(2)
    if not root.is_dir():
        print(f"错误：不是目录 -> {root}", file=sys.stderr)
        sys.exit(2)

    ROOT_DIR = root
    HOST = args.host
    PORT = args.port

    # 把启动根目录加入历史
    add_root(str(ROOT_DIR))

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"启动失败：{e}（可能是端口 {PORT} 被占用）", file=sys.stderr)
        sys.exit(1)

    print(f"根目录: {ROOT_DIR}")
    print(f"启动服务: http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
