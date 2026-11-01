# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/api.py
V7 博主实验室独立轻量 FastAPI 应用工厂与模块聚合导出 (阶段 1 核心)
路由统一前缀: /api/blogger-lab
提供独立 FastAPI 实例组装（供独立 server.py 及端到端测试使用），并导出模块核心类型。
消除模块导入时的副作用（无全局 app 实例化，无默认 SQLite 库写入，无无界全局线程池）。
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from blogger_lab.runtime import (
    BloggerLabRuntime,
    acquire_file_lock,
    release_file_lock,
    recover_interrupted_jobs_on_startup
)
from blogger_lab.router import (
    create_blogger_lab_router,
    verify_blogger_lab_local_access,
    ok_response,
    error_response,
    CreatePersonaRequest,
    ExtractJobRequest,
    RetryJobRequest,
    CreateStrategyDraftRequest,
    UpdateStrategyDraftRequest,
    ConfirmStrategyDraftRequest,
    RunScanRequest,
    ReplayStepRequest,
    PaperCycleRequest
)

# 兼容历史导入函数名
_recover_interrupted_jobs_on_startup = recover_interrupted_jobs_on_startup


def create_app(lab_root: Optional[str] = None) -> FastAPI:
    """
    创建博主实验室独立轻量 FastAPI 实例（保留用于独立服务启动与测试）
    使用受控的 BloggerLabRuntime 管理生命周期，持锁排他启动，退出有界释放。
    """
    effective_root = lab_root or os.environ.get("BLOGGER_LAB_ROOT") or str(Path(_BASE) / "data" / "blogger_lab")
    runtime = BloggerLabRuntime(lab_root=effective_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.start()
        yield
        runtime.shutdown()

    app = FastAPI(
        title="V7 Blogger Lab API",
        description="博主实验室独立轻量 API 服务（五大分区闭环与受控回放）",
        version="1.0.0",
        lifespan=lifespan
    )
    app.state.blogger_lab_runtime = runtime

    # 挂载静态资源（仅独立模式需要）
    workbench_v7_dir = Path(_BASE) / "workbench_v7"
    if workbench_v7_dir.exists():
        app.mount("/workbench_v7", StaticFiles(directory=str(workbench_v7_dir)), name="workbench_v7")
        app.mount("/static", StaticFiles(directory=str(workbench_v7_dir)), name="static")

    @app.get("/")
    @app.get("/workbench_v7/blogger_lab.html")
    def index():
        """单页入口"""
        page_path = Path(_BASE) / "workbench_v7" / "blogger_lab.html"
        if page_path.exists():
            return FileResponse(str(page_path), media_type="text/html")
        return ok_response({"message": "Blogger Lab API is running. workbench_v7/blogger_lab.html not found."})

    # 挂载可复用的 APIRouter（含 /api/blogger-lab 统一前缀与本地门禁）
    router = create_blogger_lab_router(runtime)
    app.include_router(router)

    return app
