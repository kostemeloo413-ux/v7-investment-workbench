"""Read-only admitted AIQ history, separate from the mixed market database."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import runtime_config as rc

ROOT = Path(__file__).resolve().parents[2]
POINTER = ROOT / 'data/ai_quant/input_cache/CURRENT.json'


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


class ProductionInputError(RuntimeError):
    pass


def verify_frozen_model_identity():
    manifest = json.loads((ROOT / 'models/ai_quant/production_manifest.json').read_text(encoding='utf-8'))
    for key in ('model', 'calibrator'):
        item = manifest[key]
        if file_sha(ROOT / item['production_path']) != item['sha256']:
            raise ProductionInputError('AIQ_FROZEN_' + key.upper() + '_HASH_MISMATCH')


class QualifiedQFQInput:
    """Verify immutable provenance once per cycle; every connection is read-only."""
    def __init__(self, manifest_path=None, manifest_sha256=None):
        try:
            if manifest_path is None:
                pointer = json.loads(POINTER.read_text(encoding='utf-8'))
                manifest_path = pointer['manifest_path']
                manifest_sha256 = pointer['manifest_sha256']
            self.manifest_path = Path(manifest_path).resolve()
            self.manifest_sha256 = file_sha(self.manifest_path)
            if not manifest_sha256 or self.manifest_sha256 != manifest_sha256:
                raise ProductionInputError('AIQ_MANIFEST_HASH_MISMATCH')
            m = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            verify_frozen_model_identity()
            self.manifest = m
            self.db_path = Path(m['db_path']).resolve()
            self.cutoff = m['cutoff']
            self.window = m['window']
            if (m['status'] != 'QUALIFIED_ENGINEERING_PENDING_INDEPENDENT_REVIEW'
                    or m['provider'] != 'BaoStock' or m['provider_version'] != '0.9.4'
                    or m['QFQ_anchor'] != self.cutoff or m['unexplained_missing']
                    or m['future_bar_count'] != 0 or len(self.window) != 25
                    or self.window != sorted(set(self.window)) or self.window[-1] != self.cutoff
                    or m['volume_unit'] != 'shares' or m['amount_unit'] != 'actual_CNY'
                    or m['price_contract'] != 'RAW_OHLC_TIMES_PUBLISHED_FORE_FACTOR_DIVIDED_BY_CUTOFF_FACTOR'):
                raise ProductionInputError('AIQ_INPUT_CONTRACT_INVALID')
            from competition.season_runtime import resolve_active_season_context
            context = resolve_active_season_context()
            if context['season_id'] != m['season_id']:
                raise ProductionInputError('AIQ_INPUT_SEASON_MISMATCH')
            identity = m['universe_identity']
            snapshot_path = Path(context['season_root']) / 'market_data_completeness/universe_snapshots' / (self.cutoff + '.json')
            if file_sha(snapshot_path) != identity['universe_snapshot_file_sha256']:
                raise ProductionInputError('AIQ_INPUT_UNIVERSE_MISMATCH')
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
            if set(snapshot['expected_codes']) != set(m['qualification']):
                raise ProductionInputError('AIQ_INPUT_MEMBER_MISMATCH')
            if file_sha(self.db_path) != m['db_sha256'] or file_sha(m['calendar_path']) != m['calendar_sha256']:
                raise ProductionInputError('AIQ_INPUT_DATA_OR_CALENDAR_HASH_MISMATCH')
            evidence = list(m['raw_date_evidence']) + list(m['factor_seed_evidence']) + list(m['date_factor_evidence'].values())
            evidence += [{'path':m['stock_basic_path'], 'sha256':m['stock_basic_sha256']}]
            for item in evidence:
                if file_sha(item.get('path') or item['raw_path']) != item['sha256']:
                    raise ProductionInputError('AIQ_INPUT_PROVENANCE_HASH_MISMATCH')
            start = datetime.datetime.strptime(self.window[0], '%Y%m%d').date()
            end = datetime.datetime.strptime(self.cutoff, '%Y%m%d').date()
            actual = []
            while start <= end:
                state = rc.trading_calendar_status(start)
                if state['status'] != 'VALID':
                    raise ProductionInputError('AIQ_INPUT_CALENDAR_INVALID')
                if state['trading_day']:
                    actual.append(start.strftime('%Y%m%d'))
                start += datetime.timedelta(days=1)
            if actual != self.window:
                raise ProductionInputError('AIQ_INPUT_CALENDAR_GAP')
            self.trace = {'manifest_path':str(self.manifest_path), 'manifest_sha256':self.manifest_sha256,
                          'db_path':str(self.db_path), 'db_sha256':m['db_sha256'],
                          'cutoff':self.cutoff, 'QFQ_anchor':m['QFQ_anchor'], 'symbols_read':[], 'connections':0}
            with self._connect() as conn:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ProductionInputError('AIQ_INPUT_DB_INVALID')
                if conn.execute('SELECT COUNT(*) FROM daily_kline WHERE trade_date=?',(self.cutoff,)).fetchone()[0] != identity['count']:
                    raise ProductionInputError('AIQ_INPUT_CROSS_SECTION_INCOMPLETE')
                if conn.execute("SELECT COUNT(*) FROM daily_kline WHERE code='000001'").fetchone()[0] < 21:
                    raise ProductionInputError('AIQ_INPUT_MARKET_HISTORY_INCOMPLETE')
        except ProductionInputError:
            raise
        except Exception as exc:
            raise ProductionInputError('AIQ_INPUT_UNAVAILABLE:' + type(exc).__name__) from exc

    def _connect(self):
        self.trace['connections'] += 1
        return sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True)

    def validate_as_of(self, as_of_date):
        td = str(as_of_date).replace('-', '')[:8]
        day = datetime.datetime.strptime(td, '%Y%m%d').date()
        if td < self.cutoff:
            raise ProductionInputError('AIQ_INPUT_FUTURE_BAR')
        if td == self.cutoff:
            # Historical read-only validation; cannot turn a forming bar into history.
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            if now.date() < day or (now.date() == day and now.time().replace(tzinfo=None) < datetime.time(16,30)):
                raise ProductionInputError('AIQ_INPUT_TARGET_DAY_NOT_COMPLETED')
            return
        previous = day - datetime.timedelta(days=1)
        for _ in range(40):
            state = rc.trading_calendar_status(previous)
            if state['status'] != 'VALID':
                raise ProductionInputError('AIQ_INPUT_CALENDAR_INVALID')
            if state['trading_day']:
                if previous.strftime('%Y%m%d') != self.cutoff:
                    raise ProductionInputError('AIQ_INPUT_STALE_COMPLETED_DATE')
                return
            previous -= datetime.timedelta(days=1)
        raise ProductionInputError('AIQ_INPUT_NO_COMPLETED_DATE')

    def get_kline(self, symbol):
        pure = str(symbol).zfill(6)
        if pure not in self.manifest['qualification']:
            raise ProductionInputError('AIQ_INPUT_SYMBOL_NOT_IN_V2')
        self.trace['symbols_read'].append(pure)
        with self._connect() as conn:
            # Keep provider suspension evidence in the cache; never synthesize
            # volume/amount or turn a suspended day into a model observation.
            rows = conn.execute('''SELECT trade_date,open,high,low,close,volume,amount,pct_chg
                FROM daily_kline WHERE code=? AND tradestatus='1'
                ORDER BY trade_date''', (pure,)).fetchall()
        for row in rows:
            if row[0] not in self.window or any(v is None or not math.isfinite(v) for v in row[1:7]):
                raise ProductionInputError('AIQ_INPUT_INVALID_ACTIVE_BAR')
        return rows
