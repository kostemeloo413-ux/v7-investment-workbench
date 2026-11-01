# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/replay.py
V7 博主实验室受控回放场景服务模块 (阶段 6 核心)
严格落实：
1. 回放日期、时钟与行情报价由服务端预置受控场景提供；
2. 前端/客户端只发送动作步骤、候选引用与 request_key，严禁透传任意 quote_override 或时钟；
3. 明确标示为受控样例行情，LIVE 撮合刚性禁用。
"""

from __future__ import annotations
import datetime
import json
import sqlite3
from typing import Dict, Any, Optional, List
from pathlib import Path

from scripts.runtime_config import is_trading_day
from scripts.blogger_lab.schemas import PaperCycle, LabFill, SCHEMA_VERSION

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def get_next_trading_day_str(date_str: str) -> str:
    """计算指定日期之后最近的一个合法 A 股交易日 (YYYYMMDD)"""
    clean_d = date_str.replace("-", "")[:8]
    d = datetime.datetime.strptime(clean_d, "%Y%m%d").date()
    cur = d + datetime.timedelta(days=1)
    while not is_trading_day(cur):
        cur += datetime.timedelta(days=1)
    return cur.strftime("%Y%m%d")

# 预置受控回放场景注册表
REPLAY_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "standard_t1_cycle": {
        "id": "standard_t1_cycle",
        "name": "标准A股波段T+1受控回放样例 (浦发银行 600000)",
        "symbol": "600000",
        "name_cn": "浦发银行",
        "notice": "本场景采用受控确定性样例行情（买入 10.00元 / T+1尝试 10.50元 / 止盈 10.50元），非LIVE实时撮合。",
        "description": "完整闭环：前一交易日(2026-10-26)收盘扫描产生候选 -> 次日(2026-10-27 10:00)正常开盘建仓 -> 同日(2026-10-27 14:30)尝试平仓被T+1刚性锁仓拦截 -> 第三日(2026-10-28 10:00)达成+5%止盈全额平仓。",
        "steps": [
            {
                "step_index": 1,
                "step_name": "第1步: 竞价建仓买入 (BUY)",
                "action": "BUY",
                "as_of_date": "2026-10-27",
                "clock_time": "2026-10-27 10:00:00",
                "quote_price": 10.00,
                "quote_override": {
                    "symbol": "600000",
                    "price": 10.00,
                    "prev_close": 9.90,
                    "yesterday_close": 9.90,
                    "trade_date": "20261027",
                    "quote_time": "2026-10-27 10:00:00"
                },
                "candidate_required": True,
                "expected_action": "BUY",
                "expected_status": "COMPLETED",
                "description": "在 2026-10-27 10:00 以 10.00 元买入 1000 股（滑点 0.1% 实际成交价 10.01 元，佣金 5.00 元，总扣款 10,015.00 元）"
            },
            {
                "step_index": 2,
                "step_name": "第2步: 同日盘中平仓拦截 (T+1 REJECTED)",
                "action": "CHECK_EXIT",
                "as_of_date": "2026-10-27",
                "clock_time": "2026-10-27 14:30:00",
                "quote_price": 10.60,
                "quote_override": {
                    "symbol": "600000",
                    "price": 10.60,
                    "prev_close": 9.90,
                    "yesterday_close": 9.90,
                    "trade_date": "20261027",
                    "quote_time": "2026-10-27 14:30:00"
                },
                "candidate_required": False,
                "expected_action": "HOLD",
                "expected_status": "NO_ACTION",
                "description": "在 2026-10-27 14:30 尝试平仓，受 A 股 T+1 制度刚性拦截（持仓锁定不可卖，0成交，现金与持仓不变）"
            },
            {
                "step_index": 3,
                "step_name": "第3步: 次日达成止盈平仓 (SELL COMPLETED)",
                "action": "CHECK_EXIT",
                "as_of_date": "2026-10-28",
                "clock_time": "2026-10-28 10:00:00",
                "quote_price": 10.60,
                "quote_override": {
                    "symbol": "600000",
                    "price": 10.60,
                    "prev_close": 10.00,
                    "yesterday_close": 10.00,
                    "trade_date": "20261028",
                    "quote_time": "2026-10-28 10:00:00"
                },
                "candidate_required": False,
                "expected_action": "SELL",
                "expected_status": "COMPLETED",
                "description": "在 2026-10-28 10:00 价格 10.60 元触碰止盈条件，成功全额卖出释放本金与收益"
            }
        ]
    }
}


def list_replay_scenarios() -> List[Dict[str, Any]]:
    """列出所有支持的受控回放场景描述（无敏感执行内构）"""
    res = []
    for s_id, s_data in REPLAY_SCENARIOS.items():
        res.append({
            "id": s_id,
            "name": s_data["name"],
            "symbol": s_data["symbol"],
            "name_cn": s_data["name_cn"],
            "notice": s_data["notice"],
            "description": s_data["description"],
            "steps": [
                {
                    "step_index": st["step_index"],
                    "step_name": st["step_name"],
                    "action": st["action"],
                    "as_of_date": st["as_of_date"],
                    "clock_time": st["clock_time"],
                    "quote_price": st["quote_price"],
                    "expected_action": st["expected_action"],
                    "expected_status": st["expected_status"],
                    "description": st["description"]
                }
                for st in s_data["steps"]
            ]
        })
    return res


def get_replay_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """获取指定受控回放场景详情"""
    return REPLAY_SCENARIOS.get(scenario_id)


def _get_existing_cycle(service: Any, account_id: str, request_key: str) -> Optional[PaperCycle]:
    """检查是否为已执行周期的幂等重试"""
    with sqlite3.connect(service.paper_adapter.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        r = cur.execute(
            "SELECT * FROM lab_paper_cycles WHERE account_id = ? AND request_key = ?",
            (account_id, request_key)
        ).fetchone()
        if r is not None:
            c_data = dict(r)
            raw_fills = json.loads(c_data["fills_json"])
            return PaperCycle(
                schema_version=SCHEMA_VERSION,
                cycle_id=c_data["cycle_id"],
                persona_id=c_data["persona_id"],
                mode=c_data["mode"],
                request_key=c_data["request_key"],
                input_hash=c_data["input_hash"],
                scan_run_id=c_data.get("scan_run_id"),
                candidate_id=c_data.get("candidate_id"),
                status=c_data["status"],
                action=c_data["action"],
                fills_count=c_data["fills_count"],
                fills=[LabFill(**f) for f in raw_fills],
                rejection_reason=c_data.get("rejection_reason"),
                summary=c_data["summary"],
                account_snapshot=json.loads(c_data["account_snapshot_json"]),
                created_at=c_data["created_at"]
            )
    return None


def execute_replay_step(
    service: Any,
    persona_id: str,
    scenario_id: str,
    step_index: int,
    candidate_id: Optional[str],
    request_key: str
) -> Any:
    """
    执行受控回放场景的指定单步：
    1. 校验分身归属与场景合法性；
    2. 从服务端场景注册表中加载注入的确定性时钟、回放日期与受控报价；
    3. 调用底座 run_paper_cycle 执行原子落账与撮合；
    4. 绝不透传客户端任意 quote_override 或 clock。
    """
    persona = service.get_persona(persona_id)
    if persona is None:
        raise ValueError(f"分身 '{persona_id}' 不存在，请先注册")

    scenario = REPLAY_SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ValueError(f"未知受控回放场景 '{scenario_id}'，当前仅支持: {list(REPLAY_SCENARIOS.keys())}")

    matched_step = None
    for st in scenario["steps"]:
        if st["step_index"] == step_index:
            matched_step = st
            break

    if matched_step is None:
        raise ValueError(f"场景 '{scenario_id}' 不存在步骤序号 {step_index}")

    step_clock_dt = datetime.datetime.strptime(
        matched_step["clock_time"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=SHANGHAI)

    # 步骤1检查候选参数与真实策略关联
    cand_id_to_use = candidate_id
    scan_run_id_to_use = None
    step_quote_override = dict(matched_step["quote_override"])
    step_as_of_date = matched_step["as_of_date"]

    if matched_step["step_index"] == 1:
        if not cand_id_to_use:
            raise ValueError("执行受控回放建仓第1步时，必须提供待建仓候选标的代码 (candidate_id)")

        # 1. 核验分身当前已确认生效的策略版本
        active_strat = service.get_active_strategy(persona_id)
        if active_strat is None:
            raise ValueError(f"分身 '{persona_id}' 尚未确认并发布任何策略版本，无法执行回放买入")

        # 2. 从分身已保存的扫描记录中查找经该策略评估通过的 QUALIFIED 候选
        all_scans = service.list_scan_runs(persona_id)
        matched_cand = None
        for sc in all_scans:
            if sc.strategy_version_id != active_strat.id:
                continue
            if sc.candidates:
                for c in sc.candidates:
                    if c.symbol == cand_id_to_use and c.decision == "QUALIFIED":
                        matched_cand = c
                        scan_run_id_to_use = sc.id
                        break
            if matched_cand:
                break

        if matched_cand is None:
            # 严格拒绝：没有合格候选则无成交，绝不自动拼造数据，绝不替换标的
            raise ValueError(
                f"未在当前已确认策略 '{active_strat.id}' 的扫描记录中找到合格候选标的 '{cand_id_to_use}'，回放拒绝建仓"
            )

        # 3. 围绕选中的合格候选构建合法时序与受控样例行情报价
        cand_cutoff = str(matched_cand.data_cutoff).replace("-", "")[:8]
        buy_trade_date = get_next_trading_day_str(cand_cutoff)
        buy_clock_str = f"{buy_trade_date[:4]}-{buy_trade_date[4:6]}-{buy_trade_date[6:8]} 10:00:00"
        step_clock_dt = datetime.datetime.strptime(
            buy_clock_str, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=SHANGHAI)
        step_as_of_date = f"{buy_trade_date[:4]}-{buy_trade_date[4:6]}-{buy_trade_date[6:8]}"

        base_price = matched_cand.market_snapshot.get("close")
        if base_price is None or float(base_price) <= 0:
            base_price = matched_step["quote_price"]

        step_quote_override = {
            "symbol": cand_id_to_use,
            "price": float(base_price),
            "prev_close": round(float(base_price) * 0.99, 2),
            "yesterday_close": round(float(base_price) * 0.99, 2),
            "trade_date": buy_trade_date,
            "quote_time": buy_clock_str
        }
    elif matched_step["step_index"] == 2:
        # 步骤 2: 同日盘中平仓拦截 (T+1 REJECTED)
        account = service.get_paper_account(persona_id, mode="REPLAY")
        open_positions = account.positions or {}
        if not open_positions:
            existing = _get_existing_cycle(service, account.account_id, request_key)
            if existing:
                return existing
            raise ValueError("当前账户无任何持仓，无法执行同日 T+1 平仓拦截测试")

        pos_symbol = list(open_positions.keys())[0]
        pos = open_positions[pos_symbol]
        buy_trade_date = str(getattr(pos, "entry_date", getattr(pos, "trade_date", ""))).replace("-", "")[:8]
        t1_clock_str = f"{buy_trade_date[:4]}-{buy_trade_date[4:6]}-{buy_trade_date[6:8]} 14:30:00"
        step_clock_dt = datetime.datetime.strptime(
            t1_clock_str, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=SHANGHAI)
        step_as_of_date = f"{buy_trade_date[:4]}-{buy_trade_date[4:6]}-{buy_trade_date[6:8]}"

        exit_price = round(pos.entry_price * 1.059, 2)
        step_quote_override = {
            "symbol": pos_symbol,
            "price": exit_price,
            "prev_close": pos.entry_price,
            "yesterday_close": pos.entry_price,
            "trade_date": buy_trade_date,
            "quote_time": t1_clock_str
        }
    elif matched_step["step_index"] == 3:
        # 步骤 3: 次日达成止盈平仓 (SELL COMPLETED)
        account = service.get_paper_account(persona_id, mode="REPLAY")
        open_positions = account.positions or {}
        if not open_positions:
            existing = _get_existing_cycle(service, account.account_id, request_key)
            if existing:
                return existing
            raise ValueError("当前账户无任何持仓，无法执行止盈卖出")

        pos_symbol = list(open_positions.keys())[0]
        pos = open_positions[pos_symbol]
        buy_trade_date = str(getattr(pos, "entry_date", getattr(pos, "trade_date", ""))).replace("-", "")[:8]
        sell_trade_date = get_next_trading_day_str(buy_trade_date)
        sell_clock_str = f"{sell_trade_date[:4]}-{sell_trade_date[4:6]}-{sell_trade_date[6:8]} 10:00:00"
        step_clock_dt = datetime.datetime.strptime(
            sell_clock_str, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=SHANGHAI)
        step_as_of_date = f"{sell_trade_date[:4]}-{sell_trade_date[4:6]}-{sell_trade_date[6:8]}"

        exit_price = round(pos.entry_price * 1.059, 2)
        step_quote_override = {
            "symbol": pos_symbol,
            "price": exit_price,
            "prev_close": pos.entry_price,
            "yesterday_close": pos.entry_price,
            "trade_date": sell_trade_date,
            "quote_time": sell_clock_str
        }

    # 注入受控场景时钟
    original_clock = getattr(service.paper_adapter, "clock", None)
    service.paper_adapter.clock = lambda: step_clock_dt

    try:
        cycle = service.run_paper_cycle(
            persona_id=persona_id,
            request_key=request_key,
            mode="REPLAY",
            scan_run_id=scan_run_id_to_use if matched_step["step_index"] == 1 else None,
            candidate_id=cand_id_to_use if matched_step["step_index"] == 1 else None,
            as_of_date=step_as_of_date,
            quote_override=step_quote_override
        )
        return cycle
    finally:
        if original_clock is not None:
            service.paper_adapter.clock = original_clock
