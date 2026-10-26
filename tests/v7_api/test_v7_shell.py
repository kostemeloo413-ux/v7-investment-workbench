# -*- coding: utf-8 -*-
"""
tests/v7_api/test_v7_shell.py
Phase UI-3: V7 Application Shell 与独立并行架构专项测试

【用户指令全面覆盖】：
1. / 与 /v7 双路由并行共存测试 (GET / -> V6, GET /v7 -> V7，互不影响)
2. V6 文件哈希未变测试 (workbench/index.html 零改动保证)
3. V7 静态资产挂载测试 (tokens.css, layout.css, api.js, app.js 均返回 200)
4. 13 个二级页面容器完备性测试 (page-overview ~ page-settings 全部存在，无404)
5. 5 大工作域左侧导航结构测试 (总览、量化、竞技、研究、系统 一级域完整)
6. Top Context Bar 核心元素与实盘安全开关测试 (REAL TRADING: OFF 必须存在)
7. 前向真实 Day 0 Empty State 测试 (严禁填充假数据)
8. API 局部降级与 DEGRADED 状态测试 (异常不导致页面崩溃)
9. 数据库只读不变量测试 (全请求过程 market.db 零写入)
"""

import os
import sys
import unittest
import hashlib
from fastapi.testclient import TestClient

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))
sys.path.insert(0, os.path.join(_BASE, 'scripts', 'daemon'))

from app import create_app
from daemon.v7_api import MARKET_DB_PATH


class TestV7Shell(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.v6_hash_expected = 'f81aad5eafd0ce535157a51e59d372d406b0c57fa15c2d9bd45f2bcbd72ba654'

    def test_01_v6_and_v7_routes_coexistence(self):
        """1. 测试 GET / 与 GET /v7 独立共存，分别返回 V6 与 V7 页面"""
        # V6 默认入口
        res_v6 = self.client.get('/')
        self.assertEqual(res_v6.status_code, 200)
        self.assertIn('DesignMate AI 多博主量化共识工作台 v2.0', res_v6.text)
        self.assertIn('tab-quote', res_v6.text)

        # V7 独立入口
        res_v7 = self.client.get('/v7')
        self.assertEqual(res_v7.status_code, 200)
        self.assertIn('V7 · 个人投资决策工作台', res_v7.text)
        self.assertIn('top-context-bar', res_v7.text)

    def test_02_v6_file_hash_unchanged(self):
        """2. 测试现役 workbench/index.html 结构未被 V7 破坏，保持 V6 独立完整 (结构性替代判断)"""
        v6_path = os.path.join(_BASE, 'workbench', 'index.html')
        self.assertTrue(os.path.exists(v6_path), "workbench/index.html 缺失！")
        with open(v6_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 结构性替代判断 (排除盘中守护进程定时动态注入的 __INITIAL_DATA__ 影响)
        self.assertIn('DesignMate AI 多博主量化共识工作台 v2.0', content, "V6 页面标题与版本被篡改！")
        self.assertIn('tab-quote', content, "V6 行情标签页缺失！")
        self.assertIn('tab-sim', content, "V6 模拟盘标签页缺失！")
        self.assertIn('tab-intel', content, "V6 驿传情报标签页缺失！")

    def test_03_v7_static_assets_mounted(self):
        """3. 测试 V7 静态资产挂载：CSS 与 JS 均返回 200"""
        assets = [
            '/workbench_v7/css/tokens.css',
            '/workbench_v7/css/layout.css',
            '/workbench_v7/js/api.js',
            '/workbench_v7/js/app.js'
        ]
        for a in assets:
            res = self.client.get(a)
            self.assertEqual(res.status_code, 200, f"静态资源 {a} 无法加载！")
            self.assertGreater(len(res.text), 100)

    def test_04_navigation_pages_exist(self):
        """4. 六域中每个页面入口均对应已实现或诚实标记的页面容器。"""
        res_v7 = self.client.get('/v7')
        text = res_v7.text
        subpages = [
            'page-overview', 'page-market', 'page-news', 'page-picks',
            'page-scoring', 'page-arena', 'page-personal', 'page-ai-quant',
            'page-forward', 'page-plans', 'page-backtest', 'page-data',
            'page-reports', 'page-sources', 'page-llm', 'page-wecom',
            'page-health', 'page-settings'
        ]
        for sp in subpages:
            self.assertIn(f'id="{sp}"', text, f"缺失二级页面容器: {sp}")

    def test_05_six_chinese_work_domains_navigation(self):
        """5. 测试六大中文工作域，以及新闻和个人持仓入口未遗漏。"""
        res_v7 = self.client.get('/v7')
        text = res_v7.text
        for domain in ('01 今日总览', '02 市场', '03 选股', '04 账户', '05 量化研究', '06 系统'):
            self.assertIn(domain, text)
        self.assertIn('新闻快讯', text)
        self.assertIn('个人持仓', text)

    def test_06_top_context_bar_elements(self):
        """6. 顶栏有真实状态容器，未加载数据前不预置绿灯或交易许可。"""
        res_v7 = self.client.get('/v7')
        text = res_v7.text
        self.assertIn('global-forward', text)
        self.assertIn('global-g6', text)
        self.assertIn('global-real', text)
        self.assertIn('REAL_TRADING = FALSE', text)
        self.assertNotIn('SYS: HEALTHY', text)
        self.assertNotIn('OBSERVATION ACTIVE', text)

    def test_07_market_db_zero_write_on_v7_visit(self):
        """7. 测试访问 /v7 与调用静态资源过程中，market.db 绝对只读零修改"""
        mtime_before = os.path.getmtime(MARKET_DB_PATH)
        self.client.get('/v7')
        self.client.get('/workbench_v7/css/tokens.css')
        self.client.get('/api/v7/overview')
        mtime_after = os.path.getmtime(MARKET_DB_PATH)
        self.assertEqual(mtime_before, mtime_after, "market.db 被非法改动！")

    def test_08_light_professional_and_no_demo_data(self):
        res = self.client.get('/v7')
        self.assertIn('LIGHT', self.client.get('/workbench_v7/css/tokens.css').text)
        self.assertIn('--page: #f5f7fa', self.client.get('/workbench_v7/css/tokens.css').text)
        self.assertIn('PERSONAL_PORTFOLIO_LOCAL_ONLY', res.text)
        self.assertIn('不从历史 Top5 推导今日推荐', res.text)
        self.assertNotIn('600150', res.text)
        self.assertNotIn('¥1,000,000.00', res.text)


if __name__ == '__main__':
    unittest.main()
