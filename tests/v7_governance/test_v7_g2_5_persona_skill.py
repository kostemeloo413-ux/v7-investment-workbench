import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from analysis.decision_gate import deterministic_execution_safety, evaluate_decision
from analysis.skill_contracts import build_decision, load_contracts, validate_contracts


class G25PersonaSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_contracts()
        validate_contracts(cls.payload)
        cls.contracts = {c['participant_id']: c for c in cls.payload['contracts']}

    def test_t1_persona_skills_own_buy_and_hold_action(self):
        persona_ids = {'P-LIAO-XIAOAI', 'P-COOL', 'P-YINWENTAO', 'P-HANSHU'}
        for pid in persona_ids:
            buy = build_decision(pid, 'sh600000', 'BUY', timestamp='2026-09-17T09:30:00')
            hold = build_decision(pid, 'sh600000', 'HOLD', timestamp='2026-09-17T09:30:01')
            self.assertEqual(buy['action'], 'BUY')
            self.assertEqual(hold['action'], 'HOLD')

    def test_t2_negative_sentiment_cannot_centrally_veto(self):
        result = evaluate_decision('sh600000', factor_net=-1.0, account={'cash': 100000})
        self.assertNotEqual(result['verdict'], 'VETO')
        self.assertEqual(result['ratio_multiplier'], 1.0)
        self.assertIn('NEGATIVE_FACTOR_SENTIMENT', {x['code'] for x in result['soft_risk_context']})

    def test_t3_bear_debate_cannot_centrally_veto(self):
        result = evaluate_decision('sh600000', debate_penalty=100, account={'cash': 100000})
        self.assertNotEqual(result['verdict'], 'VETO')
        self.assertIn('BEAR_DEBATE_CONTEXT', {x['code'] for x in result['soft_risk_context']})

    def test_t4_risk_off_cannot_centrally_change_ratio(self):
        with patch('analysis.decision_gate._regime', return_value='risk_off'):
            result = evaluate_decision('sh600000', account={'cash': 100000})
        self.assertEqual(result['ratio_multiplier'], 1.0)
        self.assertIn('RISK_OFF_CONTEXT', {x['code'] for x in result['soft_risk_context']})

    def test_t5_priced_in_cannot_centrally_veto_or_adjust_score(self):
        result = evaluate_decision('sh600000', priced_in=99, account={'cash': 100000})
        self.assertNotEqual(result['verdict'], 'VETO')
        self.assertEqual(result['score_delta'], 0.0)
        self.assertIn('PRICED_IN_CONTEXT', {x['code'] for x in result['soft_risk_context']})

    def test_t6_soft_risk_context_is_preserved(self):
        result = evaluate_decision('sh600000', factor_net=-1, debate_penalty=50, priced_in=90,
                                   account={'cash': 100000})
        self.assertGreaterEqual(len(result['soft_risk_context']), 3)
        self.assertEqual(result['snapshot']['soft_risk_context'], result['soft_risk_context'])

    def test_t7_halt_is_deterministic_hard_block(self):
        result = deterministic_execution_safety(
            action='BUY', account={'cash': 100000}, shares=100, price=10,
            new_entry_disabled=True,
        )
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertIn('NEW_ENTRY_DISABLED', result['hard_blocks'])

    def test_t8_cash_insufficient_is_deterministic_hard_block(self):
        result = deterministic_execution_safety(
            action='BUY', account={'cash': 100}, shares=100, price=20,
        )
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertIn('CASH_INSUFFICIENT', result['hard_blocks'])

    def test_t9_t_plus_one_is_deterministic_hard_block(self):
        result = deterministic_execution_safety(
            action='SELL', positions=[{'symbol': 'sh600000', 'shares': 100}],
            shares=100, price=10, t1_allowed=False,
        )
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertIn('T_PLUS_ONE_RESTRICTION', result['hard_blocks'])

    def test_t10_idempotency_duplicate_is_deterministic_hard_block(self):
        result = deterministic_execution_safety(
            action='BUY', account={'cash': 100000}, shares=100, price=10,
            idempotency_duplicate=True,
        )
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertIn('IDEMPOTENCY_DUPLICATE', result['hard_blocks'])

    def test_t11_risk_sentinel_cannot_trade(self):
        result = deterministic_execution_safety(
            action='BUY', account={'cash': 0, 'trading_account': False}, shares=100, price=10,
        )
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertIn('NON_TRADING_ACCOUNT', result['hard_blocks'])
        sentinel = self.contracts['P-RISK-SENTINEL']
        self.assertFalse(sentinel['position_policy']['trading_account'])

    def test_t12_rule_quant_and_ai_quant_are_distinct(self):
        self.assertEqual(self.contracts['P-RULE-QUANT']['skill_type'], 'RULE_BASED_SKILL')
        self.assertEqual(self.contracts['P-AI-QUANT']['skill_type'], 'ML_QUANT')
        self.assertNotEqual(self.contracts['P-RULE-QUANT']['participant_id'], self.contracts['P-AI-QUANT']['participant_id'])

    def test_t13_legacy_ai_alpha_maps_to_rule_quant(self):
        self.assertIn('ai-alpha', self.contracts['P-RULE-QUANT']['legacy_aliases'])
        self.assertEqual(self.contracts['P-RULE-QUANT']['display_name'], '规则量化派')

    def test_t14_legacy_anti_demo_maps_to_risk_sentinel(self):
        self.assertIn('anti-demo', self.contracts['P-RISK-SENTINEL']['legacy_aliases'])
        self.assertFalse(self.contracts['P-RISK-SENTINEL']['position_policy']['ranking_enabled'])

    def test_t15_unsupported_reason_claims_are_forbidden(self):
        forbidden = ' '.join(' '.join(c['forbidden_claims']) for c in self.contracts.values())
        self.assertIn('MA20', forbidden)
        self.assertIn('机构', forbidden)
        self.assertIn('假跌破', forbidden)

    def test_t16_new_season_remains_hold(self):
        path = os.path.join(ROOT, 'state', 'seasons', 'V7-SEASON-20260917-V1', 'season_manifest.json')
        manifest = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'INITIALIZED_HOLD')
        self.assertFalse(manifest['trading_enabled'])
        self.assertFalse(manifest['forward_epoch_started'])

    def test_t17_g25_produces_no_positions_or_trades(self):
        root = os.path.join(ROOT, 'state', 'seasons', 'V7-SEASON-20260917-V1')
        accounts = json.loads(Path(root, 'competition_accounts.json').read_text(encoding='utf-8'))['accounts']
        self.assertTrue(all(not a['positions'] for a in accounts.values()))
        aiq = os.path.join(root, 'ai_quant_paper')
        self.assertEqual(json.loads(Path(aiq, 'positions.json').read_text(encoding='utf-8')), [])
        for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
            self.assertEqual(Path(aiq, name).read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
