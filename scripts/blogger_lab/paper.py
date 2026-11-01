# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/paper.py
V7 博主实验室独立研究模拟撮合、周期管理与隔离账本服务 (阶段 5 核心实现)
落实：
1. 分身与模式隔离 (LIVE 与 REPLAY 经济状态分离，默认 10 万虚拟本金)
2. 周期服务 (run_paper_cycle): 先检查退出、发生退出不换仓、每分身最多1只持仓、全额平仓
3. 候选入场核验: 校验已保存 ScanRun、策略版本、合格状态与唯一消费记录 (防换键重复消费)
4. 严格幂等保障: (account_id, request_key) 唯一约束与 input_hash 核验 (同键同参数幂等，同键不同参数 409 冲突)
5. 事务原子性与一致性: 账户余额、持仓、成交流水、候选消费标记与周期结果在同一 SQLite 事务中落账，失败全部回滚
"""

from __future__ import annotations
import os
import sys
import json
import uuid
import sqlite3
import datetime
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from competition.rules import AShareRules
from competition.costs import UnifiedCostModel
from blogger_lab.schemas import (
    LabAccount,
    LabPosition,
    LabFill,
    PaperCycle,
    SCHEMA_VERSION
)

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def default_clock() -> datetime.datetime:
    return datetime.datetime.now(SHANGHAI)


def compute_order_input_hash(
    persona_id: str,
    direction: str,
    symbol: str,
    strategy_version: str,
    amount_or_budget: float,
    request_key: str
) -> str:
    """计算订单核心参数的规范 SHA256 哈希"""
    raw_dict = {
        "persona_id": str(persona_id),
        "direction": str(direction).upper(),
        "symbol": str(symbol).zfill(6),
        "strategy_version": str(strategy_version),
        "amount_or_budget": round(float(amount_or_budget), 2),
        "request_key": str(request_key)
    }
    payload_str = json.dumps(raw_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def compute_cycle_input_hash(
    persona_id: str,
    mode: str,
    scan_run_id: Optional[str],
    candidate_id: Optional[str],
    request_key: str,
    as_of_date: Optional[str] = None,
    quote_override: Optional[Dict[str, Any]] = None
) -> str:
    """计算模拟周期请求参数的规范 SHA256 哈希 (包含显式回放日期与场景报价)"""
    norm_quote = None
    if quote_override:
        norm_quote = {
            "price": round(float(quote_override["price"]), 4) if "price" in quote_override else None,
            "yesterday_close": round(float(quote_override["yesterday_close"]), 4) if "yesterday_close" in quote_override else None,
            "trade_date": str(quote_override.get("trade_date", "")).replace("-", "") if "trade_date" in quote_override else None,
            "is_suspended": bool(quote_override.get("is_suspended", False))
        }

    raw_dict = {
        "persona_id": str(persona_id),
        "mode": str(mode).upper(),
        "scan_run_id": str(scan_run_id or ""),
        "candidate_id": str(candidate_id or ""),
        "request_key": str(request_key),
        "as_of_date": str(as_of_date).replace("-", "")[:8] if as_of_date else "",
        "quote_override": norm_quote
    }
    payload_str = json.dumps(raw_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


class BloggerLabPaperAdapter:
    """博主实验室独立模拟撮合与研究账本服务"""

    def __init__(
        self,
        lab_root: Optional[str] = None,
        clock=None,
        store: Optional[Any] = None,
        market_adapter: Optional[Any] = None
    ):
        if lab_root is None:
            self.lab_root = Path(_BASE) / "data" / "blogger_lab"
        else:
            self.lab_root = Path(lab_root)

        self.lab_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.lab_root / "lab_paper.sqlite"
        self.clock = clock or default_clock
        self.cost_model = UnifiedCostModel()
        self._store = store
        self._market_adapter = market_adapter
        self._init_sqlite()

    @property
    def store(self):
        if self._store is None:
            from blogger_lab.store import BloggerLabStore
            self._store = BloggerLabStore(lab_root=str(self.lab_root))
        return self._store

    @property
    def market_adapter(self):
        if self._market_adapter is None:
            from blogger_lab.market import BloggerLabMarketAdapter
            self._market_adapter = BloggerLabMarketAdapter(clock=self.clock)
        return self._market_adapter

    def _init_sqlite(self):
        """初始化实验专用数据库表结构（支持模式隔离与消费跟踪）"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_accounts (
                    account_id TEXT PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'REPLAY',
                    cash REAL NOT NULL,
                    initial_capital REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 兼容性迁移检查：确保 mode 字段存在
            cols = [r[1] for r in cur.execute("PRAGMA table_info(lab_accounts)").fetchall()]
            if "mode" not in cols and "account_id" in cols:
                cur.execute("ALTER TABLE lab_accounts ADD COLUMN mode TEXT NOT NULL DEFAULT 'REPLAY'")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_mode ON lab_accounts(persona_id, mode)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_positions (
                    position_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    available_shares INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    entry_date TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_strategy_version TEXT NOT NULL,
                    exit_snapshot_json TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES lab_accounts(account_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_fills (
                    fill_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    trade_time TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    gross_amount REAL NOT NULL,
                    commission REAL NOT NULL,
                    stamp_tax REAL NOT NULL,
                    slippage_cost REAL NOT NULL,
                    cash_effect REAL NOT NULL,
                    reason TEXT,
                    UNIQUE(account_id, request_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_consumed_candidates (
                    consumption_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    scan_run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    UNIQUE(account_id, scan_run_id, symbol)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_paper_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    scan_run_id TEXT,
                    candidate_id TEXT,
                    status TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fills_count INTEGER NOT NULL,
                    fills_json TEXT NOT NULL,
                    rejection_reason TEXT,
                    summary TEXT NOT NULL,
                    account_snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, request_key)
                )
            """)
            conn.commit()

    def get_or_create_account(
        self,
        persona_id: str,
        mode: str = "REPLAY",
        initial_capital: float = 100000.0,
        conn: Optional[sqlite3.Connection] = None
    ) -> LabAccount:
        """读取或创建独立分身研究账户（按 persona_id 和 mode 严格隔离经济状态）"""
        mode_clean = str(mode).upper()

        def _fetch(db_conn: sqlite3.Connection) -> LabAccount:
            db_conn.row_factory = sqlite3.Row
            cur = db_conn.cursor()
            row = cur.execute(
                "SELECT * FROM lab_accounts WHERE persona_id = ? AND mode = ?",
                (persona_id, mode_clean)
            ).fetchone()

            if row is None:
                acc_id = f"ACC_LAB_{persona_id}_{mode_clean}_{uuid.uuid4().hex[:8]}"
                now_iso = self.clock().isoformat()
                cur.execute(
                    """
                    INSERT INTO lab_accounts (account_id, persona_id, mode, cash, initial_capital, revision, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (acc_id, persona_id, mode_clean, initial_capital, initial_capital, now_iso)
                )
                if conn is None:
                    db_conn.commit()
                return LabAccount(
                    account_id=acc_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    initial_capital=initial_capital,
                    cash=initial_capital,
                    total_asset=initial_capital,
                    revision=0,
                    positions={}
                )

            # 读取持仓
            pos_rows = cur.execute(
                "SELECT * FROM lab_positions WHERE account_id = ?",
                (row["account_id"],)
            ).fetchall()

            positions = {}
            pos_market_value = 0.0
            for pr in pos_rows:
                p = LabPosition(
                    position_id=pr["position_id"],
                    symbol=pr["symbol"],
                    name=pr["name"],
                    shares=pr["shares"],
                    available_shares=pr["available_shares"],
                    avg_cost=pr["avg_cost"],
                    entry_date=pr["entry_date"],
                    entry_price=pr["entry_price"],
                    entry_strategy_version=pr["entry_strategy_version"],
                    exit_snapshot=json.loads(pr["exit_snapshot_json"]) if pr["exit_snapshot_json"] else {}
                )
                positions[p.symbol] = p
                pos_market_value += p.shares * p.avg_cost

            return LabAccount(
                account_id=row["account_id"],
                persona_id=row["persona_id"],
                mode=row["mode"] if "mode" in row.keys() else mode_clean,
                initial_capital=row["initial_capital"],
                cash=row["cash"],
                total_asset=round(row["cash"] + pos_market_value, 2),
                revision=row["revision"],
                positions=positions
            )

        if conn is not None:
            return _fetch(conn)
        else:
            with sqlite3.connect(self.db_path) as db_conn:
                return _fetch(db_conn)

    def execute_buy(
        self,
        persona_id: str,
        symbol: str,
        name: str,
        raw_price: float,
        yesterday_close: float,
        strategy_version: str,
        budget: float,
        request_key: str,
        mode: str = "REPLAY",
        is_st: bool = False,
        board: str = "MAIN",
        is_suspended: bool = False,
        exit_snapshot: Optional[Dict[str, Any]] = None,
        reason: str = "",
        external_conn: Optional[sqlite3.Connection] = None,
        trade_date_override: Optional[str] = None
    ) -> Tuple[bool, Optional[LabFill], str]:
        """
        执行买入撮合并原子落账。
        严格绑定 account_id 与 input_hash 校验幂等，受每分身最多1只持仓限制。
        """
        now = self.clock()
        trade_date = (trade_date_override or now.strftime("%Y%m%d")).replace("-", "")
        trade_time = now.strftime("%Y-%m-%d %H:%M:%S")
        mode_clean = str(mode).upper()
        input_hash = compute_order_input_hash(
            persona_id=persona_id,
            direction="BUY",
            symbol=symbol,
            strategy_version=strategy_version,
            amount_or_budget=budget,
            request_key=request_key
        )

        # 1. 裁判检验
        can_b, msg = AShareRules.can_buy(
            raw_price, yesterday_close, is_st=is_st, board=board, is_suspended=is_suspended
        )
        if not can_b:
            return False, None, f"AShareRules 买入拦截: {msg}"

        def _do_buy(conn: sqlite3.Connection) -> Tuple[bool, Optional[LabFill], str]:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 2. 读取账户
            account = self.get_or_create_account(persona_id, mode=mode_clean, conn=conn)

            # 3. 幂等检查 (严格绑定 account_id 和 request_key)
            existing_fill = cur.execute(
                "SELECT * FROM lab_fills WHERE account_id = ? AND request_key = ?",
                (account.account_id, request_key)
            ).fetchone()

            if existing_fill is not None:
                f_data = dict(existing_fill)
                if f_data.get("input_hash") != input_hash:
                    return False, None, f"幂等冲突：request_key '{request_key}' 存在不同参数的历史指令"

                fill = LabFill(
                    fill_id=f_data["fill_id"],
                    input_hash=f_data["input_hash"],
                    direction=f_data["direction"],
                    symbol=f_data["symbol"],
                    name=f_data["name"],
                    trade_time=f_data["trade_time"],
                    trade_date=f_data["trade_date"],
                    price=f_data["price"],
                    shares=f_data["shares"],
                    gross_amount=f_data["gross_amount"],
                    commission=f_data["commission"],
                    stamp_tax=f_data["stamp_tax"],
                    slippage_cost=f_data.get("slippage_cost", 0.0),
                    cash_effect=f_data["cash_effect"],
                    reason=f_data["reason"]
                )
                return True, fill, "幂等命中：指令已在先前成交"

            # 4. 持仓上限核验 (每分身最多1只持仓)
            cur_pos_count = cur.execute(
                "SELECT COUNT(*) FROM lab_positions WHERE account_id = ? AND symbol != ?",
                (account.account_id, symbol)
            ).fetchone()[0]
            if cur_pos_count >= 1:
                return False, None, "持仓受限：每个分身最多持有 1 只股票，当前已达到持仓上限"

            effective_budget = budget
            if account.cash < effective_budget:
                effective_budget = account.cash

            # 5. 计算手数与费用
            exec_price_raw = raw_price * (1.0 + self.cost_model.slippage_rate)
            shares = AShareRules.calculate_lot_shares(effective_budget, exec_price_raw)
            if shares < 100:
                return False, None, f"可用现金 ({account.cash:.2f}) 不足以买入最小 1 手 (100股)"

            exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)
            if total_cash > account.cash:
                shares -= 100
                if shares < 100:
                    return False, None, "扣除手续费后资金不足1手"
                exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)

            # 6. 生成成交凭据
            fill_id = f"FILL_BUY_{symbol}_{uuid.uuid4().hex[:8]}"
            pos_id = f"POS_{persona_id}_{mode_clean}_{symbol}"
            fill = LabFill(
                fill_id=fill_id,
                position_id=pos_id,
                input_hash=input_hash,
                direction="BUY",
                symbol=symbol,
                name=name,
                trade_time=trade_time,
                trade_date=trade_date,
                price=exec_p,
                shares=shares,
                gross_amount=gross_amt,
                commission=comm,
                stamp_tax=0.0,
                slippage_cost=round(raw_price * self.cost_model.slippage_rate * shares, 2),
                cash_effect=-round(total_cash, 2),
                reason=reason
            )

            # 7. 原子更新现金与持仓
            new_cash = account.cash - total_cash
            cur.execute(
                "UPDATE lab_accounts SET cash = ?, revision = revision + 1, updated_at = ? WHERE account_id = ?",
                (new_cash, now.isoformat(), account.account_id)
            )

            # T+1 刚性锁仓: available_shares = 0
            cur.execute(
                """
                INSERT INTO lab_positions (
                    position_id, account_id, symbol, name, shares, available_shares,
                    avg_cost, entry_date, entry_price, entry_strategy_version, exit_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    shares = shares + excluded.shares,
                    avg_cost = excluded.avg_cost
                """,
                (
                    pos_id, account.account_id, symbol, name, shares,
                    exec_p, trade_date, exec_p, strategy_version,
                    json.dumps(exit_snapshot or {}, ensure_ascii=False)
                )
            )

            cur.execute(
                """
                INSERT INTO lab_fills (
                    fill_id, account_id, request_key, input_hash, direction, symbol, name,
                    trade_time, trade_date, price, shares, gross_amount,
                    commission, stamp_tax, slippage_cost, cash_effect, reason
                ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id, account.account_id, request_key, input_hash, fill.symbol,
                    fill.name, fill.trade_time, fill.trade_date, fill.price,
                    fill.shares, fill.gross_amount, fill.commission,
                    fill.stamp_tax, fill.slippage_cost, fill.cash_effect, fill.reason
                )
            )

            if external_conn is None:
                conn.commit()
            return True, fill, "买入撮合成交"

        if external_conn is not None:
            return _do_buy(external_conn)
        else:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    return _do_buy(conn)
                except Exception as e:
                    conn.rollback()
                    return False, None, f"买入事务提交失败，已安全回滚: {e}"

    def execute_sell(
        self,
        persona_id: str,
        symbol: str,
        raw_price: float,
        yesterday_close: float,
        request_key: str,
        mode: str = "REPLAY",
        strategy_version: str = "STRAT_DEFAULT",
        is_st: bool = False,
        board: str = "MAIN",
        is_suspended: bool = False,
        reason: str = "",
        external_conn: Optional[sqlite3.Connection] = None,
        trade_date_override: Optional[str] = None
    ) -> Tuple[bool, Optional[LabFill], str]:
        """
        执行卖出（全平）撮合并原子落账。
        严格核查 account_id 幂等、input_hash 匹配与 T+1 刚性锁仓。
        未持有持仓不得产生卖出收入。
        """
        now = self.clock()
        trade_date = (trade_date_override or now.strftime("%Y%m%d")).replace("-", "")
        trade_time = now.strftime("%Y-%m-%d %H:%M:%S")
        mode_clean = str(mode).upper()
        input_hash = compute_order_input_hash(
            persona_id=persona_id,
            direction="SELL",
            symbol=symbol,
            strategy_version=strategy_version,
            amount_or_budget=0.0,
            request_key=request_key
        )

        def _do_sell(conn: sqlite3.Connection) -> Tuple[bool, Optional[LabFill], str]:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 读取账户
            account = self.get_or_create_account(persona_id, mode=mode_clean, conn=conn)

            # 幂等检查 (严格按 account_id 和 request_key)
            existing_fill = cur.execute(
                "SELECT * FROM lab_fills WHERE account_id = ? AND request_key = ?",
                (account.account_id, request_key)
            ).fetchone()

            if existing_fill is not None:
                f_data = dict(existing_fill)
                if f_data.get("input_hash") != input_hash:
                    return False, None, f"幂等冲突：request_key '{request_key}' 存在不同参数的历史指令"

                fill = LabFill(
                    fill_id=f_data["fill_id"],
                    input_hash=f_data["input_hash"],
                    direction=f_data["direction"],
                    symbol=f_data["symbol"],
                    name=f_data["name"],
                    trade_time=f_data["trade_time"],
                    trade_date=f_data["trade_date"],
                    price=f_data["price"],
                    shares=f_data["shares"],
                    gross_amount=f_data["gross_amount"],
                    commission=f_data["commission"],
                    stamp_tax=f_data["stamp_tax"],
                    slippage_cost=f_data.get("slippage_cost", 0.0),
                    cash_effect=f_data["cash_effect"],
                    reason=f_data["reason"]
                )
                return True, fill, "幂等命中：指令已在先前成交"

            # 读取持仓 (严格限定该 account_id)
            pos_row = cur.execute(
                "SELECT * FROM lab_positions WHERE symbol = ? AND account_id = ?",
                (symbol, account.account_id)
            ).fetchone()

            if pos_row is None:
                return False, None, f"未持有标的 {symbol}，无法执行卖出"

            pos_data = dict(pos_row)
            pos_data["buy_date"] = pos_data.get("entry_date")

            # 1. T+1 裁判核验
            can_t1, avail_shares, msg_t1 = AShareRules.validate_t1_sell(pos_data, trade_date)
            if not can_t1:
                return False, None, f"AShareRules T+1 拦截: {msg_t1}"

            # 2. 跌停/停牌裁判
            can_s, msg_s = AShareRules.can_sell(
                raw_price, yesterday_close, is_st=is_st, board=board, is_suspended=is_suspended
            )
            if not can_s:
                return False, None, f"AShareRules 卖出拦截: {msg_s}"

            # 3. 卖出净收益与费用计算
            buy_cost_total = float(pos_data["avg_cost"]) * int(avail_shares)
            exec_p, gross_amt, comm, stamp, net_cash, net_pnl, pnl_pct = self.cost_model.calculate_sell_proceeds(
                raw_price, avail_shares, buy_cost_total
            )

            fill_id = f"FILL_SELL_{symbol}_{uuid.uuid4().hex[:8]}"
            fill = LabFill(
                fill_id=fill_id,
                position_id=pos_data["position_id"],
                input_hash=input_hash,
                direction="SELL",
                symbol=symbol,
                name=pos_data["name"],
                trade_time=trade_time,
                trade_date=trade_date,
                price=exec_p,
                shares=avail_shares,
                gross_amount=gross_amt,
                commission=comm,
                stamp_tax=stamp,
                slippage_cost=round(raw_price * self.cost_model.slippage_rate * avail_shares, 2),
                cash_effect=round(net_cash, 2),
                reason=reason
            )

            # 4. 原子落账 (增加现金、删除全平持仓、插入成交流水)
            cur.execute(
                "UPDATE lab_accounts SET cash = cash + ?, revision = revision + 1, updated_at = ? WHERE account_id = ?",
                (net_cash, now.isoformat(), account.account_id)
            )

            cur.execute(
                "DELETE FROM lab_positions WHERE position_id = ?",
                (pos_data["position_id"],)
            )

            cur.execute(
                """
                INSERT INTO lab_fills (
                    fill_id, account_id, request_key, input_hash, direction, symbol, name,
                    trade_time, trade_date, price, shares, gross_amount,
                    commission, stamp_tax, slippage_cost, cash_effect, reason
                ) VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id, account.account_id, request_key, input_hash, fill.symbol,
                    fill.name, fill.trade_time, fill.trade_date, fill.price,
                    fill.shares, fill.gross_amount, fill.commission,
                    fill.stamp_tax, fill.slippage_cost, fill.cash_effect, fill.reason
                )
            )

            if external_conn is None:
                conn.commit()
            return True, fill, "卖出撮合成交"

        if external_conn is not None:
            return _do_sell(external_conn)
        else:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    return _do_sell(conn)
                except Exception as e:
                    conn.rollback()
                    return False, None, f"卖出事务提交失败，已安全回滚: {e}"

    def run_paper_cycle(
        self,
        persona_id: str,
        request_key: str,
        mode: str = "REPLAY",
        scan_run_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        as_of_date: Optional[str] = None,
        quote_override: Optional[Dict[str, Any]] = None,
        failure_injection_hook: Optional[Any] = None
    ) -> PaperCycle:
        """
        执行一次独立模拟周期：
        1. 读取账户与持仓状态；
        2. 先检查退出：按开仓快照参数与当前行情求值，发生退出全额平仓，本周期不立刻换仓；
        3. 若无持仓且未退出：核验候选有效性、策略版本、持仓上限与防重复消费，执行买入；
        4. 全过程在一个 SQLite 事务中原子提交，支持完整故障回滚与严格幂等。
        """
        mode_clean = str(mode).upper()
        if mode_clean not in ("LIVE", "REPLAY"):
            raise ValueError(f"不支持的模拟模式 '{mode}'，仅支持 'LIVE' 或 'REPLAY'")

        now = self.clock()
        now_iso = now.isoformat()
        clock_trade_date = now.strftime("%Y%m%d")

        input_hash = compute_cycle_input_hash(
            persona_id=persona_id,
            mode=mode_clean,
            scan_run_id=scan_run_id,
            candidate_id=candidate_id,
            request_key=request_key,
            as_of_date=as_of_date,
            quote_override=quote_override
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. 幂等校验
            account = self.get_or_create_account(persona_id, mode=mode_clean, conn=conn)
            existing_cycle = cur.execute(
                "SELECT * FROM lab_paper_cycles WHERE account_id = ? AND request_key = ?",
                (account.account_id, request_key)
            ).fetchone()

            if existing_cycle is not None:
                c_data = dict(existing_cycle)
                if c_data.get("input_hash") != input_hash:
                    raise ValueError(f"幂等冲突：request_key '{request_key}' 存在不同输入参数的历史周期记录")

                raw_fills = json.loads(c_data["fills_json"])
                fills_list = [LabFill(**f) for f in raw_fills]
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
                    fills=fills_list,
                    rejection_reason=c_data.get("rejection_reason"),
                    summary=c_data["summary"],
                    account_snapshot=json.loads(c_data["account_snapshot_json"]),
                    created_at=c_data["created_at"]
                )

            cycle_id = f"CYCLE_{persona_id}_{uuid.uuid4().hex[:8]}"

            # 2. 模式门禁：阶段 5 仅支持受控 REPLAY 模拟撮合，LIVE 模式刚性阻断 (BLOCKED)
            if mode_clean == "LIVE":
                reason = "阶段5仅支持受控REPLAY模拟撮合，LIVE模式撮合已安全阻断 (BLOCKED)"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 3. 交易日历状态与交易日校验 (复用现有交易日历)
            from runtime_config import trading_calendar_status, TradingCalendarStatus
            cal_status = trading_calendar_status(now.date())
            if cal_status.get("status") != TradingCalendarStatus.VALID:
                reason = f"交易日历状态无效或不可用: {cal_status.get('reason')}"
                has_pos = cur.execute("SELECT 1 FROM lab_positions WHERE account_id = ?", (account.account_id,)).fetchone() is not None
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="HOLD" if has_pos else "NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            if not cal_status.get("trading_day"):
                reason = f"非交易日 (休市/周末): 当前场景日期 {now.strftime('%Y-%m-%d')} 不可撮合"
                has_pos = cur.execute("SELECT 1 FROM lab_positions WHERE account_id = ?", (account.account_id,)).fetchone() is not None
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="HOLD" if has_pos else "NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 4. 允许撮合时段/交易窗口校验 (复用既有窗口规则)
            valid_window, window_reason = AShareRules.is_valid_trade_window(now.strftime("%H:%M"))
            if not valid_window:
                reason = f"非允许撮合时段: {window_reason} (当前场景时钟 {now.strftime('%H:%M')})"
                has_pos = cur.execute("SELECT 1 FROM lab_positions WHERE account_id = ?", (account.account_id,)).fetchone() is not None
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="HOLD" if has_pos else "NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 5. REPLAY 时钟与日期一致性校验
            if as_of_date:
                clean_as_of = str(as_of_date).replace("-", "")[:8]
                if clean_as_of != clock_trade_date:
                    reason = f"回放日期 '{clean_as_of}' 与场景时钟交易日 '{clock_trade_date}' 不一致，拒绝撮合"
                    cycle = PaperCycle(
                        schema_version=SCHEMA_VERSION,
                        cycle_id=cycle_id,
                        persona_id=persona_id,
                        mode=mode_clean,
                        request_key=request_key,
                        input_hash=input_hash,
                        scan_run_id=scan_run_id,
                        candidate_id=candidate_id,
                        status="BLOCKED",
                        action="NONE",
                        fills_count=0,
                        fills=[],
                        rejection_reason=reason,
                        summary=reason,
                        account_snapshot=account.model_dump(),
                        created_at=now_iso
                    )
                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

            if quote_override and "trade_date" in quote_override:
                clean_quote_date = str(quote_override["trade_date"]).replace("-", "")[:8]
                if clean_quote_date != clock_trade_date:
                    reason = f"场景报价交易日 '{clean_quote_date}' 与场景时钟交易日 '{clock_trade_date}' 不一致，拒绝撮合"
                    cycle = PaperCycle(
                        schema_version=SCHEMA_VERSION,
                        cycle_id=cycle_id,
                        persona_id=persona_id,
                        mode=mode_clean,
                        request_key=request_key,
                        input_hash=input_hash,
                        scan_run_id=scan_run_id,
                        candidate_id=candidate_id,
                        status="BLOCKED",
                        action="NONE",
                        fills_count=0,
                        fills=[],
                        rejection_reason=reason,
                        summary=reason,
                        account_snapshot=account.model_dump(),
                        created_at=now_iso
                    )
                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

            # 4. 优先检查现有持仓与退出条件 (持仓退出绝对优先于入场防重)
            pos_row = cur.execute(
                "SELECT * FROM lab_positions WHERE account_id = ?",
                (account.account_id,)
            ).fetchone()

            if pos_row is not None:
                pos = dict(pos_row)
                pos_symbol = pos["symbol"]
                pos_name = pos["name"]
                entry_p = float(pos["entry_price"])
                exit_snap = json.loads(pos["exit_snapshot_json"]) if pos["exit_snapshot_json"] else {}

                # 获取当前标的行情与价格
                if quote_override:
                    if quote_override.get("is_suspended", False):
                        reason = f"持仓标的 {pos_name}({pos_symbol}) 停牌，阻断退出评估，继续持有"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason="标的停牌，阻断退出评估",
                            summary=reason,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle

                    curr_price = float(quote_override["price"])
                    if curr_price <= 0:
                        reason = f"持仓标的 {pos_name}({pos_symbol}) 行情价格无效 ({curr_price})，阻断退出评估"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason="行情价格无效",
                            summary=reason,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle

                    prev_close = float(quote_override.get("yesterday_close", curr_price))
                    q_date = str(quote_override.get("trade_date", clock_trade_date)).replace("-", "")[:8]
                else:
                    klines = []
                    if self.market_adapter and self.market_adapter.market_db:
                        klines = self.market_adapter.market_db.get_daily_klines(
                            pos_symbol, cutoff_date=clock_trade_date, limit=2
                        )
                    if klines and str(klines[-1].get("trade_date", "")).replace("-", "")[:8] == clock_trade_date:
                        curr_price = float(klines[-1]["close"])
                        prev_close = float(klines[-2]["close"]) if len(klines) >= 2 else curr_price
                        q_date = clock_trade_date
                    else:
                        reason = f"缺少标的 {pos_name}({pos_symbol}) 在 {clock_trade_date} 的行情数据，阻断退出评估"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason="缺少行情数据，阻断退出评估",
                            summary=reason,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle

                # 计算盈亏比例
                pnl_pct = round((curr_price - entry_p) / entry_p * 100.0, 4) if entry_p > 0 else 0.0
                sl_pct = exit_snap.get("stop_loss_pct")
                tp_pct = exit_snap.get("take_profit_pct")

                is_stop_loss = (sl_pct is not None and pnl_pct <= float(sl_pct))
                is_take_profit = (tp_pct is not None and pnl_pct >= float(tp_pct))

                if is_stop_loss or is_take_profit:
                    trigger_label = "止损" if is_stop_loss else "止盈"
                    threshold = sl_pct if is_stop_loss else tp_pct
                    exit_reason = f"触发{trigger_label} (当前盈亏 {pnl_pct:+.2f}% 触及阈值 {threshold}%)"

                    # 检查 T+1 规则
                    pos_dict_for_t1 = dict(pos)
                    pos_dict_for_t1["buy_date"] = pos_dict_for_t1.get("entry_date")
                    can_t1, _, msg_t1 = AShareRules.validate_t1_sell(pos_dict_for_t1, q_date)

                    if not can_t1:
                        # 当日买入受 T+1 限制，不可平仓
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="NO_ACTION",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason=f"AShareRules T+1 拦截: {msg_t1}",
                            summary=f"持仓标的 {pos_name}({pos_symbol}) {exit_reason}，但受 T+1 刚性锁仓限制当日不可卖出，继续持有",
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle

                    # 执行卖出平仓
                    ok_sell, fill_sell, msg_sell = self.execute_sell(
                        persona_id=persona_id,
                        symbol=pos_symbol,
                        raw_price=curr_price,
                        yesterday_close=prev_close,
                        request_key=f"{request_key}_exit",
                        mode=mode_clean,
                        strategy_version=pos["entry_strategy_version"],
                        is_suspended=bool(quote_override.get("is_suspended", False)) if quote_override else False,
                        reason=exit_reason,
                        external_conn=conn,
                        trade_date_override=q_date
                    )

                    if ok_sell and fill_sell:
                        # 重新计算账户快照
                        acc_snap = self.get_or_create_account(persona_id, mode=mode_clean, conn=conn).model_dump()
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="COMPLETED",
                            action="SELL",
                            fills_count=1,
                            fills=[fill_sell],
                            rejection_reason=None,
                            summary=f"持仓标的 {pos_name}({pos_symbol}) {exit_reason}，全额退出平仓 {fill_sell.shares} 股，净回收现金 {fill_sell.cash_effect:+.2f} 元",
                            account_snapshot=acc_snap,
                            created_at=now_iso
                        )
                    else:
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="FAILED",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason=msg_sell,
                            summary=f"平仓撮合未通过: {msg_sell}",
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )

                    # 故障注入测试点
                    if failure_injection_hook:
                        failure_injection_hook()

                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

                else:
                    # 未触发退出，继续持有
                    if candidate_id or scan_run_id:
                        reason_blk = f"持仓受限：分身已持有标的 {pos_name}({pos_symbol})，达到每分身最多 1 只持仓上限，全平前不可买入新标的"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason=reason_blk,
                            summary=reason_blk,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                    else:
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="NO_ACTION",
                            action="HOLD",
                            fills_count=0,
                            fills=[],
                            rejection_reason=None,
                            summary=f"持仓标的 {pos_name}({pos_symbol}) 当前盈亏 {pnl_pct:+.2f}% 未触及退出阈值，继续持有",
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

            # 3. 账户无持仓：检查候选入场
            if not scan_run_id and not candidate_id:
                # 仅为周期退出核验，无候选指令
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=None,
                    candidate_id=None,
                    status="NO_ACTION",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=None,
                    summary="账户无持仓且未指定买入候选，周期无操作",
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 4. 核验入场候选
            scan_run = self.store.get_scan_run(scan_run_id, persona_id) if scan_run_id else None
            if scan_run is None:
                reason = f"扫描记录 '{scan_run_id}' 不存在或不属于分身 '{persona_id}'"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            if scan_run.mode != mode_clean:
                reason = f"扫描记录模式 '{scan_run.mode}' 与模拟周期模式 '{mode_clean}' 不一致"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 匹配候选对象
            target_cand = None
            for c in scan_run.candidates:
                if c.symbol == candidate_id or f"{scan_run_id}:{c.symbol}" == candidate_id:
                    target_cand = c
                    break

            if target_cand is None:
                reason = f"标的 '{candidate_id}' 不在扫描记录 '{scan_run_id}' 的候选列表中"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            if target_cand.decision != "QUALIFIED":
                reason = f"标的 '{target_cand.symbol}' 评估状态为 '{target_cand.decision}'，非合格候选"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 约束：未来候选证据时间门禁 (解析 quote_time、形成时间及 data_cutoff，证据时点严禁晚于场景成交时钟)
            cand_cutoff = (target_cand.data_cutoff or scan_run.as_of_date or "").replace("-", "")[:8]
            if not cand_cutoff:
                reason = "候选证据缺失截止日期信息，阻断入场"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            if cand_cutoff > clock_trade_date:
                reason = f"未来候选阻断：候选证据截止日 ({cand_cutoff}) 晚于当前回放撮合交易日 ({clock_trade_date})，严禁使用未来信息回填买入"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 解析候选行情报价时点 candidate.quote_time
            cand_quote_dt = None
            if target_cand.quote_time:
                try:
                    raw_qt = target_cand.quote_time.strip()
                    if "T" in raw_qt or " " in raw_qt:
                        cand_quote_dt = datetime.datetime.fromisoformat(raw_qt)
                    else:
                        cand_quote_dt = datetime.datetime.fromisoformat(
                            f"{cand_cutoff[:4]}-{cand_cutoff[4:6]}-{cand_cutoff[6:8]}T{raw_qt}"
                        )
                    if cand_quote_dt.tzinfo is None:
                        cand_quote_dt = cand_quote_dt.replace(tzinfo=SHANGHAI)
                    else:
                        cand_quote_dt = cand_quote_dt.astimezone(SHANGHAI)
                except Exception:
                    cand_quote_dt = None

            if cand_quote_dt is None and len(cand_cutoff) == 8:
                # 若无显式时分秒时点，日线收盘证据按 A 股收盘时点 15:00:00 判定
                cand_quote_dt = datetime.datetime(
                    int(cand_cutoff[:4]), int(cand_cutoff[4:6]), int(cand_cutoff[6:8]),
                    15, 0, 0, tzinfo=SHANGHAI
                )

            if cand_quote_dt is None:
                reason = f"候选标的 {target_cand.name}({target_cand.symbol}) 证据时点缺失或无法解析，刚性阻断"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            if cand_quote_dt > now:
                reason = f"未来证据时点阻断：候选行情证据时点 ({cand_quote_dt.strftime('%Y-%m-%d %H:%M:%S')}) 晚于当前成交时钟 ({now.strftime('%Y-%m-%d %H:%M:%S')})，严禁使用未来证据撮合"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 核验机会扫描记录创建时点 scan_run.created_at
            if scan_run.created_at:
                try:
                    scan_created_dt = datetime.datetime.fromisoformat(scan_run.created_at.strip())
                    if scan_created_dt.tzinfo is None:
                        scan_created_dt = scan_created_dt.replace(tzinfo=SHANGHAI)
                    else:
                        scan_created_dt = scan_created_dt.astimezone(SHANGHAI)
                    if scan_created_dt > now:
                        reason = f"未来扫描记录阻断：机会扫描生成时间 ({scan_created_dt.strftime('%Y-%m-%d %H:%M:%S')}) 晚于当前成交时钟 ({now.strftime('%Y-%m-%d %H:%M:%S')})，候选尚未形成"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="NONE",
                            fills_count=0,
                            fills=[],
                            rejection_reason=reason,
                            summary=reason,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle
                except Exception:
                    pass

            # 5. 防重复建仓检查：核验该候选是否已被消费
            consumed_row = cur.execute(
                "SELECT * FROM lab_consumed_candidates WHERE account_id = ? AND scan_run_id = ? AND symbol = ?",
                (account.account_id, scan_run.id, target_cand.symbol)
            ).fetchone()

            if consumed_row is not None:
                reason = f"候选标的 {target_cand.name}({target_cand.symbol}) 已在先前周期被消费建仓，严禁换键重复建仓"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            # 6. 读取策略版本并获取单笔仓位与退出快照
            strat = self.store.get_strategy_version(target_cand.strategy_version_id, persona_id)
            if strat is None:
                reason = f"候选关联的策略版本 '{target_cand.strategy_version_id}' 不存在或未确认发布"
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run_id,
                    candidate_id=candidate_id,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=reason,
                    summary=reason,
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )
                self._write_cycle_row(cur, account.account_id, cycle)
                conn.commit()
                return cycle

            pos_fraction = strat.sizing_rules.position_fraction if strat.sizing_rules else 1.0
            buy_budget = round(account.cash * float(pos_fraction), 2)

            # 确定买入价格与交易日
            if quote_override:
                if quote_override.get("is_suspended", False):
                    reason = f"标的 {target_cand.name}({target_cand.symbol}) 处于停牌状态，阻断买入"
                    cycle = PaperCycle(
                        schema_version=SCHEMA_VERSION,
                        cycle_id=cycle_id,
                        persona_id=persona_id,
                        mode=mode_clean,
                        request_key=request_key,
                        input_hash=input_hash,
                        scan_run_id=scan_run_id,
                        candidate_id=candidate_id,
                        status="BLOCKED",
                        action="NONE",
                        fills_count=0,
                        fills=[],
                        rejection_reason="标的停牌，阻断买入",
                        summary=reason,
                        account_snapshot=account.model_dump(),
                        created_at=now_iso
                    )
                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

                raw_buy_price = float(quote_override["price"])
                if raw_buy_price <= 0:
                    reason = f"买入报价无效 ({raw_buy_price})，阻断买入"
                    cycle = PaperCycle(
                        schema_version=SCHEMA_VERSION,
                        cycle_id=cycle_id,
                        persona_id=persona_id,
                        mode=mode_clean,
                        request_key=request_key,
                        input_hash=input_hash,
                        scan_run_id=scan_run_id,
                        candidate_id=candidate_id,
                        status="BLOCKED",
                        action="NONE",
                        fills_count=0,
                        fills=[],
                        rejection_reason="买入报价无效",
                        summary=reason,
                        account_snapshot=account.model_dump(),
                        created_at=now_iso
                    )
                    self._write_cycle_row(cur, account.account_id, cycle)
                    conn.commit()
                    return cycle

                buy_prev_close = float(quote_override.get("yesterday_close", raw_buy_price))
                buy_trade_date = clock_trade_date
            else:
                if cand_cutoff == clock_trade_date:
                    raw_buy_price = float(target_cand.market_snapshot.get("last_price", 0.0))
                    pct_chg = float(target_cand.market_snapshot.get("pct_change_1d", 0.0) or 0.0)
                    buy_prev_close = round(raw_buy_price / (1.0 + pct_chg / 100.0), 4) if raw_buy_price > 0 else raw_buy_price
                    buy_trade_date = clock_trade_date
                else:
                    klines = []
                    if self.market_adapter and self.market_adapter.market_db:
                        klines = self.market_adapter.market_db.get_daily_klines(
                            target_cand.symbol, cutoff_date=clock_trade_date, limit=2
                        )
                    if klines and str(klines[-1].get("trade_date", "")).replace("-", "")[:8] == clock_trade_date:
                        raw_buy_price = float(klines[-1]["close"])
                        buy_prev_close = float(klines[-2]["close"]) if len(klines) >= 2 else raw_buy_price
                        buy_trade_date = clock_trade_date
                    else:
                        reason = f"缺少标的 {target_cand.name}({target_cand.symbol}) 在 {clock_trade_date} 的成交行情数据，阻断买入"
                        cycle = PaperCycle(
                            schema_version=SCHEMA_VERSION,
                            cycle_id=cycle_id,
                            persona_id=persona_id,
                            mode=mode_clean,
                            request_key=request_key,
                            input_hash=input_hash,
                            scan_run_id=scan_run_id,
                            candidate_id=candidate_id,
                            status="BLOCKED",
                            action="NONE",
                            fills_count=0,
                            fills=[],
                            rejection_reason="缺少行情数据，阻断买入",
                            summary=reason,
                            account_snapshot=account.model_dump(),
                            created_at=now_iso
                        )
                        self._write_cycle_row(cur, account.account_id, cycle)
                        conn.commit()
                        return cycle

            exit_snapshot = {
                "strategy_version_id": strat.id,
                "stop_loss_pct": strat.exit_rules.stop_loss_pct if strat.exit_rules else -3.5,
                "take_profit_pct": strat.exit_rules.take_profit_pct if strat.exit_rules else 8.5,
                "entry_date": buy_trade_date
            }

            # 7. 执行买入撮合
            ok_buy, fill_buy, msg_buy = self.execute_buy(
                persona_id=persona_id,
                symbol=target_cand.symbol,
                name=target_cand.name,
                raw_price=raw_buy_price,
                yesterday_close=buy_prev_close,
                strategy_version=strat.id,
                budget=buy_budget,
                request_key=f"{request_key}_buy",
                mode=mode_clean,
                is_suspended=bool(quote_override.get("is_suspended", False)) if quote_override else False,
                exit_snapshot=exit_snapshot,
                reason=f"命中策略 {strat.id} 候选买入",
                external_conn=conn,
                trade_date_override=buy_trade_date
            )

            if ok_buy and fill_buy:
                # 记录消费标记 (绑定该 account_id, scan_run_id, symbol)
                cons_id = f"CONS_{uuid.uuid4().hex[:8]}"
                cur.execute(
                    """
                    INSERT INTO lab_consumed_candidates (
                        consumption_id, account_id, scan_run_id, symbol, consumed_at, fill_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (cons_id, account.account_id, scan_run.id, target_cand.symbol, now_iso, fill_buy.fill_id)
                )

                # 重新获取账户快照
                acc_snap = self.get_or_create_account(persona_id, mode=mode_clean, conn=conn).model_dump()
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run.id,
                    candidate_id=target_cand.symbol,
                    status="COMPLETED",
                    action="BUY",
                    fills_count=1,
                    fills=[fill_buy],
                    rejection_reason=None,
                    summary=f"按策略 {strat.id} 模拟买入候选标的 {target_cand.name}({target_cand.symbol}) {fill_buy.shares} 股，成交均价 {fill_buy.price:.2f} 元，扣减现金 {abs(fill_buy.cash_effect):.2f} 元",
                    account_snapshot=acc_snap,
                    created_at=now_iso
                )
            else:
                cycle = PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=cycle_id,
                    persona_id=persona_id,
                    mode=mode_clean,
                    request_key=request_key,
                    input_hash=input_hash,
                    scan_run_id=scan_run.id,
                    candidate_id=target_cand.symbol,
                    status="BLOCKED",
                    action="NONE",
                    fills_count=0,
                    fills=[],
                    rejection_reason=msg_buy,
                    summary=f"买入撮合未通过: {msg_buy}",
                    account_snapshot=account.model_dump(),
                    created_at=now_iso
                )

            # 故障注入测试点
            if failure_injection_hook:
                failure_injection_hook()

            self._write_cycle_row(cur, account.account_id, cycle)
            conn.commit()
            return cycle

    def _write_cycle_row(self, cur: sqlite3.Cursor, account_id: str, cycle: PaperCycle):
        """将周期结果记录写入数据库"""
        fills_json_str = json.dumps([f.model_dump() for f in cycle.fills], ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO lab_paper_cycles (
                cycle_id, account_id, persona_id, mode, request_key, input_hash,
                scan_run_id, candidate_id, status, action, fills_count, fills_json,
                rejection_reason, summary, account_snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle.cycle_id, account_id, cycle.persona_id, cycle.mode,
                cycle.request_key, cycle.input_hash, cycle.scan_run_id,
                cycle.candidate_id, cycle.status, cycle.action, cycle.fills_count,
                fills_json_str, cycle.rejection_reason, cycle.summary,
                json.dumps(cycle.account_snapshot, ensure_ascii=False), cycle.created_at
            )
        )

    def get_paper_cycle(self, cycle_id: str, persona_id: str) -> Optional[PaperCycle]:
        """按 ID 查询模拟周期结果"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM lab_paper_cycles WHERE cycle_id = ? AND persona_id = ?",
                (cycle_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            c = dict(row)
            fills_list = [LabFill(**f) for f in json.loads(c["fills_json"])]
            return PaperCycle(
                schema_version=SCHEMA_VERSION,
                cycle_id=c["cycle_id"],
                persona_id=c["persona_id"],
                mode=c["mode"],
                request_key=c["request_key"],
                input_hash=c["input_hash"],
                scan_run_id=c.get("scan_run_id"),
                candidate_id=c.get("candidate_id"),
                status=c["status"],
                action=c["action"],
                fills_count=c["fills_count"],
                fills=fills_list,
                rejection_reason=c.get("rejection_reason"),
                summary=c["summary"],
                account_snapshot=json.loads(c["account_snapshot_json"]),
                created_at=c["created_at"]
            )

    def list_paper_cycles(
        self,
        persona_id: str,
        mode: Optional[str] = None,
        limit: int = 50
    ) -> List[PaperCycle]:
        """列出指定分身的模拟周期执行历史"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            query = "SELECT * FROM lab_paper_cycles WHERE persona_id = ?"
            params: List[Any] = [persona_id]
            if mode:
                query += " AND mode = ?"
                params.append(str(mode).upper())
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = cur.execute(query, params).fetchall()
            results = []
            for r in rows:
                c = dict(r)
                fills_list = [LabFill(**f) for f in json.loads(c["fills_json"])]
                results.append(PaperCycle(
                    schema_version=SCHEMA_VERSION,
                    cycle_id=c["cycle_id"],
                    persona_id=c["persona_id"],
                    mode=c["mode"],
                    request_key=c["request_key"],
                    input_hash=c["input_hash"],
                    scan_run_id=c.get("scan_run_id"),
                    candidate_id=c.get("candidate_id"),
                    status=c["status"],
                    action=c["action"],
                    fills_count=c["fills_count"],
                    fills=fills_list,
                    rejection_reason=c.get("rejection_reason"),
                    summary=c["summary"],
                    account_snapshot=json.loads(c["account_snapshot_json"]),
                    created_at=c["created_at"]
                ))
            return results

    def list_paper_fills(
        self,
        persona_id: str,
        mode: Optional[str] = None,
        limit: int = 50
    ) -> List[LabFill]:
        """查询成交流水"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            acc = self.get_or_create_account(persona_id, mode=mode or "REPLAY", conn=conn)
            query = "SELECT * FROM lab_fills WHERE account_id = ? ORDER BY trade_time DESC LIMIT ?"
            rows = cur.execute(query, (acc.account_id, limit)).fetchall()
            results = []
            for r in rows:
                f_data = dict(r)
                results.append(LabFill(
                    fill_id=f_data["fill_id"],
                    position_id=f_data.get("position_id"),
                    input_hash=f_data.get("input_hash"),
                    direction=f_data["direction"],
                    symbol=f_data["symbol"],
                    name=f_data["name"],
                    trade_time=f_data["trade_time"],
                    trade_date=f_data["trade_date"],
                    price=f_data["price"],
                    shares=f_data["shares"],
                    gross_amount=f_data["gross_amount"],
                    commission=f_data["commission"],
                    stamp_tax=f_data["stamp_tax"],
                    slippage_cost=f_data.get("slippage_cost", 0.0),
                    cash_effect=f_data["cash_effect"],
                    reason=f_data["reason"]
                ))
            return results
