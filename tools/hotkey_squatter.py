"""临时验证工具：占用指定全局热键，用于模拟 QQ/微信 等截图软件。

用法: python hotkey_squatter.py ctrl+shift+a ctrl+shift+z [存活秒数]
注册成功会逐条打印，然后保持存活直到超时或被杀。
"""
import sys
import time
import ctypes
from ctypes import wintypes

MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x0001, 0x0002, 0x0004
VK = {c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}


def parse(combo):
    mods = 0
    key = None
    for part in combo.lower().split("+"):
        part = part.strip()
        if part == "ctrl":
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part == "win":
            mods |= 0x0008
        else:
            key = part
    return mods, VK[key.upper()]


def main():
    combos = sys.argv[1:-1] if sys.argv[-1].isdigit() else sys.argv[1:]
    secs = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 180
    ok = []
    for i, combo in enumerate(combos):
        mods, vk = parse(combo)
        if ctypes.windll.user32.RegisterHotKey(None, i + 1, mods, vk):
            ok.append(combo)
            print(f"OCCUPIED {combo}", flush=True)
        else:
            print(f"FAIL {combo} err={ctypes.GetLastError()}", flush=True)
    print(f"SQUATTER READY ({len(ok)}/{len(combos)})", flush=True)
    time.sleep(secs)
    for i in range(len(ok)):
        ctypes.windll.user32.UnregisterHotKey(None, i + 1)


if __name__ == "__main__":
    main()
