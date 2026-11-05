"""Start the local, read-only public preview without a private trading season."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass
os.environ['V7_PUBLIC_PREVIEW'] = '1'
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from daemon.v7_api import v7_router
from scripts.public_demo import run_demo
from scripts.public_ui_fixture import ui_fixture


def create_preview_app(lab_root: Optional[str] = None) -> FastAPI:
    lab_enabled = os.environ.get("V7_BLOGGER_LAB_ENABLED", "0").strip().lower() in ("1", "true", "yes")
    lab_runtime = None
    if lab_enabled:
        from blogger_lab.runtime import BloggerLabRuntime
        from blogger_lab.router import create_blogger_lab_router
        effective_lab_root = lab_root or os.environ.get("V7_BLOGGER_LAB_ROOT") or str(ROOT / "data" / "blogger_lab")
        lab_runtime = BloggerLabRuntime(lab_root=effective_lab_root)

    @asynccontextmanager
    async def lifespan(app_inst: FastAPI):
        if lab_runtime:
            lab_runtime.start()
        yield
        if lab_runtime:
            lab_runtime.shutdown()

    app = FastAPI(
        title='知策 · public source preview',
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan if lab_enabled else None
    )

    if lab_enabled and lab_runtime:
        app.state.blogger_lab_runtime = lab_runtime
        app.include_router(create_blogger_lab_router(lab_runtime))

    @app.middleware('http')
    async def read_only_preview(request: Request, call_next):
        is_lab_route = request.url.path == '/api/blogger-lab' or request.url.path.startswith('/api/blogger-lab/')
        if request.method not in {'GET', 'HEAD'}:
            if not (lab_enabled and is_lab_route):
                return JSONResponse(
                    {'status': 'READ_ONLY_PREVIEW', 'detail': 'This preview has no task, configuration or trading write API.'},
                    status_code=403
                )
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-V7-Mode'] = 'PUBLIC_READ_ONLY_PREVIEW'
        return response

    app.include_router(v7_router)
    app.mount('/workbench_v7', StaticFiles(directory=ROOT / 'workbench_v7'), name='workbench_v7')

    @app.get('/')
    def landing():
        return RedirectResponse('/demo')

    @app.get('/demo')
    def demo_page():
        return ui_page(synthetic=True)

    @app.get('/replay')
    def replay_page():
        return FileResponse(ROOT / 'workbench_v7' / 'public_demo.html')

    @app.get('/api/ui-demo/{endpoint:path}')
    def demo_ui_data(endpoint: str, request: Request):
        if endpoint.startswith('blogger-lab/'):
            from scripts.public_lab_fixture import lab_fixture
            return lab_fixture(endpoint[len('blogger-lab/'):], request.query_params)
        return ui_fixture(endpoint)

    @app.get('/api/demo')
    def demo_result():
        return run_demo()

    @app.get('/api/health')
    def health():
        return {'status': 'OK', 'mode': 'PUBLIC_READ_ONLY_PREVIEW', 'scheduler_started': False, 'real_trading': False, 'private_season_required': False}

    @app.get('/v7')
    def workbench():
        return ui_page(synthetic=False)

    def ui_page(synthetic):
        page = (ROOT / 'workbench_v7/index.html').read_text(encoding='utf-8-sig')
        if synthetic:
            page = page.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-ui-demo="synthetic-v1">', 1)
            label = '<strong>界面示例 · 合成数据</strong>　非实时行情，不代表选股结果或真实账户'
            links = '<a href="/v7">查看无数据状态</a><a href="/replay">历史撮合回放</a>'
            lab_notice = '<aside class="notice-box notice-info" role="note"><span><strong>只读界面示例</strong> · 虚构资料及手写知识、候选、成交样例，非模型输出或实测业绩。可切换五步并查看原文；实际运行请按博主实验室文档启用。</span></aside>'
            page = page.replace('<section class="page-view" id="page-blogger-lab">', '<section class="page-view" id="page-blogger-lab">' + lab_notice, 1)
        else:
            label = '<strong>公开源码预览</strong>　未接入实时行情或实际账户'
            links = '<a href="/demo">查看新版界面示例</a><a href="/replay">查看历史撮合演示</a>'
        notice = '<aside class="public-preview-notice" role="note"><span>' + label + '</span><nav class="public-preview-links" aria-label="公开示例入口">' + links + '</nav></aside>'
        page = page.replace('<main class="main-content">', '<main class="main-content">' + notice, 1)
        return HTMLResponse(page.replace('</head>', '<link rel="stylesheet" href="/workbench_v7/css/public_ui.css?v=20261029-nav-r1"></head>', 1))

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8020)
    parser.add_argument('--check', action='store_true', help='run the offline core replay and exit')
    args = parser.parse_args()
    if args.check:
        result = run_demo()
        print(json.dumps({'status': 'PASS', 'mode': result['mode'], **result['summary']}, ensure_ascii=False))
        return
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    import uvicorn
    print(f'知策 public preview: http://127.0.0.1:{args.port}/demo', flush=True)
    uvicorn.run(create_preview_app(), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
