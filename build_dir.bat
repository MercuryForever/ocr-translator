@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  目录式打包：dist\截屏翻译\截屏翻译.exe
REM  比单文件快得多（不用每次解包 100+MB），体积也小一些，
REM  并且方便替换 OCR 模型 / 加图标 / 做增量更新。
REM  要分发就整个文件夹压缩成 zip。
REM ============================================================

cd /d "%~dp0"

if not exist .venv-build (
    echo 创建虚拟环境...
    python -m venv .venv-build || (echo 失败 & pause & exit /b 1)
)
set "PY=%~dp0.venv-build\Scripts\python.exe"

echo 安装依赖...
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PY%" -m pip install -r requirements.txt pyinstaller || (pause & exit /b 1)

echo 自检...
"%PY%" main.py --selftest

echo 打包（--onedir，走 socrans.spec 保留模型/hiddenimports 配置）...
set SOCRANS_ONEDIR=1
"%PY%" -m PyInstaller --noconfirm --clean socrans.spec
if errorlevel 1 (echo 打包失败 & pause & exit /b 1)

echo.
echo 完成：dist\截屏翻译\截屏翻译.exe
echo 分发时把整个 dist\截屏翻译 目录打包成 zip。
pause
endlocal
