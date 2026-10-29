"""Synthetic UI specimens, separate from market data, trading and /api/v7.

Dates are display-clock anchors only. No scanner, model, provider, scheduler,
account writer or production filesystem is called by this module.
"""
from datetime import datetime, timedelta, timezone
import math

MODE = 'SYNTHETIC_UI_DEMO'
ROLE_IDS = ['P-LIAO-XIAOAI', 'P-COOL', 'P-YINWENTAO', 'P-HANSHU', 'P-RULE-QUANT', 'P-AI-QUANT']
ROLE_NAMES = ['廖小爱', '酷儿', '殷文涛', '韩叔', '规则量化派', 'AI Quant']


def ui_fixture(endpoint, now=None):
    """Return a marked renderer fixture; never fall back to a live endpoint."""
    now = (now or datetime.now(timezone(timedelta(hours=8)))).astimezone(timezone(timedelta(hours=8))).replace(microsecond=0)
    stamp = now.isoformat()
    date = now.strftime('%Y%m%d')
    iso_date = now.strftime('%Y-%m-%d')
    demo = {'mode': MODE, 'synthetic': True, 'fixture_version': 'v3-r3', 'execution_authorized': False,
            'note': '全部数值与文本为界面示例，不是行情、选股结果、模型输出或真实账户。'}

    def envelope(data, status='OK'):
        return {'schema_version': 'v7.1', 'status': status, 'generated_at': stamp, 'demo': demo,
                'warnings': [], 'data_freshness': {'freshness_status': 'FRESH', 'stale': False,
                'source_timestamp': stamp, 'source_updated_at': stamp, 'source_time_kind': 'SYNTHETIC_DISPLAY_CLOCK'}, 'data': data}

    predictions = [{'rank': i + 1, 'name': '示例' + label + '股', 'symbol': f'00000{i + 1}',
                    'trade_date': date, 'predict_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'model_version': 'UI-DEMO-NO-MODEL', 'raw_score': round(.8 - i * .06, 4),
                    'up_probability': round(.65 - i * .025, 4)} for i, label in enumerate('甲乙丙丁戊')]
    top5 = {'status': 'NORMAL', 'classification': 'CURRENT_DAY_TOP5', 'predictions': predictions,
            'pulse': {'row_count': 5, 'trade_date': date, 'predict_time': now.strftime('%Y-%m-%d %H:%M:%S'), 'model_version': 'UI-DEMO-NO-MODEL'}}
    names = ['示例资源', '示例科技', '示例科技', '示例能源', '示例制造']
    codes = ['600001', '600002', '600002', '600003', '600004']
    participants = []
    for i in range(5):
        participants.append({'participant_id': ROLE_IDS[i], 'display_name': ROLE_NAMES[i], 'stock_name': names[i], 'symbol': codes[i],
            'action': 'NO_SIGNAL' if i in (1, 3) else 'BUY', 'execution_authorized': False,
            'scan_timestamp': stamp, 'scan_date_qualification': 'CURRENT_TRADING_DATE',
            'decision_reason': '界面合成示例：展示观点、证据与确认条件的阅读顺序，不构成选股建议。',
            'holding_horizon': '示例周期', 'skill_rules_failed': ['SYNTHETIC_NOT_A_SIGNAL'] if i in (1, 3) else [],
            'exit_plan': {'stop_loss': -.04}, 'candidate_evidence': {},
            'scan_evidence': {'status': 'RECORDED_CURRENT_SCAN', 'identity_status': 'MATCH', 'source_name': MODE,
                'scan_at': stamp, 'evidence_refs': [], 'source_record': {'source': MODE, 'scan_timestamp': stamp, 'stale': False}}})

    players = []
    for i, pct in enumerate([-2.4, 0., 1.7, 0., -1.1, -3.2]):
        total = 100000 + pct * 1000
        players.append({'player_id': ROLE_IDS[i], 'name': ROLE_NAMES[i], 'rank': i + 1,
            'account_type': 'SYNTHETIC_UI_SPECIMEN', 'status': 'STRATEGY_DISABLED' if i in (1, 3) else 'HOLD',
            'data_status': 'UNKNOWN', 'stale': False, 'initial_capital': 100000, 'total_equity': total,
            'cash': total, 'market_value': 0, 'net_pnl': pct * 1000, 'net_pnl_pct': pct, 'max_drawdown_pct': None,
            'positions_count': 0, 'positions': [], 'transactions': [], 'transactions_total': 0,
            'orders_count': 0, 'fills_count': 0, 'ledger_updated_at': stamp, 'ledger_time_kind': MODE,
            'projection_status': 'SYNTHETIC', 'demo': demo})

    if endpoint == 'overview':
        return envelope({'active_season': {'season_id': 'UI-DEMO-ONLY', 'status': 'HOLD'},
            'system_control': {'real_trading': False, 'execution_mode': 'PAPER_SIMULATION', 'g6_status': 'NOT_EVALUATED',
                'safety': {'status': 'DEMO_ONLY', 'new_entry_disabled': True, 'trading_enabled': False}},
            'system_health': {'business_status': 'DEMO_ONLY', 'daemon_liveness': 'NOT_STARTED'},
            'forward_epoch': {'epoch_id': 'UI-DEMO-NOT-FORWARD', 'status': 'HOLD', 'forward_day_count': 0},
            'today_picks': {'execution_authorized': False}})
    if endpoint == 'market':
        indexes = []
        minute_now = now.hour * 60 + now.minute
        minutes = [minute for minute in list(range(570, 691)) + list(range(780, 901)) if minute <= minute_now]
        for i, (symbol, code, name, base) in enumerate([('sh000001', '000001', '上证指数', 3200), ('sz399001', '399001', '深证成指', 10500), ('sz399006', '399006', '创业板指', 2100)]):
            points = []
            for j, minute in enumerate(minutes):
                value = round(base * (1 + .0015 * math.sin(j / 13 + i) - .006 * j / 241 + .0005 * math.sin(j / 3)), 2)
                points.append({'time': f'{minute // 60:02}{minute % 60:02}', 'bar_at': f'{iso_date}T{minute // 60:02}:{minute % 60:02}:00+08:00', 'value': value})
            indexes.append({'symbol': symbol, 'code': code, 'name': name,
                'intraday': {'status': 'AVAILABLE', 'stale': False, 'source': MODE, 'market_date': date,
                    'market_time': points[-1]['bar_at'] if points else None, 'points': points},
                'snapshot': {'status': 'UNKNOWN', 'stale': False}})
        return envelope({'environment': {'status': 'VERIFIED', 'stale': False, 'classification': 'bear',
            'rule_version': 'existing_market_regime_v1', 'observed_at': stamp,
            'inputs': {'avg_pct': -.6, 'sh_pct': -.6, 'sz_pct': -.8, 'cyb_pct': -.4, 'kcb_pct': -.6}}, 'indexes': indexes})
    if endpoint == 'picks':
        return envelope({'data_status': 'OK', 'trade_date': date, 'scan_date_status': 'CURRENT_TRADING_DATE',
            'as_of': stamp, 'execution_authorized': False, 'participants': participants, 'ai_quant_top5': top5, 'risk_sentinel': {}})
    if endpoint == 'ai-quant':
        return envelope(top5)
    if endpoint == 'news':
        articles = []
        for i, title in enumerate(['界面示例：市场信息与研究观点分开阅读', '界面示例：按需展开保存内容', '界面示例：关注证据时间与风险边界', '界面示例：新闻列表支持独立滚动']):
            article_time = (now - timedelta(minutes=i)).isoformat()
            articles.append({'title': title, 'source': '合成示例', 'category': '界面说明', 'source_record': MODE,
                'published_at': article_time, 'published_at_raw': article_time.replace('T', ' ')[:19],
                'published_at_status': 'SYNTHETIC_DISPLAY_CLOCK', 'stale': False,
                'url': f'https://github.com/kostemeloo413-ux/v7-investment-workbench/blob/main/docs/UI_PREVIEW.md#news-{i + 1}',
                'content': '这是一条手写的界面展示文本，不是采集新闻。公开示例不包含私人新闻库、账户记录或模型连接信息。'})
        return envelope({'capture_started_at': stamp, 'source': MODE, 'hot_news': articles[:2], 'ordinary_news': articles[2:], 'all_news': articles})
    if endpoint == 'arena':
        return envelope({'view_version': 'arena-ledger-view-v1', 'season_id': 'UI-DEMO-ONLY', 'players_count': 6,
            'primary_metric': 'SYNTHETIC_ILLUSTRATION', 'leaderboard': players, 'comparability_warning': '界面合成指标，不是模拟账本或策略表现。'})
    if endpoint.startswith('arena/'):
        player = next((p for p in players if p['player_id'] == endpoint.split('/')[-1]), None)
        return envelope(player or {}, 'OK' if player else 'UNAVAILABLE')
    if endpoint == 'paper':
        return envelope({'capital_summary': {'cash': 100000, 'total_equity': 100000}, 'positions': [], 'orders': [], 'fills': []})
    if endpoint == 'forward':
        return envelope({'season_id': 'UI-DEMO-ONLY', 'epoch_id': 'UI-DEMO-NOT-FORWARD', 'status': 'HOLD', 'forward_day_count': 0, 'latest_cycle': None})
    if endpoint == 'trade-plans':
        return envelope({'plans': [], 'total_records': 0})
    if endpoint == 'research':
        return envelope({'reports': []})
    return envelope({}, 'UNAVAILABLE')
