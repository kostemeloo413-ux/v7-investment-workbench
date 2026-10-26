# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_universe.py
Phase 1: Research Data & Universe Gate 单元测试
覆盖《指南》第 40 节 Gate 1 要求：
1. 历史股票池合理性（Point-in-Time 动态性）
2. 杜绝幸存者偏差（未来上市标的不得穿越至过去）
3. 数据质量校验（完整性、主键、格式）
4. 板块过滤与 ST 过滤准确性
"""
import os
import sys
import unittest
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.universe import (
    eligible_universe,
    validate_universe_integrity,
    sync_instrument_master_from_local,
    infer_board_and_exchange
)


class TestUniverse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 确保数据库中存在 instrument_master 数据
        sync_instrument_master_from_local()

    def test_infer_board_and_exchange(self):
        """测试各代码段板块与前缀推导"""
        board, ex, full = infer_board_and_exchange('600519')
        self.assertEqual(board, 'MAIN')
        self.assertEqual(ex, 'SH')
        self.assertEqual(full, 'sh600519')

        board, ex, full = infer_board_and_exchange('000001')
        self.assertEqual(board, 'MAIN')
        self.assertEqual(ex, 'SZ')
        self.assertEqual(full, 'sz000001')

        board, ex, full = infer_board_and_exchange('300750')
        self.assertEqual(board, 'GEM')
        self.assertEqual(ex, 'SZ')

        board, ex, full = infer_board_and_exchange('688981')
        self.assertEqual(board, 'STAR')
        self.assertEqual(ex, 'SH')

    def test_universe_integrity(self):
        """Gate 1: 检查数据完整性与主键约束"""
        chk = validate_universe_integrity()
        self.assertTrue(chk['passed'], f"数据完整性未通过: {chk.get('issues')}")
        self.assertGreaterEqual(chk['total_instruments'], 3000)

    def test_point_in_time_no_lookahead(self):
        """Gate 1: 检验 Point-in-Time 动态特性，严禁把后期上市的标的穿越塞入早期样本"""
        u_early = eligible_universe('20220101', min_list_days=60)
        u_late = eligible_universe('20260901', min_list_days=60)
        
        # 早期股票数量必须少于晚期，且差异必须显著（2022~2026年间有百余只新股上市）
        self.assertLess(len(u_early), len(u_late))
        self.assertGreaterEqual(len(u_late) - len(u_early), 100)

        # 任何在 u_early 中的股票，其记录的 list_date 绝不能晚于 20220101
        for s in u_early:
            self.assertLessEqual(s['list_date'], '20220101', f"发现未来上市标的穿越进入2022年样本: {s}")

    def test_filter_st_and_board(self):
        """检验 ST 股票与板块过滤有效性"""
        u_no_st = eligible_universe('20260901', exclude_st=True)
        # 确认返回的股票名不包含 ST
        for s in u_no_st:
            self.assertNotIn('ST', s['name'].upper())
            self.assertNotIn('退', s['name'])

        # 检验仅主板
        for s in u_no_st:
            self.assertEqual(s['board'], 'MAIN')


if __name__ == '__main__':
    unittest.main()
