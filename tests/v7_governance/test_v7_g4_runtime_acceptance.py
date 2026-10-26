# -*- coding: utf-8 -*-
"""G4 runtime acceptance tests.

All business callables are mocked.  The suite verifies that every runtime
entrypoint reaches the same fail-closed invocation gate without starting a
daemon, scheduler, Forward cycle, or live economic mutation.
"""

import json
import sys
import unittest
import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for item in (ROOT, ROOT / 'scripts'):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from daemon import trading_daemon as daemon
from tests.v7_governance import test_v7_g2_6_runtime_safety as g26_safety


class _Lock:
    def close(self):
        return None


class _Scheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, target, **kwargs):
        self.jobs.append((target, kwargs))


class G4RuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.pass_gate = {
            'status': 'PASS', 'pass': True, 'season_id': 'V7-SEASON-20260917-V1',
            'execution_state_safe': True, 'findings': [],
        }
        self.fail_gate = {
            'status': 'FAIL', 'pass': False, 'season_id': 'V7-SEASON-20260917-V1',
            'execution_state_safe': False, 'findings': ['RECONCILIATION_FAILED'],
        }
        # Existing G4 policy tests exercise economic reconciliation in
        # isolation.  FIX2 session behavior is covered by the dedicated
        # calendar suite with injected Asia/Shanghai timestamps.
        def valid_session_verdict(job_name, **_kwargs):
            timestamp = ('2026-09-21T15:08:00+08:00'
                         if job_name == 'close_settlement'
                         else '2026-09-21T10:00:00+08:00')
            verdict = {
                'job': job_name,
                'market': 'A_SHARE',
                'timezone': 'Asia/Shanghai',
                'classification': 'ECONOMIC_MUTATING',
                'preflight_domain': 'MARKET_SESSION',
                'allowed': True,
                'pass': True,
                'status': 'PASS',
                'calendar_valid': True,
                'trading_day': True,
                'timestamp': timestamp,
                'trade_date': '2026-09-21',
                'session': 'CLOSED' if job_name == 'close_settlement' else 'OPEN',
                'reason_codes': [],
            }
            if job_name == 'close_settlement':
                verdict['close_slot'] = '2026-09-21T15:08:00+08:00'
                verdict['close_stale_end'] = '2026-09-21T19:08:00+08:00'
            return verdict

        self._session_patch = patch.object(
            daemon, 'runtime_market_session_preflight',
            side_effect=valid_session_verdict)
        self._session_patch.start()
        self.addCleanup(self._session_patch.stop)

    def _g26_fixture(self):
        fixture = g26_safety.G26Fixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_t01_policy_is_explicit(self):
        self.assertEqual(daemon.classify_runtime_job('intraday_cycle'), 'ECONOMIC_MUTATING')

    def test_t02_intraday_is_economic(self):
        self.assertEqual(daemon.classify_runtime_job('intraday_cycle'), 'ECONOMIC_MUTATING')

    def test_t03_close_is_economic(self):
        self.assertEqual(daemon.classify_runtime_job('close_settlement'), 'ECONOMIC_MUTATING')

    def test_t04_daily_review_is_data_mutating(self):
        self.assertEqual(daemon.classify_runtime_job('daily_review'), 'DATA_MUTATING')

    def test_t05_news_is_informational(self):
        self.assertEqual(daemon.classify_runtime_job('news_refresh'), 'INFORMATIONAL')

    def test_t06_precheck_is_informational(self):
        self.assertEqual(daemon.classify_runtime_job('precheck_briefing'), 'INFORMATIONAL')

    def test_t07_scheduler_target_uses_canonical_gate(self):
        scheduler = _Scheduler()
        conf = {'jobs': {'intraday_cycle': {
            'trigger': '*/5 9-14 * * 1-5', 'builtin': 'intraday_cycle'}}}
        with patch.object(daemon, 'execute_runtime_job') as invoke:
            daemon.build_scheduler(conf, scheduler)
            scheduler.jobs[0][0]()
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.kwargs['source'], 'scheduler:intraday_cycle')

    def test_t08_scheduler_hold_skips_business(self):
        business = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, source='scheduler')
        self.assertEqual(result['status'], 'HOLD')
        business.assert_not_called()

    def test_t09_scheduler_exception_fail_closed(self):
        business = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', side_effect=RuntimeError('boom')), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('close_settlement', business, source='scheduler')
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('RUNTIME_GATE_EXCEPTION:RuntimeError', result['reason_codes'])
        business.assert_not_called()

    def test_t10_allow_calls_business_once(self):
        business = MagicMock(return_value='done')
        with patch.object(daemon, 'runtime_job_preflight', return_value=(True, self.pass_gate)), \
             patch.object(daemon, 'run_with_guard', side_effect=lambda name, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, source='scheduler')
        self.assertEqual(result['status'], 'PASS')
        business.assert_called_once()

    def test_t11_catchup_uses_unified_gate(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertIn("source=f'catchup:{schedule_name}'", source)
        self.assertIn('execute_runtime_job(', source)

    def test_t12_catchup_hold_has_no_business_call(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn, source='catchup:intraday_cycle')
        self.assertFalse(result['allowed'])
        fn.assert_not_called()

    def test_t13_once_uses_unified_gate(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertIn("execute_runtime_job(args.once, source='once')", source)

    def test_t14_once_hold_has_no_business_call(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('close_settlement', fn, source='once')
        self.assertEqual(result['status'], 'HOLD')
        fn.assert_not_called()

    def test_t15_api_uses_unified_gate(self):
        source = (ROOT / 'scripts' / 'daemon' / 'app.py').read_text(encoding='utf-8')
        self.assertIn("execute_runtime_job(\n        name, fn, source='api')", source)

    def test_t16_api_hold_has_no_business_call(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn, source='api')
        self.assertEqual(result['status'], 'HOLD')
        fn.assert_not_called()

    def test_t17_remote_manual_api_is_403(self):
        from daemon.app import create_app
        app = create_app()
        route = next(r for r in app.routes if getattr(r, 'path', None) == '/api/run/{job}')
        request = MagicMock(); request.client = MagicMock(host='203.0.113.8')
        with self.assertRaises(Exception) as raised:
            route.endpoint('intraday_cycle', request)
        self.assertEqual(getattr(raised.exception, 'status_code', None), 403)

    def test_t18_local_manual_api_is_controlled(self):
        from daemon.app import create_app
        app = create_app()
        route = next(r for r in app.routes if getattr(r, 'path', None) == '/api/run/{job}')
        request = MagicMock(); request.client = MagicMock(host='127.0.0.1')
        with patch('daemon.app._trigger_job', return_value={'status': 'QUEUED_FOR_RUNTIME_GATE'}) as trigger:
            result = route.endpoint('intraday_cycle', request)
        self.assertEqual(result['status'], 'QUEUED_FOR_RUNTIME_GATE')
        trigger.assert_called_once_with('intraday_cycle')

    def test_t19_unknown_job_is_404(self):
        from daemon.app import create_app
        app = create_app()
        route = next(r for r in app.routes if getattr(r, 'path', None) == '/api/run/{job}')
        request = MagicMock(); request.client = MagicMock(host='127.0.0.1')
        with self.assertRaises(Exception) as raised:
            route.endpoint('unknown_job', request)
        self.assertEqual(getattr(raised.exception, 'status_code', None), 404)

    def test_t20_background_thread_cannot_bypass(self):
        source = (ROOT / 'scripts' / 'daemon' / 'app.py').read_text(encoding='utf-8')
        self.assertNotIn('run_with_guard(f\'manual_', source)
        self.assertIn("source='api'", source)

    def test_t21_missing_active_season_fails_closed(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, {
                'status': 'FAIL', 'findings': ['ACTIVE_SEASON_MISSING']})), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertEqual(result['status'], 'HOLD')
        fn.assert_not_called()

    def test_t22_malformed_active_season_fails_closed(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(True, None)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertIn('RUNTIME_GATE_MALFORMED', result['reason_codes'])
        fn.assert_not_called()

    def test_t23_strategy_reconciliation_failure_holds(self):
        with patch.object(daemon, 'startup_runtime_gate', return_value=self.fail_gate):
            ok, result = daemon.runtime_job_preflight('intraday_cycle')
        self.assertFalse(ok); self.assertEqual(result['status'], 'FAIL')

    def test_t24_aiq_failure_holds(self):
        gate = dict(self.fail_gate, findings=['AIQ_RECONCILIATION_FAILED'])
        with patch.object(daemon, 'startup_runtime_gate', return_value=gate):
            ok, _ = daemon.runtime_job_preflight('close_settlement')
        self.assertFalse(ok)

    def test_t25_arena_failure_holds(self):
        gate = dict(self.fail_gate, findings=['ARENA_RECONCILIATION_FAILED'])
        with patch.object(daemon, 'startup_runtime_gate', return_value=gate):
            ok, _ = daemon.runtime_job_preflight('intraday_cycle')
        self.assertFalse(ok)

    def test_t26_gate_exception_is_hold(self):
        fn = MagicMock()
        with patch.object(daemon, 'startup_runtime_gate', side_effect=RuntimeError('bad')), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('RUNTIME_GATE_EXCEPTION:RuntimeError', result['reason_codes'])
        fn.assert_not_called()

    def test_t27_no_business_on_any_hold(self):
        for source in ('scheduler', 'catchup', 'once', 'api'):
            fn = MagicMock()
            with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
                 patch.object(daemon, 'write_heartbeat'):
                daemon.execute_runtime_job('intraday_cycle', fn, source=source)
            fn.assert_not_called()

    def test_t28_failure_reason_is_deterministic(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertEqual(result['reason_codes'], ['RECONCILIATION_FAILED'])

    def test_t29_hold_does_not_create_order_or_fill(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertEqual(fn.call_count, 0)

    def test_t30_hold_does_not_increment_forward(self):
        fn = MagicMock()
        with patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', fn)
        self.assertFalse(result['allowed'])

    def test_t31_startup_gate_is_before_scheduler(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertLess(source.index("runtime_job_preflight('daemon_boot')"), source.index('scheduler.start()'))

    def test_t32_startup_fail_does_not_start_scheduler(self):
        with patch.object(daemon, 'acquire_singleton', return_value=_Lock()), \
             patch.object(daemon, 'runtime_job_preflight', return_value=(False, self.fail_gate)), \
             patch.object(daemon, 'BackgroundScheduler') as scheduler:
            result = daemon.run_acceptance_canary({'jobs': {}})
        self.assertEqual(result['exit_status'], 'FAIL')
        scheduler.assert_not_called()

    def test_t33_startup_pass_constructs_scheduler(self):
        fake = _Scheduler()
        names = {name: {'builtin': name, 'trigger': '*/5 * * * *'} for name in daemon.BUILTINS}
        with patch.object(daemon, 'acquire_singleton', return_value=_Lock()), \
             patch.object(daemon, 'runtime_job_preflight', return_value=(True, self.pass_gate)), \
             patch.object(daemon, 'BackgroundScheduler', return_value=fake):
            result = daemon.run_acceptance_canary({'jobs': names})
        self.assertEqual(result['exit_status'], 'PASS')
        self.assertTrue(fake.jobs)

    def test_t34_canary_registers_required_jobs(self):
        names = {name: {'builtin': name, 'trigger': '*/5 * * * *'} for name in daemon.BUILTINS}
        fake = _Scheduler()
        with patch.object(daemon, 'acquire_singleton', return_value=_Lock()), \
             patch.object(daemon, 'runtime_job_preflight', return_value=(True, self.pass_gate)), \
             patch.object(daemon, 'BackgroundScheduler', return_value=fake):
            result = daemon.run_acceptance_canary({'jobs': names})
        self.assertEqual(result['exit_status'], 'PASS')
        self.assertEqual(set(result['registered_jobs']), set(daemon.BUILTINS))

    def test_t35_singleton_duplicate_blocks(self):
        with patch.object(daemon, 'acquire_singleton', return_value=None):
            result = daemon.run_acceptance_canary({'jobs': {}})
        self.assertEqual(result['reason_codes'], ['DAEMON_SINGLETON_HELD'])

    def test_t36_startup_pass_does_not_clear_entry_disabled(self):
        fixture = self._g26_fixture()
        halt_path = Path(fixture.ctx['governance_safety_path'])
        before = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertIs(before['new_entry_disabled'], True)
        result = __import__('competition.season_runtime', fromlist=['startup_runtime_gate']).startup_runtime_gate(
            update_safety=True, context=fixture.ctx)
        after = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'PASS')
        self.assertIs(after['new_entry_disabled'], True)
        self.assertEqual(before['new_entry_disabled'], after['new_entry_disabled'])

    def test_t37_startup_pass_does_not_enable_trading(self):
        fixture = self._g26_fixture()
        halt_path = Path(fixture.ctx['governance_safety_path'])
        before = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertIs(before['trading_enabled'], False)
        result = __import__('competition.season_runtime', fromlist=['startup_runtime_gate']).startup_runtime_gate(
            update_safety=True, context=fixture.ctx)
        after = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'PASS')
        self.assertIs(after['trading_enabled'], False)
        self.assertEqual(before['trading_enabled'], after['trading_enabled'])

    def test_t38_real_trading_is_false(self):
        fixture = self._g26_fixture()
        halt_path = Path(fixture.ctx['governance_safety_path'])
        before = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertIs(before['real_trading'], False)
        result = __import__('competition.season_runtime', fromlist=['startup_runtime_gate']).startup_runtime_gate(
            update_safety=True, context=fixture.ctx)
        after = json.loads(halt_path.read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'PASS')
        self.assertIs(after['real_trading'], False)
        self.assertEqual(before['real_trading'], after['real_trading'])

    def test_t39_watchdog_spawned_daemon_reaches_main_gate(self):
        source = (ROOT / 'scripts' / 'daemon' / 'watchdog.py').read_text(encoding='utf-8')
        self.assertIn('trading_daemon.py', source)
        self.assertIn("'--no-browser'", source)
        self.assertIn("runtime_job_preflight('daemon_boot')", (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8'))

    def test_t40_unsafe_startup_has_no_scheduler(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertIn('禁止启动 scheduler', source)

    def test_t41_watchdog_restart_cannot_bypass(self):
        source = (ROOT / 'scripts' / 'daemon' / 'watchdog.py').read_text(encoding='utf-8')
        self.assertNotIn('--once', source)

    def test_t42_health_separates_safety_fields(self):
        source = (ROOT / 'scripts' / 'daemon' / 'app.py').read_text(encoding='utf-8')
        for field in ('execution_state_safe', 'new_entry_disabled', 'trading_enabled', 'real_trading'):
            self.assertIn(field, source)

    def test_t43_alive_does_not_claim_trading(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        canary = source[source.index('def run_acceptance_canary'):source.index('def start_web')]
        self.assertIn("'trading_enabled': False", canary)
        self.assertIn("'forward_enabled': False", canary)

    def test_t44_canary_real_false(self):
        with patch.object(daemon, 'acquire_singleton', return_value=None):
            result = daemon.run_acceptance_canary({'jobs': {}})
        self.assertFalse(result['real_trading'])

    def test_t45_canary_uses_real_startup_gate(self):
        with patch.object(daemon, 'acquire_singleton', return_value=None), \
             patch.object(daemon, 'runtime_job_preflight') as preflight:
            daemon.run_acceptance_canary({'jobs': {}})
        preflight.assert_not_called()  # singleton fail is fail-closed
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertIn("runtime_job_preflight('daemon_boot')", source)

    def test_t46_canary_reports_registered_jobs(self):
        self.assertIn('registered_jobs', daemon.run_acceptance_canary.__code__.co_consts or ())

    def test_t47_canary_business_count_zero(self):
        names = {name: {'builtin': name, 'trigger': '*/5 * * * *'} for name in daemon.BUILTINS}
        with patch.object(daemon, 'acquire_singleton', return_value=_Lock()), \
             patch.object(daemon, 'runtime_job_preflight', return_value=(True, self.pass_gate)):
            result = daemon.run_acceptance_canary(names and {'jobs': names})
        self.assertEqual(result['scheduler_business_execution_count'], 0)

    def test_t48_canary_does_not_start_process(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        canary = source[source.index('def run_acceptance_canary'):source.index('def start_web')]
        self.assertNotIn('scheduler.start()', canary)
        self.assertNotIn('start_web(', canary)

    def test_t49_canary_does_not_start_watchdog(self):
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        canary = source[source.index('def run_acceptance_canary'):source.index('def start_web')]
        self.assertNotIn('watchdog', canary.lower())

    def test_t50_canary_has_zero_economic_mutation(self):
        names = {name: {'builtin': name, 'trigger': '*/5 * * * *'} for name in daemon.BUILTINS}
        with patch.object(daemon, 'acquire_singleton', return_value=_Lock()), \
             patch.object(daemon, 'runtime_job_preflight', return_value=(True, self.pass_gate)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.run_acceptance_canary({'jobs': names})
        self.assertEqual(result['scheduler_business_execution_count'], 0)
        self.assertEqual(result['exit_status'], 'PASS')

    def test_t51_data_mutating_dispatches_to_data_preflight(self):
        with patch.object(daemon, 'data_runtime_preflight', return_value=(True, {
                'status': 'PASS', 'pass': True, 'preflight_domain': 'DATA'})) as data_gate, \
             patch.object(daemon, 'startup_runtime_gate') as economic_gate:
            ok, result = daemon.runtime_job_preflight('daily_review')
        self.assertTrue(ok)
        self.assertEqual(result['preflight_domain'], 'DATA')
        data_gate.assert_called_once_with('daily_review')
        economic_gate.assert_not_called()

    def test_t52_economic_failure_does_not_block_daily_review(self):
        business = MagicMock(return_value='data-ok')
        with patch.object(daemon, 'data_runtime_preflight', return_value=(True, {
                'status': 'PASS', 'pass': True, 'preflight_domain': 'DATA'})), \
             patch.object(daemon, 'startup_runtime_gate', return_value=self.fail_gate) as economic_gate, \
             patch.object(daemon, 'run_with_guard', side_effect=lambda name, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('daily_review', business, source='test')
        self.assertEqual(result['status'], 'PASS')
        business.assert_called_once()
        economic_gate.assert_not_called()

    def test_t53_economic_failure_blocks_intraday(self):
        business = MagicMock()
        with patch.object(daemon, 'startup_runtime_gate', return_value=self.fail_gate), \
             patch.object(daemon, 'run_with_guard') as guarded, \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, source='test')
        self.assertEqual(result['status'], 'HOLD')
        business.assert_not_called()
        guarded.assert_not_called()

    def test_t54_economic_failure_blocks_close_settlement(self):
        business = MagicMock()
        with patch.object(daemon, 'startup_runtime_gate', return_value=self.fail_gate), \
             patch.object(daemon, 'run_with_guard') as guarded, \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('close_settlement', business, source='test')
        self.assertEqual(result['status'], 'HOLD')
        business.assert_not_called()
        guarded.assert_not_called()

    def test_t55_data_preflight_failure_blocks_daily_review(self):
        business = MagicMock()
        with patch.object(daemon, 'data_runtime_preflight', return_value=(False, {
                'status': 'FAIL', 'pass': False, 'findings': ['DATA_WRITER_UNAVAILABLE']})), \
             patch.object(daemon, 'run_with_guard') as guarded, \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('daily_review', business, source='test')
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('DATA_WRITER_UNAVAILABLE', result['reason_codes'])
        business.assert_not_called()
        guarded.assert_not_called()

    def test_t56_data_preflight_exception_fail_closes(self):
        business = MagicMock()
        with patch.object(daemon, 'data_runtime_preflight', side_effect=RuntimeError('data gate')), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('daily_review', business, source='test')
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('RUNTIME_GATE_EXCEPTION:RuntimeError', result['reason_codes'])
        business.assert_not_called()

    def test_t57_data_preflight_malformed_result_fail_closes(self):
        business = MagicMock()
        with patch.object(daemon, 'data_runtime_preflight', return_value=(True, None)), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('daily_review', business, source='test')
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(result['reason_codes'], ['RUNTIME_GATE_MALFORMED'])
        business.assert_not_called()

    def test_t58_new_entry_disabled_does_not_block_data_preflight(self):
        fixture = self._g26_fixture()
        with patch('competition.season_runtime.resolve_active_season_context', return_value=fixture.ctx):
            ok, result = daemon.data_runtime_preflight('daily_review')
        self.assertTrue(ok)
        self.assertEqual(result['preflight_domain'], 'DATA')

    def test_t59_trading_disabled_does_not_block_data_preflight(self):
        fixture = self._g26_fixture()
        with patch('competition.season_runtime.resolve_active_season_context', return_value=fixture.ctx):
            ok, result = daemon.data_runtime_preflight('daily_review')
        self.assertTrue(ok)
        self.assertEqual(result['preflight_domain'], 'DATA')

    def test_t60_daily_review_path_has_no_economic_mutation(self):
        fixture = self._g26_fixture()
        ledger_root = Path(fixture.ctx['competition_ledger_root'])
        before = {p.relative_to(ledger_root).as_posix(): p.read_bytes()
                  for p in ledger_root.rglob('*') if p.is_file()}
        business = MagicMock(return_value='mocked-data-maintenance')
        with patch.object(daemon, 'data_runtime_preflight', return_value=(True, {
                'status': 'PASS', 'pass': True, 'preflight_domain': 'DATA'})), \
             patch.object(daemon, 'run_with_guard', side_effect=lambda name, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('daily_review', business, source='test')
        after = {p.relative_to(ledger_root).as_posix(): p.read_bytes()
                 for p in ledger_root.rglob('*') if p.is_file()}
        self.assertEqual(result['status'], 'PASS')
        business.assert_called_once()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
