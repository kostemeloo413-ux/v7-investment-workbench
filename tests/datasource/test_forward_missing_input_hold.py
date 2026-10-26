import unittest,sys
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from ai_quant.forward_runner import ForwardPaperRunner

class ForwardMissingInputTests(unittest.TestCase):
    def test_unwired_input_holds_before_database_ranking_execution_or_telemetry(self):
        runner=object.__new__(ForwardPaperRunner)
        runner.engine=Mock();runner.ranking_engine=Mock()
        with patch('ai_quant.forward_runner.hdb._connect',side_effect=AssertionError('Must not use daily bars as live quotes')):
            result=runner.run_5minute_cycle(quotes_provider=None)
        self.assertEqual(result['execution_status'],'HOLD_MARKET_INPUT_UNAVAILABLE')
        self.assertEqual(result['execution_stats']['filled_order_count'],0)
        self.assertEqual(result['orders'],[]);self.assertEqual(result['fills'],[])
        runner.engine.ensure_pre_cycle_safety.assert_not_called();runner.ranking_engine.rank_market.assert_not_called()
