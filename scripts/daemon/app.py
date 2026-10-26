# -*- coding: utf-8 -*-
"""
scripts/daemon/app.py
轻量 Web 服务（FastAPI）：daemon 进程内嵌运行

路由：
  GET  /                 → 跳转工作台首页（workbench/index.html）
  GET  /confirm          → 「今日操作确认单」页（LLM观点+策略共识+模拟盘+数据新鲜度）
  GET  /api/health       → 心跳/任务状态/推送额度/数据库覆盖（看门狗与前端健康灯共用）
  GET  /api/data         → 最新 workbench/data.json（附服务器读取时间戳）
  POST /api/run/{job}    → 手动触发一次内置任务（等价 --once，用于按钮补跑）

静态安全说明：只读服务，不提供任何写文件接口（除触发既有任务）。
"""

import os
import sys
import json
import threading
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)

import runtime_config as rc
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse

DATA_PATH = os.path.join(_BASE, 'workbench', 'data.json')
HTML_PATH = os.path.join(_BASE, 'workbench', 'index.html')
V7_HTML_PATH = os.path.join(_BASE, 'workbench_v7', 'index.html')
V7_DIR = os.path.join(_BASE, 'workbench_v7')

# 与 trading_daemon.BUILTINS 解耦：延迟导入避免循环依赖
_builtin_names = ['precheck_briefing', 'intraday_cycle', 'news_refresh', 'close_settlement', 'daily_review']


def _trigger_job(name):
    from daemon.trading_daemon import BUILTINS, execute_runtime_job
    fn = BUILTINS.get(name)
    if not fn:
        raise HTTPException(404, f'未知任务: {name}')
    # The response only acknowledges queueing.  The canonical runtime gate is
    # evaluated inside the worker immediately before the business callable.
    th = threading.Thread(target=lambda: execute_runtime_job(
        name, fn, source='api'),
                          daemon=True)
    th.start()
    return {
        'ok': True,
        'status': 'QUEUED_FOR_RUNTIME_GATE',
        'job': name,
        'detail': '任务已进入统一运行时安全闸；业务任务尚未获得执行许可',
    }


def create_app() -> FastAPI:
    app = FastAPI(title='AI多博主量化共识交易工作台 · 守护服务', docs_url=None, redoc_url=None)

    # Phase UI-2: 挂载 V7 只读聚合 View Model 路由
    from daemon.v7_api import v7_router
    app.include_router(v7_router)

    # Phase UI-3: 挂载 V7 独立并行入口与静态目录 (V6 / 保持绝对不变)
    from fastapi.staticfiles import StaticFiles
    if os.path.exists(V7_DIR):
        app.mount('/workbench_v7', StaticFiles(directory=V7_DIR), name='workbench_v7')

    @app.get('/v7')
    def index_v7():
        if os.path.exists(V7_HTML_PATH):
            return FileResponse(V7_HTML_PATH)
        return HTMLResponse('<h3>workbench_v7/index.html 不存在</h3>', status_code=404)

    @app.get('/')
    def index():
        if os.path.exists(HTML_PATH):
            return FileResponse(HTML_PATH)
        return HTMLResponse('<h3>workbench/index.html 不存在</h3>', status_code=404)

    @app.get('/favicon.ico')
    def favicon():
        return HTMLResponse('', status_code=204)

    @app.get('/api/health')
    def health():
        hb = rc._load_json(rc.HEARTBEAT_PATH, {})
        alive = rc._load_json(rc.ALIVE_PATH, {})
        fails = rc._load_json(os.path.join(rc.STATE_DIR, 'job_failures.json'), {})
        try:
            from notify.wecom_bot import quota_status
            quota = quota_status()
        except Exception:
            quota = {'error': 'notify 模块不可用'}
        try:
            from datasource import db as history_db
            n_stk, lo, hi = history_db.kline_coverage()
            dbinfo = {'stocks': n_stk, 'from': lo, 'to': hi}
        except Exception as e:
            dbinfo = {'error': str(e)}
        now = datetime.datetime.now()
        hb_age_min = None
        if alive.get('epoch'):
            hb_age_min = round((now.timestamp() - float(alive['epoch'])) / 60, 1)
        safety = {
            'active_season': None,
            'execution_state_safe': None,
            'new_entry_disabled': None,
            'trading_enabled': None,
            'real_trading': None,
        }
        try:
            from competition.season_runtime import resolve_active_season_context
            context = resolve_active_season_context()
            safety['active_season'] = context['season_id']
            state = rc._load_json(context['governance_safety_path'], {})
            for key in ('execution_state_safe', 'new_entry_disabled',
                        'trading_enabled', 'real_trading'):
                safety[key] = state.get(key)
        except Exception:
            # Health must distinguish unavailable safety state from a healthy
            # process; do not synthesize an ALLOW value.
            pass
        return {
            'server_time': rc.now_str(),
            'trading_day': rc.is_trading_day(),
            'market_phase': rc.market_phase(),
            'daemon_pid': alive.get('pid'),
            'alive_age_minutes': hb_age_min,
            'stale_threshold_minutes': rc.HEARTBEAT_STALE_MINUTES,
            'healthy': (hb_age_min is not None and hb_age_min <= rc.HEARTBEAT_STALE_MINUTES),
            'heartbeat': hb,
            'job_failures': fails,
            'push_quota': quota,
            'history_db': dbinfo,
            'llm_configured': bool(rc.LLM_API_KEY),
            'wecom_configured': bool(rc.WECOM_WEBHOOK_URL),
            'active_season': safety['active_season'],
            'execution_state_safe': safety['execution_state_safe'],
            'new_entry_disabled': safety['new_entry_disabled'],
            'trading_enabled': safety['trading_enabled'],
            'real_trading': safety['real_trading'],
        }

    @app.get('/api/data')
    def data():
        if not os.path.exists(DATA_PATH):
            raise HTTPException(404, 'data.json 尚未生成')
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return {'served_at': rc.now_str(), 'data': d}

    @app.get('/confirm')
    def confirm():
        sys.path.insert(0, os.path.join(_BASE, 'scripts'))
        try:
            from analysis.confirm_page import render_html
            return HTMLResponse(render_html())
        except Exception as e:
            return HTMLResponse(f'<h3>确认单渲染失败: {e}</h3>'
                                '<p>请检查 scripts/analysis/confirm_page.py</p>',
                                status_code=500)

    @app.post('/api/run/{job}')
    def run(job: str, request: Request):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)触发任务')
        if job not in _builtin_names:
            raise HTTPException(404, f'未知任务: {job}')
        return _trigger_job(job)

    # ---------------- 配置读取/保存（仅限本机回环地址，防局域网/隧道被外部篡改） ----------------
    ENV_FILE = os.path.join(_BASE, '.env')
    ENV_ALLOWED = ['WECOM_WEBHOOK_URL', 'DEEPSEEK_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL']

    def _is_local(request):
        host = (request.client.host if request.client else '') or ''
        return host in ('127.0.0.1', '::1', 'localhost')

    def _read_env_file():
        if not os.path.exists(ENV_FILE):
            return []
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            return f.read().splitlines()

    @app.get('/api/config')
    def config_status(request: Request):
        lines = _read_env_file()
        vals = {}
        for ln in lines:
            for k in ENV_ALLOWED:
                if ln.strip().startswith(k + '='):
                    v = ln.split('=', 1)[1].strip()
                    vals[k] = ('已配置(已隐藏)' if v else '未配置') if k.endswith(('KEY', 'URL')) else v
        return {'allowed_keys': ENV_ALLOWED,
                'masked': {k: vals.get(k, '未配置') for k in ENV_ALLOWED},
                'autostart': _autostart_status(),
                'writable_from_local': True}

    # ---------------- 开机自启（用户 2026-08-29：设置里可切换） ----------------
    AUTOSTART_LNK = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming',
                                 'Microsoft', 'Windows', 'Start Menu', 'Programs',
                                 'Startup', '交易工作台.lnk')

    def _autostart_target():
        exe = os.path.join(_BASE, '交易工作台.exe')
        if os.path.exists(exe):
            return exe
        return os.path.join(_BASE, '启动工作台.bat')

    def _autostart_status():
        return {'enabled': os.path.exists(AUTOSTART_LNK), 'target': _autostart_target()}

    @app.get('/api/decision-gate')
    def decision_gate_view():
        """阶段五：今日否决清单 + 最近决策快照（前端决策门面板数据源）。"""
        import os as _os
        today = rc.now_str()[:10]
        vetoes = []
        try:
            vp = _os.path.join(rc.STATE_DIR, 'veto_log.json')
            log = rc._load_json(vp, []) or []
            vetoes = [v for v in log if str(v.get('ts', '')).startswith(today)][-30:]
        except Exception:
            pass
        traces = []
        try:
            from datasource import db as hdb
            traces = hdb.recent_traces(12)
        except Exception:
            pass
        return {'date': today, 'vetoes': vetoes, 'recent_traces': traces}

    @app.post('/api/config/autostart')
    def autostart_toggle(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        enabled = bool(payload.get('enabled'))
        target = _autostart_target()
        if enabled:
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s'); "
                  "$s.TargetPath='%s'; $s.WorkingDirectory='%s'; $s.Save()"
                  % (AUTOSTART_LNK, target, _BASE))
            import subprocess as _sp
            r = _sp.run(['powershell', '-NoProfile', '-Command', ps],
                        capture_output=True, text=True)
            if not os.path.exists(AUTOSTART_LNK):
                raise HTTPException(500, f'快捷方式创建失败: {r.stderr[:120]}')
        else:
            if os.path.exists(AUTOSTART_LNK):
                os.remove(AUTOSTART_LNK)
        return _autostart_status()

    @app.post('/api/config')
    def config_save(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        lines = _read_env_file()
        updates = {k: str(payload.get(k, '')).strip() for k in ENV_ALLOWED if k in payload}
        if not updates:
            raise HTTPException(400, '没有可保存的配置项')
        for k, v in updates.items():
            found = False
            for i, ln in enumerate(lines):
                if ln.strip().startswith(k + '='):
                    lines[i] = f'{k}={v}'
                    found = True
                    break
            if not found:
                lines.append(f'{k}={v}')
        with open(ENV_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        # 热生效：刷新运行时配置
        rc.ENV.update(updates)
        if 'WECOM_WEBHOOK_URL' in updates:
            rc.WECOM_WEBHOOK_URL = updates['WECOM_WEBHOOK_URL']
        if 'DEEPSEEK_API_KEY' in updates:
            rc.LLM_API_KEY = updates['DEEPSEEK_API_KEY']
        if 'LLM_BASE_URL' in updates:
            rc.LLM_BASE_URL = updates['LLM_BASE_URL']
        if 'LLM_MODEL' in updates:
            rc.LLM_MODEL = updates['LLM_MODEL']
        return {'ok': True, 'saved': list(updates.keys())}

    # ---------------- LLM 多模型配置档管理（仅本机可写） ----------------
    LLM_PRESETS = [
        {'name': 'DeepSeek Chat（推荐）', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat', 'note': '日常研判主力，便宜快'},
        {'name': 'DeepSeek Reasoner', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-reasoner', 'note': '深度推理，慢一些贵一些'},
        {'name': '智谱 GLM-4.5', 'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'model': 'glm-4.5', 'note': 'open.bigmodel.cn 申请Key'},
        {'name': '通义千问 Qwen-Plus', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'model': 'qwen-plus', 'note': '阿里云百炼兼容模式'},
        {'name': 'Kimi（月之暗面）', 'base_url': 'https://api.moonshot.cn/v1', 'model': 'moonshot-v1-128k', 'note': 'api.moonshot.cn 申请Key'},
        {'name': 'Ollama 本地模型', 'base_url': 'http://127.0.0.1:11434/v1', 'model': 'qwen2.5:7b', 'note': 'Key 任意填，需本机跑 Ollama'},
    ]

    def _mask(p):
        p = dict(p)
        p['api_key_configured'] = bool((p.get('api_key') or '').strip())
        p['api_key'] = ''
        return p

    @app.get('/api/models')
    def models_list():
        data = rc.load_llm_profiles()
        return {
            'active': data.get('active'),
            'active_profile': _mask(rc.get_active_profile()),
            'profiles': [_mask(p) for p in data.get('profiles', [])],
            'presets': LLM_PRESETS,
            'llm_enabled': bool(rc.LLM_API_KEY),
        }

    @app.post('/api/models')
    def models_save(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        name = str(payload.get('name', '')).strip()
        base_url = str(payload.get('base_url', '')).strip()
        model = str(payload.get('model', '')).strip()
        api_key = str(payload.get('api_key', '')).strip()
        if not name or not base_url or not model:
            raise HTTPException(400, '名称/BaseURL/模型名 均为必填')
        profile = {'id': payload.get('id') or None, 'name': name,
                   'base_url': base_url, 'model': model, 'api_key': api_key}
        saved, is_new = rc.upsert_llm_profile(profile)
        return {'ok': True, 'is_new': is_new, 'profile': _mask(saved)}

    @app.post('/api/models/switch')
    def models_switch(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        ok, res = rc.activate_llm_profile(str(payload.get('id', '')))
        if not ok:
            raise HTTPException(404, res)
        return {'ok': True, 'active': res['id'], 'name': res['name'],
                'model': res['model'], 'base_url': res['base_url']}

    @app.post('/api/models/delete')
    def models_delete(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        new_active = rc.delete_llm_profile(str(payload.get('id', '')))
        return {'ok': True, 'new_active': new_active}

    @app.post('/api/models/test')
    def models_test(request: Request, payload: dict):
        if not _is_local(request):
            raise HTTPException(403, '仅允许本机(localhost)修改配置')
        base_url = str(payload.get('base_url', '')).strip()
        model = str(payload.get('model', '')).strip()
        api_key = str(payload.get('api_key', '')).strip()
        if not base_url or not model:
            raise HTTPException(400, '缺少 base_url/model')
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key or 'sk-none', base_url=base_url, timeout=20)
            resp = client.chat.completions.create(
                model=model, messages=[{'role': 'user', 'content': 'ping'}],
                max_tokens=5)
            return {'ok': True, 'reply': (resp.choices[0].message.content or '')[:40]}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:160]}'}

    # ---------------- 卖出监控提醒（供原界面提醒卡读取） ----------------
    @app.get('/api/sell-alerts')
    def sell_alerts():
        st = rc._load_json(rc.SELL_ALERTS_PATH, {})
        try:
            from analysis.sell_monitor import _thresholds
            th = _thresholds()
        except Exception:
            th = {}
        feed = rc._load_json(os.path.join(rc.STATE_DIR, 'trade_feed.json'), {}) or {}
        return {
            'date': st.get('date'),
            'count': len(st.get('sent', {})),
            'history': (st.get('history') or [])[-8:],
            'thresholds': th,
            'trade_feed': (feed.get('items') or [])[-10:],
        }

    # ---------------- 统一市场状态机 + 事件因子快照（P1/P0 共享） ----------------
    @app.get('/api/regime')
    def get_regime():
        try:
            data = rc._load_json(os.path.join(_BASE, 'workbench', 'data.json'), {}) or {}
            cached = data.get('market_regime')
            if cached:
                return cached
            from analysis.regime import compute_regime
            try:
                from analysis.event_factors import factor_snapshot
                efs = factor_snapshot(since_hours=24)
            except Exception:
                efs = []
            return compute_regime({}, efs)
        except Exception as e:
            return {'error': str(e)}

    @app.get('/api/event-factors')
    def get_event_factors(symbol=None, since_hours=72, limit=20):
        try:
            from analysis.event_factors import top_symbols, symbol_sentiment
            tops = top_symbols(top_k=limit, since_hours=int(since_hours))
            for row in tops:
                if symbol and row['symbol'] == str(symbol):
                    return {'symbol': row['symbol'], 'detail': row}
            return {'top': tops, 'queried_symbol': symbol, 'detail': None}
        except Exception as e:
            return {'error': str(e)}

    # 阶段五·任务F：月度系统体检与因子权重审计报告
    @app.get('/api/system-checkup')
    def get_system_checkup():
        try:
            from analysis.factor_weight_audit import load_audit
            audit = load_audit()
            now = datetime.datetime.now()
            rep_path = os.path.join(_BASE, 'outputs', f'system_checkup_{now.strftime("%Y%m")}.md')
            content = ''
            if os.path.exists(rep_path):
                content = open(rep_path, encoding='utf-8').read()
            return {
                'audit': audit,
                'report_available': bool(content),
                'report_path': rep_path if content else None,
                'report_markdown': content if content else None
            }
        except Exception as e:
            return {'error': str(e)}

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=rc.DAEMON_PORT)
