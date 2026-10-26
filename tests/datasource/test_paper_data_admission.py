import unittest,sys,tempfile,json,sqlite3,datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from competition.activation_boundary import check_new_entry_boundary,BoundaryResult
from competition.data_admission import latest_completed_data_admission

class PaperDataAdmissionTests(unittest.TestCase):
    def result(self,decision='2026-10-12T09:35:00+08:00'):
        return BoundaryResult(True,'PASS',decision,'2026-09-30T15:30:00+08:00','2026-10-12T09:40:00+08:00','V2')

    def test_prior_day_decision_blocks_before_data_or_economic_access(self):
        with patch('competition.activation_boundary.evaluate_buy_boundary',return_value=self.result('2026-10-09T14:30:00+08:00')),patch('competition.data_admission.latest_completed_data_admission') as data:
            result=check_new_entry_boundary('2026-10-09T14:30:00+08:00')
        self.assertFalse(result.allowed);self.assertEqual(result.reason,'STALE_PRIOR_TRADING_DATE_DECISION');data.assert_not_called()

    def test_current_day_buy_requires_data_admission(self):
        for allowed in [False,True]:
            with self.subTest(allowed=allowed),patch('competition.activation_boundary.evaluate_buy_boundary',return_value=self.result()),patch('competition.data_admission.latest_completed_data_admission',return_value=(allowed,'DATA_HOLD' if not allowed else 'PASS')):
                result=check_new_entry_boundary('2026-10-12T09:35:00+08:00',context={'season_id':'V2'})
            self.assertEqual(result.allowed,allowed)

    def test_missing_latest_completed_day_fail_closes(self):
        with tempfile.TemporaryDirectory() as td,patch('runtime_config.trading_calendar_status',side_effect=lambda d:{'status':'VALID','trading_day':d.weekday()<5}):
            result=latest_completed_data_admission({'season_root':td,'season_id':'V2'},datetime.datetime.fromisoformat('2026-10-12T09:25:00+08:00'))
        self.assertFalse(result[0]);self.assertIn('FileNotFoundError',result[1])

    def test_unknown_calendar_fail_closes(self):
        with patch('runtime_config.trading_calendar_status',return_value={'status':'UNKNOWN','trading_day':False}):
            result=latest_completed_data_admission({},datetime.datetime.fromisoformat('2026-10-12T09:25:00+08:00'))
        self.assertEqual(result,(False,'DATA_ADMISSION_CALENDAR_INVALID'))
