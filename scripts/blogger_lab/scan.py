# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/scan.py
V7 博主实验室今日机会发现引擎 (Stage 4 R2 修订版)
严格遵循只读原则、三值确定性解释、应完成交易日截止点与标的资格核验契约。
"""

from __future__ import annotations
import os
import sys
import time
import uuid
import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import (
    ScanRun,
    ScanCandidate,
    StockEvaluation,
    StrategyVersion,
    StrategyDraft,
    SCHEMA_VERSION
)
from blogger_lab.store import BloggerLabStore, current_time_iso
from blogger_lab.market import BloggerLabMarketAdapter
from blogger_lab.strategy import evaluate_entry_rules, FIELD_CATALOG

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def is_market_trading_day(d: datetime.date) -> bool:
    """复用现有 A 股交易日历判定交易日，日历不可读取时如实失败不猜测"""
    try:
        from runtime_config import trading_calendar_status, TradingCalendarStatus
        status = trading_calendar_status(d)
        if status.get("status") == TradingCalendarStatus.VALID or status.get("status") == "VALID":
            return bool(status.get("trading_day"))
        raise RuntimeError(f"交易日历状态无效: {status.get('reason')}")
    except Exception as e:
        raise RuntimeError(f"无法从 trading_calendar_status 读取有效交易日历: {e}")


def get_expected_completed_trading_day(
    ref_dt: datetime.datetime,
    mode: str = "REPLAY",
    as_of_date_str: Optional[str] = None
) -> Tuple[str, bool]:
    """
    确定本次扫描应使用的最近完整日线交易日 (YYYYMMDD) 与时段标记。
    - LIVE 盘中 (9:15-15:00) 严格截到前一完成交易日，不读取当日未完成日线；
    - LIVE 日终 (>= 15:00) 或非交易日明确应有的完整交易日；
    - REPLAY 模式明确截止交易日，严格阻断请求未来；
    - REPLAY 盘中截止点保护：若请求当天且当前时钟尚未收盘 (< 15:00)，截断到前一完成交易日，避免同日盘中读取未来收盘行。
    返回: (expected_cutoff_yyyymmdd, is_intraday)
    """
    mode_clean = mode.upper()
    ref_date = ref_dt.date()

    if mode_clean == "LIVE":
        is_today_trade = is_market_trading_day(ref_date)
        # 盘中时段：交易日的 9:15-15:00 之前
        is_intraday = is_today_trade and (ref_dt.time() < datetime.time(15, 0))

        if is_intraday:
            # 盘中：严格截到前一完成交易日
            cur = ref_date - datetime.timedelta(days=1)
            while not is_market_trading_day(cur):
                cur -= datetime.timedelta(days=1)
            return cur.strftime("%Y%m%d"), True
        elif is_today_trade:
            # 交易日 15:00 收盘后：今日日线已完成
            return ref_date.strftime("%Y%m%d"), False
        else:
            # 非交易日 (周末/节假日)：截到最近一个历史完成交易日
            cur = ref_date - datetime.timedelta(days=1)
            while not is_market_trading_day(cur):
                cur -= datetime.timedelta(days=1)
            return cur.strftime("%Y%m%d"), False
    else:
        # REPLAY 模式
        target_str = (as_of_date_str or ref_dt.strftime("%Y%m%d")).replace("-", "")
        today_str = ref_dt.strftime("%Y%m%d")
        if target_str > today_str:
            raise ValueError(f"REPLAY 模式严禁请求未来截止点: {target_str} > 当前时钟 {today_str}")

        target_date = datetime.datetime.strptime(target_str, "%Y%m%d").date()

        # 随同处理的截止点遗漏保护 (P2)：
        # 若请求的是当天 (target_date == ref_date) 且当前时钟处于 15:00 之前盘中：
        # 当天的完整收盘行情尚未发生！必须截断到前一完成交易日，并标记 is_intraday=True！
        if target_date == ref_date and is_market_trading_day(ref_date) and (ref_dt.time() < datetime.time(15, 0)):
            cur = ref_date - datetime.timedelta(days=1)
            while not is_market_trading_day(cur):
                cur -= datetime.timedelta(days=1)
            return cur.strftime("%Y%m%d"), True

        # 若请求历史日期或收盘后
        cur = target_date
        while not is_market_trading_day(cur):
            cur -= datetime.timedelta(days=1)
        return cur.strftime("%Y%m%d"), False


class BloggerLabOpportunityScanner:
    """今日机会发现与规则证据扫描引擎"""

    def __init__(
        self,
        store: BloggerLabStore,
        market_adapter: Optional[BloggerLabMarketAdapter] = None
    ):
        self.store = store
        self.market_adapter = market_adapter or BloggerLabMarketAdapter()

    def run_scan(
        self,
        persona_id: str,
        strategy_version_id: Optional[str] = None,
        mode: str = "REPLAY",
        as_of_date: Optional[str] = None,
        universe_symbols: Optional[List[str]] = None,
        universe_limit: int = 50,
        custom_quotes: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> ScanRun:
        """
        执行今日机会发现扫描：
        - 1. 严格门禁：未确认策略草案严禁用于机会扫描，必须为 CONFIRMED 策略版本；
        - 2. 标的范围：手动与默认列表共用主板有效资格校验，ST/停牌/非主板刚性排除；
        - 3. 完整日线：按交易日历确定应有的完整交易日截止点，LIVE 盘中严格截到前一交易日；
        - 4. 严禁补零：数据缺失、陈旧或单位未知刚性返回 UNKNOWN 阻断，不成为可成交候选；
        - 5. 稳定排序：候选股票按代码稳定排序，不捏造胜率评分；
        - 6. 结果留痕：完整记录扫描目标数、完成评估数、排除数与逐条件证据，严密对账持久化落库。
        """
        start_time = time.time()
        now_dt = self.market_adapter.clock()
        now_iso = now_dt.isoformat()
        today_str = now_dt.strftime("%Y%m%d")

        # 1. 策略版本门禁检查
        persona = self.store.get_persona(persona_id)
        if not persona:
            raise ValueError(f"博主分身 '{persona_id}' 不存在")

        target_strat_id = strategy_version_id or persona.active_strategy_id
        if not target_strat_id:
            raise ValueError(f"分身 '{persona_id}' 未指定策略版本且无已激活的策略版本")

        # 检查是否误传了未确认草案 ID
        draft_check = self.store.get_strategy_draft(target_strat_id, persona_id)
        if draft_check is not None and draft_check.status != "CONFIRMED":
            raise ValueError(f"未确认策略草案 '{target_strat_id}' (status={draft_check.status}) 严禁用于机会扫描！必须确认发布为不可变策略版本后方可执行。")

        strategy = self.store.get_strategy_version(target_strat_id, persona_id)
        if not strategy:
            raise ValueError(f"不可变策略版本 '{target_strat_id}' 不存在或未获确认发布")

        if not strategy.entry_rules:
            raise ValueError(f"策略版本 '{target_strat_id}' 中无有效入场规则，无法执行筛选")

        # 2. 模式与日期时点判定
        mode_clean = str(mode).upper()
        if mode_clean not in ("LIVE", "REPLAY"):
            raise ValueError(f"不支持的扫描模式 '{mode}'，仅支持 'LIVE' 或 'REPLAY'")

        completed_cutoff_date, is_intraday = get_expected_completed_trading_day(
            now_dt,
            mode=mode_clean,
            as_of_date_str=as_of_date
        )

        if mode_clean == "REPLAY":
            effective_as_of = completed_cutoff_date
            quote_time = f"{completed_cutoff_date[:4]}-{completed_cutoff_date[4:6]}-{completed_cutoff_date[6:8]}T15:00:00+08:00"
        else:
            effective_as_of = today_str
            quote_time = now_iso

        # 3. 确定股票筛选范围 (Universe) 并统一执行资格与日期核验
        target_universe: List[Dict[str, Any]] = []
        if universe_symbols:
            candidate_codes = [str(s).zfill(6) for s in universe_symbols]
        else:
            sample_eligible = self.market_adapter.market_db.get_eligible_universe_sample(
                limit=universe_limit,
                as_of_date=effective_as_of
            )
            candidate_codes = [item["symbol"] for item in sample_eligible]

        for code in candidate_codes:
            is_eligible, inelig_reason, inst = self.market_adapter.market_db.check_instrument_eligibility(
                code,
                as_of_date=effective_as_of
            )
            item = dict(inst) if inst else {}
            item.update({
                "symbol": code,
                "name": (inst.get("name") if inst else "") or item.get("name", ""),
                "is_eligible": is_eligible,
                "ineligibility_reason": inelig_reason,
                "instrument": inst
            })
            target_universe.append(item)

        # 真实路径无合格证据或无可用标的，刚性生成 FAILED 记录
        if not target_universe:
            execution_ms = round((time.time() - start_time) * 1000.0, 2)
            reason = getattr(self.market_adapter.market_db, "error", None) or "QFQ 合格历史与日期资格证据不可用，刚性阻断"
            run_id = f"scan_{persona_id}_{uuid.uuid4().hex[:8]}"
            scan_run = ScanRun(
                schema_version=SCHEMA_VERSION,
                id=run_id,
                persona_id=persona_id,
                strategy_version_id=strategy.id,
                mode=mode_clean,
                status="FAILED",
                as_of_date=effective_as_of,
                universe_target_count=0,
                universe_evaluated_count=0,
                universe_excluded_count=0,
                candidates_count=0,
                candidates=[],
                evaluations=[],
                exclusion_reasons={"missing_qfq_evidence": 1},
                summary=f"[{mode_clean}] {reason}，无法执行扫描",
                execution_ms=execution_ms,
                created_at=now_iso
            )
            self.store.save_scan_run(scan_run)
            return scan_run

        universe_target_count = len(target_universe)
        evaluations: List[StockEvaluation] = []
        candidates: List[ScanCandidate] = []
        exclusion_reasons: Dict[str, int] = {}

        # 4. 逐股计算指标与规则求值
        for inst in target_universe:
            symbol = inst["symbol"]
            name = inst.get("name", "")

            # 4.0 资格检查门禁 (ST、停牌、非主板、未上市/退市刚性排除)
            if not inst.get("is_eligible", True):
                reason = f"资格核验未通过: {inst.get('ineligibility_reason')}"
                exclusion_reasons["ineligible_instrument"] = exclusion_reasons.get("ineligible_instrument", 0) + 1
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status="UNKNOWN",
                    checks=[],
                    market_snapshot={
                        "symbol": symbol,
                        "name": name,
                        "eligibility": "INELIGIBLE",
                        "rejection_reason": inst.get("ineligibility_reason")
                    },
                    rejection_reason=reason
                ))
                continue

            # 4.1 读取历史已完成日线 (严格截断到应有的已完成交易日)
            raw_klines = self.market_adapter.market_db.get_daily_klines(
                symbol,
                cutoff_date=completed_cutoff_date,
                limit=60
            )
            # 防御性过滤：绝不采纳晚于应完成交易日的数据
            klines = [k for k in raw_klines if str(k.get("trade_date", "")).replace("-", "") <= completed_cutoff_date]

            # 数据充分性检查 (最少需要 20 根日线以支持 20MA 计算)
            if len(klines) < 20:
                reason = f"历史日线不足 20 日 (实际 {len(klines)} 日)"
                exclusion_reasons["insufficient_history"] = exclusion_reasons.get("insufficient_history", 0) + 1
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status="UNKNOWN",
                    checks=[],
                    market_snapshot={"symbol": symbol, "name": name, "kline_count": len(klines)},
                    rejection_reason=reason
                ))
                continue

            latest_kline_date = str(klines[-1]["trade_date"])

            # 数据时效性检查 (日线截止点必须匹配应有的最新完整交易日)
            if latest_kline_date != completed_cutoff_date:
                try:
                    exp_dt = datetime.datetime.strptime(completed_cutoff_date, "%Y%m%d")
                    latest_k_dt = datetime.datetime.strptime(latest_kline_date, "%Y%m%d")
                    gap_days = (exp_dt - latest_k_dt).days
                except Exception:
                    gap_days = 0

                reason = f"日线数据陈旧 (最新为 {latest_kline_date}，未达到应有完整交易日 {completed_cutoff_date}，相差 {gap_days} 自然日)，刚性排除"
                exclusion_reasons["stale_data"] = exclusion_reasons.get("stale_data", 0) + 1
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status="UNKNOWN",
                    checks=[],
                    market_snapshot={"latest_trade_date": latest_kline_date, "expected_cutoff": completed_cutoff_date},
                    rejection_reason=reason
                ))
                continue

            # 4.2 计算技术指标快照
            closes = [float(k["close"]) for k in klines]
            volumes = [float(k["volume"]) for k in klines]

            ma20 = round(sum(closes[-20:]) / 20.0, 4)
            ma5 = round(sum(closes[-5:]) / 5.0, 4)
            ma10 = round(sum(closes[-10:]) / 10.0, 4)
            ma60 = round(sum(closes[-60:]) / 60.0, 4) if len(closes) >= 60 else None

            # volume_ratio_5d: 当日成交量 / 前5日均量 (不包含当日的5日基准均量)
            if len(volumes) >= 6:
                volume_ma5 = round(sum(volumes[-6:-1]) / 5.0, 4)
            else:
                volume_ma5 = round(sum(volumes[-5:]) / 5.0, 4)

            last_bar = klines[-1]
            # 校验整个历史日线窗口的来源与口径一致性
            kline_sources = {str(k.get("source") or "market_db") for k in klines}
            if len(kline_sources) == 1:
                kline_src = list(kline_sources)[0]
                adj_basis = "BaoStock_QFQ" if "qfq" in kline_src.lower() else "RAW"
            else:
                kline_src = "MIXED:" + ",".join(sorted(kline_sources))
                adj_basis = "MIXED_UNVERIFIED"

            # 阻断未经验证的混合口径
            if adj_basis == "MIXED_UNVERIFIED":
                reason = f"历史日线窗口包含混合未经验证的数据来源 ({kline_src})，阻断为 UNKNOWN"
                exclusion_reasons["mixed_sources_unverified"] = exclusion_reasons.get("mixed_sources_unverified", 0) + 1
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status="UNKNOWN",
                    checks=[],
                    market_snapshot={"symbol": symbol, "name": name, "kline_source": kline_src},
                    rejection_reason=reason
                ))
                continue

            manifest = self.market_adapter.market_db.manifest
            if manifest:
                price_contract = manifest.get("price_contract")
                volume_unit = manifest.get("volume_unit", "shares")
                qfq_anchor = manifest.get("QFQ_anchor")
                manifest_path_str = self.market_adapter.market_db.manifest_path
                manifest_sha = self.market_adapter.market_db.manifest_sha256
                window_dates = manifest.get("window", [])
                date_qual_status = inst.get("date_qualification_status", "QUALIFIED")
                elig_basis = "MAIN_BOARD_ACTIVE_DATE_EFFECTIVE_QFQ_QUALIFIED"
            elif self.market_adapter.market_db.is_custom_fixture:
                price_contract = "CONTROLLED_FIXTURE"
                volume_unit = "shares"
                qfq_anchor = None
                manifest_path_str = None
                manifest_sha = None
                window_dates = [str(k.get("trade_date")) for k in klines]
                date_qual_status = "CONTROLLED_FIXTURE"
                elig_basis = "CONTROLLED_FIXTURE"
            else:
                price_contract = "UNVERIFIED"
                volume_unit = "shares"
                qfq_anchor = None
                manifest_path_str = None
                manifest_sha = None
                window_dates = [str(k.get("trade_date")) for k in klines]
                date_qual_status = "NO_EVIDENCE"
                elig_basis = "UNVERIFIED"

            # 获取当前有效价格与涨跌幅
            if mode_clean == "LIVE":
                # LIVE 模式获取实时报价
                custom_q = custom_quotes.get(symbol) if custom_quotes else None
                quote_ev = self.market_adapter.get_live_quote_evidence(symbol, custom_quote_dict=custom_q)

                if quote_ev.status != "VALID":
                    reason = f"实时报价不可用: status={quote_ev.status}"
                    exclusion_reasons[f"quote_{quote_ev.status.lower()}"] = exclusion_reasons.get(f"quote_{quote_ev.status.lower()}", 0) + 1
                    evaluations.append(StockEvaluation(
                        symbol=symbol,
                        name=name,
                        status="UNKNOWN",
                        checks=[],
                        market_snapshot={"quote_status": quote_ev.status},
                        rejection_reason=reason
                    ))
                    continue

                # 检查 LIVE 现价与历史 QFQ 锚点复权一致性
                # 未经复权对齐认证的真实网络实时报价源 (如 tencent_live_quote) 阻断为 UNKNOWN
                if quote_ev.source == "tencent_live_quote":
                    reason = "LIVE 实时报价源 (tencent_live_quote) 与历史 QFQ 复权口径未经验证一致，阻断为 UNKNOWN"
                    exclusion_reasons["unverified_live_qfq_consistency"] = exclusion_reasons.get("unverified_live_qfq_consistency", 0) + 1
                    evaluations.append(StockEvaluation(
                        symbol=symbol,
                        name=name,
                        status="UNKNOWN",
                        checks=[],
                        market_snapshot={"symbol": symbol, "name": name, "quote_source": quote_ev.source},
                        rejection_reason=reason
                    ))
                    continue

                q_vals = quote_ev.values
                current_price = float(q_vals.get("price", 0.0))
                prev_close = float(q_vals.get("previous_close", closes[-1]))
                pct_chg = round((current_price - prev_close) / prev_close * 100.0, 4) if prev_close > 0 else 0.0
                quote_time = quote_ev.observed_at
                vol_ratio = round(volumes[-1] / volume_ma5, 4) if volume_ma5 > 0 else None
                data_src = f"kline:{kline_src}|quote:{quote_ev.source}"
            else:
                # REPLAY 模式基于已完成日线收盘价与全日成交量
                current_price = float(last_bar["close"])
                pct_chg = float(last_bar.get("pct_chg", 0.0) or 0.0)
                vol_ratio = round(float(last_bar["volume"]) / volume_ma5, 4) if volume_ma5 > 0 else None
                data_src = f"kline:{kline_src}"

            # 组装行情指标快照（与 FIELD_CATALOG 白名单指标严格对齐，保存实际来源与口径依据）
            market_snapshot = {
                "symbol": symbol,
                "name": name,
                "as_of_date": effective_as_of,
                "data_cutoff": completed_cutoff_date,
                "last_price": current_price,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma60": ma60,
                "close_gt_ma20": (current_price > ma20),
                "ma5_gt_ma10": (ma5 > ma10),
                "last_price_to_ma20_pct": round((current_price - ma20) / ma20 * 100.0, 4) if ma20 > 0 else None,
                "last_price_to_ma5_pct": round((current_price - ma5) / ma5 * 100.0, 4) if ma5 > 0 else None,
                "ma5_to_ma10_pct": round((ma5 - ma10) / ma10 * 100.0, 4) if ma10 > 0 else None,
                "volume_ratio_5d": vol_ratio,
                "pct_change_1d": pct_chg,
                "source": data_src,
                "kline_source_family": kline_src,
                "price_unit": "CNY",
                "volume_unit": volume_unit,
                "price_contract": price_contract,
                "adjustment_basis": adj_basis,
                "qfq_anchor": qfq_anchor,
                "manifest_path": manifest_path_str,
                "manifest_sha256": manifest_sha,
                "window_dates": window_dates,
                "date_qualification_status": date_qual_status,
                "qualification_evidence_date": inst.get("qualification_evidence_date", effective_as_of),
                "eligibility_basis": elig_basis
            }

            # 4.3 确定性规则求值
            eval_status, check_details = evaluate_entry_rules(strategy.entry_rules, market_snapshot)

            if eval_status == "TRUE":
                candidates.append(ScanCandidate(
                    symbol=symbol,
                    name=name,
                    strategy_version_id=strategy.id,
                    checks=check_details,
                    market_snapshot=market_snapshot,
                    quote_time=quote_time,
                    data_cutoff=completed_cutoff_date,
                    rank=1,
                    decision="QUALIFIED"
                ))
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status="TRUE",
                    checks=check_details,
                    market_snapshot=market_snapshot,
                    rejection_reason=None
                ))
            else:
                failed_checks = [c for c in check_details if c.get("status") != "TRUE"]
                if failed_checks:
                    fc = failed_checks[0]
                    rejection = f"条件 [{fc.get('field')} {fc.get('op')} {fc.get('target')}] 未满足: {fc.get('reason')}"
                else:
                    rejection = "入场条件不满足"
                evaluations.append(StockEvaluation(
                    symbol=symbol,
                    name=name,
                    status=eval_status,
                    checks=check_details,
                    market_snapshot=market_snapshot,
                    rejection_reason=rejection
                ))

        # 5. 候选按股票代码稳定升序排序
        candidates.sort(key=lambda c: c.symbol)
        for idx, cand in enumerate(candidates):
            cand.rank = idx + 1

        # 6. 统计汇总与状态生成 (严格对账)
        universe_target_count = len(target_universe)
        universe_excluded_count = len([e for e in evaluations if e.status == "UNKNOWN"])
        universe_evaluated_count = len([e for e in evaluations if e.status in ("TRUE", "FALSE")])

        execution_ms = round((time.time() - start_time) * 1000.0, 2)
        if candidates:
            run_status = "COMPLETED"
        elif universe_evaluated_count > 0:
            run_status = "NO_CANDIDATES"
        else:
            run_status = "FAILED"

        time_note = "盘中实时" if is_intraday else "非交易时段预筛选/日终研究"
        if candidates:
            summary_text = (
                f"[{mode_clean}|{time_note}] 扫描目标 {universe_target_count} 只，"
                f"实际评估 {universe_evaluated_count} 只，受限/缺数据排除 {universe_excluded_count} 只，"
                f"命中合格候选 {len(candidates)} 只 (耗时 {execution_ms:.1f}ms)"
            )
        elif universe_evaluated_count > 0:
            summary_text = (
                f"[{mode_clean}|{time_note}] 扫描目标 {universe_target_count} 只，"
                f"实际评估 {universe_evaluated_count} 只，受限/缺数据排除 {universe_excluded_count} 只，"
                f"无满足条件的合格候选 (自然零候选) (耗时 {execution_ms:.1f}ms)"
            )
        else:
            summary_text = (
                f"[{mode_clean}|{time_note}] 扫描目标 {universe_target_count} 只，"
                f"全部标的数据不可用/资格受限已排除 ({universe_excluded_count} 只)，无有效评估数据 (耗时 {execution_ms:.1f}ms)"
            )

        run_id = f"scan_{persona_id}_{uuid.uuid4().hex[:8]}"
        scan_run = ScanRun(
            schema_version=SCHEMA_VERSION,
            id=run_id,
            persona_id=persona_id,
            strategy_version_id=strategy.id,
            mode=mode_clean,
            status=run_status,
            as_of_date=effective_as_of,
            universe_target_count=universe_target_count,
            universe_evaluated_count=universe_evaluated_count,
            universe_excluded_count=universe_excluded_count,
            candidates_count=len(candidates),
            candidates=candidates,
            evaluations=evaluations,
            exclusion_reasons=exclusion_reasons,
            summary=summary_text,
            execution_ms=execution_ms,
            created_at=now_iso
        )

        # 7. 持久化存储
        self.store.save_scan_run(scan_run)
        return scan_run
