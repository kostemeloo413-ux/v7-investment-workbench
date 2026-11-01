# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/market.py
V7 博主实验室只读市场数据适配器 (R2 修订版)
严格遵循只读原则，落实验证准入、未来数据拦截、退市过滤与报价时效性契约。
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import datetime
import hashlib
import math
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import MarketEvidence, SCHEMA_VERSION

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def current_time_shanghai() -> datetime.datetime:
    return datetime.datetime.now(SHANGHAI)


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_default_qfq_cache() -> Tuple[Optional[Path], Optional[Dict[str, Any]], Optional[str], Optional[Path]]:
    """
    解析当前默认的合格 QFQ 缓存数据库路径、manifest 字典与其真实 SHA256。
    严格只读，校验 CURRENT.json 指针与其指向的 manifest.json。
    """
    pointer_path = Path(_BASE) / "data" / "ai_quant" / "input_cache" / "CURRENT.json"
    if not pointer_path.exists():
        return None, None, None, None
    try:
        content = json.loads(pointer_path.read_text(encoding="utf-8"))
        if content.get("status") == "REJECTED":
            return None, None, None, None
        manifest_path_str = content.get("manifest_path", "")
        if not manifest_path_str:
            return None, None, None, None
        manifest_path = Path(manifest_path_str)
        if not manifest_path.exists():
            return None, None, None, None
        actual_sha = compute_file_sha256(manifest_path)
        expected_sha = content.get("manifest_sha256", "")
        if expected_sha and actual_sha.lower() != expected_sha.lower():
            return None, None, None, None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "QUALIFIED_ENGINEERING_PENDING_INDEPENDENT_REVIEW":
            return None, None, None, None
        qdb_path = Path(manifest.get("db_path", ""))
        if not qdb_path.exists():
            return None, None, None, None
        return qdb_path, manifest, actual_sha, manifest_path
    except Exception:
        return None, None, None, None


class ReadOnlyMarketDB:
    """以只读模式连接市场 SQLite 数据库，优先复用已准入同口径 QFQ 历史与日期资格证据"""
    def __init__(self, db_path: Optional[str] = None):
        self.is_custom_fixture = (db_path is not None)
        self.manifest: Optional[Dict[str, Any]] = None
        self.manifest_path: Optional[str] = None
        self.manifest_sha256: Optional[str] = None
        self.error: Optional[str] = None

        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            qdb, manifest, sha, mpath = resolve_default_qfq_cache()
            if qdb and qdb.exists() and manifest:
                self.db_path = qdb
                self.manifest = manifest
                self.manifest_path = str(mpath)
                self.manifest_sha256 = sha
            else:
                self.db_path = None
                self.manifest = None
                self.manifest_path = None
                self.manifest_sha256 = None
                self.error = "合格 QFQ 缓存或日期准入清单不可用/已拒绝，禁止静默回退旧混合库"

    def get_connection(self) -> sqlite3.Connection:
        if not self.db_path:
            raise FileNotFoundError(self.error or "Market DB 未配置或不可用")
        if not self.db_path.exists():
            raise FileNotFoundError(f"Market DB 不存在: {self.db_path}")
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def get_eligible_universe_sample(
        self,
        limit: int = 50,
        as_of_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取主板资格池样本（排除 ST、停牌，且必须核验上市与退市日期及日期准入证据）。
        as_of_date 为 YYYYMMDD 格式，默认取今日。
        """
        if not self.db_path or not self.db_path.exists():
            return []
        target_date = (as_of_date or datetime.datetime.now(SHANGHAI).strftime("%Y%m%d")).replace("-", "")
        with self.get_connection() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT symbol, name, exchange, board, is_st, is_suspended, list_date, delist_date
                FROM instrument_master
                WHERE board = 'MAIN'
                  AND is_st = 0
                  AND is_suspended = 0
                  AND (delist_date IS NULL OR delist_date > ?)
                  AND (list_date IS NULL OR list_date <= ?)
                ORDER BY symbol ASC
                LIMIT ?
                """,
                (target_date, target_date, limit)
            ).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                if self.manifest:
                    q_info = self.manifest.get("qualification", {}).get(item["symbol"])
                    if q_info:
                        item["date_qualification_status"] = q_info.get("status")
                        item["qualification_evidence_date"] = self.manifest.get("cutoff")
                results.append(item)
            return results

    def get_instrument_detail(self, symbol: str) -> Optional[Dict[str, Any]]:
        """查询标的的主数据详情记录，附带日期准入资格证据（若可用）"""
        if not self.db_path or not self.db_path.exists():
            return None
        code = str(symbol).zfill(6)
        with self.get_connection() as conn:
            cur = conn.cursor()
            row = cur.execute(
                """
                SELECT symbol, name, exchange, board, is_st, is_suspended, list_date, delist_date
                FROM instrument_master
                WHERE symbol = ?
                """,
                (code,)
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            if self.manifest:
                q_info = self.manifest.get("qualification", {}).get(code)
                if q_info:
                    res["date_qualification_status"] = q_info.get("status")
                    res["qualification_evidence_date"] = self.manifest.get("cutoff")
                    res["manifest_path"] = self.manifest_path
                    res["manifest_sha256"] = self.manifest_sha256
                    res["available_dates"] = q_info.get("available_dates")
                    res["valid_active_bars"] = q_info.get("valid_active_bars")
                    res["suspension_dates"] = q_info.get("suspension_dates", [])
            return res

    def check_instrument_eligibility(
        self,
        symbol: str,
        as_of_date: str
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        统一标的资格校验：主板、非ST、非停牌、上市未退市，且核验日期准入资格证据。
        返回: (is_eligible, rejection_reason, instrument_dict)
        """
        code = str(symbol).zfill(6)
        if not self.is_custom_fixture and (not self.db_path or not self.manifest):
            return False, self.error or "合格 QFQ 历史与日期准入资格证据不可用，刚性阻断", None
        inst = self.get_instrument_detail(code)
        if not inst:
            return False, f"标的 {code} 不在股票主数据字典中 (未知代码或无主数据记录)", None

        board = str(inst.get("board", "")).upper()
        if board != "MAIN":
            return False, f"非主板标的 (板块为 {board})，不符合主板资格池", inst

        if int(inst.get("is_st", 0)) != 0:
            return False, "ST或*ST风险警示标的，刚性排除", inst

        if int(inst.get("is_suspended", 0)) != 0:
            return False, "标的处于停牌状态，刚性排除", inst

        target_date = as_of_date.replace("-", "")
        list_date = inst.get("list_date")
        if list_date and str(list_date).replace("-", "") > target_date:
            return False, f"标的在目标日尚未上市 (上市日 {list_date} > 目标日 {target_date})", inst

        delist_date = inst.get("delist_date")
        if delist_date and str(delist_date).replace("-", "") <= target_date:
            return False, f"标的在目标日已退市 (退市日 {delist_date} <= 目标日 {target_date})", inst

        # 日期准入资格证据核验 (Date-Effective Qualification Evidence)
        if self.manifest:
            cutoff = str(self.manifest.get("cutoff", "")).replace("-", "")
            if target_date != cutoff:
                return False, f"目标日 {target_date} 缺少已核准的日期资格证据 (当前 QFQ 准入证据截止日为 {cutoff})", inst

            q_info = self.manifest.get("qualification", {}).get(code)
            if not q_info:
                return False, f"标的 {code} 在目标日 {target_date} 无日期准入证据", inst

            q_status = q_info.get("status")
            if q_status != "QUALIFIED":
                return False, f"标的 {code} 在目标日 {target_date} 资格状态为 {q_status}，未获准入", inst

            # 停牌核验 (来自日期资格证据)
            suspensions = q_info.get("suspension_dates", [])
            if target_date in suspensions:
                return False, f"标的 {code} 在目标日 {target_date} 处于停牌状态 (来自日期资格停牌证据)", inst
        elif self.is_custom_fixture:
            # 显式传入的隔离夹具：记录受控测试身份，绝不冒充真实 QUALIFIED 证据
            inst["date_qualification_status"] = "CONTROLLED_FIXTURE"
            inst["qualification_evidence_date"] = target_date
        else:
            return False, f"标的 {code} 缺少日期准入资格证据 (无合格 QFQ 证据清单)", inst

        return True, None, inst

    def get_daily_klines(
        self,
        symbol: str,
        cutoff_date: Optional[str] = None,
        limit: int = 60
    ) -> List[Dict[str, Any]]:
        """
        读取单只股票的历史日线（按 trade_date 升序排列）。
        cutoff_date 格式支持 YYYYMMDD 或 YYYY-MM-DD。
        """
        if not self.db_path or not self.db_path.exists():
            return []
        code = str(symbol).zfill(6)
        query = (
            "SELECT code, trade_date, open, high, low, close, volume, amount, pct_chg, source "
            "FROM daily_kline WHERE code = ?"
        )
        params: List[Any] = [code]

        if cutoff_date:
            norm_date = cutoff_date.replace("-", "")
            query += " AND trade_date <= ?"
            params.append(norm_date)

        query += " ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            cur = conn.cursor()
            rows = cur.execute(query, params).fetchall()
            result = [dict(r) for r in rows]
            result.reverse()
            return result


class BloggerLabMarketAdapter:
    """博主实验室只读市场证据适配器"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        clock=None
    ):
        self.market_db = ReadOnlyMarketDB(db_path)
        self.clock = clock or current_time_shanghai
        self.pointer_path = Path(_BASE) / "data" / "ai_quant" / "input_cache" / "CURRENT.json"

    def read_qfq_cache_status(self) -> Dict[str, Any]:
        """
        读取当前 QFQ 合格证据缓存指针信息，并严格校验 manifest 存在性、哈希与准入状态。
        """
        if not self.pointer_path.exists():
            return {"status": "MISSING", "error": "CURRENT.json pointer not found"}
        try:
            raw_ptr = self.pointer_path.read_text(encoding="utf-8")
            content = json.loads(raw_ptr)
        except Exception as e:
            return {"status": "ERROR", "error": f"CURRENT.json 解析失败: {e}"}

        # 指针自身状态检查
        ptr_status = content.get("status")
        if ptr_status == "REJECTED":
            return {"status": "REJECTED", "error": "指针标记为 REJECTED"}

        manifest_path_str = content.get("manifest_path", "")
        if not manifest_path_str:
            return {"status": "POINTER_BROKEN", "error": "缺少 manifest_path"}

        manifest_path = Path(manifest_path_str)
        if not manifest_path.exists():
            return {
                "status": "POINTER_BROKEN",
                "manifest_path": str(manifest_path),
                "error": "manifest 文件在磁盘上不存在"
            }

        # 校验文件真实哈希
        actual_sha = compute_file_sha256(manifest_path)
        expected_sha = content.get("manifest_sha256", "")
        if not expected_sha or actual_sha.lower() != expected_sha.lower():
            return {
                "status": "REJECTED",
                "manifest_path": str(manifest_path),
                "actual_sha": actual_sha,
                "expected_sha": expected_sha,
                "error": "manifest SHA256 哈希校验不匹配"
            }

        # 解析 manifest 内容
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {
                "status": "REJECTED",
                "manifest_path": str(manifest_path),
                "error": f"manifest 文件损坏/非合法 JSON: {e}"
            }

        m_status = m.get("status")
        if m_status != "QUALIFIED_ENGINEERING_PENDING_INDEPENDENT_REVIEW":
            return {
                "status": "REJECTED",
                "manifest_path": str(manifest_path),
                "admitted_status": m_status,
                "error": f"manifest 未获准入: status={m_status}"
            }

        return {
            "status": "VALID",
            "manifest_path": str(manifest_path),
            "manifest_sha256": actual_sha,
            "admitted_status": m_status,
            "manifest_exists": True
        }

    def get_history_evidence(
        self,
        symbol: str,
        cutoff_date: Optional[str] = None,
        bars_count: int = 60,
        limit: Optional[int] = None
    ) -> MarketEvidence:
        """
        从 SQLite 获取历史行情证据。
        落实时效、连续性与未来数据拦截契约。
        """
        now = self.clock()
        now_iso = now.isoformat()
        today_str = now.strftime("%Y%m%d")
        code = str(symbol).zfill(6)
        effective_limit = limit if limit is not None else bars_count

        # 若未指定 cutoff_date，默认严格截断至今日，严禁查询未来日期
        target_cutoff = cutoff_date.replace("-", "") if cutoff_date else today_str

        # 未来请求日期拦截
        if target_cutoff > today_str:
            return MarketEvidence(
                id=f"HIST_{code}_{now_iso}",
                symbol=code,
                observed_at=now_iso,
                data_cutoff=target_cutoff,
                source="market_db_sqlite:daily_kline",
                units={"price": "CNY/share"},
                adjustment="RAW",
                status="INVALID_FUTURE_DATA",
                values={"error": f"Requested cutoff {target_cutoff} is in the future relative to now {today_str}"}
            )

        klines = self.market_db.get_daily_klines(code, cutoff_date=target_cutoff, limit=effective_limit)

        if not klines:
            return MarketEvidence(
                id=f"HIST_{code}_{now_iso}",
                symbol=code,
                observed_at=now_iso,
                data_cutoff=target_cutoff,
                source="market_db_sqlite:daily_kline",
                units={"price": "CNY/share", "volume": "shares", "amount": "CNY"},
                adjustment="RAW",
                status="MISSING",
                values={"bars": [], "count": 0}
            )

        latest_date = str(klines[-1]["trade_date"])

        # 检查是否包含未来数据
        if latest_date > today_str:
            return MarketEvidence(
                id=f"HIST_{code}_{latest_date}",
                symbol=code,
                observed_at=now_iso,
                data_cutoff=latest_date,
                source="market_db_sqlite:daily_kline",
                units={"price": "CNY/share", "volume": "shares", "amount": "CNY"},
                adjustment="RAW",
                status="INVALID_FUTURE_DATA",
                values={"error": f"Database bar contains future date {latest_date} > {today_str}"}
            )

        # 检查时效与连续性：若请求的是近期（如 20261029），但数据库最新只有 1999 年，相差巨大，判定为 STALE
        try:
            req_dt = datetime.datetime.strptime(target_cutoff, "%Y%m%d")
            latest_dt = datetime.datetime.strptime(latest_date, "%Y%m%d")
            days_diff = (req_dt - latest_dt).days
        except Exception:
            days_diff = 0

        # 若数据最新日期距离请求 cutoff 超过 15 个自然日，判定为陈旧不可用
        if days_diff > 15:
            return MarketEvidence(
                id=f"HIST_{code}_{latest_date}",
                symbol=code,
                observed_at=now_iso,
                data_cutoff=latest_date,
                source="market_db_sqlite:daily_kline",
                units={"price": "CNY/share", "volume": "shares", "amount": "CNY"},
                adjustment="RAW",
                status="STALE",
                values={
                    "count": len(klines),
                    "latest_bar": klines[-1],
                    "error": f"Data stale: requested cutoff {target_cutoff} but latest in DB is {latest_date} (gap {days_diff} days)"
                }
            )

        # 正常有效历史数据计算 MA
        closes = [float(k["close"]) for k in klines]
        ma_values = {}
        for w in (5, 10, 20, 60):
            if len(closes) >= w:
                ma_values[f"ma{w}"] = round(sum(closes[-w:]) / w, 4)
            else:
                ma_values[f"ma{w}"] = None

        last_source = str(klines[-1].get("source") or "daily_kline")
        vol_unit = self.market_db.manifest.get("volume_unit", "shares") if self.market_db.manifest else "shares"
        adj = "QFQ" if "qfq" in last_source.lower() else "RAW"

        manifest_provenance = {}
        if self.market_db.manifest:
            manifest_provenance = {
                "manifest_path": self.market_db.manifest_path,
                "manifest_sha256": self.market_db.manifest_sha256,
                "price_contract": self.market_db.manifest.get("price_contract"),
                "volume_unit": vol_unit,
                "qfq_anchor": self.market_db.manifest.get("QFQ_anchor"),
                "window": self.market_db.manifest.get("window", [])
            }

        return MarketEvidence(
            id=f"HIST_{code}_{latest_date}",
            symbol=code,
            observed_at=now_iso,
            data_cutoff=latest_date,
            source=f"market_db_sqlite:{last_source}",
            units={"price": "CNY/share", "volume": vol_unit, "amount": "CNY"},
            adjustment=adj,
            status="VALID",
            values={
                "count": len(klines),
                "latest_bar": klines[-1],
                "ma": ma_values,
                "bars": klines,
                "provenance": manifest_provenance
            }
        )

    def get_live_quote_evidence(
        self,
        symbol: str,
        custom_quote_dict: Optional[Dict[str, Any]] = None,
        max_age_seconds: int = 90
    ) -> MarketEvidence:
        """
        获取单股实时报价证据。严格核查时间有效性、时区、未来时戳、价格非负有限性及陈旧度。
        """
        now = self.clock()
        now_iso = now.isoformat()
        today_str = now.strftime("%Y%m%d")
        code = str(symbol).zfill(6)

        # 1. 注入的样例/固定测试数据分支
        if custom_quote_dict is not None:
            raw_obs = custom_quote_dict.get("observed_at")
            if not isinstance(raw_obs, str) or not raw_obs.strip():
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=now_iso,
                    data_cutoff=str(custom_quote_dict.get("trade_date", today_str)),
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="INVALID_TIMESTAMP",
                    values={"error": "Missing or empty observed_at timestamp"}
                )

            # 解析 ISO 时间
            try:
                obs_dt = datetime.datetime.fromisoformat(raw_obs)
            except Exception as e:
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=now_iso,
                    data_cutoff=str(custom_quote_dict.get("trade_date", today_str)),
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="INVALID_TIMESTAMP",
                    values={"error": f"Invalid ISO timestamp '{raw_obs}': {e}"}
                )

            # 必须包含时区
            if obs_dt.tzinfo is None:
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=now_iso,
                    data_cutoff=str(custom_quote_dict.get("trade_date", today_str)),
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="INVALID_TIMESTAMP",
                    values={"error": f"Timestamp '{raw_obs}' has no timezone"}
                )

            # 校验未来时戳
            if obs_dt > now + datetime.timedelta(seconds=5):
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=raw_obs,
                    data_cutoff=str(custom_quote_dict.get("trade_date", today_str)),
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="INVALID_FUTURE_DATA",
                    values={"error": f"Quote timestamp {raw_obs} is in the future"}
                )

            # 校验价格有效性
            price_val = custom_quote_dict.get("price")
            if price_val is None or not isinstance(price_val, (int, float)) or not math.isfinite(price_val) or price_val <= 0:
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=raw_obs,
                    data_cutoff=str(custom_quote_dict.get("trade_date", today_str)),
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="INVALID_PRICE",
                    values={"error": f"Invalid price: {price_val}"}
                )

            # 校验交易日期跨度 (若给的是 1999 年的报价，与当前 2026 脱节)
            t_date = str(custom_quote_dict.get("trade_date", today_str)).replace("-", "")
            try:
                t_dt = datetime.datetime.strptime(t_date, "%Y%m%d")
                now_d_dt = datetime.datetime.strptime(today_str, "%Y%m%d")
                if abs((now_d_dt - t_dt).days) > 7:
                    return MarketEvidence(
                        id=f"QUOTE_{code}_{raw_obs}",
                        symbol=code,
                        observed_at=raw_obs,
                        data_cutoff=t_date,
                        source="injected_quote_provider",
                        units={"price": "CNY/share"},
                        adjustment="NONE",
                        status="STALE",
                        values={"error": f"Quote trade_date {t_date} too far from current date {today_str}"}
                    )
            except Exception:
                pass

            # 校验秒级 freshness
            age = (now - obs_dt).total_seconds()
            if age > max_age_seconds:
                return MarketEvidence(
                    id=f"QUOTE_{code}_{raw_obs}",
                    symbol=code,
                    observed_at=raw_obs,
                    data_cutoff=t_date,
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="STALE",
                    values={"error": f"Quote age {age:.1f}s > max {max_age_seconds}s", "quote": custom_quote_dict}
                )

            if custom_quote_dict.get("is_suspended"):
                return MarketEvidence(
                    id=f"QUOTE_{code}_{raw_obs}",
                    symbol=code,
                    observed_at=raw_obs,
                    data_cutoff=t_date,
                    source="injected_quote_provider",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="SUSPENDED",
                    values=custom_quote_dict
                )

            return MarketEvidence(
                id=f"QUOTE_{code}_{now_iso}",
                symbol=code,
                observed_at=raw_obs,
                data_cutoff=t_date,
                source="injected_quote_provider",
                units={"price": "CNY/share", "volume": "shares", "turnover": "CNY"},
                adjustment="NONE",
                status="VALID",
                values=custom_quote_dict
            )

        # 2. 真实网络行情读取分支
        try:
            from ai_quant.live_quotes import TencentQuotesProvider
            provider = TencentQuotesProvider(symbols=[code], clock=self.clock)
            q = provider.evidence.get(code)
            if not q:
                return MarketEvidence(
                    id=f"QUOTE_{code}_{now_iso}",
                    symbol=code,
                    observed_at=now_iso,
                    data_cutoff=today_str,
                    source="tencent_live_quote",
                    units={"price": "CNY/share"},
                    adjustment="NONE",
                    status="MISSING",
                    values={"error": "Quote not returned by provider"}
                )

            valid_q = provider.valid_quote(code, now=now)
            status = "VALID" if valid_q else "STALE"
            if q.get("is_st"):
                status = "SUSPENDED"

            return MarketEvidence(
                id=f"QUOTE_{code}_{q.get('received_at', now_iso)}",
                symbol=code,
                observed_at=q.get("received_at", now_iso),
                data_cutoff=q.get("market_date", today_str),
                source="tencent_live_quote",
                units={"price": "CNY/share", "volume": "shares", "turnover": "CNY"},
                adjustment="NONE",
                status=status,
                values={
                    "price": float(q.get("price", 0.0)),
                    "previous_close": float(q.get("previous_close", 0.0)),
                    "volume_shares": float(q.get("volume_shares", 0.0)),
                    "turnover_CNY": float(q.get("turnover_CNY", 0.0)),
                    "is_st": bool(q.get("is_st", False)),
                    "suspension_status": q.get("suspension_status", "UNKNOWN")
                }
            )
        except Exception as e:
            return MarketEvidence(
                id=f"QUOTE_{code}_{now_iso}",
                symbol=code,
                observed_at=now_iso,
                data_cutoff=today_str,
                source="tencent_live_quote",
                units={"price": "CNY/share"},
                adjustment="NONE",
                status="UNKNOWN",
                values={"error": str(e)}
            )
