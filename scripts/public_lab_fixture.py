"""Read-only Blogger Lab renderer specimens; no runtime, model or storage imports.

Everything returned here is authored illustration, including knowledge and fills.
It cannot be used by the real /api/blogger-lab research or execution endpoints.
"""
from copy import deepcopy

MODE = 'SYNTHETIC_UI_DEMO'
PERSONA_ID = 'demo_paperboat'
SOURCE_ID = 'demo_source'
SOURCE_TEXT = (
    '纸舟研究员｜虚构研究笔记，仅供界面演示。\n'
    '收盘价站上20日均线时进入观察，成交量需超过前日的1.2倍。\n'
    '单只标的开仓资金不超过总资产的20%。\n'
    '单笔亏损达到3.5%时止损；浮盈达到5%时止盈。\n'
    '这些数字只用于说明规则结构，不构成投资建议。'
)
STAMP = '2026-10-30T16:00:00+08:00'


def lab_fixture(endpoint, query=None):
    """Match only known specimen paths. Unknown resources stay unavailable."""
    query = query or {}
    persona = {'id': PERSONA_ID, 'name': '纸舟研究员 · 虚构示例',
               'description': '手写资料与规则示例，非真实博主分身或模型输出',
               'status': 'ACTIVE', 'active_strategy_id': 'demo_strategy_v1'}
    batch = {'id': 'demo_batch', 'status': 'READY', 'total_files': 1,
             'ready_files': 1, 'failed_files': 0, 'total_chars': len(SOURCE_TEXT), 'total_chunks': 1}
    items = []
    for kind, statement, quote in [
        ('ENTRY_RULE', '收盘价高于20日均线，成交量超过前日的1.2倍。',
         '收盘价站上20日均线时进入观察，成交量需超过前日的1.2倍。'),
        ('POSITION_RISK', '单只标的开仓资金上限为总资产的20%。',
         '单只标的开仓资金不超过总资产的20%。'),
        ('EXIT_RULE', '按事先约定的止损与止盈条件退出。',
         '单笔亏损达到3.5%时止损；浮盈达到5%时止盈。'),
    ]:
        start = SOURCE_TEXT.index(quote)
        items.append({'id': 'demo_item_' + str(len(items) + 1), 'kind': kind, 'statement': statement,
                      'citations': [{'source_id': SOURCE_ID, 'chunk_id': 'demo_chunk',
                                     'start': start, 'end': start + len(quote),
                                     'chunk_offset_in_source': 0, 'quote': quote}]})
    draft = {'id': 'demo_draft', 'revision': 1, 'draft_hash': 'demo-unverified-illustration',
             'entry_rules': [
                 {'field': 'close_above_ma20', 'op': '==', 'value': True, 'unit': 'boolean', 'parameter_origin': 'source'},
                 {'field': 'volume_ratio_prev', 'op': '>=', 'value': 1.2, 'unit': 'ratio', 'parameter_origin': 'source'},
             ],
             'exit_rules': {'stop_loss_pct': -3.5, 'take_profit_pct': 5.0},
             'sizing_rules': {'position_fraction': 0.2, 'max_positions': 1},
             'unresolved_fields': [], 'unsupported_conditions': []}
    strategy = {**draft, 'id': 'demo_strategy_v1', 'draft_id': draft['id'],
                'draft_revision': 1, 'confirmed_at': STAMP}
    scan = {'id': 'demo_scan', 'mode': 'REPLAY', 'as_of_date': '20261028',
            'universe_target_count': 3, 'universe_evaluated_count': 3,
            'universe_excluded_count': 0, 'candidates_count': 1, 'execution_ms': 0,
            'candidates': [{'symbol': 'DEMO-A', 'name': '示例甲股 · 合成',
                            'decision': 'QUALIFIED', 'data_cutoff': '20261028',
                            'checks': [{'field': 'close_above_ma20', 'status': 'TRUE'},
                                       {'field': 'volume_ratio_prev', 'status': 'TRUE'}],
                            'market_snapshot': {'last_price': 10, 'prev_close': 9.9, 'ma20': 9.8}}]}
    account = {'cash': 100090.95, 'total_asset': 100090.95}
    # Hand-calculated illustrative ledger: 200 shares, minimum commission 5.
    # Displayed fill prices already include slippage; all values are synthetic.
    fills = [
        {'direction': 'BUY', 'symbol': 'DEMO-A', 'name': '示例甲股 · 合成',
         'price': 10.01, 'shares': 200, 'commission': 5, 'stamp_tax': 0,
         'slippage_cost': 2, 'cash_effect': -2007,
         'trade_time': '2026-10-29T10:00:00+08:00'},
        {'direction': 'SELL', 'symbol': 'DEMO-A', 'name': '示例甲股 · 合成',
         'price': 10.52, 'shares': 200, 'commission': 5, 'stamp_tax': 1.052,
         'slippage_cost': 2.1061, 'cash_effect': 2097.948,
         'trade_time': '2026-10-30T10:00:00+08:00'},
    ]
    job = {'id': 'demo_job', 'status': 'SUCCEEDED', 'total_chunks': 1, 'processed_chunks': 1,
           'succeeded_chunks': 1, 'partial_chunks': 0, 'failed_chunks': 0}
    overview = {'latest_job': job, 'latest_knowledge': {'items': items},
                'active_strategy': strategy, 'latest_draft': draft, 'latest_scan': scan, 'account': account}
    routes = {
        'personas': [persona],
        f'personas/{PERSONA_ID}/overview': overview,
        f'personas/{PERSONA_ID}/batches': [batch],
        f'personas/{PERSONA_ID}/strategies': {'active_strategy': strategy, 'drafts': [draft]},
        f'personas/{PERSONA_ID}/knowledge': {'items': items, 'versions': [{'id': 'demo_knowledge'}]},
        f'personas/{PERSONA_ID}/paper': {'account': account, 'positions': [], 'fills': fills},
    }
    data = routes.get(endpoint)
    if endpoint == f'sources/{SOURCE_ID}/excerpt' and query.get('persona_id') == PERSONA_ID:
        try:
            start = max(0, int(query.get('start_char', 0)))
            end = min(len(SOURCE_TEXT), max(start, int(query.get('end_char', len(SOURCE_TEXT)))))
            data = {'filename': '虚构研究笔记.txt', 'source_id': SOURCE_ID,
                    'start_char': start, 'end_char': end, 'total_chars': len(SOURCE_TEXT),
                    'excerpt': SOURCE_TEXT[start:end]}
        except (TypeError, ValueError):
            data = None
    return {'status': 'ok' if data is not None else 'unavailable', 'data': deepcopy(data),
            'demo': {'mode': MODE, 'synthetic': True, 'execution_authorized': False,
                     'note': '虚构资料及手写知识、候选、成交样例；不是模型输出、扫描结果或实测业绩。'},
            'errors': [] if data is not None else [{'message': '此只读示例没有该资源。'}]}
