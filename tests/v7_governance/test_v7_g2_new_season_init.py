import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from competition.accounts import ArenaAccountsManager
from ai_quant.paper_executor import PaperExecutionEngine


SEASON_ID = 'V7-SEASON-20260917-V1'


def _player_specs():
    return {
        'P-LIAO-XIAOAI': {'name': '廖小爱', 'role': 'return_bearing', 'icon': '🛡️', 'account_id': 'LIAO-XIAOAI-V1'},
        'P-COOL': {'name': '酷儿', 'role': 'return_bearing', 'icon': '⚡', 'account_id': 'COOL-V1'},
        'P-YINWENTAO': {'name': '殷文涛', 'role': 'return_bearing', 'icon': '🔥', 'account_id': 'YINWENTAO-V1'},
        'P-HANSHU': {'name': '韩叔', 'role': 'return_bearing', 'icon': '🎯', 'account_id': 'HANSHU-V1'},
        'P-RULE-QUANT': {'name': '规则量化派', 'role': 'return_bearing', 'icon': '🧠', 'account_id': 'RULE-QUANT-V1'},
        'P-AI-QUANT': {'name': 'AI Quant', 'role': 'return_bearing', 'icon': '🤖', 'account_id': 'AIQ-PAPER-V2'},
        'P-RISK-SENTINEL': {
            'name': '反指哨兵', 'role': 'risk_sentinel', 'icon': '🚨',
            'account_id': 'RISK-SENTINEL-V1', 'initial_capital': 0,
            'return_bearing': False, 'ranking_enabled': False, 'trading_account': False,
        },
    }


def test_new_season_accounts_are_scoped_and_clean(tmp_path):
    state_path = tmp_path / 'season' / 'competition_accounts.json'
    manager = ArenaAccountsManager(SEASON_ID, state_path=str(state_path), player_specs=_player_specs(), explicit_initialize=True)
    assert state_path.exists()
    payload = json.loads(state_path.read_text(encoding='utf-8'))
    assert payload['season_id'] == SEASON_ID
    assert len(payload['accounts']) == 7
    returns = [a for a in payload['accounts'].values() if a['return_bearing']]
    assert len(returns) == 6
    assert all(a['cash'] == 100000.0 and a['total_equity'] == 100000.0 for a in returns)
    sentinel = payload['accounts']['P-RISK-SENTINEL']
    assert sentinel['initial_capital'] == 0.0
    assert sentinel['ranking_enabled'] is False
    assert sentinel['trading_account'] is False
    assert all(not a['positions'] for a in payload['accounts'].values())
    assert manager.state_path == os.path.abspath(state_path)


def test_new_ai_quant_account_uses_v2_identity_and_empty_lifecycle(tmp_path):
    paper_root = tmp_path / 'ai_quant_paper'
    engine = PaperExecutionEngine(
        storage_dir=str(paper_root),
        account_id='AIQ-PAPER-V2',
        initial_capital=100000,
        season_id=SEASON_ID,
    )
    engine.idempotency_registry.save()
    for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
        (paper_root / name).touch()
    reopened = PaperExecutionEngine(
        storage_dir=str(paper_root),
        account_id='AIQ-PAPER-V2',
        initial_capital=100000,
        season_id=SEASON_ID,
    )
    assert reopened.account.account_id == 'AIQ-PAPER-V2'
    assert reopened.account.initial_capital == 100000.0
    assert reopened.account.cash == 100000.0
    assert reopened.account.total_equity == 100000.0
    assert reopened.account.positions == []
    assert reopened.idempotency_registry.registry_corrupted is False
    assert reopened.idempotency_registry.records == {}
    assert json.loads((paper_root / 'processed_idempotency_registry.json').read_text())['records'] == {}
    assert (paper_root / 'orders.jsonl').read_bytes() == b''
    assert (paper_root / 'fills.jsonl').read_bytes() == b''
    assert (paper_root / 'paper_trade_results.jsonl').read_bytes() == b''
    assert (paper_root / 'equity_curve.jsonl').read_bytes() == b''


def test_default_legacy_paths_are_not_used_by_injected_new_paths(tmp_path):
    legacy_path = tmp_path / 'legacy' / 'competition_accounts.json'
    new_path = tmp_path / 'new' / 'competition_accounts.json'
    ArenaAccountsManager('LEGACY', state_path=str(legacy_path))
    ArenaAccountsManager(SEASON_ID, state_path=str(new_path), player_specs=_player_specs(), explicit_initialize=True)
    assert json.loads(legacy_path.read_text(encoding='utf-8'))['season_id'] == 'LEGACY'
    assert json.loads(new_path.read_text(encoding='utf-8'))['season_id'] == SEASON_ID
