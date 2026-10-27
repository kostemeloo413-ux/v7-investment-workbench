"""Synthetic source fixtures only, isolated SQLite and temporary state."""
import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from datasource import daily_recovery as recovery, db as hdb

TARGET = '20261026'
NOW = dt.datetime.fromisoformat('2026-10-27T10:00:00+08:00')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root/'market.db'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(hdb.SCHEMA)
        self.clock = patch.object(recovery,'now',return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def capture_fixture(self, root=None, target=TARGET, extra_identities=()):
        root = root or self.root/'capture'
        (root/'raw').mkdir(parents=True)
        (root/'normalized').mkdir()
        data = dict(stock_basic=[dict(code='sh.600000',code_name='ALPHA',type='1',status='1',ipoDate='2000-01-01',outDate='')],
            all_stock=[dict(code='sh.600000',tradeStatus='1',code_name='ALPHA')],
            daily=[dict(code='sh.600000',date=dt.datetime.strptime(target,'%Y%m%d').date().isoformat(),open='10',high='11',low='9',close='10.5',
                        volume='100000',amount='1000000',pctChg='5',tradestatus='1',isST='0',adjustflag='3')])
        data['stock_basic'].extend(extra_identities)
        cap = dict(provider='BaoStock',package_version='0.9.4',trade_date=target,capture_complete=True,protocol_call_count=3,
            completed_at=NOW.isoformat(),sources={},protocol_calls=[])
        for i,(name,rows) in enumerate(data.items()):
            payload = json.dumps(rows).encode()
            normalized = root/'normalized'/f'{name}.json'
            normalized.write_bytes(payload)
            raw = root/'raw'/f'fixture_{name}.bin'
            raw.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            frame = dict(raw_file=raw.name,response_sha256=digest,ordinal=i+1,request_sha256='f'*64,response_bytes=len(payload))
            cap['protocol_calls'].append(frame)
            cap['sources'][name] = dict(provider='BaoStock',query_status='SUCCESS',result_code=0,
                row_count=len(rows),unique_count=len(rows),trade_date=None if name=='stock_basic' else target,
                normalized_path=str(normalized),sha256=digest,source_id='SYNTHETIC_TEST_ONLY:'+name+':'+digest,
                observed_at=NOW.isoformat(),response_sha256=hashlib.sha256(len(payload).to_bytes(8,'big')+payload).hexdigest(),
                raw_protocol_responses=[frame])
        (root/'capture_manifest.json').write_text(json.dumps(cap),encoding='utf-8')
        return root,recovery.verify_capture(root,target)

    def test_completed_day_and_contiguous_gap(self):
        self.assertEqual(recovery.completed_trade_date(NOW), '20261026')
        self.assertEqual(recovery.completed_trade_date(NOW.replace(hour=17)), '20261027')
        self.assertEqual(recovery.missing_dates('20261009','20261013'),['20261012','20261013'])
        with patch.object(recovery.rc,'trading_calendar_status',return_value={'status':'INVALID'}):
            with self.assertRaisesRegex(RuntimeError,'CALENDAR_INVALID'):
                recovery.completed_trade_date(NOW)

    def test_approved_factor_ceiling_never_resets_old_requests(self):
        from ai_quant.cache_rollover import protocol_allowance
        ledger = {'requests':[{'trade_date':'20261014'} for _ in range(2)]}
        self.assertEqual(protocol_allowance(ledger,'20261014',20,0,True),(2,18))
        self.assertEqual(protocol_allowance(ledger,'20261014',32,0,True,32),(2,30))
        ledger['requests'] += [{'trade_date':'20261014'} for _ in range(30)]
        with self.assertRaisesRegex(RuntimeError,'BUDGET_EXHAUSTED'):
            protocol_allowance(ledger,'20261014',32,0,True,32)
        with self.assertRaisesRegex(RuntimeError,'BUDGET_INVALID'):
            protocol_allowance(ledger,'20261014',41,0,True,41)
        self.assertEqual(protocol_allowance({'requests':[{'trade_date':'20261016'}]*2},'20261016',40,0,True,40),(2,38))
        from ai_quant.cache_rollover import approved_daily_protocol_limit
        self.assertEqual(approved_daily_protocol_limit('20261016'),40)
        self.assertEqual(approved_daily_protocol_limit('20261023'),40)
        self.assertEqual(approved_daily_protocol_limit('20261022'),32)

    def test_capture_hash_tamper_rejected(self):
        root,_ = self.capture_fixture()
        (root/'normalized/daily.json').write_text('[]')
        with self.assertRaisesRegex(RuntimeError,'HASH_MISMATCH'):
            recovery.verify_capture(root,TARGET)

    def test_official_delisting_date_resolves_only_verified_exclusions(self):
        _,cap = self.capture_fixture(target='20261019',extra_identities=[dict(code='sh.601059',
            code_name='DELISTED',type='1',status='0',ipoDate='2023-02-01',outDate='2026-10-19')])
        before = json.dumps(cap,sort_keys=True)
        snapshot = recovery.build_v2_snapshot(cap,trade_date='20261019')
        self.assertEqual(snapshot['snapshot_readiness'],'READY_CANDIDATE')
        self.assertEqual(snapshot['expected_codes'],['600000.SH'])
        self.assertEqual(snapshot['qualification_failures'][0]['qualification_reason'],'DELISTED')
        self.assertEqual(json.dumps(cap,sort_keys=True),before)  # original provider evidence untouched

    def test_unknown_missing_status_cannot_be_reclassified_to_pass(self):
        _,cap = self.capture_fixture(target='20261019',extra_identities=[dict(code='sh.601999',
            code_name='UNKNOWN',type='1',status='0',ipoDate='2000-01-01',outDate='2026-10-19')])
        with self.assertRaisesRegex(RuntimeError,'V2_SNAPSHOT_NOT_READY'):
            recovery.build_v2_snapshot(cap,trade_date='20261019')

    def test_empty_first_attempt_then_success_is_reused_after_restart(self):
        calls = []
        def capture(target, root):
            calls.append(root)
            if len(calls)==1:return dict(capture_complete=False,protocol_call_count=3,stop_reason='EMPTY')
            return self.capture_fixture(root)[1]
        with patch.object(recovery,'bounded_baostock_session',side_effect=lambda **kw:contextlib.nullcontext()), \
             patch.object(recovery,'capture_target_date',side_effect=capture) as fetch:
            with self.assertRaisesRegex(RuntimeError,'INCOMPLETE'):
                recovery.capture_for_date(self.root,TARGET)
            first,_ = recovery.capture_for_date(self.root,TARGET)
            again,_ = recovery.capture_for_date(self.root,TARGET)
            self.assertEqual(first,again)
            self.assertEqual(fetch.call_count,2)

    def test_dry_run_has_no_provider_or_state_mutation(self):
        context = dict(season_root=str(self.root/'season'),season_id='TEST-SEASON')
        with patch('competition.season_runtime.resolve_active_season_context',return_value=context), \
             patch('ai_quant.production_input.QualifiedQFQInput',return_value=SimpleNamespace(cutoff='20261023')), \
             patch.object(recovery,'capture_for_date') as fetch:
            result = recovery.inspect_backlog(target_date=TARGET)
        self.assertEqual([d['trade_date'] for d in result['dates']],[TARGET])
        self.assertEqual(result['network_requests'],0)
        self.assertFalse(Path(context['season_root']).exists())
        fetch.assert_not_called()

    def test_capture_retries_have_one_shared_daily_budget(self):
        def failed_capture(target, root):
            return dict(capture_complete=False,protocol_call_count=5,stop_reason='EMPTY')
        with patch.object(recovery,'bounded_baostock_session',side_effect=lambda **kw:contextlib.nullcontext()), \
             patch.object(recovery,'capture_target_date',side_effect=failed_capture) as fetch:
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError,'INCOMPLETE'):
                    recovery.capture_for_date(self.root,TARGET)
            with self.assertRaisesRegex(RuntimeError,'BUDGET_EXHAUSTED'):
                recovery.capture_for_date(self.root,TARGET)
            self.assertEqual(fetch.call_count,3)
        ledger = recovery.read(self.root/'recovery_runs'/TARGET/'capture_budget.json')
        self.assertEqual(len(ledger['attempts']),3)
        self.assertTrue(all(a['protocol_calls']==5 for a in ledger['attempts']))

    def test_authorization_conflict_and_resume(self):
        _,cap = self.capture_fixture()
        bars = recovery.captured_bars(cap,['600000'],TARGET)
        denied = lambda *a:(False,'TEST_DENIED')
        with self.assertRaisesRegex(RuntimeError,'DB_WRITER_DENIED'):
            recovery.insert_verified_bars(self.db,bars,authorize=denied)
        allowed = lambda *a:(True,'PASS')
        self.assertEqual(recovery.insert_verified_bars(self.db,bars,authorize=allowed),1)
        self.assertEqual(recovery.insert_verified_bars(self.db,bars,authorize=allowed),0)
        extra = dict(bars[0],code='000001')
        conflict = dict(bars[0],close=10.6)
        with self.assertRaisesRegex(RuntimeError,'BAR_CONFLICT'):
            recovery.insert_verified_bars(self.db,[extra,conflict],authorize=allowed)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM daily_kline').fetchone()[0],1)

    def test_full_publication_uses_real_frozen_validators_and_retains_hold(self):
        root,cap = self.capture_fixture()
        context = dict(season_root=str(self.root/'season'),season_id='TEST-SEASON')
        health = Path(context['season_root'])/'market_data_completeness/health'/f'{TARGET}.json'
        health.parent.mkdir(parents=True)
        health.write_text('{"status":"HOLD","reason":"original synthetic fixture"}')
        original = health.read_bytes()
        with patch.object(recovery.rc,'DB_PATH',str(self.db)), \
             patch.object(hdb._GOVERNANCE_POLICY,'validate_db_write',return_value=(True,'TEST')):
            result = recovery.publish_date(context,TARGET,root,cap)
            self.assertEqual(result['status'],'PASS')
            base = health.parents[1]
            self.assertEqual(recovery.read(base/f'{TARGET}.json')['successful_symbol_count'],1)
            acceptance = recovery.read(next((base/'recovery_acceptances').glob('*.json')))
            trusted = recovery.read(acceptance['trusted_validator_path'])
            self.assertEqual(trusted['status'],'VERIFIED')
            self.assertTrue(trusted['verify_result'])
            self.assertEqual(health.read_bytes(),original)
            self.assertEqual(acceptance['formal_g6_sha256'],recovery.sha(base/f'{TARGET}.json'))
            again = recovery.publish_date(context,TARGET,root,cap)
            self.assertEqual(again['inserted_rows'],0)


if __name__ == '__main__':
    unittest.main()
