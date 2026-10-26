# -*- coding: utf-8 -*-
"""
scripts/daemon/tray_launcher.py
托盘一键启动器：双击 bat 后看到的那个"程序"

功能：
- 拉起 daemon（含 Web 工作台）与 watchdog 两个子进程；
- Windows 托盘图标实时着色：绿=心跳正常 / 红=异常或未运行；
- 托盘右键菜单：打开工作台 · 手动跑一轮流水线 · 重启守护 · 全部退出。

依赖 pystray + Pillow（已随 .venv 安装）。无桌面环境时(如服务器)会自动降级为
纯控制台守护模式。
"""

import os
import sys
import time
import subprocess
import threading

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)

import runtime_config as rc

VENV_PYW = os.path.join(_BASE, '.venv', 'Scripts', 'pythonw.exe')
VENV_PY = os.path.join(_BASE, '.venv', 'Scripts', 'python.exe')
DAEMON_SCRIPT = os.path.join(_BASE, 'scripts', 'daemon', 'trading_daemon.py')
WATCHDOG_SCRIPT = os.path.join(_BASE, 'scripts', 'daemon', 'watchdog.py')

# 代理看门狗（用户 2026-08-29：与工作台合一启动；只启动/停止，绝不删改原exe）
PROXY_WD_NAME = 'GeminiProxyWatchdog.exe'
_PROXY_WD_CANDIDATES = [
    os.environ.get('PROXY_WATCHDOG_EXE', ''),
    os.path.join(os.path.expanduser('~'), 'Documents', PROXY_WD_NAME),
    r'C:\watchdog\GeminiProxyWatchdog.exe',
]
PROXY_WD_EXE = next((p for p in _PROXY_WD_CANDIDATES if p and os.path.exists(p)), '')

procs = {'daemon': None, 'watchdog': None}
_proxywd_spawned_by_us = False   # 只清理自己拉起的；用户手动开的不动


def proxywd_running():
    r = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {PROXY_WD_NAME}'],
                       capture_output=True, text=True)
    return PROXY_WD_NAME.lower() in (r.stdout or '').lower()


def start_proxy_watchdog():
    """查重启动代理看门狗；原exe只读执行。"""
    global _proxywd_spawned_by_us
    if not PROXY_WD_EXE:
        print('[tray] 未找到 GeminiProxyWatchdog.exe（.env 可配 PROXY_WATCHDOG_EXE），跳过')
        return False
    if proxywd_running():
        print('[tray] 代理看门狗已在运行，不重复启动')
        return True
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([PROXY_WD_EXE], creationflags=flags,
                     cwd=os.path.dirname(PROXY_WD_EXE))
    _proxywd_spawned_by_us = True
    print(f'[tray] 代理看门狗已拉起: {PROXY_WD_EXE}')
    return True


def stop_proxy_watchdog():
    """仅当看门狗是本托盘拉起的才停止；用户手动开的不动。"""
    if _proxywd_spawned_by_us and proxywd_running():
        subprocess.run(['taskkill', '/F', '/IM', PROXY_WD_NAME],
                       capture_output=True)
        print('[tray] 代理看门狗已停止（本托盘拉起的）')


def python_exe():
    return VENV_PYW if os.path.exists(VENV_PYW) else sys.executable


def spawn(name, script, extra_args=()):
    flags = 0
    if hasattr(subprocess, 'DETACHED_PROCESS'):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    logf = open(os.path.join(rc.STATE_DIR, f'{name}_launcher.log'), 'ab')
    proc = subprocess.Popen(
        [python_exe(), '-X', 'utf8', script, *extra_args],
        cwd=_BASE, creationflags=flags,
        stdout=logf, stderr=subprocess.STDOUT)
    procs[name] = proc.pid
    return proc


def health_ok():
    try:
        import urllib.request
        with urllib.request.urlopen(
                f'http://127.0.0.1:{rc.DAEMON_PORT}/api/health', timeout=3) as r:
            data = __import__('json').loads(r.read().decode('utf-8'))
        return bool(data.get('healthy'))
    except Exception:
        return False


# ---------------------------------------------------------------- 图标

def make_icon(color):
    from PIL import Image, ImageDraw
    import pystray
    img = Image.new('RGB', (64, 64), color)
    d = ImageDraw.Draw(img)
    d.rectangle([14, 14, 50, 50], outline='white', width=4)
    d.line([22, 40, 32, 28, 44, 44], fill='white', width=4, joint='curve')
    return pystray.Icon('trading_workbench', icon=img, title='交易工作台 启动中…')


_ICON_STATE = {'icon': None, 'green': None, 'red': None, 'color': True}


def poll_health_loop():
    while True:
        ok = health_ok()
        try:
            _ICON_STATE['icon'].icon = _ICON_STATE['green'] if ok else _ICON_STATE['red']
            _ICON_STATE['icon'].title = ('交易工作台 运行中 ✅' if ok
                                         else '交易工作台 异常 ⚠️ (看门狗处理中)')
        except Exception:
            pass
        time.sleep(12)


def menu_open_web(icon, item):
    os.startfile(f'http://127.0.0.1:{rc.DAEMON_PORT}/v7')


def menu_run_pipeline(icon, item):
    import urllib.request
    req = urllib.request.Request(
        f'http://127.0.0.1:{rc.DAEMON_PORT}/api/run/intraday_cycle', method='POST')
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print('[tray] 手动触发失败:', e)


def menu_restart(icon, item):
    for name in ('watchdog', 'daemon'):
        pid = procs.get(name)
        if pid:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True)
    time.sleep(2)
    boot()


def menu_exit(icon, item):
    for name in ('watchdog', 'daemon'):
        pid = procs.get(name)
        if pid:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True)
    stop_proxy_watchdog()
    icon.stop()


def boot():
    start_proxy_watchdog()          # 先拉代理看门狗（LLM依赖），失败不阻塞后续
    if not health_ok():
        spawn('daemon', DAEMON_SCRIPT, ['--no-browser'])
    spawn('watchdog', WATCHDOG_SCRIPT)


def main():
    os.makedirs(rc.STATE_DIR, exist_ok=True)
    try:
        import pystray
    except ImportError:
        # 无GUI环境降级：仅前台跑 daemon，Ctrl+C 退出
        print('[tray] 未安装 pystray，降级为前台守护模式 (Ctrl+C 退出)')
        subprocess.call([sys.executable, '-X', 'utf8', DAEMON_SCRIPT])
        return

    boot()

    _ICON_STATE['green'] = make_icon((16, 185, 129))
    _ICON_STATE['red'] = make_icon((220, 38, 38))
    icon = _ICON_STATE['green']
    _ICON_STATE['icon'] = icon
    icon.menu = pystray.Menu(
        pystray.MenuItem('打开V7工作台', menu_open_web, default=True),
        pystray.MenuItem('手动跑一轮流水线', menu_run_pipeline),
        pystray.MenuItem('重启守护进程+看门狗', menu_restart),
        pystray.MenuItem('全部退出', menu_exit),
    )
    threading.Thread(target=poll_health_loop, daemon=True).start()
    print(f'[tray] 托盘启动完成。V7工作台: http://127.0.0.1:{rc.DAEMON_PORT}/v7')
    icon.run(detach=False)


if __name__ == '__main__':
    main()
