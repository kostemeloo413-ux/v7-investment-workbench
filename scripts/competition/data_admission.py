"""Read-only admission of the latest completed trading day's frozen G6."""
from __future__ import annotations
import datetime
import json
import sqlite3
from pathlib import Path


def latest_completed_data_admission(context, now):
    import runtime_config as rc
    from datasource.completeness import validate_completeness_evidence, validate_target_rows
    from datasource.active_g6_v2_route import validate_v2_snapshot
    try:
        target = now.date()
        for _ in range(20):
            target -= datetime.timedelta(days=1)
            calendar = rc.trading_calendar_status(target)
            if calendar.get('status') != 'VALID':
                return False, 'DATA_ADMISSION_CALENDAR_INVALID'
            if calendar.get('trading_day') is True: break
        else: return False, 'DATA_ADMISSION_CALENDAR_UNRESOLVED'
        td = target.strftime('%Y%m%d')
        root = Path(context['season_root'])/'market_data_completeness'
        def load(path): return json.loads(path.read_text(encoding='utf-8'))
        snapshot = load(root/'universe_snapshots'/f'{td}.json')
        evidence = load(root/f'{td}.json')
        v2 = load(root/'v2_universe_snapshots'/f'{td}.json')
        valid, reasons = validate_v2_snapshot(v2, td)
        if not valid or v2.get('snapshot_readiness') != 'READY_CANDIDATE':
            return False, 'DATA_ADMISSION_V2_INVALID'
        if snapshot.get('universe_scope') != 'DATE_EFFECTIVE_UNIVERSE_V2' or set(snapshot['expected_codes']) != {s.split('.')[0] for s in v2['expected_codes']}:
            return False, 'DATA_ADMISSION_UNIVERSE_MISMATCH'
        if snapshot.get('universe_version') != 'DATE_EFFECTIVE_UNIVERSE_V2:'+v2['snapshot_identity_sha256']:
            return False, 'DATA_ADMISSION_SNAPSHOT_IDENTITY_MISMATCH'
        valid, reasons = validate_completeness_evidence(evidence, snapshot, context['season_id'], td, now=now)
        if not valid: return False, 'DATA_ADMISSION_G6_INVALID:'+','.join(reasons)
        with sqlite3.connect(Path(rc.DB_PATH).resolve().as_uri()+'?mode=ro', uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM daily_kline WHERE trade_date=?', (td,)).fetchall()
        grouped = {}
        for row in rows: grouped.setdefault(row['code'], []).append(dict(row))
        for code in snapshot['expected_codes']:
            bars = grouped.get(code, [])
            valid, reason = validate_target_rows(code, td, bars)
            if not valid or bars[0]['source'] != evidence['source_by_symbol'].get(code):
                return False, 'DATA_ADMISSION_DATABASE_INVALID:'+code
        return True, 'PASS'
    except Exception as exc:
        return False, 'DATA_ADMISSION_UNAVAILABLE:'+type(exc).__name__
