# -*- coding: utf-8 -*-
"""
scripts/daemon/watchdog.py
外置独立看门狗（阶段一改造核心）

为什么必须独立进程：旧"都察院"与被监控的流水线同生共死，流水线死了它也不会响。
本脚本与 daemon 互为独立进程，互相通过 state/*.json 的活体时间戳监察：

- 每 30s 检查 state/daemon_alive.json：
    超过 HEARTBEAT_STALE_MINUTES 未更新 → 视为假死/退出
      ①企微 force 告警（最多 PUSH_FORCE_LIMIT 条/天）
      ②taskkill 旧进程 → 用项目 venv 重新拉起 trading_daemon.py（每日≤3次防风暴）
  心跳恢复后静默复位。
- 同时自身每轮写 state/watchdog_alive.json，daemon 端如检测它缺失可反向拉活
  （daemon 侧实现从简，不做自愈重启）。

启动方式（由托盘启动器拉起，也可手动）：
    .venv\\Scripts\\pythonw.exe scripts\\daemon\\watchdog.py
"""

import os
import sys
import json
import time
import subprocess
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)

import runtime_config as rc

CHECK_INTERVAL = 30
RESTART_BUDGET_PER_DAY = 3


def acquire_singleton(name):
    """单例守卫（OS级文件锁）：state/<name>.lock 上的排他字节锁由本进程持有，
    进程存活期间第二实例的加锁尝试必然失败 → 直接退出。
    进程崩溃/退出时操作系统自动释放锁，无陈旧锁问题。"""
    import msvcrt
    lock_path = os.path.join(rc.STATE_DIR, f'{name}.lock')
    os.makedirs(rc.STATE_DIR, exist_ok=True)
    f = open(lock_path, 'a+b')
    f.seek(0)
    try:
        # Overlap legacy EOF-offset locks without stopping the current watchdog.
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 16)
    except OSError:
        f.close()
        log(f'检测到已运行实例（{name}.lock 被持有），本次启动直接退出')
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()).encode('utf-8'))
    f.flush()
    return f


_SINGLETON_FD = None   # 持有句柄防GC提前释放锁


def log(msg):
    line = f"[watchdog {rc.now_str()}] {msg}"
    print(line)
    try:
        os.makedirs(rc.STATE_DIR, exist_ok=True)
        with open(os.path.join(rc.STATE_DIR, 'watchdog.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _mark_self_alive():
    rc._save_json(rc.WATCHDOG_ALIVE_PATH,
                  {'pid': os.getpid(), 'ts': rc.now_str(), 'epoch': time.time()})


def _restart_budget_left():
    path = os.path.join(rc.STATE_DIR, 'watchdog_restarts.json')
    st = rc._load_json(path, {})
    today = datetime.date.today().strftime('%Y-%m-%d')
    if st.get('date') != today:
        st = {'date': today, 'count': 0}
    return st, RESTART_BUDGET_PER_DAY - int(st.get('count', 0))


def _record_restart():
    st, left = _restart_budget_left()
    st['count'] = int(st.get('count', 0)) + 1
    rc._save_json(os.path.join(rc.STATE_DIR, 'watchdog_restarts.json'), st)
    return RESTART_BUDGET_PER_DAY - st['count']


def _kill_pid(pid):
    try:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                       capture_output=True, timeout=10)
        log(f'已 taskkill 旧守护进程 pid={pid}')
    except Exception as e:
        log(f'taskkill 失败 pid={pid}: {e}')


def _spawn_daemon():
    py = os.path.join(_BASE, '.venv', 'Scripts', 'pythonw.exe')
    if not os.path.exists(py):
        py = os.path.join(_BASE, '.venv', 'Scripts', 'python.exe')
    flags = 0
    if hasattr(subprocess, 'DETACHED_PROCESS'):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [py, '-X', 'utf8', os.path.join(_BASE, 'scripts', 'daemon', 'trading_daemon.py'),
         '--no-browser'],
        cwd=_BASE, creationflags=flags,
        stdout=open(os.path.join(rc.STATE_DIR, 'daemon_stdout.log'), 'ab'),
        stderr=subprocess.STDOUT)
    log(f'已重新拉起守护进程 pid={proc.pid}')
    return proc


def main():
    global _SINGLETON_FD
    _SINGLETON_FD = acquire_singleton('watchdog')
    if _SINGLETON_FD is None:
        sys.exit(0)
    log(f'看门狗启动 pid={os.getpid()} (检查间隔{CHECK_INTERVAL}s, '
        f'心跳阈值{rc.HEARTBEAT_STALE_MINUTES}min, 每日重启预算{RESTART_BUDGET_PER_DAY})')
    warned_stale = False
    while True:
        _mark_self_alive()
        alive = rc._load_json(rc.ALIVE_PATH, {})
        stale_min = None
        if alive.get('epoch'):
            stale_min = (time.time() - float(alive['epoch'])) / 60
        else:
            stale_min = 9999       # 从未见心跳：视为未运行

        if stale_min <= rc.HEARTBEAT_STALE_MINUTES:
            warned_stale = False
            time.sleep(CHECK_INTERVAL)
            continue

        # ---- 异常状态 ----
        old_pid = alive.get('pid')
        if not warned_stale:
            from notify.wecom_bot import push
            ok, detail = push(
                f'⚠️ 守护进程异常: 超过{rc.HEARTBEAT_STALE_MINUTES:.0f}分钟无心跳',
                [f'时间: {rc.now_str()}', f'最后心跳: {alive.get("ts")}',
                 f'pid={old_pid}',
                 '正在尝试自动恢复…详情见工作台健康页'],
                level='warn', force=True)
            log(f'告警推送: {ok} {detail}')
            warned_stale = True

        st, left = _restart_budget_left()
        if left > 0:
            if old_pid:
                _kill_pid(old_pid)
                time.sleep(2)
            _spawn_daemon()
            used = _record_restart()
            log(f'本轮已用重启次数 {used}/{RESTART_BUDGET_PER_DAY}')
            time.sleep(90)     # 给新进程起来的时间
        else:
            log(f'重启预算耗尽({RESTART_BUDGET_PER_DAY}/天)，仅保留告警，等人工介入')
            time.sleep(300)
    # unreachable


if __name__ == '__main__':
    main()
