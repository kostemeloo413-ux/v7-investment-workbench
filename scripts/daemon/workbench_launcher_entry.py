# -*- coding: utf-8 -*-
"""
scripts/daemon/workbench_launcher.py
交易工作台.exe 的入口（PyInstaller 打包用）

职责（用户 2026-08-29：代理看门狗+工作台合一启动）：
  1. 拉起 GeminiProxyWatchdog.exe（进程查重，绝不删改原exe）；
  2. 等代理端口就绪（≤10秒，不通也继续——daemon 内 LLM 有诚实降级）；
  3. 以 pythonw 拉起 tray_launcher.py（托盘+daemon+watchdog）；
  4. 自身退出（托盘常驻在 pythonw 进程里）。

打包：pyinstaller --onefile --noconsole --name 交易工作台 workbench_launcher_entry.py
"""
import os
import sys
import time


def app_base():
    if getattr(sys, 'frozen', False):          # PyInstaller 打包后：exe 所在目录=项目根
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    base = app_base()
    pyw = os.path.join(base, '.venv', 'Scripts', 'pythonw.exe')
    tray = os.path.join(base, 'scripts', 'daemon', 'tray_launcher.py')

    # 1) 代理看门狗（查重逻辑在 tray_launcher 内，这里提前拉起让 LLM 尽快可用）
    wd = os.environ.get('PROXY_WATCHDOG_EXE', '') or \
        os.path.join(os.path.expanduser('~'), 'Documents', 'GeminiProxyWatchdog.exe')
    if not os.path.exists(wd):
        wd = r'C:\watchdog\GeminiProxyWatchdog.exe'
    if os.path.exists(wd):
        import subprocess
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq GeminiProxyWatchdog.exe'],
                           capture_output=True, text=True)
        if 'GeminiProxyWatchdog.exe' not in (r.stdout or ''):
            flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([wd], creationflags=flags, cwd=os.path.dirname(wd))
        # 2) 等代理端口（LLM 默认 8317；不通也继续）
        import socket
        for _ in range(10):
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect(('127.0.0.1', 8317))
                s.close()
                break
            except Exception:
                s.close()
                time.sleep(1)

    # 3) 拉起托盘（托盘负责 daemon+watchdog+健康图标）
    if os.path.exists(pyw) and os.path.exists(tray):
        flags = 0x00000008 | 0x00000200
        import subprocess
        subprocess.Popen([pyw, '-X', 'utf8', tray], cwd=base, creationflags=flags)


if __name__ == '__main__':
    main()
