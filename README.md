# FileSearch

一个只依赖 Python 标准库的文件夹浏览 / 搜索小工具。启动一个本地 HTTP 服务，用浏览器访问就能：

- 在指定根目录下按关键字模糊匹配文件夹
- 逐级浏览、点开文件预览 / 下载
- 保留搜索关键字回退、返回时高亮上次点击项、记住滚动位置
- 记录最近访问的目录 / 最近使用过的根目录
- 在网页里新建文件夹
- 通过系统对话框选文件夹作为根目录
- 一键用系统资源管理器打开当前目录
- 把当前浏览的子目录提升为新的根目录
- 检测本机安装的 IDE（VS Code / Cursor / Trae / Sublime / JetBrains 全家桶等），一键用原生 IDE 打开

只有一个脚本文件：`folder_search.py`。

## 环境要求

- Python 3.8+
- 系统文件夹选择器需要 `tkinter`（Windows 官方 Python 自带；Debian/Ubuntu 上装 `python3-tk`；macOS 官方 Python 自带）
- "在系统打开" 按钮：
  - Windows：`os.startfile`
  - macOS：`open`
  - Linux：`xdg-open` → `gio` → `nautilus` → `dolphin` → `thunar` → `pcmanfm` → `nemo` 按顺序尝试

## 快速开始

```bash
# 以脚本所在目录为根启动
python folder_search.py

# 指定根目录
python folder_search.py D:\Downloads

# 用当前工作目录
python folder_search.py .

# 指定端口和监听地址
python folder_search.py "C:\My Docs" -H 0.0.0.0 -p 8000
```

启动后打开 <http://127.0.0.1:5000> 即可。

## CLI 参数

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `ROOT` | 要浏览的根目录，可选。留空 = 脚本所在目录；`.` = 当前工作目录 | 脚本目录 |
| `-p, --port` | 监听端口 | `5000` |
| `-H, --host` | 监听地址 | `127.0.0.1` |
| `--help` | 显示帮助 | |

## 界面一览

- **主区**：搜索框 + 结果列表 / 目录内容
- **右侧栏**
  - `📌 当前根目录`：显示当前根、切换到管理页、调起系统选择器
  - `🔀 快速切换`：最近使用过的其它根目录
  - `📍 当前位置`：
    - `🗔 在系统打开`：在服务器上用文件管理器打开当前目录
    - `⬆ 提升为新根`：把当前子目录设为新的根目录（仅在浏览子目录时显示）
    - `🟦 在 VS Code 打开` 等：检测到的每个 IDE 一个按钮
  - `🛠️ 新建文件夹`：在当前目录下创建子目录
  - `🕘 最近访问`：会话内浏览过的目录

## 根目录管理

- 每次切换根目录都会写入 `.fs_roots.json`（脚本同目录）
- 最多保留 20 条，按最近使用排序
- 支持从历史中移除某条
- 每个浏览器通过 cookie `fs_root` 记住自己选择的根目录，互不影响

## 系统文件夹选择器

点击「📂 浏览选择文件夹...」会在**运行服务器的机器上**弹出一个 tkinter 文件夹选择对话框。选择后自动切换根目录。

- 弹窗跑在子进程里，避免与 HTTP 服务器主线程冲突
- 同一时间只允许打开一个，避免叠窗
- 没装 `tkinter` 会返回明确的错误消息，不影响其它功能

## IDE 自动检测

启动首次调用时扫描一次，结果全局缓存。检测顺序：

1. 先在 `PATH` 里查各 IDE 的短命令（`code` / `cursor` / `subl` / `idea` / `pycharm` 等）
2. 找不到再查常见安装路径
3. JetBrains 系产品额外扫描 Toolbox 安装目录：
   - Windows：`%ProgramFiles%\JetBrains`、`%LOCALAPPDATA%\JetBrains\Toolbox\apps`
   - macOS：`~/Applications/JetBrains Toolbox`、`~/Library/Application Support/JetBrains/Toolbox/apps`
   - Linux：`~/.local/share/JetBrains/Toolbox/apps`、`/opt/JetBrains`

目前支持检测的 IDE：VS Code / VS Code Insiders / Cursor / Trae / Sublime Text / IntelliJ IDEA / PyCharm / WebStorm / GoLand。想添加新的 IDE，在 `_ide_candidates()` 里追加一项即可。

macOS 上，如果检测到的可执行文件是 `.app/Contents/MacOS/<binary>`，会自动改用 `open -a <bundle> <path>` 启动，以便复用已运行的实例。

## 安全说明

- 路径解析统一走 `safe_resolve`，保证请求路径不能越过当前根目录
- 「在系统打开」和「在 IDE 打开」都是**在运行服务器的机器上**触发的动作，不是浏览器所在机器。如果你把服务暴露到 `0.0.0.0`，请确保网络环境可信
- `.fs_roots.json` 里保存的是明文绝对路径列表，无敏感信息

## 端点速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页 + 关键字搜索 |
| GET | `/browse/<rel>` | 浏览子目录，`?sq=` 目录内搜索，`?q=` 保留外层搜索 |
| GET | `/file/<rel>` | 下载 / 预览文件 |
| GET | `/roots` | 根目录管理页 |
| GET | `/roots/use?path=` | 切换到已知根目录 |
| GET | `/roots/remove?path=` | 从历史移除 |
| GET | `/history/clear` | 清空最近访问 |
| POST | `/roots/add` | 手动输入路径添加 |
| POST | `/roots/pick` | 调起系统选择器 |
| POST | `/roots/use` | 表单形式切换（提升为新根用） |
| POST | `/mkdir` | 新建子目录 |
| POST | `/open` | 在系统资源管理器打开当前目录 |
| POST | `/open-ide` | 用检测到的 IDE 打开当前目录 |

## 已知限制

- 只依赖标准库，没做鉴权。用于本机 / 内网可信环境
- 大目录下 `os.scandir` 是同步的，条目非常多（数十万级）时首屏会有明显停顿
- IDE 检测使用了 `rglob` 扫描 JetBrains 目录，Toolbox 装了很多产品时首次启动会有短暂延迟；结果缓存到进程退出
