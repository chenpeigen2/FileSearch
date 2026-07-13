"""本地打包脚本：用 PyInstaller 把 folder_search.py 打成单文件可执行。

用法：
    python build.py                 # 打当前平台的二进制到 ./dist
    python build.py --clean         # 打包前清掉 build/、dist/、*.spec
    python build.py --name filesrch # 自定义产物名（默认 filesearch-<os>-<arch>）

依赖：
    pip install pyinstaller

产物：
    dist/filesearch-windows-x86_64.exe
    dist/filesearch-macos-arm64
    dist/filesearch-linux-x86_64
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "folder_search.py"


def platform_tag() -> str:
    system = platform.system().lower()  # windows / darwin / linux
    if system == "darwin":
        system = "macos"
    arch = platform.machine().lower()
    # 归一 arch
    arch = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "i386": "x86",
        "aarch64": "arm64",
    }.get(arch, arch)
    return f"{system}-{arch}"


def default_name() -> str:
    base = f"filesearch-{platform_tag()}"
    if platform.system() == "Windows":
        base += ".exe"
    return base


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，尝试自动安装...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"]
        )


def clean():
    for p in ("build", "dist"):
        path = ROOT / p
        if path.exists():
            print(f"删除 {path}")
            shutil.rmtree(path, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        print(f"删除 {spec}")
        spec.unlink()


def build(name: str, onefile: bool = True):
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", Path(name).stem,  # PyInstaller 内部名字（不带扩展名）
        "--console",
    ]
    if onefile:
        cmd.append("--onefile")
    # tkinter 的 hidden import（PyInstaller 一般能自动识别，加上更保险）
    cmd += ["--hidden-import", "tkinter", "--hidden-import", "tkinter.filedialog"]
    cmd.append(str(ENTRY))

    print("PyInstaller 命令:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))

    # PyInstaller 输出到 dist/<stem>[.exe]，重命名成我们想要的
    stem = Path(name).stem
    default_out = ROOT / "dist" / (stem + (".exe" if platform.system() == "Windows" else ""))
    target = ROOT / "dist" / name
    if default_out != target and default_out.exists():
        if target.exists():
            target.unlink()
        default_out.rename(target)

    print(f"\n构建完成: {target}")
    print(f"大小: {target.stat().st_size / (1024*1024):.1f} MB")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--clean", action="store_true", help="打包前清理产物")
    parser.add_argument("--name", default=None, help="产物文件名（默认按平台命名）")
    parser.add_argument("--no-onefile", action="store_true",
                        help="生成目录版本而不是单文件（启动更快）")
    args = parser.parse_args()

    if not ENTRY.exists():
        print(f"找不到入口脚本: {ENTRY}", file=sys.stderr)
        sys.exit(1)

    if args.clean:
        clean()

    ensure_pyinstaller()
    build(args.name or default_name(), onefile=not args.no_onefile)


if __name__ == "__main__":
    main()
