# -*- coding: utf-8 -*-
"""Minimal V7 G2.5 Skill Contract V2 loader and decision-shape validator.

This module validates ownership and output shape. It does not select symbols,
score investments, or override a Skill action.
"""

import datetime
import json
import os


_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONTRACT_PATH = os.path.join(_BASE, 'config', 'skill_contracts_v2.json')
VALID_ACTIONS = {'BUY', 'SELL', 'HOLD', 'NO_SIGNAL'}
REQUIRED_FIELDS = {
    'participant_id', 'skill_type', 'display_name', 'version', 'status',
    'investment_style', 'universe_policy', 'candidate_filters', 'entry_rules',
    'position_policy', 'exit_rules', 'holding_horizon', 'evidence_requirements',
    'soft_risk_inputs', 'forbidden_claims', 'audit_fields',
}


def load_contracts(path=DEFAULT_CONTRACT_PATH):
    with open(path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    contracts = payload.get('contracts')
    if payload.get('schema_version') != 'SKILL_CONTRACT_V2' or not isinstance(contracts, list):
        raise ValueError('invalid SKILL_CONTRACT_V2 root')
    return payload


def validate_contracts(payload):
    contracts = payload.get('contracts', [])
    if len(contracts) != 7:
        raise ValueError('expected six return skills plus Risk Sentinel')
    ids = {c.get('participant_id') for c in contracts}
    if len(ids) != len(contracts):
        raise ValueError('duplicate participant_id')
    for contract in contracts:
        missing = REQUIRED_FIELDS - set(contract)
        if missing:
            raise ValueError(f"missing contract fields: {sorted(missing)}")
    return True


def build_decision(participant_id, symbol, action, *, skill_version='V2', timestamp=None,
                   candidate_evidence=None, skill_rules_triggered=None,
                   skill_rules_failed=None, decision_reason='', position_plan=None,
                   exit_plan=None, holding_horizon='', soft_risk_context=None,
                   confidence=None, data_freshness=None, evidence_refs=None):
    """Return a schema-shaped Skill decision without changing the requested action."""
    action = str(action).upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f'invalid Skill action: {action}')
    return {
        'participant_id': participant_id,
        'skill_version': skill_version,
        'decision_id': f'{participant_id}:{symbol}:{timestamp or datetime.datetime.now().isoformat()}',
        'timestamp': timestamp or datetime.datetime.now().isoformat(),
        'symbol': symbol,
        'action': action,
        'candidate_evidence': candidate_evidence or [],
        'skill_rules_triggered': skill_rules_triggered or [],
        'skill_rules_failed': skill_rules_failed or [],
        'decision_reason': decision_reason,
        'position_plan': position_plan,
        'exit_plan': exit_plan,
        'holding_horizon': holding_horizon,
        'soft_risk_context': soft_risk_context or [],
        'confidence': confidence,
        'data_freshness': data_freshness,
        'evidence_refs': evidence_refs or [],
    }
