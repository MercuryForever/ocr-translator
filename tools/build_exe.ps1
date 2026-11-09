# 一键打包脚本（PowerShell）。
#
# 用法：
#   pwsh -File tools\build_exe.ps1              # 单文件 dist\截屏翻译.exe
#   pwsh -File tools\build_exe.ps1 -Onedir      # 目录式 dist\截屏翻译\（推荐分发）
#   pwsh -File tools\build_exe.ps1 -PyPath "..."  # 指定构建解释器
#
# 依赖解析两种模式（按顺序尝试）：
#   1. 构建解释器已 import 得动全部依赖（pip install 过）→ 直接用；
#   2. 否则回退 PYTHONPATH=<repo>\vendor312（tools\fetch_wheels.py 解包的 wheel，
#      免 pip 的内网/受限环境）。PyInstaller 的 Analysis 要求依赖可导入，
#      这一步是打包成败的分水岭，缺失时立刻报错而不是等 10 分钟编译后炸。
param(
    [switch]$Onedir,
    [string]$PyPath = "",
    [switch]$SkipSelftest
)
# ⚠ 不能设 Stop：PS 5.1 下原生命令（python 探针）写 stderr 会变成
#   终止错误，"探测 PyInstaller 是否存在" 这种预期失败直接杀掉脚本。
#   本脚本所有失败点都显式 exit 1，Continue 是正确选择。
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repo

# ---- 1. 选解释器 ---------------------------------------------------
# ⚠ 必须硬性钉在 Python 3.12：vendor312/ 的 wheel 组合是按 cp312 精选的，
#   其中 PyQt6-sip/onnxruntime/PyYAML 是 cp313 ABI —— 在 Anaconda 3.13 下
#   "恰好也能 import"，探针全绿，但 PyInstaller 会把 Anaconda 的
#   python313.dll 打进包，运行时 c0000005 秒崩（Windows 事件日志
#   "错误模块名称 python313.DLL" 实证）。所以：
#   a) 候选顺序把已知可用的 DSH 3.12 运行时放最前；
#   b) 选中的解释器版本不是 (3,12) 就直接报错退出，绝不静默用错。
if (-not $PyPath) {
    foreach ($cand in @("C:\Users\Mercury\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
                        "python")) {
        try {
            $v = & $cand -c "import sys; print(sys.version_info[:2])" 2>$null
            if ($v -match '\(3, 12\)') { $PyPath = $cand; break }
        } catch { }
    }
}
if (-not $PyPath) { Write-Host "找不到 Python 3.12，用 -PyPath 指定（vendor312 按 cp312 精选，其他小版本不支持）"; exit 1 }
$pyver = (& $PyPath -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Select-Object -Last 1).Trim()
if ($pyver -ne "3.12") {
    Write-Host "构建解释器必须是 3.12（vendor312 按 cp312 精选），当前 $PyPath 是 $pyver。"
    Write-Host "用 -PyPath 指定 3.12 解释器。"
    exit 1
}
Write-Host "[1/4] 构建解释器: $PyPath ($pyver)"

# ---- 2. 依赖可导入性 + vendor 注入 ---------------------------------
# ⚠ 探针经 stdin 喂给 Python，不要写 `python -c $probe`：
#   PS 5.1 给原生进程传参时会重新转义内嵌双引号，Python 收到的
#   `mods=["PyQt6",...]` 引号碎掉 → File "<string>" SyntaxError。
$probe = 'import importlib.util as u; mods=["PyQt6","cv2","numpy","PIL","yaml","docx","shapely","pyclipper","onnxruntime","rapidocr_onnxruntime"]; missing=[m for m in mods if u.find_spec(m) is None]; print(",".join(missing))'
$missing = ($probe | & $PyPath - 2>$null | Select-Object -Last 1).Trim()
# ⚠ import 成功时无任何 stdout —— `$null -ne (无输出)` 恒为 False，
#   成功会被误判成缺失。必须让探针打印标记再匹配。
$probePI = "import PyInstaller; print('pyinstaller-ok')"
$hasPyInstaller = (($probePI | & $PyPath - 2>$null | Select-Object -Last 1) -match 'pyinstaller-ok')
if ($missing) {
    $vendor = Join-Path $repo "vendor312"
    if (-not (Test-Path $vendor)) { Write-Host "依赖缺失 [$missing] 且无 vendor312/；先 pip install 或运行 tools\fetch_wheels.py"; exit 1 }
    $env:PYTHONPATH = $vendor
    $re = ($probe | & $PyPath - 2>$null | Select-Object -Last 1).Trim()
    if ($re) { Write-Host "vendor312 注入后仍缺: $re"; exit 1 }
    Write-Host "[2/4] 依赖经 PYTHONPATH=vendor312 注入（免 pip 模式）"
} else {
    Write-Host "[2/4] 依赖已在解释器环境中"
}
if (-not $hasPyInstaller -and $env:PYTHONPATH) {
    $hasPyInstaller = (($probePI | & $PyPath - 2>$null | Select-Object -Last 1) -match 'pyinstaller-ok')
}
if (-not $hasPyInstaller) {
    Write-Host "缺 PyInstaller：pip install pyinstaller 或 python tools\fetch_wheels.py --dest vendor312 --only pyinstaller --only pyinstaller-hooks-contrib --only altgraph --only pefile --only pywin32-ctypes --only packaging --only setuptools"
    exit 1
}

# ---- 3. 自检 -------------------------------------------------------
if (-not $SkipSelftest) {
    Write-Host "[3/4] 打包前自检（[x] 未过会打出坏包，[!] 是可选环境缺口）"
    & $PyPath main.py --selftest
    if ($LASTEXITCODE -ne 0) { Write-Host "自检有未过关键项，中止（-SkipSelftest 可跳过）"; exit 1 }
} else { Write-Host "[3/4] 跳过自检" }

# ---- 4. PyInstaller ------------------------------------------------
if ($Onedir) { $env:SOCRANS_ONEDIR = "1" } else { Remove-Item Env:SOCRANS_ONEDIR -ErrorAction SilentlyContinue }
Write-Host "[4/4] PyInstaller 打包中（5-15 分钟）..."
& $PyPath -m PyInstaller --clean --noconfirm socrans.spec
if ($LASTEXITCODE -ne 0) { Write-Host "打包失败，见上方日志"; exit 1 }

$art = if ($Onedir) { "dist\截屏翻译" } else { "dist\截屏翻译.exe" }
if (Test-Path $art) {
    $size = if ($Onedir) {
        "{0:N0} MB" -f ((Get-ChildItem $art -Recurse | Measure-Object Length -Sum).Sum / 1MB)
    } else {
        "{0:N0} MB" -f ((Get-Item $art).Length / 1MB)
    }
    Write-Host "`n完成：$art  ($size)"
} else {
    Write-Host "未找到产物 $art"
    exit 1
}
