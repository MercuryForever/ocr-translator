@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  打单文件 exe（dist\截屏翻译.exe，约 200MB）
REM  想更快启动 / 更小体积，用 build_dir.bat
REM ============================================================

cd /d "%~dp0"

echo [1/5] 检查 Python...
python -c "import sys; assert sys.version_info >= (3,10), sys.version" 2>nul
if errorlevel 1 (
    echo 需要 Python 3.10+，且 python 命令可用。
    pause & exit /b 1
)

echo [2/5] 创建虚拟环境 .venv-build ...
if not exist .venv-build (
    python -m venv .venv-build || (echo 创建 venv 失败 & pause & exit /b 1)
)
set "PY=%~dp0.venv-build\Scripts\python.exe"

echo [3/5] 安装依赖（首次约 5-10 分钟）...
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo 依赖安装失败。若是网络受限，见 README「离线安装」。
    pause & exit /b 1
)

echo [4/5] 自检（打包前先确认 OCR 真能识别）...
"%PY%" main.py --selftest
if errorlevel 1 (
    echo.
    echo 自检有未通过项。上面标 [x] 的是关键项，标 [!] 的可选。
    choice /c YN /m "仍要继续打包吗"
    if errorlevel 2 pause & exit /b 1
)

echo [5/5] PyInstaller 打包...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
"%PY%" -m PyInstaller --clean --noconfirm socrans.spec
if errorlevel 1 (
    echo 打包失败。常见原因见 README 的「打包故障排查」。
    pause & exit /b 1
)

echo.
echo ============================================================
if exist "dist\截屏翻译.exe" (
    echo 成功：dist\截屏翻译.exe
    for %%A in ("dist\截屏翻译.exe") do echo 体积：%%~zA 字节
) else (
    echo 未找到产物，请检查 dist\ 目录
)
echo.
echo 提示：单文件 exe 首次启动要解包 100+MB 到临时目录，
echo       冷启动 3-8 秒属正常。介意请用 build_dir.bat。
echo ============================================================
pause
endlocal
