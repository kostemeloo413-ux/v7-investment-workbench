# -*- coding: utf-8 -*-
"""Season-scoped Forward bookkeeping with one authoritative Day qualification path."""

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_BASE, os.path.join(_BASE, 'scripts')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_quant.forward_start_gate import (
    evaluate_forward_day_qualification,
    load_forward_manifest,
    resolve_forward_manifest_path,
)
from ai_quant.storage_utils import atomic_write_json


class ForwardEpochManager:
    """Writes Day counters only after authoritative persisted evidence has passed."""

    def __init__(self, manifest_path=None, context=None):
        self.context = context
        self.manifest_path = manifest_path or resolve_forward_manifest_path(context)
        result = load_forward_manifest(self.manifest_path)
        if not result.is_valid:
            raise RuntimeError(f'forward manifest unavailable: {result.status}')
        self.manifest = result.value

    def _save_manifest(self):
        atomic_write_json(self.manifest_path, self.manifest)

    def record_forward_day_end(self, trade_date_str, daily_telemetry_summary, market_timestamp=None,
                               replay=False, backfill=False):
        """Count exactly one verified trading date; legacy caller evidence is intentionally ignored."""
        qualification = evaluate_forward_day_qualification(
            trade_date_str, market_timestamp, context=self.context, manifest=self.manifest,
            replay=replay, backfill=backfill,
        )
        current_count = int(self.manifest['forward_day_count'])
        if not qualification['pass']:
            return {
                'accepted': False, 'reason_code': qualification['reason_codes'][0],
                'reason_codes': qualification['reason_codes'], 'forward_day_count': current_count,
            }
        dates = list(self.manifest['qualified_trading_dates'])
        dates.append(qualification['trade_date'])
        dates.sort()
        self.manifest['qualified_trading_dates'] = dates
        self.manifest['forward_day_count'] = len(dates)
        self.manifest['forward_day_counter'] = {
            'current_forward_day_count': len(dates), 'qualified_trading_dates': list(dates),
        }
        self.manifest['last_qualified_market_timestamp'] = qualification['market_timestamp']
        self.manifest['last_qualification_evidence'] = {
            'daily_telemetry_summary': daily_telemetry_summary,
            'daily_reconciliation': qualification['daily_reconciliation'],
        }
        self._save_manifest()
        return {'accepted': True, 'trade_date': qualification['trade_date'], 'forward_day_count': len(dates)}


def initialize_legacy_compatibility_manifest(*args, **kwargs):
    """Legacy epoch initialization is intentionally forbidden."""
    raise RuntimeError('legacy Forward epoch initialization is forbidden')
