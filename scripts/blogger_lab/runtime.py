# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/runtime.py
V7 博主实验室运行时管理器 (阶段 1 核心)
管理单实例排他文件锁、一次性启动恢复、业务服务门面与单工作器线程池生命周期。
"""

from __future__ import annotations
import os
import sys
import datetime
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Any, Set
from concurrent.futures import ThreadPoolExecutor, Future, wait as futures_wait

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.service import BloggerLabService
from blogger_lab.store import current_time_iso


def acquire_file_lock(lock_path: Path) -> Optional[Any]:
    """
    在指定文件上获取操作系统级排他排他锁。
    Windows 下使用 msvcrt.locking，非 Windows 下使用 fcntl.flock。
    若获取失败返回 None，不阻塞、不抛出未捕获异常。
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "a+b")
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 32)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}:{datetime.datetime.now().isoformat()}".encode("utf-8"))
        f.flush()
        return f
    except (OSError, IOError):
        try:
            f.close()
        except Exception:
            pass
        return None
    except Exception:
        return None


def release_file_lock(lock_file_handle: Any) -> None:
    """释放并关闭文件锁"""
    if lock_file_handle is None:
        return
    try:
        lock_file_handle.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock_file_handle.fileno(), msvcrt.LK_UNLCK, 32)
        else:
            import fcntl
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_file_handle.close()
    except Exception:
        pass


def recover_interrupted_jobs_on_startup(lab_root: Optional[str] = None) -> int:
    """
    启动检查：若 SQLite 中存在处于 RUNNING 状态的任务，如实标记为 INTERRUPTED，
    不假称仍在运行，并允许用户在页面中点击重试。
    返回恢复的任务数量。
    注意：此函数仅由持有排他锁的实例在启动时调用一次，严禁在导入时或每个 HTTP 请求中反复执行。
    """
    db_path = (Path(lab_root) if lab_root else Path(_BASE) / "data" / "blogger_lab") / "lab.sqlite"
    if not db_path.exists():
        return 0
    recovered_count = 0
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            now_iso = current_time_iso()
            cur.execute(
                """
                UPDATE extraction_jobs
                SET status = 'INTERRUPTED',
                    error_message = '服务重启导致任务中断，请点击重试续跑未完成分块',
                    updated_at = ?
                WHERE status = 'RUNNING'
                """,
                (now_iso,)
            )
            recovered_count = cur.rowcount
            cur.execute(
                """
                UPDATE chunks
                SET status = 'FAILED'
                WHERE status = 'RUNNING'
                """
            )
            conn.commit()
    except Exception as e:
        print(f"[BloggerLabRuntime Warning] 检查中断任务失败: {e}")
    return recovered_count


class LabThreadPoolExecutor(ThreadPoolExecutor):
    """
    博主实验室受控线程池：
    跟踪提交的 Future，并在运行时关闭时拒绝接收新任务。
    """
    def __init__(self, runtime: "BloggerLabRuntime", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._runtime = runtime

    def submit(self, fn, *args, **kwargs) -> Future:
        if self._runtime.status == "SHUTDOWN":
            raise RuntimeError("博主实验室运行时已进入关闭状态，拒绝接收新任务")
        future = super().submit(fn, *args, **kwargs)
        self._runtime._track_future(future)
        return future


class BloggerLabRuntime:
    """
    博主实验室生命周期与资源运行时 (阶段 1 核心)
    保证：
    1. 惰性初始化：未启用时不创建任何目录、数据库或线程池；
    2. 单实例排他锁：在 <lab_root>/lab.lock 获取 OS 锁；
    3. 冲突防护：加锁失败置为 LOCKED_CONFLICT，不中断前一实例正在运行的任务；
    4. 一次性恢复：仅在加锁成功后执行一次 INTERRUPTED 任务自愈；
    5. 锁所有权保障与有界优雅退出：
       - shutdown 时停止接收新任务并取消未开始任务；
       - 正在运行的任务完成前坚决保持同根排他锁；
       - 有界等待 timeout_sec 后可返回，若任务仍运行则在任务结束时由后台线程延迟释放排他锁与资源。
    """

    def __init__(self, lab_root: Optional[str] = None):
        self.raw_root = lab_root
        self.lab_root: Optional[Path] = Path(lab_root).resolve() if lab_root else None
        self.lock_handle: Optional[Any] = None
        self.is_locked: bool = False
        self.status: str = "UNINITIALIZED"  # UNINITIALIZED, READY, LOCKED_CONFLICT, CONFIG_ERROR, ERROR, SHUTDOWN
        self.error_message: Optional[str] = None
        self.service: Optional[BloggerLabService] = None
        self.executor: Optional[ThreadPoolExecutor] = None

        self._futures: Set[Future] = set()
        self._futures_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()

    def _track_future(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.add(future)
        future.add_done_callback(self._on_future_done)

    def _on_future_done(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.discard(future)

    def start(self) -> bool:
        """启动运行时并获取单实例所有权"""
        with self._lifecycle_lock:
            if self.status == "READY":
                return True
            if self.status == "SHUTDOWN":
                self.error_message = "运行时已关闭，不可复用"
                return False

            if not self.lab_root:
                self.status = "CONFIG_ERROR"
                self.error_message = "未配置有效的数据根目录 (lab_root 为空)"
                return False

            try:
                self.lab_root.mkdir(parents=True, exist_ok=True)
                lock_path = self.lab_root / "lab.lock"
                self.lock_handle = acquire_file_lock(lock_path)
                if not self.lock_handle:
                    self.status = "LOCKED_CONFLICT"
                    self.is_locked = False
                    self.error_message = f"实验数据根目录已被其他实例锁定 ({self.lab_root})"
                    return False

                self.is_locked = True

                # 仅在取得排他锁后执行恰好一次中断恢复
                recover_interrupted_jobs_on_startup(lab_root=str(self.lab_root))

                # 初始化业务门面与受控单工作器线程池
                self.service = BloggerLabService(lab_root=str(self.lab_root))
                self.executor = LabThreadPoolExecutor(self, max_workers=1, thread_name_prefix="lab_worker")
                self.status = "READY"
                self.error_message = None
                return True
            except Exception as e:
                self.status = "ERROR"
                self.error_message = f"博主实验室运行时启动失败: {e}"
                self._release_resources_internal()
                return False

    def _do_release_resources(self) -> None:
        """幂等释放排他锁与运行时资源"""
        with self._lifecycle_lock:
            self._release_resources_internal()

    def _release_resources_internal(self) -> None:
        """内部实际释放逻辑（在 _lifecycle_lock 保护下执行）"""
        if self.lock_handle:
            release_file_lock(self.lock_handle)
            self.lock_handle = None
            self.is_locked = False
        self.service = None
        if self.executor:
            try:
                self.executor.shutdown(wait=False)
            except Exception:
                pass
            self.executor = None

    def shutdown(self, timeout_sec: float = 2.0) -> None:
        """
        关闭运行时并释放资源：
        1. 停止接收新任务，取消尚未开始的任务；
        2. 仍在运行的任务结束前坚决保持同根排他锁；
        3. 在 timeout_sec 内进行有界等待；若超时任务未完，允许 shutdown 返回，
           由后台守护线程在任务最终完成后安全释放锁。
        """
        with self._lifecycle_lock:
            if self.status == "SHUTDOWN":
                return
            self.status = "SHUTDOWN"

        # 1. 取消队列中尚未开始的任务
        if self.executor:
            try:
                if sys.version_info >= (3, 9):
                    self.executor.shutdown(wait=False, cancel_futures=True)
                else:
                    self.executor.shutdown(wait=False)
            except Exception:
                pass

        # 2. 检查是否有尚未完成的 active futures
        with self._futures_lock:
            active_futures = [f for f in self._futures if not f.done()]

        if not active_futures:
            # 无运行中任务，立即清理并释放排他锁
            self._do_release_resources()
            return

        # 3. 有正在运行的任务：在 timeout_sec 内进行有界等待
        done, not_done = futures_wait(active_futures, timeout=timeout_sec)
        if not not_done:
            # 活跃任务在超时时间内顺利完成
            self._do_release_resources()
            return

        # 4. 超时后仍有任务在运行：保持锁所有权，启动后台守护线程延迟释放
        def _delayed_release_worker():
            try:
                futures_wait(list(not_done))
            except Exception:
                pass
            self._do_release_resources()

        t = threading.Thread(
            target=_delayed_release_worker,
            daemon=True,
            name="lab_delayed_lock_releaser"
        )
        t.start()
