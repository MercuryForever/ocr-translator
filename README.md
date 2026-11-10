# 截屏翻译 · socrans

按一个热键 → 框选屏幕任意区域 → 得到**原文 + 译文双语对照**，可复制或导出。

面向"看懂外语软件界面 / 论文截图 / 游戏对话"这类场景：文字在屏幕上，不在文件里，
所以必须截图；又因为要反复对照阅读，所以必须双语并排而不是一句替换。

```
全局热键 Ctrl+Shift+A
   → 全屏冻结 + 框选（带像素放大镜）
   → 裁剪（自动处理多屏与高 DPI）
   → OCR（RapidOCR / PP-OCR，完全离线）
   → 版面分析（行 → 段落 → 分栏 → 标题/代码/表格）
   → 翻译（Google / DeepL / 百度 / MyMemory / 本地 OPUS-MT，自动降级）
   → 双语对照窗口（左右 / 上下 / 逐段三种布局）
   → 复制或导出 TXT / MD / DOCX / CSV / HTML / RTF / JSON
```

**目录**：[使用者指南（下载 / 使用 / FAQ）](#第一部分--使用者指南) ·
[开发者文档（源码 / 打包 / 设计决策）](#第二部分--开发者文档)

---

# 第一部分 · 使用者指南

## 1. 快速开始

### 1.1 两种形态，功能完全相同

| 形态 | 位置 | 适合 |
|---|---|---|
| **目录式（推荐）** | `dist\截屏翻译\截屏翻译.exe` | 日常自用：启动零解包、响应最快。**整个文件夹一起拷走**，别只拿 exe |
| **单文件** | `dist\截屏翻译.exe` | 分发给别人：一个 exe 走天下，代价是**首次启动要解包 3~8 秒**，之后正常 |

> 若单文件版提示缺 `VCRUNTIME140.dll` / `MSVCP140.dll`，装一次
> [VC++ 2015-2022 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe) 即可（多数机器已有）。

### 1.2 首次启动

双击 exe 后会依次发生：

1. 出现**主窗口**（任务栏 / Alt-Tab 可见）和右下角**托盘图标**；
2. 状态行显示热键注册状态、OCR 引擎加载（首次 1~2 秒）、翻译后端探测结果；
3. 之后程序常驻托盘，主窗口可随时用 `Ctrl+H` 或托盘菜单唤出。
   点主窗口的 ✕ 是**隐藏到托盘**，不是退出；退出请走托盘右键菜单或 `Ctrl+Q`。

**不需要管理员权限。** 配置、日志、导出都写在你的用户目录里
（`%APPDATA%\socrans\`）；如果你发现"每次都要提权才能启动"，那是历史遗留的
目录权限问题，见 [FAQ 4.3](#43-需要管理员权限吗)。

### 1.3 三步上手

```
按 Ctrl+Shift+A      →  全屏变暗进入框选（自带像素放大镜，任意方向拖拽）
按 Enter 或双击      →  自动识别 + 翻译，右下角气泡提示进度
读双语对照窗口       →  Ctrl+C 复制译文，或点「导出」保存文件
```

## 2. 使用指南

### 2.1 键位

| 操作 | 键位 |
|---|---|
| 唤起截图 | 全局热键 `Ctrl+Shift+A`（可在设置中修改） |
| 框选 | 左键拖拽（任意方向） |
| 移动 / 缩放选区 | 拖选区内部 / 拖 8 个控制点 |
| 微调 | 方向键 1px，`Ctrl+方向键` 10px，`+/-` 等比缩放 |
| 确认翻译 | `Enter` / 双击 / 中键 |
| 只复制图片不翻译 | `C` |
| 清空选区 | `R` |
| 取消 | `Esc` / 右键 |
| 结果窗复制 | `Ctrl+C` 译文，`Ctrl+Shift+C` 双语，`Ctrl+点击`某段复制原文 |
| 打开主窗口 | `Ctrl+H` / 托盘菜单「主窗口…」 |
| 退出程序 | 托盘右键菜单「退出」/ 主窗口 `Ctrl+Q` |

### 2.2 主窗口、托盘与提示气泡

* **主窗口**——上半部是状态行：热键注册状态 / OCR 引擎 / 翻译后端降级链 /
  上次结果；下半部是带时间戳的提示日志（最近 80 条）；底部六个按钮：
  截图翻译 / 显示上次结果 / 设置 / 打开日志 / 隐藏 / **退出**。
* **关闭主窗口 = 隐藏到托盘，不是退出**（和 QQ 一个习惯）：点 × 后主窗口消失，
  程序仍在托盘里，热键照常可用；首次隐藏会弹气泡提醒"程序仍在运行"。
  想**彻底退出**：点主窗口的「退出」按钮，或右键托盘图标 → 退出，或 Ctrl+Q。
  不带托盘运行（`--no-tray` 或系统不支持托盘）时，点 × 直接退出进程。
* **托盘图标**——右键菜单含 截图翻译 / 主窗口 / 设置 / 退出。
  程序常驻托盘，热键全局有效（其他程序在前台也能按）。
* **气泡提示**——右下角弹出，4.5 秒自动消失，点击即关。
  翻译完成、导出成功、热键占用、出错原因**都走这个气泡**，
  它不经过 Windows 通知中心——专注模式开着也不会错过提示。

### 2.3 结果窗口：双语对照

* **三种布局**：左右并排 / 上下排列 / 逐段交错，随时切换；
* **悬停联动**：鼠标放到任一段，对应的原文/译文互相高亮；
* 每段卡片左侧有**置信度色条**，识别不确定的段落一眼可见；
* 识别失败的条目**保留原文**并标注原因，不会整页白屏；
* **重翻译**：换目标语言后只重新翻译，不重新截图识别；
* **查看原始行**：可展开 OCR 逐行原始结果，用于核对识别质量。

### 2.4 导出

结果窗口可导出 **TXT / MD / DOCX / CSV / HTML / RTF / JSON** 七种格式，
复制按钮另可复制纯译文 / 双语对照。导出文件名自动清洗 Windows 非法字符，
重名自动追加 `(1)`；CSV 自带 BOM，Excel 打开不乱码。

### 2.5 设置

设置窗口分六个标签页，**每一页都有「测试」按钮**——保存前先实际验证配置
（测试热键能否注册、翻译后端能否连通、OCR 引擎能否加载）。常用项：

* **热键**：修改后立即生效，无需重启；
* **翻译后端与密钥**：见下一节；
* **源语言 / 目标语言**：源语言选 `auto` 可自动判断；
* **OCR 引擎**：`auto` 自动选可用的（默认 RapidOCR，无需任何配置）；
* **主题**：明色 / 暗色。

## 3. 翻译后端与密钥

程序按以下顺序尝试翻译后端，前一个失败自动换下一个（主窗口"翻译链"一行
显示当前实际使用的后端）：

```
google（零配置）→ deepl（需 key）→ baidu（需 key）→ mymemory（零配置）→ opus（本地离线）
```

* **什么都不配也能用**：google 连不上时会自动落到 mymemory
  （免费匿名档，约 5000 字符/天，超了会明确提示配额）；
* **要翻译质量**：申请 [DeepL API](https://www.deepl.com/pro-api) 免费 key
  （有月度字符上限，以官网为准），填进设置页即可；
* **中国大陆网络**：google 端点经常不可达，建议申请
  [百度翻译开放平台](https://fanyi-api.baidu.com/) 的 appid + key；
* **完全离线**：用源码方式运行并 `pip install argos-translate`，
  opus 后端自动可用（本地模型，不出网）；
* 密钥在设置页填写，也可以用环境变量提供（优先级更高，适合不想落盘的场景）。
  注意 google 走的是网页端点的**非官方接口**，不保证稳定，商用请换正式 API。

## 4. 常见问题（FAQ）

### 4.1 按了热键没反应

1. 打开主窗口（托盘图标双击）看状态行"热键"是否显示**已注册**；
2. 未注册 → 多半是被别的程序占用了，见 4.2；
3. 已注册但没反应 → 程序可能正在处理上一次截图（按钮禁用、气泡显示"处理中"）；
   或当前是全屏游戏/独占模式应用抢下了键盘输入。

### 4.2 提示"热键已被其他程序占用"

`Ctrl+Shift+A` 是 QQ / 微信 / Snipaste 等截图工具的常用键。谁后启动谁拿不到。
**从 1.1.0 起程序会自动改用备用热键**（按序尝试 `Ctrl+Alt+X` → `Ctrl+Shift+X`
→ `Ctrl+Alt+Q` → `Ctrl+Alt+S`），气泡和主窗口会明确告知实际生效的组合；
想固定下来就在设置里保存，或关掉占用者的快捷键。
程序检测到占用时会用气泡 + 主窗口明确告知，不会默默失效。

> 另一个常见原因：**之前用管理员身份运行过本程序，那个实例还没退出** ——
> 它注册的热键会把新实例顶掉（两份数据目录互不可见，但内核热键是全机的）。
> 先从托盘退出旧实例再启动。双开本身现在也会被拦住：程序用全机可见的
> 命名互斥量做单实例保护，第二个实例会弹出"已经在运行"提示后自动退出，
> 与两个实例分别用什么权限/目录运行无关。

### 4.3 需要管理员权限吗？

**不需要，也请尽量别用管理员身份运行。** 程序的所有数据都写在用户目录。
"双击没反应、提权才有反应"的现象在本项目排查中已确认**不是权限问题**：
未提权启动的日志显示配置读写、OCR 加载、事件循环全部正常，
真正的症状是热键被占用/被旧实例抢注（见 4.2）——提权"治好"它纯属巧合。
如果你每次都要"以管理员身份运行"才能启动，几乎可以肯定是数据目录
（`%APPDATA%\socrans`）曾被管理员运行创建过，权限归属被污染。修法：

1. 删除 `%APPDATA%\socrans` 文件夹；
2. 用**普通权限**重新启动，目录会自动重建。

程序自带三层兜底：`--doctor` 会报告数据目录逐个候选的失败原因与修复建议；
数据目录全坏时启动会弹窗提供一键提权重启；发布包里的 `run_admin.bat`
也可以显式以管理员启动。但这些都是应急手段——修好权限后就别再点"是"了。

### 4.4 点 × 之后任务管理器里进程还在 —— 是退出不彻底吗？

不是。**关闭主窗口 = 隐藏到托盘**（和 QQ 一样），程序在托盘里待命、热键照常可用，
首次隐藏会弹气泡提醒"程序仍在运行"。想真正退出，任选其一：

* 主窗口点「退出」按钮；
* 右键托盘图标 → 退出；
* 快捷键 Ctrl+Q。

退出时程序会等 OCR 预热 / 翻译线程收尾（最长约 8 秒）再结束——
任务管理器里看到进程多活几秒属正常。`--no-tray` 运行（或系统没有托盘）时
点 × 直接退出，不驻留。

### 4.5 双击 exe 一闪而过 / 出现黑色控制台窗口

* 双击启动 GUI 时控制台会自动收起，属正常现象；从命令行带参数启动
  （`--doctor` / `--selftest` / `--file 图片.png`）时控制台保留，
  便于看输出；
* 启动失败不会无声无息：原因会写入日志
  `%APPDATA%\socrans\logs\crash.log`（单文件/目录式便携运行时在
  exe 旁 `.socrans\logs\`）。打不开界面时先看这个文件，
  或在 cmd 里运行 `截屏翻译.exe --doctor` 把结论发出来。

### 4.6 杀毒软件报毒 / 删除 exe

PyInstaller 打包的 Python 程序被误报是常见现象（单文件形态概率更高）。
可将程序目录加入杀软白名单；在意的话用**目录式**形态（误报率低），
或改用源码方式运行（见第二部分）。

### 4.7 翻译失败或很慢

* 失败的条目会**逐后端标注原因**（例如
  `google: translate.googleapis.com 在 2 秒内无法建立连接`），
  按提示处理即可；
* 首次翻译包含 2 秒预检与后端探测，实测 3~4 秒；之后相同内容秒回（缓存）；
* mymemory 免费匿名档约 5000 字符/天，长文建议配 DeepL 或百度 key；
* 程序对"某后端坏了"有冷静期机制（90 秒~10 分钟按错误类型分级），
  在设置页点「保存」或「测试」会立即清除冷静期，修好配置不用干等。

### 4.8 识别不到文字 / 识别质量差

* 选区太小、纯图片、字号过小时会**明确提示**而不是弹空窗；
* 小字（<13px）程序会自动放大再识别；截图时尽量框完整的行或段落，
  不要只框单个字符；
* 中英混排、深色主题、多屏高 DPI 都已适配；暗背景亮文字同样支持；
* 想先拿现成图片测试：命令行 `截屏翻译.exe --file 图片.png`。

### 4.9 我的数据在哪？怎么彻底卸载？

* 用户数据：`%APPDATA%\socrans\`（config.json 设置 / logs 日志 /
  history 历史 / exports 导出缓存）；
* 便携回退：若用户目录不可写，程序改用 exe 旁的 `.socrans\`；
* 彻底卸载 = 删除程序目录（dist\截屏翻译 或 截屏翻译.exe）+
  删除上述两个数据目录。注册表、系统目录均无残留。

## 5. 隐私

* OCR **全程本地**：截图不出机器，识别由内置的 RapidOCR 模型完成；
* **只发送文字**：翻译仅上传识别出的文本，图像本身不上传；
* 要完全断网使用：源码方式运行 + `pip install argos-translate`，
  全链路离线。
* 截图可能含他人隐私，分享导出文件前请自行确认内容。

---

# 第二部分 · 开发者文档

## 一、源码运行与启动器

**Windows 双击 `run.bat` 即可**（它会自动挑一个能用的 Python，见下文）。
命令行方式：

```bash
python tools/launcher.py           # 推荐：自动挑选可用解释器后启动
python main.py                     # 直接用当前 python（要求它本身能加载 PyQt6）
python main.py --doctor            # 只检查"这个解释器能不能撑起程序"，秒级返回
python main.py --selftest          # 完整自检：真的识别一次 + 真的翻译一次
python main.py --file 图.png        # 翻译一张已有图片，不开 GUI
```

`--selftest` 逐项报告 GUI / 抓屏 / OCR / 版面 / 翻译 / 导出 / 热键 / 离线能力，
并且**真的识别一次、真的翻译一次**，而不是只检查 import。

### 为什么推荐 launcher 而不是直接 `python main.py`

本机实测：PATH 上的 `python` 是 msys64 的，`D:\anaconda3\python.exe` 是 3.13 的，
**两者加载 PyQt6 都会失败**：

```
ImportError: DLL load failed while importing QtWidgets: 找不到指定的模块
```

原因是 Anaconda/msys 把自己那份 Qt5/ICU DLL 放进 DLL 搜索路径，
抢在 PyQt6 自带的 Qt6 DLL 前面。同一份代码在 python.org 的 3.12 下完全正常。

`where python` 判断不出这件事，**只有真的 import 一次才知道**。
所以 `tools/launcher.py` 会枚举候选解释器（当前 python → 项目 venv → `py -0p` →
PATH → conda → 常见安装目录），逐个跑 `main.py --doctor`，取第一个通过的。
本机实测 1.7 秒完成，输出如下：

```
socrans 启动器（当前解释器：C:\msys64\mingw64\bin\python.exe）
  跳过 C:\msys64\mingw64\bin\python.exe —— DLL load failed while importing QtCore
  跳过 D:\anaconda3\python.exe        —— DLL load failed while importing QtCore
选用解释器：...\python.exe
✓ PyQt6 可用（Qt 6.12.0，1 块屏幕，1707x1067）
```

想固定解释器就设环境变量：`set SOCRANS_PY=C:\path\to\python.exe`。

### 故障排查：双击 run.bat 后窗口一闪就没 / 毫无反应

这是本项目修过的真实故障，四条原因按概率排序：

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 窗口闪一下消失，什么都没看到 | 旧版 `run.bat` 用 `chcp 65001` + 中文 + LF 换行，cmd 解析器中途丢位置 | 已改为纯 ASCII 线性脚本；若仍复现，直接跑 `python tools/launcher.py` |
| 报 `DLL load failed while importing QtWidgets` | 当前 python 是 Anaconda/msys 的（见上一节） | 用 `run.bat` / `launcher.py`，或换 python.org 的解释器 |
| 提示"已在运行"后立即退出 | 上次进程被强杀，`.lock` 残留 | 现在锁里带进程创建时间，能识别 PID 复用并自愈；手动清：删 `%APPDATA%\socrans\.lock` |
| 说"找不到 python" | 机器上确实没有 3.10+，或只装了 Microsoft Store 占位程序 | 装 python.org 版本并勾选 *Add python.exe to PATH* |

所有启动期异常现在都会被捕获并**打印可读结论 + 写入 `%APPDATA%\socrans\logs\crash.log`**。
之前是裸 traceback 打到 stderr，而 GUI 程序没有控制台，等于静默消失。

键位表与主窗口/托盘/气泡的使用说明在[第一部分 §2](#2-使用指南)。

---

## 二、选型结论与理由

### 2.1 GUI：PyQt6（不是 Tkinter）

| | PyQt6 | Tkinter |
|---|---|---|
| 全屏透明覆盖层 | `FramelessWindowHint` + 精确像素绘制 | `overrideredirect` 在多屏/DPI 下坐标漂移 |
| 高 DPI | 原生 Per-Monitor v2 | 需 manifest + 手动缩放，字体常糊 |
| 富文本对照 | `QLabel` 自动换行 + 样式表 + 选择复制 | `Text` 组件做双栏对齐很勉强 |
| 托盘 | `QSystemTrayIcon` | 需第三方库（pystray） |
| 体积 | ~90MB | ~2MB |

Tkinter 唯一的优势是零依赖体积。但本项目的核心体验是**截图框选**——
覆盖层要跨多屏（含负坐标）、要按 DPR 精确裁剪、要画放大镜，
这些在 Tkinter 上要么做不到要么做不对。所以选 PyQt6。

> 建议：如果目标只是"一个输入框 + 一个按钮"的翻译工具，Tkinter 够用；
> 只要涉及屏幕覆盖层，就别省这个依赖。

### 2.2 OCR：RapidOCR（PP-OCR + ONNXRuntime）

实测数据（本机 Python 3.12 / 20 核 / CPU 推理）：

| 维度 | RapidOCR | PaddleOCR | Tesseract |
|---|---|---|---|
| 安装体积 | 17MB wheel + 模型内置 | ~700MB | ~40MB + 语言包 |
| 模型来源 | **wheel 自带，装完即离线** | 首次运行联网下载 | 需装 tesseract.exe |
| 加载耗时 | **0.45s** | 8~25s | ~0.1s |
| 单次识别 | **0.44s** | 2~6s | 0.15~0.6s |
| 中文精度 | PP-OCRv4，强 | 同左（同一模型） | 明显弱 |
| 中英混排 | 一行内直接混排 | 同左 | 需切 lang，且丢空格 |
| 单文件打包 | 可行 | 极难（JIT + 自定义 op） | 可行但要外带 exe |

结论：**桌面小工具要"双击就能用 + 出单 exe"，RapidOCR 是唯一同时满足
中文精度、离线、可打包的选择**。PaddleOCR 保留为可插拔后端（需要
PP-OCRv5 的版面复原或更多语种时切），Tesseract 保留给 CPU 很弱的机器与冷门语种。

### 2.3 翻译：多后端 + 自动降级链

```
google（零配置）→ deepl（需 key，质量最佳）→ baidu（需 key，国内可达性最好）
   → mymemory（零配置，配额低但几乎一定能连上）→ opus（本地离线）
```

**这条链不是理论设计，是被本机网络逼出来的。** 实测连通性：

| 端点 | 结果 |
|---|---|
| `translate.googleapis.com` | **TCP 超时（被拦截）** |
| `api-free.deepl.com` | 可连 |
| `fanyi-api.baidu.com` | 可连 |
| `api.mymemory.translated.net` | 可连 |
| `pypi.org` | 可连 |

也就是说"排在第一位的后端在这台机器上必然失败"是常态。所以每个后端都按会失败来设计。

在线选 Google 做默认（零配置、覆盖广）；要质量就配 DeepL；
中国大陆用户建议配百度（Google 常被拦）；完全断网就装 argos-translate。

---

## 三、实测数据：预处理策略（4 轮实验）

代码里的每个阈值都来自 `tools/experiment_*` 的实测，不是拍的。

### 3.1 结论

| 估计行高 | 处理 | 说明 |
|---|---|---|
| < 13px | **2.0x NEAREST** | 保住笔画边缘与词间空隙 |
| ≥ 13px | **2.0x LANCZOS** | 大图需要平滑插值 |
| 任一边 > 2500px | 等比缩小 INTER_AREA | PP-OCR det 内部阈值 2000，主动做更可控 |
| 短边 < 40px | 3.0x 内 NEAREST | 误框/单字符，det 否则直接判空 |

n=21 单元格（3 种明暗极性 × 7 个字号）汇总，指标 = 逐字精确命中（含空格）：

| 策略 | exact | loose（内容命中） | 中位耗时 |
|---|---|---|---|
| **本方案（fixed2.0）** | **2.90/4** | 3.43/4 | **453ms** |
| lanczos2.0 恒放大 | 2.33/4 | 3.52/4 | 876ms |
| adaptive（<13px 不放大） | 1.86/4 | 3.57/4 | 494ms |
| none（完全不放大） | 1.29/4 | 3.57/4 | 952ms |

### 3.2 三个推翻我自己先前判断的结论

1. **"小字不要放大"是错的。** 第二轮我读到"px<20 放大为负收益"，
   但那是在候选只有 lanczos/cubic 的表里得出的；同一张表里
   `2.0x nearest` 在小字上是**最优**（2.50 vs 不放大 2.00 vs lanczos 0.50）。
   我把"别用 lanczos 放大"误写成了"别放大"。修正估计器后复核确认。

2. **真正的分野是插值方式，不是放不放大。** Lanczos/Cubic 的负瓣在细笔画边缘
   产生振铃，把只有 1~2px 宽的词间空隙糊成实心；NEAREST 无振铃。
   exact 2.90 vs 2.33 的差主要来自这里。

3. **丢空格只影响 exact，几乎不影响内容**（全策略 loose 3.43~3.57）。
   即 `Quarterlyrevenuegrew18percent.` 的内容是对的。
   因此**不做**默认的空格修复：实测 `repair_spaces` 净收益 **+0** ——
   PP-OCR 的 `cal_rec_boxes` 把字符框在检测框内**等距铺开**，
   框间隙不含真实空白信息，gap 检测根本无从判断词边界。
   代码保留但默认关闭（`ocr.restore_spaces`），供真实字符框模型使用。

### 3.3 一个差点毁掉全部结论的 bug

`estimate_text_height` 最初只按"暗像素 = 文字"做行投影。
**深色主题截图里文字是亮像素**，于是 Otsu 的两类被反着用，
10px 的字被估成 130~230px，导致所有小字都走了"大幅放大"分支。

第 3 轮实验的全部 `adaptive` 数据因此无效（同一实验里的固定策略
`none/lanczos2.0/nearest2.0` 不读这个估计器，所以仍然有效）。
修正后改为**取少数类当墨迹**，三种底色下估计值完全一致：

```
black_on_white  10:9 12:10 14:13 16:14 18:16 20:16 24:20 28:24 36:30
gray_on_light   10:9 12:10 14:13 16:14 18:16 20:16 24:20 28:24 36:30
white_on_dark   10:9 12:10 14:13 16:14 18:16 20:16 24:20 28:24 36:30
纯色/噪声图 → 0.0（不误判为有文字）
```

教训：**任何"看起来是启发式"的判据都要先验证它对输入的极性是否中立。**

### 3.4 线程数：默认全核是最慢的选择

| 线程 | 中位耗时 |
|---|---|
| 1 | 875ms |
| 2 | 613ms |
| **4** | **484ms** |
| 8 | 675ms |
| -1（全核） | **1661ms** |

PP-OCR 的 det/rec batch 很小，OpenMP 核间同步开销大于并行收益。
代码里默认 `min(4, cores/2)`，实测把首次识别从 1.66s 降到 0.48s。

> 诚实说明：单元格之间波动很大（同策略 437~1364ms），受 CPU 频率调度影响明显，
> 所以 exact/loose 表里的 median 只用于**相对比较**，不要当 SLA 承诺。
> 真正稳定有效的提速是限制线程数这一条。

---

## 四、实测数据：版面分析

OCR 返回的是检测框碎片。**逐行翻译会切碎句子、毁掉语序**，所以必须先配对。

| 能力 | 判据 | 验证方式 |
|---|---|---|
| 行带归并 | y 中心差 < 0.6× 行高中位数 | 单测 |
| 分栏检测 | 列覆盖图的贯穿空白带（≥6% 宽、居中、两栏垂直重叠 ≥50%） | 单测 + 双栏渲染图 |
| 段落合并 | 自适应空行阈值 + 6 个结构信号 | 87 项单测 |
| 标题 | 字号相对**全文中位数** > 1.2 档 | 单测 |
| 代码 | 符号密度 + 强特征（`{}`/`=>`/函数调用） | 单测 |
| 表格 | 竖线/制表符，或"每字符宽 / 行高" > 1.15 | 单测 |

### 4.1 三个真实缺陷，都是渲染图暴露的（单测当时全绿）

1. **`hard and not soft` 式判据在"空行 + 同缩进"时误合并。**
   `soft` 只要求"左边缘对齐 + x 有重叠"，而同一段落风格的连续两段**永远满足**它，
   于是落到最后的 `else` 被合并 —— 6 段文档只出 3 段，整页被当成一句话翻译。
   改成显式优先级分支后正确。

2. **`_code_score` 把句末点号算作代码符号。**
   `"Save As..."` 的密度 = 3 点 / 11 字符 × 6 = 1.63 → 判成代码块。
   修正：句读标点不计入，且缺少强特征时上限压到 0.55（宁可漏判代码，
   不可把正文判成代码）。

3. **`_table_grid` 用绝对阈值 `width/len(text) > 1.5`。**
   18px 字号的正常正文每字符就占 ~9px，于是**所有段落都被判成表格**，
   连带让标题分类失效（表格优先级更高）。修正为除以行高归一化。

4. **分栏最初按"行带中心 x"分组。**
   横跨两栏的行带中心恰好落在分割线上 → 所有行带被判到同一侧 →
   `len(left) < 2` 于是放弃分栏。改为**按行分组再各自重新归并行带**。

### 4.2 空行阈值为什么必须自适应

绝对阈值 `gap > 1.9 × 行高` 在行距宽松的排版上失效。实测：

```
行高=20  行距=40 → gap 样本 [20,100,20,100]  旧阈值 38 → 切 2 刀 ✓
行高=20  行距=60 → gap 样本 [40,120,40,120]  旧阈值 38 → 切 4 刀 ✗（应 2 刀）
```

现在改为对所有相邻 gap 排序、取"最大相邻跳跃点"当分界，
跳跃不明显时退回绝对阈值。四种行距（40/44/52/60）都稳定得到 3 段。

---

## 五、实测数据：翻译降级链

本机真实网络下（无任何密钥）：

```
调用前链: ['google', 'mymemory']
首次 3.44s   engines=['mymemory']
首次 warnings=['google：translate.googleapis.com 在 2 秒内无法建立连接']
调用后链: ['mymemory']   google 冷静期 89s
第 2 次 1.07s（缓存命中时 0.00s）
```

### 5.1 首次调用从 13.9s 降到 3.2s

最初每次调用都要**挂满 HTTP 读超时**才知道 Google 不通。
现在请求前做一次 2 秒 TCP 预检（`translate/http.py: reachable`）：
握手不通就当不可达，直接跳过后端。

### 5.2 按后端冷静期，不是全局"已降级"布尔

第一版用一个全局 `_degraded`：Google 一超时就跳过**所有**在线后端。
本机实测会把已经证明可达的 MyMemory 一起跳掉 —— 这是真实故障，
因为这台机器恰好就是"Google 必挂、MyMemory 必通"。

现在按后端记账（网络 90s / 配额 300s / 认证 600s / 引擎异常 60s），
并把**上次成功的后端提到链首**，所以超时只付一次代价。
设置页保存或用户点"重试"时清空冷静期 —— 否则修好配置还要干等 90 秒。

### 5.3 条数守恒：错位比翻译差更严重

双语对照一旦行错位，整列都不可信。所以：

* 后端返回条数 ≠ 输入条数 → **整批丢弃**，绝不截取前 N 条；
* 第一个后端只译好一部分 → 第二个后端**只补失败的那几条**，
  且写回时用**原始下标**（不是 batch 内下标）；
* 失败条目保留原文 + 显式标注失败原因，不静默删除。

最后一条有个反例值得说：`main.py --file` 中英混排文档时，
三个**不同的句子**曾被并成一段（因为段间空隙不够大），
修好自适应阈值后正确分成 4 段。这类问题只有真跑真实图片才会发现。

### 5.4 不用 requests

本机 `import requests` 直接失败（`No module named 'urllib3'`）——
Anaconda 环境里 requests/urllib3 版本撕扯很常见。
翻译层只需要 GET + 表单 POST + JSON 解析，全部用标准库 `urllib` 实现，
错误分型（Network / Quota / Auth / Engine）收在 `translate/http.py` 一处。

---

## 六、目录结构

```
ocr-translator/
├── main.py                     入口：GUI / --selftest / --file / --capture
├── requirements.txt
├── socrans.spec                PyInstaller 配置
├── build_exe.bat               单文件打包（venv + pip 路线）
├── build_dir.bat               目录式打包（启动更快）
├── tools/build_exe.ps1         免 pip 一键打包（PYTHONPATH 注入 vendor312）
├── run.bat                     免安装源码启动
├── run_admin.bat               以管理员身份启动（自提权 UAC，仅数据目录全坏时用）
├── ocrtranslator/
│   ├── bootstrap.py            依赖目录 / Qt 插件 / DPI / 线程 / 临时目录
│   ├── config.py               JSON 配置 + 环境变量优先的密钥 + 原子写
│   ├── doctor.py               启动前置检查（--doctor 的实现）
│   ├── models.py               数据类（纯 Python，可安全跨线程）
│   ├── languages.py            语言代码映射 + 极简语向检测
│   ├── logger.py               滚动文件日志 + 内存环形缓冲（UI 读）
│   ├── layout.py               行带 → 分栏 → 段落 → 块分类
│   ├── export.py               TXT/MD/DOCX/CSV/HTML/RTF/JSON + 文件名清洗
│   ├── app.py                  流程编排（Worker 线程 / 取消 / 复用）
│   ├── capture/
│   │   ├── grab.py             多屏抓屏 + DPR 精确裁剪
│   │   ├── overlay.py          框选覆盖层 + 像素放大镜
│   │   └── hotkey.py           Win32 RegisterHotKey（非 Windows 回落 pynput）
│   ├── ocr/
│   │   ├── base.py             引擎接口 + RGBA→numpy
│   │   ├── preprocess.py       实测策略（放大/插值/行高估计/空格修复）
│   │   ├── rapid.py            默认引擎
│   │   ├── paddle.py           可选（兼容 2.x/3.x 三种返回结构）
│   │   ├── tesseract.py        兜底（逐词重组行）
│   │   └── __init__.py         注册表 + auto 选择
│   ├── translate/
│   │   ├── http.py             urllib 原语 + TCP 预检 + 错误分型
│   │   ├── base.py             后端接口 + 分批 + 批量拆分对齐
│   │   ├── cache.py            LRU + TTL，键含实际后端名
│   │   ├── google_web.py / deepl.py / baidu.py / mymemory.py / opus.py
│   │   └── __init__.py         降级链 + 冷静期 + 条数守恒
│   └── ui/
│       ├── theme.py            QSS 生成 + 明暗解析
│       ├── result_window.py    双语对照（三种布局 + 悬停联动 + 原始行）
│       ├── settings.py         六页设置 + 每页"测试"按钮
│       ├── hub.py              主窗口（状态行+提示日志）+ 应用内气泡提示
│       └── tray.py             托盘 + 程序化图标（零资源文件）
├── tools/
│   ├── launcher.py             解释器自动挑选（run.bat 的实际入口）
│   ├── fetch_wheels.py         受限环境免 pip 装依赖 / 离线铺包
│   ├── check_bindings.py       作用域级"未绑定名字"检查（含自检）
│   ├── check_spec.py           用桩执行 spec，验证模型文件真被收集
│   ├── verify_pipeline.py      端到端离线验证（生产代码路径）
│   ├── render_ui.py            离屏渲染各布局到 PNG
│   ├── runtime_hook.py         PyInstaller 启动钩子
│   └── experiment_*.py         预处理实验（第 3 节数据来源）
└── tests/                      124 项单元测试
```

---

## 七、关键设计决策

| 决策 | 理由 |
|---|---|
| 数据类不用 Qt 对象 | Worker 线程构造、跨线程传递；QImage 隐式共享数据不加锁 |
| OCR session 加 `RLock` | onnxruntime 同一 session 并发推理会**在 C++ 层 segfault**，Python 栈看不到 |
| 结果用"一个滚动容器 + 每行卡片" | 两栏各自滚动时行高不同必然**逐渐错位** |
| 图标全用 QPainter 画 | 零资源文件 = 消除 `sys._MEIPASS` vs `__file__` 这一整类打包故障 |
| 全局热键用 Win32 `RegisterHotKey` | `keyboard` 库要管理员权限且 hook 所有进程（含密码框） |
| 热键被占用自动换备用组合 | QQ/微信/Snipaste 抢 `Ctrl+Shift+A` 极常见；静默失败的用户体验是"程序坏了"（用户原话）。换键后气泡+主窗口+托盘三处告知实际生效的组合 |
| 单实例用 Global\\ 命名互斥量 + 文件锁双闸 | 文件锁依赖 app_dir —— 管理员/沙箱跑过的实例数据目录被重定向，两个实例互相看不见对方的锁，却会抢同一组热键（本机实测"要提权才能用"的真实根因）。内核互斥量全机器可见、进程死掉 OS 自动释放，无残留锁问题 |
| 配置原子写 + 损坏自动备份 | 用户正按热键等结果，配置解析失败不能让程序退出 |
| 密钥：环境变量 > 混淆配置 | 避免明文 key 被顺手 commit；同时明确这不是安全边界 |
| 数据目录探测可写性并回退 | 本机 `%APPDATA%` 就被 ACL 拒绝过；程序必须能起 |
| 截图前隐藏自己的窗口 | 否则把结果窗的译文 OCR 进去，形成"翻译翻译"套娃 |
| 确认选区**不抓实屏** | confirm 时主窗口已被还原，实抓会截进残影；改从 start() 时冻结的干净拼图裁剪（`capture_from_image`），且选区期间画面冻结、所见即所选 |
| 抓屏前等一拍（settle 200ms） | `hide()` 是异步请求，DWM 合成器随后才更新画面——返回后立刻抓屏，正在消失的窗口像素被定格进冻结背景（真机实测残影）；hide 与抓屏之间等 200ms 让桌面真正画完 |
| 主窗口用 `Window` 旗标而非 QDialog | `hide_app_windows` 抓屏前只隐藏 `windowType==Window(1)` 的顶层窗口；QDialog(3) 会被跳过 → 主窗口内容被截进 OCR 输入 |
| 选区矩形按"坐标排序后双 QPoint 角点构造" | `QRect(p1, p2)` 按角点原样存储，只有"从右往左拖"恰好是正序，其余三个方向 width/height 为负、选区画不出来（实测"只能从右往左拖"）；也不能用 `.normalized()` 兜底——它对反序角点会丢四角各 1px，左右方向选区差 2px |
| 遮罩用 4 个矩形挖洞 | `setClipPath` + 半透明层在 Per-Monitor DPI v2 下有 1px 漏光 |

### 为什么不做"单击智能选窗口"

Windows 上要靠 `WindowFromPoint` + DWM 边框推断，跨平台就废了；
而纯像素扫描找文字行在大图上要几十毫秒，会**卡住鼠标移动**。
改为拖拽 + 像素级放大镜，可控性和手感都更好。

---

## 八、异常处理清单

| 场景 | 行为 |
|---|---|
| 选区没有文字 | 明确提示"可能是纯图片/字号过小/选区太小"，不弹空窗 |
| OCR 引擎缺失 | 提示可执行的安装命令；`auto` 自动换下一个引擎 |
| Paddle 模型未缓存 | 直接跳过（首次识别要下载 30s，不可接受） |
| 网络不可达 | 2s TCP 预检 → 跳过后端 → 冷静期 90s → 降级到离线 |
| 配额耗尽 | 识别为 quota，提示查用量，冷静期 300s |
| 密钥错误 | 识别为 auth，提示重填，**保存设置时立即清空冷静期** |
| 后端返回条数不符 | 整批丢弃（宁缺勿错位） |
| 单条翻译失败 | 保留原文 + 显式标注原因，不拖垮整批 |
| 后端内部异常 | 归一成 TranslationError，不穿透到 GUI 线程 |
| 写文件目标名被占用 | 自动追加 `(1)`；目标是同名**目录**时提前拒绝 |
| 缺 python-docx | 自动降级导出 RTF（Word 能开）并说明 |
| `%TEMP%` 不可写 | 探测后改用项目内目录并记日志 |
| 重复启动 | 实例锁 + 明确提示"已在运行（PID）" |
| 任何未预期异常 | 落盘日志 + 可见提示（GUI 程序不能只 print） |

---

## 九、离线 / 受限环境安装

```bash
# 1) 有网但 pip 装不进（临时目录被 ACL 拒绝等）
python tools/fetch_wheels.py            # wheel 解包到 vendor/，只需写权限
python main.py                          # bootstrap 自动识别 vendor/

# 2) 完全离线的目标机
python tools/fetch_wheels.py --download-only    # 只下载 .whl 到 .wheels/
# 拷 .wheels/ 过去，然后：
pip install --no-index --find-links .wheels -r requirements.txt
```

`fetch_wheels.py` 会按当前解释器**精确匹配 wheel tag**（含 `cp37-abi3` 这类
向后兼容的 stable ABI），只接受 wheel、不接受 sdist（sdist 需要编译，
失败信息会误导）。`bootstrap.install()` 逐个候选试 import，
版本不兼容的 vendor 目录会被自动跳过并试下一个 —— 避免
"DLL load failed" 这种与真实原因（Python 小版本不匹配）毫无关系的报错。

---

## 十、打包

```bash
build_exe.bat     # dist\截屏翻译.exe  单文件，~200MB，冷启动 3-8s
build_dir.bat     # dist\截屏翻译\     启动快、便于替换资源、可加壳
```

受限环境（pip 被策略挡、建不了 venv）用免 pip 的一键脚本，它把
`vendor312/`（`tools/fetch_wheels.py` 解包的 wheel）以 PYTHONPATH 注入
构建，PyInstaller 同样解包自 vendor312：

```powershell
pwsh -File tools\build_exe.ps1 -Onedir   # 目录式（推荐：启动零解包，热键响应快）
pwsh -File tools\build_exe.ps1           # 单文件（分发简单，首次启动解包 3-8s）
```

两者都走同一份 `socrans.spec`（onedir 由环境变量 `SOCRANS_ONEDIR=1` 切换）。
打包前会先跑 `--selftest`，并可用 `python tools/check_spec.py`
在不装 PyInstaller 的情况下验证 spec 逻辑（含"3 个 .onnx 模型是否真被收集"）。

**控制台模式（console=True）**：`--file/--doctor/--selftest` 的输出要在 pwsh
可见，所以 exe 保留控制台；双击启动 GUI 时多余的黑窗由 main.py 的
`_detach_console()`（FreeConsole + 输出重定向到 console.log）收掉，
从 pwsh 带参数启动时控制台保留。

四个必须显式处理的坑（已写进 `socrans.spec`、`tools/runtime_hook.py` 与
`main.py`）：

1. **rapidocr 的 .onnx 是 package-data**，PyInstaller 默认只收 .py。
   漏掉的话报错发生在"按下热键之后"，最难定位。
2. **不要自己 `collect_data_files("PyQt6")`** —— Qt 插件由 PyInstaller 自带 hook 收集，
   重复收集会得到两份插件目录，运行时报 "Could not find the Qt platform plugin windows"。
3. **windowed 模式 stdout/stderr 是 None**，任何启动期异常都会静默消失。
   runtime hook 把它们接到日志文件。
4. **FreeConsole 之后原 stdout 句柄已死**——日志文件开不出来时必须兜一个
   可写替代品，否则后续 print() 抛 OSError 直接砸进 _fatal（双击变"崩溃弹窗"）。

`upx=False`：UPX 压缩 onnxruntime/opencv 的 DLL 会破坏其导出表。

**构建解释器必须钉死 3.12**：`vendor312/` 的 wheel 组合按 cp312 精选，但
PyQt6-sip/onnxruntime/PyYAML 是 cp313 ABI —— 在 Anaconda 3.13 下"恰好也能
import"，依赖探针全绿，PyInstaller 却会把 Anaconda 的 `python313.dll`
打进包，产物运行即 c0000005 秒崩（Windows 事件日志"错误模块名称
python313.DLL"实证）。所以 `build_exe.ps1` 现在只接受 3.12 解释器，
否则直接报错退出（`-PyPath` 显式指定最稳）。

### 为什么"启动要管理员权限"——以及为什么其实不需要

程序设计上**不需要管理员**：配置/日志/模型缓存全部写进每用户可写目录
（候选链：`SOCRANS_HOME` → `%APPDATA%\socrans` → `~/.socrans` → exe 旁
`.socrans` → `%TEMP%`，逐个 mkdir + 写探测文件）。"必须提权才能启动"
几乎总是因为**数据目录曾被"以管理员身份运行"创建过**，ACL 归
Administrators 所有，普通权限从此写不进去，且一级级连锁污染。

处理方式（三层）：

1. `--doctor` 会报告数据目录逐个候选的失败原因与 4 条修复建议
   （删旧目录重建 / `icacls` 授权 / `SOCRANS_HOME` / `run_admin.bat`）；
2. 数据目录一路坏到临时目录时，GUI 启动早期弹窗提供**一键提权重启**
   （`ShellExecuteW "runas"`，标准 UAC；"不再提示"可跳过）；
3. `run_admin.bat` 自提权包装（`net session` 探测 → PowerShell
   `Start-Process -Verb RunAs` 重启自身），放在 dist 旁双击即用。

正确的长期修法是第 1 层：删掉被污染的目录（`%APPDATA%\socrans`），让
普通权限启动时重建 —— UAC 每次都要点，能不用就不用。

**OPUS-MT（离线翻译）默认不进包**：torch + transformers + 模型是 GB 级，
且 PyInstaller 打 argos-translate 极难。包里 opus 后端会如实报告
"未安装：pip install argos-translate"。要离线翻译请用源码方式运行并
安装 argos-translate（外置，不内嵌）。

---

## 十一、测试

```bash
python -m unittest discover -s tests -p "test_*.py"   # 124 项
python tools/verify_pipeline.py                        # 端到端（真实 OCR）
python tools/render_ui.py                              # 各布局渲染成 PNG
python tools/check_bindings.py --self-test             # 检查器本身的正/负对照
python tools/check_bindings.py                         # 作用域级未绑定名字
```

测试重点覆盖**降级链与下标映射**（全部用假后端，不依赖网络状态）：
部分失败时的补译下标、条数不符整批丢弃、缓存按后端隔离、
冷静期按错误类型分级、preferred 提前、真实 AUTO_CHAIN 顺序。

`check_bindings.py` 有个值得记的教训：第一版把整个文件的所有绑定名
收集成一个集合，于是 `run_gui` 里 import 的 `Qt` 让**同文件另一个函数**里的
`Qt` 被误判为已绑定 —— 检查器报"0 处问题"，程序照样 `NameError`。
现在按作用域判断（含"类作用域对其方法不可见"这条 CPython 语义），
并用一份含 4 个已知缺陷 + 14 个合法用名的对照文件自检。

---

## 十二、许可与合规

* 代码：MIT。
* PP-OCR 模型：Apache-2.0。PaddleOCR：Apache-2.0。Tesseract：CPML。
* **Google 网页端点是非官方接口**，不保证稳定，也可能限流。
  商用请改用 Cloud Translation API 或 DeepL/百度（都有正式条款）。
* 隐私说明（OCR 全程本地、只上传文本、离线方案）见
  [第一部分 §5](#5-隐私)。
