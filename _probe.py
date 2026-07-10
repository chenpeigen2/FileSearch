"""临时诊断：找 Sublime / Android Studio 的可执行文件在哪。"""
import os
from pathlib import Path

targets = {
    "sublime_text.exe", "subl.exe",
    "studio64.exe", "studio.exe", "studio64.bat", "studio.bat",
}

roots = [
    os.environ.get("ProgramFiles", ""),
    os.environ.get("ProgramFiles(x86)", ""),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs") if os.environ.get("LOCALAPPDATA") else "",
    os.path.expanduser("~/scoop/apps"),
    "D:\\",
    "E:\\",
    "F:\\",
]

for root in roots:
    if not root or not os.path.isdir(root):
        continue
    print(f"--- {root} ---")
    stack = [(Path(root), 0)]
    hits = []
    while stack:
        cur, d = stack.pop()
        try:
            for e in os.scandir(cur):
                try:
                    if e.is_file(follow_symlinks=False):
                        if e.name.lower() in {t.lower() for t in targets}:
                            hits.append(e.path)
                    elif e.is_dir(follow_symlinks=False) and d < 6:
                        stack.append((Path(e.path), d + 1))
                except OSError:
                    pass
        except OSError:
            pass
    for h in hits:
        print(" ", h)
