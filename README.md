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
- 检测本机安装的 IDE / 编辑器（VS Code / Cursor / Zed / Sublime / Notepad++ / Android Studio / JetBrains 全家桶 / Visual Studio / Xcode / Eclipse 等），一键用原生程序打开
- IDE 扫描结果持久化到磁盘，重启秒加载；深度扫描在后台线程跑，不阻塞页面

只有一个脚本文件：`folder_search.py`，运行时会在同目录写两个隐藏状态文件（`.fs_roots.json`、`.fs_ides.json`）。

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
    - `🔄 重新扫描 IDE`：清缓存并重新做完整扫描
  - `🛠️ 新建文件夹`：在当前目录下创建子目录
  - `🕘 最近访问`：会话内浏览过的目录

「在系统打开」/「在 IDE 打开」/「重新扫描」都走 AJAX 提交，操作后不刷新页面、不动浏览器历史，右上角弹一条 toast 提示。

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

分**快扫描**和**慢扫描**两阶段，配合磁盘缓存 `.fs_ides.json`：

### 加载流程

1. **服务启动时**先读 `.fs_ides.json`（若存在），过滤掉已卸载的项（检查 `exe` 是否仍存在）作为初始结果
2. 主线程立刻做一次**快扫描**（`shutil.which` 查 PATH + 检查静态候选路径），大约 100~1000 ms 返回
3. 主线程把合并后的结果落盘，然后启动 HTTP 服务
4. **后台线程**做**慢扫描**：JetBrains Toolbox 目录 + 通用安装位置的 BFS（限深 6 层）
5. 慢扫描完成后合并进内存缓存 → 落盘 → `generation` 递增
6. 前端每 3 秒轮询 `/ides/status`，`generation` 变化就重绘 IDE 按钮列表并弹 toast 提示

### 扫描的目录

- **PATH 命令**：`code` / `code-insiders` / `cursor` / `subl` / `idea` / `pycharm` / `webstorm` / `goland` / `studio` / `notepad++` / `zed` / `windsurf` 等
- **Windows**
  - `%ProgramFiles%` / `%ProgramFiles(x86)%` / `%LOCALAPPDATA%\Programs`
  - `~/scoop/apps`、`C:\ProgramData\chocolatey\lib`
  - 所有**非系统盘**下的 `Program Files`、`Program Files (x86)`、`Programs`、`Apps`、`AS`、`Tools` 等
  - JetBrains Toolbox：`%LOCALAPPDATA%\JetBrains\Toolbox\apps`
- **macOS**
  - `/Applications`、`~/Applications`（按 `.app` 名匹配）
  - JetBrains Toolbox：`~/Library/Application Support/JetBrains/Toolbox/apps`
  - 检测到的 `.app/Contents/MacOS/<binary>` 会自动改用 `open -a <bundle> <path>` 启动，复用已运行实例
- **Linux**
  - `/opt`、`/usr/local`、`/var/lib/flatpak/exports/bin`、`~/.local/share/flatpak/exports/bin`
  - JetBrains Toolbox：`~/.local/share/JetBrains/Toolbox/apps`、`/opt/JetBrains`

### 目前覆盖的 IDE / 编辑器

VS Code、VS Code Insiders、Cursor、Trae、Windsurf、Zed、Fleet、Sublime Text、Notepad++、Notepad3、UltraEdit、EmEditor、EditPlus、Android Studio、IntelliJ IDEA、PyCharm、WebStorm、GoLand、CLion、Rider、PhpStorm、RubyMine、RustRover、DataGrip、Visual Studio、Xcode、TextMate、BBEdit、Nova、Eclipse、NetBeans、Qt Creator、Kate、gedit、Geany。

想加新的，在 `_ide_candidates()` 里追加一项即可。

### 缓存管理

- 结果写到 `.fs_ides.json`（脚本同目录），下次启动秒读
- 卸载 IDE 后：加载时会校验 `exe`，失效项自动剔除
- 装了新 IDE：点侧边栏「🔄 重新扫描 IDE」，会删掉 `.fs_ides.json` 重新扫；或直接删掉该文件重启

## 安全说明

- 路径解析统一走 `safe_resolve`，保证请求路径不能越过当前根目录
- 「在系统打开」/「在 IDE 打开」/「浏览选择文件夹」都是**在运行服务器的机器上**触发的动作，不是浏览器所在机器。如果把服务暴露到 `0.0.0.0`，请确保网络环境可信
- `.fs_roots.json` 和 `.fs_ides.json` 里保存的是明文绝对路径，无凭据信息，但会暴露安装位置——已默认加入 `.gitignore`

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
| GET | `/ides/status` | 返回当前 IDE 列表 + 后台扫描状态（前端轮询用） |
| POST | `/roots/add` | 手动输入路径添加 |
| POST | `/roots/pick` | 调起系统选择器 |
| POST | `/roots/use` | 表单形式切换（提升为新根用） |
| POST | `/mkdir` | 新建子目录 |
| POST | `/open` | 在系统资源管理器打开当前目录（AJAX） |
| POST | `/open-ide` | 用检测到的 IDE 打开当前目录（AJAX） |
| POST | `/ides/rescan` | 清缓存并重新扫描 IDE（AJAX） |

## 已知限制

- 只依赖标准库，没做鉴权。用于本机 / 内网可信环境
- 大目录下 `os.scandir` 是同步的，条目非常多（数十万级）时首屏会有明显停顿
- 首次深度扫描 IDE 可能需要几十秒（尤其装了 Chocolatey/Scoop 或大盘符时），但**在后台线程执行，不阻塞任何页面请求**；结果落盘后续启动秒读
- IDE 检测按可执行文件名匹配，同名工具（比如 fork 版本）可能误检；可以手动删除 `.fs_ides.json` 对应项并重启
