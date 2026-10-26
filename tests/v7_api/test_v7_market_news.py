# -*- coding: utf-8 -*-
"""UI-A2 isolated tests for truthful read-only market/news projections."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "scripts" / "daemon"))

from app import create_app
from daemon.v7_market_news import build_market_view, build_news_view

SH = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 9, 23, 16, 10, tzinfo=SH)


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def brief_fixture():
    article = {
        "id": "n-1", "title": "真实标题 <img src=x onerror=alert(1)>",
        "summary": "真实摘要 <script>alert(1)</script>", "content": "正文",
        "category": "市场", "source": "测试来源", "url": "https://news.example/a",
        "time": "2026-09-23 15:20:00", "blogger_views": {"廖小爱": "占位观点"},
        "blogger_views_meta": {},
    }
    quote = {"code": "000001", "name": "上证指数", "price": 3123.4,
             "pre_close": 3100.0, "change": 23.4, "pct": 0.75,
             "volume": 20, "amount": 300}
    return {
        "schema_version": "1.1", "fetched_at": "2026-09-23 16:00:00",
        "market_quotes": {"sh000001": quote},
        "index_intraday": {"sh000001": {
            "date": "20260923", "points": [["1559", 3123.4]],
            "fetched_at": "2026-09-23 16:00:30"}},
        "news": [article], "hotspots": [dict(article)],
    }


class TestMarketNewsViewModel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.brief = Path(self.temp.name) / "brief.json"
        self.pool = Path(self.temp.name) / "pool.json"
        self.cache = Path(self.temp.name) / "index.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_market_valid_saved_snapshot_is_not_promoted_to_realtime(self):
        write_json(self.brief, brief_fixture())
        view = build_market_view(self.brief, self.cache, FIXED_NOW)
        index = view["data"]["indexes"][0]
        self.assertEqual(view["status"], "DEGRADED")
        self.assertEqual(index["snapshot"]["last_price"], 3123.4)
        self.assertIsNone(index["snapshot"]["market_observed_at"])
        self.assertIsNone(index["snapshot"]["market_date"])
        self.assertEqual(index["snapshot"]["status"], "UNKNOWN")
        self.assertEqual(index["intraday"]["market_time"], "2026-09-23T15:59:00+08:00")
        self.assertEqual(index["intraday"]["status"], "UNKNOWN")
        self.assertIn("volume", index["snapshot"]["unavailable_fields"])

    def test_market_wrong_symbol_withholds_values(self):
        fixture = brief_fixture()
        fixture["market_quotes"]["sh000001"]["code"] = "399001"
        write_json(self.brief, fixture)
        view = build_market_view(self.brief, self.cache, FIXED_NOW)
        self.assertIsNone(view["data"]["indexes"][0]["snapshot"]["last_price"])
        self.assertTrue(any("code mismatch" in item for item in view["warnings"]))

    def test_capture_date_is_not_inferred_as_quote_market_date(self):
        fixture = brief_fixture()
        fixture["index_intraday"] = {}
        write_json(self.brief, fixture)
        view = build_market_view(self.brief, self.cache, FIXED_NOW)
        index = view["data"]["indexes"][0]
        self.assertIsNone(index["snapshot"]["market_date"])
        self.assertEqual(index["intraday"]["status"], "MISSING")
        self.assertEqual(index["snapshot"]["capture_started_at"], "2026-09-23T16:00:00+08:00")

    def test_market_missing_empty_corrupt_and_stale_sources(self):
        absent = build_market_view(self.brief, self.cache, FIXED_NOW)
        self.assertEqual(absent["status"], "UNAVAILABLE")
        write_json(self.brief, {"fetched_at": "2026-09-22 16:00:00", "market_quotes": {}, "index_intraday": {}, "news": []})
        empty = build_market_view(self.brief, self.cache, FIXED_NOW)
        self.assertEqual(empty["status"], "EMPTY")
        write_json(self.brief, brief_fixture())
        stale = build_market_view(self.brief, self.cache, datetime(2026, 9, 24, 10, 0, tzinfo=SH))
        self.assertEqual(stale["status"], "STALE")
        self.brief.write_text("{bad", encoding="utf-8")
        corrupt = build_market_view(self.brief, self.cache, FIXED_NOW)
        self.assertEqual(corrupt["status"], "UNAVAILABLE")

    def test_news_maps_real_fields_and_keeps_source_time_separate_from_capture(self):
        write_json(self.brief, brief_fixture())
        view = build_news_view(self.brief, self.pool, FIXED_NOW)
        data = view["data"]
        self.assertEqual(data["capture_started_at"], "2026-09-23T16:00:00+08:00")
        self.assertEqual(data["count"], 1)
        item = data["all_news"][0]
        self.assertEqual(item["published_at_raw"], "2026-09-23 15:20:00")
        self.assertEqual(item["published_at"], "2026-09-23T15:20:00+08:00")
        self.assertEqual(item["published_at_status"], "RECORDED_SOURCE_TIME_UNVERIFIED")
        self.assertEqual(item["source"], "测试来源")
        self.assertEqual(item["ai_analysis"]["status"], "UNAVAILABLE")
        self.assertEqual(item["url"], "https://news.example/a")

    def test_news_duplicate_hotspot_is_deduplicated(self):
        write_json(self.brief, brief_fixture())
        view = build_news_view(self.brief, self.pool, FIXED_NOW)
        self.assertEqual(view["data"]["count"], 1)
        self.assertEqual(view["data"]["duplicate_count"], 1)
        self.assertTrue(view["data"]["hot_news"][0]["is_hot"])

    def test_ai_analysis_requires_metadata_and_is_separate(self):
        fixture = brief_fixture()
        fixture["news"][0]["blogger_views_meta"] = {"model": "audited-model"}
        write_json(self.brief, fixture)
        item = build_news_view(self.brief, self.pool, FIXED_NOW)["data"]["all_news"][0]
        self.assertEqual(item["ai_analysis"]["status"], "AVAILABLE")
        self.assertEqual(item["ai_analysis"]["generated_by"], "audited-model")
        self.assertNotIn("blogger_views", item)

    def test_unsafe_links_rejected_and_external_markup_remains_text(self):
        fixture = brief_fixture()
        fixture["news"][0]["url"] = "javascript:alert(1)"
        write_json(self.brief, fixture)
        item = build_news_view(self.brief, self.pool, FIXED_NOW)["data"]["all_news"][0]
        self.assertIsNone(item["url"])
        fixture["news"][0]["url"] = "https://user:secret@news.example/article"
        write_json(self.brief, fixture)
        credential_url_item = build_news_view(self.brief, self.pool, FIXED_NOW)["data"]["all_news"][0]
        self.assertIsNone(credential_url_item["url"])
        self.assertIn("<script>", item["summary"])
        js = (BASE / "workbench_v7" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("textContent = item.title", js)
        self.assertNotIn("innerHTML", js)

    def test_news_stale_empty_fallback_and_missing(self):
        write_json(self.brief, brief_fixture())
        stale = build_news_view(self.brief, self.pool, datetime(2026, 9, 24, 10, 0, tzinfo=SH))
        self.assertEqual(stale["status"], "STALE")
        write_json(self.brief, {"news": [], "hotspots": []})
        write_json(self.pool, {"date": "2026-09-23", "pool": [brief_fixture()["news"][0]]})
        fallback = build_news_view(self.brief, self.pool, FIXED_NOW)
        self.assertEqual(fallback["data"]["source_mode"], "DATED_NEWS_POOL_FALLBACK")
        self.assertEqual(fallback["status"], "DEGRADED")
        write_json(self.pool, {"date": "2026-09-22", "pool": [brief_fixture()["news"][0]]})
        old_fallback = build_news_view(self.brief, self.pool, FIXED_NOW)
        self.assertEqual(old_fallback["status"], "STALE")
        write_json(self.pool, {"date": "2026-09-23", "pool": []})
        empty = build_news_view(self.brief, self.pool, FIXED_NOW)
        self.assertEqual(empty["status"], "EMPTY")

    def test_real_local_readonly_api_matches_persisted_news(self):
        brief_path = BASE / "data" / "pipeline" / "news_brief.json"
        if not brief_path.exists():
            self.skipTest("real persisted news brief unavailable")
        original = json.loads(brief_path.read_text(encoding="utf-8"))
        client = TestClient(create_app())
        market = client.get("/api/v7/market")
        news = client.get("/api/v7/news")
        self.assertEqual(market.status_code, 200)
        self.assertEqual(news.status_code, 200)
        self.assertIn(market.json()["status"], {"DEGRADED", "STALE", "EMPTY", "UNAVAILABLE"})
        payload = news.json()
        self.assertEqual(payload["data"]["source_mode"], "NEWS_OFFICER_BRIEF")
        raw = original.get("news", [])
        raw.extend(original.get("hotspots", []))
        actual_ids = {str(item.get("id")) for item in raw if isinstance(item, dict) and item.get("id") is not None}
        mapped_ids = {item["id"] for item in payload["data"]["all_news"] if item.get("id")}
        self.assertTrue(mapped_ids.issubset(actual_ids))
        self.assertTrue(payload["data"]["all_news"])

    def test_ui_a1_static_css_asset_route_is_live(self):
        client = TestClient(create_app())
        page = client.get("/v7")
        css = client.get("/workbench_v7/css/tokens.css")
        self.assertEqual(page.status_code, 200)
        self.assertIn('/workbench_v7/css/tokens.css', page.text)
        self.assertEqual(css.status_code, 200)
        self.assertIn("--page: #f5f7fa", css.text)

    def test_market_failure_is_independent_from_news_api_and_overview(self):
        from daemon import v7_api
        client = TestClient(create_app())
        with patch.object(v7_api, "build_market_view", side_effect=OSError("source unavailable")):
            market = client.get("/api/v7/market").json()
            news = client.get("/api/v7/news").json()
            overview = client.get("/api/v7/overview").json()
        self.assertEqual(market["status"], "UNAVAILABLE")
        self.assertEqual(news["status"], "DEGRADED")
        self.assertIsNotNone(overview["data"])

    def test_navigation_hash_is_restored_and_external_text_is_not_html(self):
        html = (BASE / "workbench_v7" / "index.html").read_text(encoding="utf-8")
        js = (BASE / "workbench_v7" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="news-hot-list"', html)
        self.assertIn('id="market-index-grid"', html)
        self.assertIn("window.location.hash.slice(1)", js)
        self.assertIn("link.textContent = item.title", js)

    def test_news_time_is_not_capture_time(self):
        write_json(self.brief, brief_fixture())
        view = build_news_view(self.brief, self.pool, FIXED_NOW)
        item = view["data"]["all_news"][0]
        self.assertNotEqual(item["published_at"], view["data"]["capture_started_at"])


if __name__ == "__main__":
    unittest.main()
