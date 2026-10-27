"""Data-recovery runtime routing and read-only execution visibility."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from daemon import trading_daemon as daemon
from daemon.v7_arena import arena_execution_health


class RecoveryRuntimeTests(unittest.TestCase):
    def test_recovery_is_data_job_and_hold_remains_visible(self):
        self.assertEqual(daemon.classify_runtime_job('daily_data_recovery'),'DATA_MUTATING')
        result = dict(status='HOLD',reason_codes=['PROVIDER_EMPTY'],economic_writes=0)
        with patch.object(daemon,'runtime_job_preflight',return_value=(True,dict(status='PASS'))), \
             patch.object(daemon,'write_heartbeat'), \
             patch('datasource.daily_recovery.recover_backlog',return_value=result):
            actual = daemon.execute_runtime_job('daily_data_recovery')
        self.assertEqual(actual['status'],'HOLD')
        self.assertTrue(actual['business_job_called'])
        self.assertEqual(actual['reason_codes'],['PROVIDER_EMPTY'])

    def test_denied_data_gate_cannot_start_recovery(self):
        with patch.object(daemon,'runtime_job_preflight',return_value=(False,dict(findings=['DENIED']))), \
             patch.object(daemon,'write_heartbeat'), \
             patch('datasource.daily_recovery.recover_backlog') as recover:
            result = daemon.execute_runtime_job('daily_data_recovery')
        self.assertFalse(result['business_job_called'])
        recover.assert_not_called()

    def test_retry_schedule_is_registered_at_two_bounded_slots(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        schedule = daemon.load_schedule()
        self.assertEqual(schedule['jobs']['daily_data_retry']['trigger'],'30 17,19 * * mon-fri')
        scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
        names = daemon.build_scheduler(schedule,scheduler)
        self.assertIn('daily_data_retry',names)
        self.assertIn('daily_review',names)
        self.assertFalse(schedule['jobs']['daily_data_retry']['catchup'])

    def test_daily_review_propagates_recovery_hold(self):
        hold = dict(status='HOLD',reason_codes=['DATA_GAP'])
        with patch.object(daemon,'daily_data_recovery',return_value=hold), \
             patch.object(daemon,'_update_aiq_cache_after_g6') as old_path:
            self.assertEqual(daemon.daily_review(),hold)
        old_path.assert_not_called()

    def test_business_status_is_independent_of_snapshot_liveness(self):
        stamp = dt.datetime.fromisoformat('2026-10-27T10:00:00+08:00')
        with patch('competition.data_admission.latest_completed_data_admission',return_value=(False,'DATA_ADMISSION_UNAVAILABLE:FileNotFoundError')):
            health = arena_execution_health({},stamp,dict(timestamp='2026-10-27 09:59:00',
                execution_status='HOLD_ENTRY_INPUT',exit_check_status='CHECKED',
                entry_status='BLOCKED_HISTORY_OR_MODEL',history_cutoff='20261009'))
        self.assertEqual(health['new_entry_data_status'],'HOLD')
        self.assertEqual(health['aiq']['exit_check_status'],'CHECKED')
        self.assertEqual(health['aiq']['cycle_freshness'],'FRESH')
        self.assertEqual(health['aiq']['history_cutoff'],'20261009')


if __name__ == '__main__':
    unittest.main()
