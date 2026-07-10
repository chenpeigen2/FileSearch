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


# 保证同一时间只有一个文件选择对话框
_picker_lock = threading.Lock()


def pick_folder_dialog(initial_dir: str = "") -> tuple:
    """在子进程里调起系统文件夹选择器（tkinter）。返回 (ok, path_or_err)。

    放到子进程执行有两个原因：
    - tkinter 的 mainloop / Tk 实例必须在主线程，服务器工作线程里直接用会报错；
    - 对话框结束后进程退出，能干净地释放 Tk 资源。
    """
    import subprocess
    code = (
        "import sys\n"
        "try:\n"
        "    import tkinter as tk\n"
        "    from tkinter import filedialog\n"
        "except Exception as e:\n"
        "    sys.stderr.write('tkinter unavailable: ' + str(e))\n"
        "    sys.exit(2)\n"
        "init = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    root.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "p = filedialog.askdirectory(title='选择要浏览的文件夹', initialdir=init or None, mustexist=True)\n"
        "root.destroy()\n"
        "sys.stdout.write(p or '')\n"
    )
    if not _picker_lock.acquire(blocking=False):
        return False, "已有一个选择窗口打开，请先处理"
    try:
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, initial_dir or ""],
                capture_output=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return False, "选择对话框超时"
        except OSError as e:
            return False, f"无法启动选择器: {e}"
        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            return False, err or "选择器启动失败（可能未安装 tkinter）"
        picked = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        if not picked:
            return False, "已取消"
        return True, picked
    finally:
        _picker_lock.release()


def open_in_system(path: Path) -> tuple:
    """在服务器上用系统默认方式打开一个目录/文件。返回 (ok, err)。"""
    import subprocess
    import shutil
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True, ""
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return True, ""
        # Linux/BSD：优先 xdg-open，其次尝试各桌面自带的文件管理器
        for cmd in ("xdg-open", "gio", "nautilus", "dolphin", "thunar", "pcmanfm", "nemo"):
            exe = shutil.which(cmd)
            if not exe:
                continue
            if cmd == "gio":
                subprocess.Popen([exe, "open", str(path)])
            else:
                subprocess.Popen([exe, str(path)])
            return True, ""
        return False, "未找到 xdg-open 或可用的文件管理器"
    except OSError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ---------- IDE 检测 ----------
# 每一项：id -> (显示名, emoji, [候选命令/路径], [启动参数模板])
# 候选优先用 PATH 上的短名；找不到再看常见安装路径
def _ide_candidates():
    home = Path.home()
    localapp = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

    def win(*parts):
        return str(Path(*parts))

    ides = {
        "vscode": {
            "name": "VS Code", "emoji": "🟦",
            "cmds": ["code"],
            "win_paths": [
                win(localapp, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                win(localapp, "Programs", "Microsoft VS Code", "Code.exe"),
                win(program_files, "Microsoft VS Code", "bin", "code.cmd"),
                win(program_files, "Microsoft VS Code", "Code.exe"),
            ],
            "mac_paths": ["/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"],
            "linux_paths": ["/usr/bin/code", "/snap/bin/code", "/usr/local/bin/code"],
        },
        "vscode-insiders": {
            "name": "VS Code Insiders", "emoji": "🟩",
            "cmds": ["code-insiders"],
            "win_paths": [
                win(localapp, "Programs", "Microsoft VS Code Insiders", "bin", "code-insiders.cmd"),
            ],
            "mac_paths": ["/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders"],
            "linux_paths": ["/usr/bin/code-insiders"],
        },
        "cursor": {
            "name": "Cursor", "emoji": "⬛",
            "cmds": ["cursor"],
            "win_paths": [
                win(localapp, "Programs", "cursor", "resources", "app", "bin", "cursor.cmd"),
                win(localapp, "Programs", "cursor", "Cursor.exe"),
            ],
            "mac_paths": ["/Applications/Cursor.app/Contents/Resources/app/bin/cursor"],
            "linux_paths": ["/usr/bin/cursor"],
        },
        "trae": {
            "name": "Trae", "emoji": "🟪",
            "cmds": ["trae"],
            "win_paths": [
                win(localapp, "Programs", "Trae", "bin", "trae.cmd"),
                win(localapp, "Programs", "Trae", "Trae.exe"),
            ],
            "mac_paths": ["/Applications/Trae.app/Contents/Resources/app/bin/trae"],
            "linux_paths": [],
        },
        "sublime": {
            "name": "Sublime Text", "emoji": "🟧",
            "cmds": ["subl", "sublime_text"],
            "win_paths": [
                win(program_files, "Sublime Text", "subl.exe"),
                win(program_files, "Sublime Text 3", "subl.exe"),
                win(program_files, "Sublime Text", "sublime_text.exe"),
                win(program_files, "Sublime Text 3", "sublime_text.exe"),
                win(program_files_x86, "Sublime Text", "sublime_text.exe"),
                win(program_files_x86, "Sublime Text 3", "sublime_text.exe"),
                win(localapp, "Programs", "Sublime Text", "sublime_text.exe"),
            ],
            "mac_paths": ["/Applications/Sublime Text.app/Contents/SharedSupport/bin/subl"],
            "linux_paths": [
                "/usr/bin/subl", "/snap/bin/subl", "/snap/bin/sublime-text",
                "/opt/sublime_text/sublime_text", "/usr/bin/sublime_text",
            ],
        },
        "idea": {
            "name": "IntelliJ IDEA", "emoji": "🟥",
            "cmds": ["idea", "idea64"],
            "win_paths": [],  # 由 glob 补充
            "mac_paths": ["/Applications/IntelliJ IDEA.app/Contents/MacOS/idea",
                          "/Applications/IntelliJ IDEA CE.app/Contents/MacOS/idea"],
            "linux_paths": ["/usr/bin/idea", "/snap/bin/intellij-idea-community",
                            "/snap/bin/intellij-idea-ultimate", "/opt/idea/bin/idea.sh"],
        },
        "pycharm": {
            "name": "PyCharm", "emoji": "🟨",
            "cmds": ["pycharm", "pycharm64"],
            "win_paths": [],
            "mac_paths": ["/Applications/PyCharm.app/Contents/MacOS/pycharm",
                          "/Applications/PyCharm CE.app/Contents/MacOS/pycharm"],
            "linux_paths": ["/usr/bin/pycharm", "/snap/bin/pycharm-community",
                            "/snap/bin/pycharm-professional", "/opt/pycharm/bin/pycharm.sh"],
        },
        "webstorm": {
            "name": "WebStorm", "emoji": "🟫",
            "cmds": ["webstorm", "webstorm64"],
            "win_paths": [],
            "mac_paths": ["/Applications/WebStorm.app/Contents/MacOS/webstorm"],
            "linux_paths": ["/usr/bin/webstorm", "/snap/bin/webstorm",
                            "/opt/webstorm/bin/webstorm.sh"],
        },
        "goland": {
            "name": "GoLand", "emoji": "🟩",
            "cmds": ["goland", "goland64"],
            "win_paths": [],
            "mac_paths": ["/Applications/GoLand.app/Contents/MacOS/goland"],
            "linux_paths": ["/usr/bin/goland", "/snap/bin/goland",
                            "/opt/goland/bin/goland.sh"],
        },
        "clion": {
            "name": "CLion", "emoji": "🟦",
            "cmds": ["clion", "clion64"],
            "win_paths": [],
            "mac_paths": ["/Applications/CLion.app/Contents/MacOS/clion"],
            "linux_paths": ["/usr/bin/clion", "/snap/bin/clion",
                            "/opt/clion/bin/clion.sh"],
        },
        "rider": {
            "name": "Rider", "emoji": "🟪",
            "cmds": ["rider", "rider64"],
            "win_paths": [],
            "mac_paths": ["/Applications/Rider.app/Contents/MacOS/rider"],
            "linux_paths": ["/usr/bin/rider", "/snap/bin/rider",
                            "/opt/rider/bin/rider.sh"],
        },
        "phpstorm": {
            "name": "PhpStorm", "emoji": "🟪",
            "cmds": ["phpstorm", "phpstorm64"],
            "win_paths": [],
            "mac_paths": ["/Applications/PhpStorm.app/Contents/MacOS/phpstorm"],
            "linux_paths": ["/usr/bin/phpstorm", "/snap/bin/phpstorm",
                            "/opt/phpstorm/bin/phpstorm.sh"],
        },
        "rubymine": {
            "name": "RubyMine", "emoji": "🟥",
            "cmds": ["rubymine", "rubymine64"],
            "win_paths": [],
            "mac_paths": ["/Applications/RubyMine.app/Contents/MacOS/rubymine"],
            "linux_paths": ["/usr/bin/rubymine", "/snap/bin/rubymine",
                            "/opt/rubymine/bin/rubymine.sh"],
        },
        "rustrover": {
            "name": "RustRover", "emoji": "🟧",
            "cmds": ["rustrover", "rustrover64"],
            "win_paths": [],
            "mac_paths": ["/Applications/RustRover.app/Contents/MacOS/rustrover"],
            "linux_paths": ["/usr/bin/rustrover", "/snap/bin/rustrover",
                            "/opt/rustrover/bin/rustrover.sh"],
        },
        "datagrip": {
            "name": "DataGrip", "emoji": "🟩",
            "cmds": ["datagrip", "datagrip64"],
            "win_paths": [],
            "mac_paths": ["/Applications/DataGrip.app/Contents/MacOS/datagrip"],
            "linux_paths": ["/usr/bin/datagrip", "/snap/bin/datagrip",
                            "/opt/datagrip/bin/datagrip.sh"],
        },
        "android-studio": {
            "name": "Android Studio", "emoji": "🟢",
            "cmds": ["studio", "studio64"],
            "win_paths": [
                win(program_files, "Android", "Android Studio", "bin", "studio64.exe"),
                win(program_files, "Android", "Android Studio", "bin", "studio.exe"),
                win(program_files_x86, "Android", "Android Studio", "bin", "studio64.exe"),
                win(localapp, "Programs", "Android Studio", "bin", "studio64.exe"),
            ],
            "mac_paths": ["/Applications/Android Studio.app/Contents/MacOS/studio"],
            "linux_paths": [
                "/opt/android-studio/bin/studio.sh",
                "/usr/local/android-studio/bin/studio.sh",
                "/snap/bin/android-studio",
                "/usr/bin/android-studio",
            ],
        },
        "zed": {
            "name": "Zed", "emoji": "⚡",
            "cmds": ["zed"],
            "win_paths": [
                win(localapp, "Programs", "Zed", "Zed.exe"),
            ],
            "mac_paths": ["/Applications/Zed.app/Contents/MacOS/cli"],
            "linux_paths": ["/usr/bin/zed", "/opt/zed/bin/zed"],
        },
        "fleet": {
            "name": "Fleet", "emoji": "🚀",
            "cmds": ["fleet"],
            "win_paths": [],
            "mac_paths": ["/Applications/Fleet.app/Contents/MacOS/Fleet"],
            "linux_paths": [],
        },
        "windsurf": {
            "name": "Windsurf", "emoji": "🌊",
            "cmds": ["windsurf"],
            "win_paths": [
                win(localapp, "Programs", "Windsurf", "bin", "windsurf.cmd"),
                win(localapp, "Programs", "Windsurf", "Windsurf.exe"),
            ],
            "mac_paths": ["/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf"],
            "linux_paths": ["/usr/bin/windsurf", "/opt/Windsurf/windsurf"],
        },
        "notepad++": {
            "name": "Notepad++", "emoji": "📝",
            "cmds": ["notepad++"],
            "win_paths": [
                win(program_files, "Notepad++", "notepad++.exe"),
                win(program_files_x86, "Notepad++", "notepad++.exe"),
                win(localapp, "Programs", "Notepad++", "notepad++.exe"),
            ],
            "mac_paths": [],
            "linux_paths": [],
        },
        "notepad3": {
            "name": "Notepad3", "emoji": "🗒️",
            "cmds": ["notepad3"],
            "win_paths": [
                win(program_files, "Notepad3", "Notepad3.exe"),
                win(program_files_x86, "Notepad3", "Notepad3.exe"),
            ],
            "mac_paths": [],
            "linux_paths": [],
        },
        "ultraedit": {
            "name": "UltraEdit", "emoji": "✏️",
            "cmds": ["uedit64", "uedit32"],
            "win_paths": [
                win(program_files, "IDM Computer Solutions", "UltraEdit", "uedit64.exe"),
                win(program_files_x86, "IDM Computer Solutions", "UltraEdit", "uedit32.exe"),
            ],
            "mac_paths": ["/Applications/UltraEdit.app/Contents/MacOS/UltraEdit"],
            "linux_paths": ["/usr/bin/uex", "/opt/uex/uex"],
        },
        "emeditor": {
            "name": "EmEditor", "emoji": "🟠",
            "cmds": ["EmEditor"],
            "win_paths": [
                win(program_files, "EmEditor", "EmEditor.exe"),
                win(program_files_x86, "EmEditor", "EmEditor.exe"),
            ],
            "mac_paths": [],
            "linux_paths": [],
        },
        "editplus": {
            "name": "EditPlus", "emoji": "🟡",
            "cmds": ["editplus"],
            "win_paths": [
                win(program_files, "EditPlus", "editplus.exe"),
                win(program_files_x86, "EditPlus", "editplus.exe"),
            ],
            "mac_paths": [],
            "linux_paths": [],
        },
        "vs": {
            "name": "Visual Studio", "emoji": "🟣",
            "cmds": ["devenv"],
            "win_paths": [
                # VS 2022 / 2019 / 2017 常见位置（Community / Professional / Enterprise）
                win(program_files, "Microsoft Visual Studio", "2022", "Community", "Common7", "IDE", "devenv.exe"),
                win(program_files, "Microsoft Visual Studio", "2022", "Professional", "Common7", "IDE", "devenv.exe"),
                win(program_files, "Microsoft Visual Studio", "2022", "Enterprise", "Common7", "IDE", "devenv.exe"),
                win(program_files_x86, "Microsoft Visual Studio", "2019", "Community", "Common7", "IDE", "devenv.exe"),
                win(program_files_x86, "Microsoft Visual Studio", "2019", "Professional", "Common7", "IDE", "devenv.exe"),
                win(program_files_x86, "Microsoft Visual Studio", "2019", "Enterprise", "Common7", "IDE", "devenv.exe"),
                win(program_files_x86, "Microsoft Visual Studio", "2017", "Community", "Common7", "IDE", "devenv.exe"),
            ],
            "mac_paths": [],  # VS for Mac 已停止维护
            "linux_paths": [],
        },
        "xcode": {
            "name": "Xcode", "emoji": "🔨",
            "cmds": [],
            "win_paths": [],
            "mac_paths": ["/Applications/Xcode.app/Contents/MacOS/Xcode"],
            "linux_paths": [],
        },
        "textmate": {
            "name": "TextMate", "emoji": "📘",
            "cmds": ["mate"],
            "win_paths": [],
            "mac_paths": ["/Applications/TextMate.app/Contents/MacOS/TextMate"],
            "linux_paths": [],
        },
        "bbedit": {
            "name": "BBEdit", "emoji": "🔷",
            "cmds": ["bbedit"],
            "win_paths": [],
            "mac_paths": ["/Applications/BBEdit.app/Contents/MacOS/BBEdit"],
            "linux_paths": [],
        },
        "nova": {
            "name": "Nova", "emoji": "✨",
            "cmds": ["nova"],
            "win_paths": [],
            "mac_paths": ["/Applications/Nova.app/Contents/MacOS/Nova"],
            "linux_paths": [],
        },
        "eclipse": {
            "name": "Eclipse", "emoji": "🌑",
            "cmds": ["eclipse"],
            "win_paths": [
                win(program_files, "Eclipse Foundation", "eclipse.exe"),
                win(program_files, "eclipse", "eclipse.exe"),
                win(program_files_x86, "eclipse", "eclipse.exe"),
            ],
            "mac_paths": ["/Applications/Eclipse.app/Contents/MacOS/eclipse"],
            "linux_paths": ["/usr/bin/eclipse", "/opt/eclipse/eclipse"],
        },
        "netbeans": {
            "name": "NetBeans", "emoji": "🔵",
            "cmds": ["netbeans"],
            "win_paths": [
                win(program_files, "NetBeans", "bin", "netbeans64.exe"),
            ],
            "mac_paths": ["/Applications/NetBeans.app/Contents/MacOS/netbeans"],
            "linux_paths": ["/usr/bin/netbeans", "/opt/netbeans/bin/netbeans"],
        },
        "qtcreator": {
            "name": "Qt Creator", "emoji": "🟢",
            "cmds": ["qtcreator"],
            "win_paths": [
                win(program_files, "Qt", "Tools", "QtCreator", "bin", "qtcreator.exe"),
            ],
            "mac_paths": ["/Applications/Qt Creator.app/Contents/MacOS/Qt Creator"],
            "linux_paths": ["/usr/bin/qtcreator", "/opt/qtcreator/bin/qtcreator"],
        },
        "kate": {
            "name": "Kate", "emoji": "🐱",
            "cmds": ["kate"],
            "win_paths": [
                win(program_files, "Kate", "bin", "kate.exe"),
            ],
            "mac_paths": ["/Applications/kate.app/Contents/MacOS/kate"],
            "linux_paths": ["/usr/bin/kate", "/snap/bin/kate"],
        },
        "gedit": {
            "name": "gedit", "emoji": "📄",
            "cmds": ["gedit"],
            "win_paths": [],
            "mac_paths": [],
            "linux_paths": ["/usr/bin/gedit", "/snap/bin/gedit"],
        },
        "geany": {
            "name": "Geany", "emoji": "🟡",
            "cmds": ["geany"],
            "win_paths": [
                win(program_files, "Geany", "bin", "geany.exe"),
                win(program_files_x86, "Geany", "bin", "geany.exe"),
            ],
            "mac_paths": [],
            "linux_paths": ["/usr/bin/geany", "/snap/bin/geany"],
        },
    }

    return ides


def _ide_extend_slow(ides):
    """慢扫描：JetBrains Toolbox + 通用 glob 兜底。在传入的 ides 上原地扩充路径。"""
    home = Path.home()
    localapp = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

    # JetBrains 系产品
    # Android Studio 不属于 JetBrains Toolbox，由通用扫描处理
    jb_map_win = {
        "idea": ["idea64.exe", "idea.exe"],
        "pycharm": ["pycharm64.exe", "pycharm.exe"],
        "webstorm": ["webstorm64.exe", "webstorm.exe"],
        "goland": ["goland64.exe", "goland.exe"],
        "clion": ["clion64.exe", "clion.exe"],
        "rider": ["rider64.exe", "rider.exe"],
        "phpstorm": ["phpstorm64.exe", "phpstorm.exe"],
        "rubymine": ["rubymine64.exe", "rubymine.exe"],
        "rustrover": ["rustrover64.exe", "rustrover.exe"],
        "datagrip": ["datagrip64.exe", "datagrip.exe"],
    }
    jb_map_mac = {
        "idea": ["idea"], "pycharm": ["pycharm"], "webstorm": ["webstorm"],
        "goland": ["goland"], "clion": ["clion"], "rider": ["rider"],
        "phpstorm": ["phpstorm"], "rubymine": ["rubymine"],
        "rustrover": ["rustrover"], "datagrip": ["datagrip"],
    }
    jb_map_nix = {
        "idea": ["idea.sh"], "pycharm": ["pycharm.sh"], "webstorm": ["webstorm.sh"],
        "goland": ["goland.sh"], "clion": ["clion.sh"], "rider": ["rider.sh"],
        "phpstorm": ["phpstorm.sh"], "rubymine": ["rubymine.sh"],
        "rustrover": ["rustrover.sh"], "datagrip": ["datagrip.sh"],
    }

    if sys.platform.startswith("win"):
        jb_roots = [
            Path(program_files) / "JetBrains",
            Path(program_files_x86) / "JetBrains",
            home / "AppData" / "Local" / "JetBrains" / "Toolbox" / "apps",
        ]
        # 非系统盘上的 JetBrains 目录
        for extra in _win_extra_install_roots():
            jb_roots.append(extra / "JetBrains")
        # Toolbox 结构较深（apps/<Product>/ch-0/<version>/bin/xxx.exe），需要 6 层
        _fill_from_glob(ides, jb_roots, jb_map_win, "win_paths", max_depth=6)
    elif sys.platform == "darwin":
        jb_roots = [
            home / "Applications" / "JetBrains Toolbox",
            Path("/Applications/JetBrains Toolbox"),
            home / "Library" / "Application Support" / "JetBrains" / "Toolbox" / "apps",
        ]
        _fill_from_glob(ides, jb_roots, jb_map_mac, "mac_paths", max_depth=6)
    else:
        jb_roots = [
            home / ".local" / "share" / "JetBrains" / "Toolbox" / "apps",
            Path("/opt/JetBrains"),
        ]
        _fill_from_glob(ides, jb_roots, jb_map_nix, "linux_paths", max_depth=6)

    # 通用 glob 兜底：按 exe 名扫描常见根目录
    generic_win = {
        "notepad++": ["notepad++.exe"],
        "sublime": ["sublime_text.exe"],
        "android-studio": ["studio64.exe", "studio.exe"],
        "vscode": ["Code.exe"],
        "cursor": ["Cursor.exe"],
        "windsurf": ["Windsurf.exe"],
        "trae": ["Trae.exe"],
        "zed": ["Zed.exe"],
        "notepad3": ["Notepad3.exe"],
        "ultraedit": ["uedit64.exe", "uedit32.exe"],
        "emeditor": ["EmEditor.exe"],
        "editplus": ["editplus.exe"],
        "geany": ["geany.exe"],
        "eclipse": ["eclipse.exe"],
        "qtcreator": ["qtcreator.exe"],
        "netbeans": ["netbeans64.exe"],
    }
    if sys.platform.startswith("win"):
        scoop = home / "scoop" / "apps"
        choco = Path("C:\\ProgramData\\chocolatey\\lib")
        gen_roots = [
            Path(program_files),
            Path(program_files_x86),
            Path(localapp) / "Programs" if localapp else None,
            scoop,
            choco,
        ]
        # 兜底：枚举所有非系统盘的常见安装目录
        gen_roots.extend(_win_extra_install_roots())
        gen_roots = [r for r in gen_roots if r]
        # 通用安装目录结构较浅：<root>/<vendor>/<app>/bin/<exe> 4 层足够
        _fill_from_glob(ides, gen_roots, generic_win, "win_paths", max_depth=4)
    elif sys.platform == "darwin":
        mac_bundle_map = {
            "sublime": ["Sublime Text"],
            "vscode": ["Visual Studio Code"],
            "cursor": ["Cursor"],
            "windsurf": ["Windsurf"],
            "trae": ["Trae"],
            "zed": ["Zed"],
            "xcode": ["Xcode"],
            "textmate": ["TextMate"],
            "bbedit": ["BBEdit"],
            "nova": ["Nova"],
            "eclipse": ["Eclipse"],
            "netbeans": ["NetBeans"],
            "qtcreator": ["Qt Creator"],
            "kate": ["kate"],
            "fleet": ["Fleet"],
        }
        _fill_from_apps(ides, [Path("/Applications"), home / "Applications"], mac_bundle_map)
    else:
        gen_linux = {
            "vscode": ["code"],
            "vscode-insiders": ["code-insiders"],
            "cursor": ["cursor"],
            "sublime": ["sublime_text", "subl"],
            "notepad++": ["notepad-plus-plus"],
            "eclipse": ["eclipse"],
            "netbeans": ["netbeans"],
            "qtcreator": ["qtcreator"],
            "geany": ["geany"],
            "gedit": ["gedit"],
            "kate": ["kate"],
            "zed": ["zed"],
        }
        _fill_from_glob(
            ides,
            [Path("/opt"), Path("/usr/local"),
             Path("/var/lib/flatpak/exports/bin"),
             home / ".local" / "share" / "flatpak" / "exports" / "bin"],
            gen_linux, "linux_paths", max_depth=4,
        )
    return ides


def _win_extra_install_roots():
    """Windows：列出所有可用盘符下的常见安装目录（非系统盘）。

    比如返回 [Path("D:\\Program Files"), Path("D:\\Program Files (x86)"),
             Path("D:\\Programs"), Path("D:\\AS"), Path("E:\\Program Files"), ...]
    只返回实际存在的目录。
    """
    if not sys.platform.startswith("win"):
        return []
    import string
    common_subdirs = [
        "Program Files", "Program Files (x86)",
        "Programs", "Apps", "Applications",
        "AS", "Tools", "SDK", "Dev",
    ]
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").rstrip("/")
    extras = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        if f"{letter}:" .lower() == system_drive.lower():
            continue  # 系统盘的这些目录已经由 ProgramFiles/ProgramFiles(x86) 覆盖
        for sub in common_subdirs:
            p = Path(drive) / sub
            if p.is_dir():
                extras.append(p)
    return extras


def _fill_from_apps(ides, roots, bundle_map):
    """macOS：在 /Applications 之类目录下，按 .app 名匹配。写入 mac_paths。"""
    for ide_id, names in bundle_map.items():
        for root in roots:
            if not root.is_dir():
                continue
            for name in names:
                app_dir = root / f"{name}.app"
                if app_dir.is_dir():
                    # 优先找 Contents/Resources/app/bin/<cli>，其次 Contents/MacOS/<binary>
                    macos_dir = app_dir / "Contents" / "MacOS"
                    picked = None
                    if macos_dir.is_dir():
                        try:
                            for p in macos_dir.iterdir():
                                if p.is_file() and os.access(p, os.X_OK):
                                    picked = p
                                    break
                        except OSError:
                            pass
                    if picked is not None:
                        ides[ide_id]["mac_paths"].append(str(picked))
                    break


def _fill_from_glob(ides, roots, exe_map, target_key, max_depth=None):
    """在 roots 下按文件名匹配可执行文件，写入 ides[id][target_key]。

    优化：一次扫描一个 root，把 exe_map 里所有目标文件名合并成一个大 name set，
    只走一遍目录树；命中后按名字反查回 ide_id。相比之前"每个 IDE 独立跑一次
    整棵树"快数倍。
    """
    # 反向索引：exe 文件名 -> [ide_id, ...]
    name_to_ids = {}
    for ide_id, exe_names in exe_map.items():
        target = ides.get(ide_id, {}).get(target_key)
        if target is None:
            continue
        for name in exe_names:
            name_to_ids.setdefault(name, []).append(ide_id)
    if not name_to_ids:
        return ides

    # 每个 root 只扫一次，收集所有能匹配到的可执行文件
    for root in roots:
        if not root.is_dir():
            continue
        hits = _find_files_by_names(root, name_to_ids, max_depth)
        for fname, path in hits.items():
            for ide_id in name_to_ids.get(fname, []):
                ides[ide_id][target_key].append(path)
    return ides


def _find_files_by_names(root: Path, name_map, max_depth):
    """在 root 下 BFS 匹配 name_map 里的文件名，每个 name 只取第一个命中。

    返回 {filename: absolute_path}。最坏情况扫到 max_depth 层为止。
    """
    remaining = set(name_map.keys())
    found = {}
    try:
        stack = [(root, 0)]
        while stack and remaining:
            cur, depth = stack.pop()
            try:
                for entry in os.scandir(cur):
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if entry.name in remaining:
                                found[entry.name] = entry.path
                                remaining.discard(entry.name)
                                if not remaining:
                                    return found
                        elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
            except (OSError, PermissionError):
                continue
    except OSError:
        pass
    return found


def _find_file_by_names(root: Path, names: set, max_depth):
    """在 root 下查找文件名在 names 集合里的第一个文件；max_depth=None 用 rglob，否则 BFS 限深。"""
    try:
        if max_depth is None:
            for name in names:
                for hit in root.rglob(name):
                    if hit.is_file():
                        return str(hit)
            return None
        # BFS
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                for entry in os.scandir(cur):
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.name in names:
                            return entry.path
                        if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
            except (OSError, PermissionError):
                continue
        return None
    except OSError:
        return None


_ide_cache = None                # 上次快扫描 + 慢扫描（若已完成）合并后的结果
_ide_cache_lock = threading.Lock()
_ide_scan_state = {
    "scanning": False,           # 后台是否正在跑慢扫描
    "generation": 0,             # 每次慢扫描完成 +1，前端据此判断"有没有新结果"
    "started_at": 0.0,
    "finished_at": 0.0,
}
_ide_scan_state_lock = threading.Lock()

# 持久化文件（跟 .fs_roots.json 放一起）
IDES_FILE = Path(__file__).resolve().parent / ".fs_ides.json"


def load_ides_from_disk():
    """从磁盘读上次扫描结果。只保留可执行文件仍然存在的项。返回 {id: {name, emoji, exe}}。"""
    try:
        with open(IDES_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    valid = {}
    for ide_id, info in data.items():
        if not isinstance(info, dict):
            continue
        exe = info.get("exe")
        if isinstance(exe, str) and exe and os.path.isfile(exe):
            valid[ide_id] = {
                "name": info.get("name", ide_id),
                "emoji": info.get("emoji", ""),
                "exe": exe,
            }
    return valid


def save_ides_to_disk(ides):
    """把当前 ides 结果写入磁盘。失败静默忽略。"""
    try:
        with open(IDES_FILE, "w", encoding="utf-8") as fp:
            json.dump(ides, fp, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _run_detect(candidates):
    """给定候选表，扫描并返回 {id: {name, emoji, exe}}。纯计算，无副作用。"""
    import shutil
    found = {}
    for ide_id, info in candidates.items():
        exe = None
        for cmd in info.get("cmds", []):
            p = shutil.which(cmd)
            if p:
                exe = p
                break
        if not exe:
            if sys.platform.startswith("win"):
                paths = info.get("win_paths", [])
            elif sys.platform == "darwin":
                paths = info.get("mac_paths", [])
            else:
                paths = info.get("linux_paths", [])
            for p in paths:
                if p and os.path.isfile(p):
                    exe = p
                    break
        if exe:
            found[ide_id] = {"name": info["name"], "emoji": info["emoji"], "exe": exe}
    return found


def _slow_scan_worker():
    """后台线程：跑慢扫描并合并进 _ide_cache。"""
    import traceback
    global _ide_cache
    try:
        cands = _ide_candidates()
        _ide_extend_slow(cands)
        found = _run_detect(cands)
        with _ide_cache_lock:
            if _ide_cache is None:
                _ide_cache = found
            else:
                merged = dict(_ide_cache)
                merged.update(found)
                _ide_cache = merged
            snapshot = dict(_ide_cache)
        save_ides_to_disk(snapshot)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        with _ide_scan_state_lock:
            _ide_scan_state["scanning"] = False
            _ide_scan_state["finished_at"] = _now()
            _ide_scan_state["generation"] += 1


def _now():
    import time
    return time.time()


def _start_slow_scan_if_idle():
    """若后台扫描没有在跑，就启动一个。返回是否新启动了。"""
    with _ide_scan_state_lock:
        if _ide_scan_state["scanning"]:
            return False
        _ide_scan_state["scanning"] = True
        _ide_scan_state["started_at"] = _now()
    threading.Thread(target=_slow_scan_worker, daemon=True).start()
    return True


def detect_ides():
    """返回当前已知的 IDE 集合（磁盘缓存 + 快扫描 + 已完成的慢扫描结果）。

    首次调用时：
      1. 从 .fs_ides.json 读上次的结果（校验可执行文件仍存在）作为初始
      2. 跑一次快扫描并与磁盘缓存合并
      3. 在后台线程启动慢扫描（不阻塞），完成后再落盘
    """
    global _ide_cache
    with _ide_cache_lock:
        if _ide_cache is not None:
            return dict(_ide_cache)

    # 首次：磁盘 + 快扫描
    from_disk = load_ides_from_disk()
    cands = _ide_candidates()
    quick = _run_detect(cands)
    merged = dict(from_disk)
    merged.update(quick)  # 快扫描的路径覆盖磁盘（PATH 可能已变）

    with _ide_cache_lock:
        if _ide_cache is None:
            _ide_cache = merged
        snapshot = dict(_ide_cache)

    # 首次读到过磁盘缓存的话，落盘一份（合并了快扫描的结果）
    if from_disk or quick:
        save_ides_to_disk(snapshot)

    # 启动后台慢扫描
    _start_slow_scan_if_idle()
    return snapshot


def get_scan_state():
    with _ide_scan_state_lock:
        return dict(_ide_scan_state)


def rescan_ides():
    """强制重扫：清内存和磁盘缓存，重跑快扫描 + 后台慢扫描。返回快扫描结果。"""
    global _ide_cache
    with _ide_cache_lock:
        _ide_cache = None
    try:
        if IDES_FILE.exists():
            IDES_FILE.unlink()
    except OSError:
        pass
    return detect_ides()


def open_in_ide(ide_id: str, target: Path) -> tuple:
    """用检测到的 IDE 打开目录。返回 (ok, err)。"""
    import subprocess
    ides = detect_ides()
    info = ides.get(ide_id)
    if not info:
        return False, "未检测到该 IDE"
    exe = info["exe"]
    try:
        # macOS：如果指向的是 .app 里的原生二进制，用 `open -a <AppName> <path>` 更稳
        if sys.platform == "darwin" and ".app/Contents/MacOS/" in exe.replace("\\", "/"):
            # 从路径里提取 <AppName>
            marker = "/Applications/"
            app_path = exe
            i = app_path.find(".app/")
            if i > 0:
                # 找到 .app 之前的完整 app 路径
                app_bundle = app_path[: i + len(".app")]
                subprocess.Popen(["open", "-a", app_bundle, str(target)])
                return True, ""
        subprocess.Popen([exe, str(target)])
        return True, ""
    except OSError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


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

    // ---- 修正 autofocus 输入框：光标移到末尾 ----
    function caretToEnd() {
        var el = document.querySelector('input[autofocus]');
        if (!el || typeof el.value !== 'string' || !el.value) return;
        try {
            var n = el.value.length;
            // 有的浏览器需要先 focus 才能设置 selection
            el.focus();
            el.setSelectionRange(n, n);
        } catch (e) {}
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', caretToEnd);
    } else {
        caretToEnd();
    }

    // ---- toast ----
    function toast(msg, kind) {
        var el = document.createElement('div');
        el.textContent = msg;
        el.style.cssText =
            'position:fixed;left:50%;top:20px;transform:translateX(-50%);' +
            'padding:10px 18px;border-radius:8px;font-size:14px;z-index:9999;' +
            'box-shadow:0 4px 12px rgba(0,0,0,.15);opacity:0;transition:opacity .2s;' +
            (kind === 'err'
                ? 'background:#fdecea;color:#a12525;border:1px solid #f2b8b3;'
                : 'background:#e5f6ea;color:#1d7a3c;border:1px solid #a7d8b6;');
        document.body.appendChild(el);
        requestAnimationFrame(function () { el.style.opacity = '1'; });
        setTimeout(function () {
            el.style.opacity = '0';
            setTimeout(function () { el.remove(); }, 250);
        }, 2200);
    }

    // ---- AJAX 表单：不刷新页面，不动历史 ----
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.getAttribute('data-ajax') !== '1') return;
        e.preventDefault();
        var btn = form.querySelector('button[type=submit]');
        if (btn) btn.disabled = true;
        var fd = new FormData(form);
        fetch(form.action, {
            method: form.method || 'POST',
            body: new URLSearchParams(fd),
            headers: { 'X-Requested-With': 'fetch' },
        }).then(function (r) {
            return r.json().catch(function () { return { ok: r.ok, msg: r.statusText }; });
        }).then(function (data) {
            toast(data.msg || (data.ok ? '完成' : '失败'), data.ok ? 'ok' : 'err');
        }).catch(function (err) {
            toast('请求失败: ' + err, 'err');
        }).finally(function () {
            if (btn) btn.disabled = false;
        });
    });

    // ---- IDE 扫描状态轮询：generation 变化就重绘按钮列表 ----
    (function pollIdes() {
        var container = document.getElementById('fs-ide-list');
        if (!container) return;
        var lastGen = -1;
        var stopAt = Date.now() + 5 * 60 * 1000; // 最多轮询 5 分钟
        function render(list) {
            var rel = container.getAttribute('data-current-rel') || '';
            if (!list || !list.length) {
                container.innerHTML = '<div class="history-empty" style="margin-top:6px;">未检测到 IDE</div>';
                return;
            }
            var parts = [];
            list.forEach(function (ide) {
                var f = document.createElement('form');
                f.method = 'post';
                f.action = '/open-ide';
                f.setAttribute('data-ajax', '1');
                f.style.marginTop = '6px';
                f.innerHTML =
                    '<input type="hidden" name="rel">' +
                    '<input type="hidden" name="ide">' +
                    '<button type="submit" ' +
                    'style="background:#1d1d1f;padding:6px 10px;font-size:13px;width:100%;"></button>';
                f.querySelector('input[name=rel]').value = rel;
                f.querySelector('input[name=ide]').value = ide.id;
                var btn = f.querySelector('button');
                btn.title = ide.exe;
                btn.textContent = (ide.emoji || '') + ' 在 ' + ide.name + ' 打开';
                parts.push(f);
            });
            container.innerHTML = '';
            parts.forEach(function (n) { container.appendChild(n); });
        }
        function tick() {
            fetch('/ides/status', { headers: { 'X-Requested-With': 'fetch' } })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.generation !== lastGen) {
                        if (lastGen !== -1 && data.ides && data.ides.length) {
                            toast('IDE 扫描完成，共 ' + data.ides.length + ' 个', 'ok');
                        }
                        lastGen = data.generation;
                        render(data.ides);
                    }
                    if (data.scanning && Date.now() < stopAt) {
                        setTimeout(tick, 3000);
                    }
                })
                .catch(function () { /* 忽略 */ });
        }
        tick();
    })();
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

    # 当前所在目录的操作按钮
    cur_abs = str(current_root() / current_rel) if current_rel else str(current_root())
    promote_html = ""
    if current_rel:
        promote_html = (
            f'<form method="post" action="/roots/use" style="margin-top:6px;">'
            f'  <input type="hidden" name="path" value="{html.escape(cur_abs)}">'
            f'  <button type="submit" style="background:#5a5a5f;padding:6px 10px;font-size:13px;width:100%;">⬆ 提升为新根</button>'
            f'</form>'
        )

    # 检测到的 IDE 按钮
    ides = detect_ides()
    ide_rows = []
    for ide_id, info in ides.items():
        ide_rows.append(
            f'<form method="post" action="/open-ide" data-ajax="1" style="margin-top:6px;">'
            f'  <input type="hidden" name="rel" value="{html.escape(current_rel)}">'
            f'  <input type="hidden" name="ide" value="{html.escape(ide_id)}">'
            f'  <button type="submit" title="{html.escape(info["exe"])}"'
            f'    style="background:#1d1d1f;padding:6px 10px;font-size:13px;width:100%;">'
            f'    {info["emoji"]} 在 {html.escape(info["name"])} 打开</button>'
            f'</form>'
        )
    rescan_form = (
        '<form method="post" action="/ides/rescan" data-ajax="1" style="margin-top:8px;">'
        '  <button type="submit" style="background:transparent;color:#0071e3;'
        '    border:1px dashed #0071e3;padding:5px 10px;font-size:12px;width:100%;">'
        '    🔄 重新扫描 IDE</button>'
        '</form>'
    )
    empty_hint = (
        '' if ide_rows
        else '<div class="history-empty" style="margin-top:6px;" data-ide-empty="1">'
             '未检测到 IDE（后台仍在扫描...）</div>'
    )
    ide_inner = "".join(ide_rows) + empty_hint
    ide_html = (
        f'<div id="fs-ide-list" data-current-rel="{html.escape(current_rel)}">'
        f'{ide_inner}</div>'
        f'{rescan_form}'
    )

    here_actions = f"""
    <h2 class="mt">📍 当前位置</h2>
    <div class="hint"><code>{html.escape(cur_abs)}</code></div>
    <form method="post" action="/open" data-ajax="1">
        <input type="hidden" name="rel" value="{html.escape(current_rel)}">
        <button type="submit" style="background:#5a5a5f;padding:6px 10px;font-size:13px;width:100%;">🗔 在系统打开</button>
    </form>
    {promote_html}
    {ide_html}
    """

    return f"""
<aside class="sidebar">
    <h2>📌 当前根目录</h2>
    <div class="hint"><code>{html.escape(cur_root_str)}</code></div>
    <a class="back" href="/roots" style="margin:0;">🗂️ 管理根目录</a>
    <form method="post" action="/roots/pick" style="margin-top:8px;">
        <button type="submit" style="background:#5a5a5f;padding:6px 10px;font-size:13px;">📂 浏览选择文件夹...</button>
    </form>

    <div class="history-header">
        <h2>🔀 快速切换</h2>
    </div>
    {quick_html}

    {here_actions}

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
        <form method="post" action="/roots/pick" style="margin:-10px 0 20px;">
            <button type="submit" style="background:#5a5a5f;">📂 浏览选择文件夹...</button>
            <span style="color:#888;font-size:12px;margin-left:8px;">会在服务器上弹出系统选择器</span>
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
        from http.cookies import SimpleCookie
        raw = self.headers.get("Cookie", "") or ""
        result = {}
        try:
            jar = SimpleCookie()
            jar.load(raw)
            for k, morsel in jar.items():
                result[k] = unquote(morsel.value)
        except Exception:  # noqa: BLE001
            pass
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
        if not p.is_dir():
            _ctx.root = None
            return
        # 白名单校验：cookie 里的路径必须已经在根目录历史里，或等于全局默认 ROOT_DIR
        norm = os.path.normcase(os.path.normpath(str(p)))
        allowed = {os.path.normcase(os.path.normpath(str(ROOT_DIR)))}
        for r in get_roots():
            allowed.add(os.path.normcase(os.path.normpath(r)))
        if norm in allowed:
            _ctx.root = p
        else:
            _ctx.root = None  # 不认这个 cookie（可能是攻击者塞进来的）

    def _set_root_cookie(self, path_str: str):
        # 设置 cookie；path_str 为空表示清除
        if path_str:
            v = quote(path_str)
            self.send_header(
                "Set-Cookie",
                f"fs_root={v}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly",
            )
        else:
            self.send_header(
                "Set-Cookie",
                "fs_root=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly",
            )

    def _same_origin(self) -> bool:
        """校验 POST 请求的 Origin/Referer，防止跨站表单触发状态变更。

        允许：Origin 或 Referer 以 http(s)://<Host> 开头；两者都缺则拒绝。
        """
        host = self.headers.get("Host") or ""
        if not host:
            return False
        prefixes = (f"http://{host}", f"https://{host}",
                    f"http://{host}/", f"https://{host}/")
        origin = self.headers.get("Origin") or ""
        if origin:
            return origin == f"http://{host}" or origin == f"https://{host}" \
                or origin.startswith(prefixes)
        referer = self.headers.get("Referer") or ""
        if referer:
            return referer.startswith(prefixes)
        return False

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200,
                    extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, body: str, status: int = 200):
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status)

    def _send_text(self, msg: str, status: int = 404):
        self._send_bytes(msg.encode("utf-8"), "text/plain; charset=utf-8", status)

    def _send_json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status)

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

        if path == "/ides/status":
            ides = detect_ides()
            state = get_scan_state()
            self._send_json({
                "ides": [
                    {"id": k, "name": v["name"], "emoji": v["emoji"], "exe": v["exe"]}
                    for k, v in ides.items()
                ],
                "scanning": state["scanning"],
                "generation": state["generation"],
            })
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

        if path == "/roots/use":
            new_root = (form.get("path", [""])[0] or "").strip()
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

        if path == "/open":
            rel = (form.get("rel", [""])[0] or "").strip()
            is_ajax = self.headers.get("X-Requested-With", "") == "fetch"
            target = safe_resolve(rel)
            back_url = (f"/browse/{quote(rel.strip('/'))}" if rel.strip("/") else "/")
            if target is None or not target.is_dir():
                if is_ajax:
                    self._send_json({"ok": False, "msg": "目录不存在"}, 400)
                else:
                    self._redirect(back_url + "?msg=" + quote("目录不存在") + "&kind=err")
                return
            ok, err = open_in_system(target)
            if is_ajax:
                self._send_json({"ok": ok, "msg": "已在系统打开" if ok else f"打开失败: {err}"})
            elif ok:
                self._redirect(back_url + "?msg=" + quote("已在系统打开") + "&kind=ok")
            else:
                self._redirect(back_url + "?msg=" + quote(f"打开失败: {err}") + "&kind=err")
            return

        if path == "/open-ide":
            rel = (form.get("rel", [""])[0] or "").strip()
            ide_id = (form.get("ide", [""])[0] or "").strip()
            is_ajax = self.headers.get("X-Requested-With", "") == "fetch"
            target = safe_resolve(rel)
            back_url = (f"/browse/{quote(rel.strip('/'))}" if rel.strip("/") else "/")
            if target is None or not target.is_dir():
                if is_ajax:
                    self._send_json({"ok": False, "msg": "目录不存在"}, 400)
                else:
                    self._redirect(back_url + "?msg=" + quote("目录不存在") + "&kind=err")
                return

            ok, err = open_in_ide(ide_id, target)
            ide_name = detect_ides().get(ide_id, {}).get("name", ide_id)
            if is_ajax:
                self._send_json({
                    "ok": ok,
                    "msg": f"已用 {ide_name} 打开" if ok else f"打开失败: {err}",
                })
            elif ok:
                self._redirect(back_url + "?msg=" + quote(f"已用 {ide_name} 打开") + "&kind=ok")
            else:
                self._redirect(back_url + "?msg=" + quote(f"打开失败: {err}") + "&kind=err")
            return

        if path == "/ides/rescan":
            is_ajax = self.headers.get("X-Requested-With", "") == "fetch"
            found = rescan_ides()
            names = [info["name"] for info in found.values()]
            msg = (
                f"快速扫描已完成（{len(names)} 个：{', '.join(names)}），"
                "后台仍在深度扫描..."
                if names else "快速扫描未检测到 IDE，后台仍在深度扫描..."
            )
            if is_ajax:
                self._send_json({
                    "ok": True, "msg": msg,
                    "ides": list(found.keys()),
                    "scanning": get_scan_state()["scanning"],
                })
            else:
                self._redirect("/?msg=" + quote(msg) + "&kind=ok")
            return

        if path == "/roots/pick":
            initial = str(current_root())
            ok, result = pick_folder_dialog(initial)
            if not ok:
                self._redirect("/roots?msg=" + quote(f"未切换：{result}") + "&kind=err")
                return
            try:
                p = Path(result).expanduser().resolve()
            except OSError:
                self._redirect("/roots?msg=" + quote("选择的路径无效") + "&kind=err")
                return
            if not p.is_dir():
                self._redirect("/roots?msg=" + quote(f"目录不存在: {result}") + "&kind=err")
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

    # 启动时先跑一遍快扫描并触发后台慢扫描；不阻塞主线程
    had_cache = IDES_FILE.exists()
    initial = detect_ides()
    names = ", ".join(v["name"] for v in initial.values()) or "无"
    if had_cache:
        print(f"IDE 已加载磁盘缓存 + 快速扫描: 共 {len(initial)} 个 ({names})")
    else:
        print(f"IDE 快速扫描: 找到 {len(initial)} 个 ({names})")
    print("IDE 深度扫描: 已在后台开始，完成后前端会自动刷新并写入 .fs_ides.json")

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
