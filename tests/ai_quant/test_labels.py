# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_labels.py
Phase 2: Label Factory 严格数学与时序契约单元测试
严格对齐《R2_LABEL_CONTRACT_FINAL_V2.md》与《Phase R2-Code-Patch 执行指令》：
1. Test 1: 双轨 Entry Proxy 入场时序与基准价格计算测试 (VWAP vs Next Open)
2. Test 2: Entry 日极值隔离测试 (严格从 entry_idx + 1 起算，严禁包含 Entry 当日 High/Low)
3. Test 3: 未来 N 日时间跨度严格消歧测试 (修订1: 实际 Entry Date 之后第 N 个完整交易日收盘，跨度完全对称)
4. Test 4: 纯净毛收益与成本模型解耦测试 (修订2: Market Label 纯净毛收益 + 独立成本模型派生)
5. Test 5: 滑点 4 档敏感性注入测试 (0.05%, 0.10%, 0.20%, 0.30%)
6. Test 6: 尾部未成熟样本 Null 保护与成熟度闸门测试 (未 mature 坚决不赋假数据)
7. Test 7: 真实 SQLite 数据库集成回归测试 (确保 000001 正确构建 L-INTRADAY-V1.0)
"""
import os
import sys
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.labels import (
    calculate_single_stock_labels,
    derive_simulated_net_return,
    LabelFactory,
    LABEL_VERSION,
    ENTRY_PROXY_VWAP,
    ENTRY_PROXY_NEXT_OPEN
)


class TestLabelsContract(unittest.TestCase):

    def setUp(self):
        # 构造确定性的合成日K线序列测试数学正确性
        # 日期, open, high, low, close, volume, amount
        # 设定：从第0日到第6日 (共7根K线)
        self.mock_klines = [
            # T=0: 0101 信号日
            ('20260101', 10.0, 10.5, 9.8, 10.2, 10000, 102000),  # T=0: vwap = 102000/10000 = 10.20
            # T=1: 0102
            ('20260102', 10.4, 11.0, 10.1, 10.8, 15000, 157500),  # T=1: open = 10.40, close = 10.80
            # T=2: 0103
            ('20260103', 10.7, 11.5, 10.4, 11.2, 20000, 220000),  # T=2: close = 11.20
            # T=3: 0104
            ('20260104', 11.1, 11.3, 10.0, 10.5, 12000, 126000),  # T=3: close = 10.50
            # T=4: 0105
            ('20260105', 10.4, 10.6,  9.5,  9.8, 18000, 180000),  # T=4: low = 9.50, close = 9.80
            # T=5: 0106
            ('20260106',  9.9, 12.0,  9.7, 11.8, 25000, 287500),  # T=5: high = 12.00, close = 11.80
            # T=6: 0107
            ('20260107', 11.7, 12.2, 11.5, 12.0, 22000, 264000),  # T=6: high = 12.20, close = 12.00
        ]

    def test_1_dual_entry_proxies(self):
        """Test 1: 验证双轨 Entry Proxy 的入场时序与基准价格"""
        slip = 0.0010
        # 轨一：主轨 VWAP Proxy (T日入场)
        lbl_vwap = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                                 entry_proxy_mode='vwap', slippage_rate=slip)
        self.assertEqual(lbl_vwap['entry_proxy_type'], ENTRY_PROXY_VWAP)
        self.assertEqual(lbl_vwap['signal_date'], '20260101')
        self.assertEqual(lbl_vwap['entry_date'], '20260101')
        # T=0 的 vwap = 102000 / 10000 = 10.20 -> entry_price = 10.20 * 1.001 = 10.2102
        expected_vwap_entry = round(10.20 * (1.0 + slip), 4)
        self.assertEqual(lbl_vwap['entry_price'], expected_vwap_entry)
        self.assertEqual(lbl_vwap['slippage_param'], slip)

        # 轨二：辅轨 Next Open Proxy (T+1日入场)
        lbl_open = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                                 entry_proxy_mode='next_open', slippage_rate=slip)
        self.assertEqual(lbl_open['entry_proxy_type'], ENTRY_PROXY_NEXT_OPEN)
        self.assertEqual(lbl_open['signal_date'], '20260101')
        self.assertEqual(lbl_open['entry_date'], '20260102')
        # T=1 的 open = 10.40 -> entry_price = 10.40 * 1.001 = 10.4104
        expected_open_entry = round(10.40 * (1.0 + slip), 4)
        self.assertEqual(lbl_open['entry_price'], expected_open_entry)

    def test_2_entry_date_extreme_isolation(self):
        """Test 2: 验证 Entry 日极值隔离，严禁从 Entry 日计算 MAE/MFE"""
        # 以 VWAP 模式为例，entry_date = 20260101 (idx=0)
        # 0101 当天 low=9.8, high=10.5
        # 次日 0102 low=10.1, 0103 low=10.4, 0104 low=10.0, 0105 low=9.5
        # 极值扫描必须严格从 entry_idx + 1 (0102) 起算，严禁碰 0101 的 9.8！
        lbl = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                            entry_proxy_mode='vwap', slippage_rate=0.001,
                                            horizons=(1, 2, 3, 5))
        entry_p = lbl['entry_price']
        # 0102~0106 (entry_idx+1 ~ entry_idx+5) 中：
        # 最低价在 0105 为 9.5
        # 最高价在 0106 为 12.0 (注意：0101的高点10.5不参与，0107的高点12.2超过5日也不参与)
        expected_mae = round((9.5 / entry_p) - 1.0, 6)
        expected_mfe = round((12.0 / entry_p) - 1.0, 6)
        self.assertEqual(lbl['realized_mae_daily_bar'], expected_mae)
        self.assertEqual(lbl['realized_mfe_daily_bar'], expected_mfe)
        # 确认不含废除的 expected_mae / expected_mfe 字段
        self.assertNotIn('expected_mae', lbl)
        self.assertNotIn('expected_mfe', lbl)

    def test_3_future_nd_time_alignment(self):
        """Test 3: 验证未来 N 日时间跨度严格消歧 (修订1: 实际 Entry Date 之后第 N 个完整交易日)"""
        # 1. VWAP 模式: entry_idx = 0 (0101)
        # 1d -> entry_idx + 1 = 1 (0102, close=10.80)
        # 5d -> entry_idx + 5 = 5 (0106, close=11.80)
        lbl_vwap = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                                 entry_proxy_mode='vwap', slippage_rate=0.001,
                                                 horizons=(1, 2, 3, 5))
        p_entry_v = lbl_vwap['entry_price']
        exp_ret_1d_v = round((10.80 / p_entry_v) - 1.0, 6)
        exp_ret_5d_v = round((11.80 / p_entry_v) - 1.0, 6)
        self.assertEqual(lbl_vwap['future_1d_gross_return'], exp_ret_1d_v)
        self.assertEqual(lbl_vwap['future_5d_gross_return'], exp_ret_5d_v)

        # 2. Next Open 模式: entry_idx = 1 (0102)
        # 1d -> entry_idx + 1 = 2 (0103, close=11.20)
        # 5d -> entry_idx + 5 = 6 (0107, close=12.00)
        lbl_open = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                                 entry_proxy_mode='next_open', slippage_rate=0.001,
                                                 horizons=(1, 2, 3, 5))
        p_entry_o = lbl_open['entry_price']
        exp_ret_1d_o = round((11.20 / p_entry_o) - 1.0, 6)
        exp_ret_5d_o = round((12.00 / p_entry_o) - 1.0, 6)
        self.assertEqual(lbl_open['future_1d_gross_return'], exp_ret_1d_o)
        self.assertEqual(lbl_open['future_5d_gross_return'], exp_ret_5d_o)

    def test_4_gross_return_and_cost_decoupling(self):
        """Test 4: 验证纯净毛收益与成本解耦，以及派生函数 derive_simulated_net_return"""
        lbl = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                            entry_proxy_mode='vwap', slippage_rate=0.001)
        gross_5d = lbl['future_5d_gross_return']
        self.assertIsNotNone(gross_5d)
        # 确保 label 字典中绝无扣除规费后的复合 net return 字段
        self.assertNotIn('future_return_5d', lbl)

        # 调用独立成本模型派生净收益
        sim_net_5d = derive_simulated_net_return(gross_5d, trade_amount=100000.0,
                                                 commission_rate=0.0003, min_commission=5.0,
                                                 stamp_tax_rate=0.0005, slippage_rate=0.001,
                                                 entry_price_already_slipped=True)
        # 纯净毛收益减去规费与卖出滑点后，净收益必然严格小于毛收益
        self.assertLess(sim_net_5d, gross_5d)

    def test_5_slippage_sensitivity_injection(self):
        """Test 5: 验证 4 档滑点参数化注入敏感性 (0.05%, 0.10%, 0.20%, 0.30%)"""
        slips = [0.0005, 0.0010, 0.0020, 0.0030]
        prev_price = 0.0
        for s in slips:
            lbl = calculate_single_stock_labels(self.mock_klines, current_idx=0,
                                                entry_proxy_mode='vwap', slippage_rate=s)
            self.assertEqual(lbl['slippage_param'], s)
            # 滑点越大，买入成本越高
            self.assertGreater(lbl['entry_price'], prev_price)
            prev_price = lbl['entry_price']

    def test_6_maturity_and_null_protection(self):
        """Test 6: 验证未成熟截面 Null 保护与成熟度闸门"""
        # 在 idx=5 (0106) 时：
        # 对于 VWAP 模式，entry_idx = 5，后面仅剩 1 根 K 线 (0107, idx=6)，无法满足 5d 要求
        lbl_im = calculate_single_stock_labels(self.mock_klines, current_idx=5,
                                               entry_proxy_mode='vwap', horizons=(1, 2, 3, 5))
        self.assertFalse(lbl_im['is_mature'])
        self.assertIsNotNone(lbl_im['future_1d_gross_return'])  # 1d 有数据
        self.assertIsNone(lbl_im['future_5d_gross_return'])     # 5d 未成熟必须为 None
        self.assertIsNone(lbl_im['is_profitable_5d'])

        # 对于倒数第 1 根 (idx=6)，Next Open 甚至无法入场
        lbl_no_entry = calculate_single_stock_labels(self.mock_klines, current_idx=6,
                                                     entry_proxy_mode='next_open')
        self.assertFalse(lbl_no_entry['is_mature'])
        self.assertIsNone(lbl_no_entry['entry_price'])

    def test_7_real_db_regression(self):
        """Test 7: 真实 SQLite 数据库中回归跑通，验证版本与完整性"""
        factory = LabelFactory()
        mature_samples = factory.build_labels_for_stock('000001', entry_proxy_mode='vwap', only_mature=True)
        self.assertGreater(len(mature_samples), 1000)
        # 抽查样本字段合规性
        sample = mature_samples[-1]
        self.assertEqual(sample['label_version'], LABEL_VERSION)
        self.assertEqual(sample['entry_proxy_type'], ENTRY_PROXY_VWAP)
        self.assertIn('future_5d_gross_return', sample)
        self.assertIn('realized_mae_daily_bar', sample)
        self.assertIn('realized_mfe_daily_bar', sample)
        self.assertNotIn('expected_mae', sample)
        self.assertNotIn('expected_mfe', sample)


if __name__ == '__main__':
    unittest.main()
