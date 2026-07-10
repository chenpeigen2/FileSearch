import os
from pathlib import Path

names = {"sublime_text.exe", "subl.exe", "studio64.exe", "studio.exe"}
roots = [
    os.environ.get("ProgramFiles", ""),
    os.environ.get("ProgramFiles(x86)", ""),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs") if os.environ.get("LOCALAPPDATA") else "",
    os.path.expanduser("~/scoop/apps"),
    "D:\\",
    "E:\\",
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
                    if e.is_file(follow_symlinks=False) and e.name.lower() in {n.lower() for n in names}:
                        hits.append(e.path)
                    elif e.is_dir(follow_symlinks=False) and d < 6:
                        stack.append((Path(e.path), d + 1))
                except OSError:
                    pass
        except OSError:
            pass
    for h in hits:
        print(" ", h)
