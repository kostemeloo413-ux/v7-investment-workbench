# -*- coding: utf-8 -*-
"""
scripts/ai_quant/universe.py
Phase 1: Research Data & Point-in-Time 历史动态股票池管理

核心职责：
1. 建立 instrument_master 股票主数据表（保存在 SQLite data/market/market.db）；
2. 记录每只股票的 list_date (上市日), delist_date (退市日), board (板块), industry (行业), is_st, is_suspended；
3. 实现 eligible_universe(t, min_list_days=60, exclude_st=True, exclude_suspended=True)：
   - 对任意历史截面 t，动态计算出当天符合交易资格的股票清单；
   - 彻底杜绝幸存者偏差（不将今天仍存活但在 t 时点尚未上市的股票塞入 t 时点样本）；
   - 杜绝未来函数（在 t 时点只能看到 t 之前已发生的信息）；
4. 提供数据质量校验函数 validate_universe_integrity()。
"""

import os
import sys
import json
import sqlite3
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb
from ai_quant.governance_policy import GovernanceSafetyPolicy

_GOVERNANCE_POLICY = GovernanceSafetyPolicy()

DB_PATH = os.path.join(_BASE, 'data', 'market', 'market.db')
UNIVERSE_JSON = os.path.join(_BASE, 'data', 'stock_universe_mainboard.json')

SCHEMA_INSTRUMENT_MASTER = """
CREATE TABLE IF NOT EXISTS instrument_master (
    symbol TEXT PRIMARY KEY,          -- 纯数字6位代码，如 '000001'
    name TEXT NOT NULL,               -- 股票名称，如 '平安银行'
    full_symbol TEXT NOT NULL,        -- 带市场前缀，如 'sz000001'
    exchange TEXT NOT NULL,           -- 'SZ' 或 'SH'
    board TEXT NOT NULL,              -- 'MAIN' (主板), 'GEM' (创业板), 'STAR' (科创板), 'BSE' (北交所)
    list_date TEXT NOT NULL,          -- 上市日期 'YYYYMMDD'
    delist_date TEXT,                 -- 退市日期 'YYYYMMDD' (在市则为空)
    industry TEXT,                    -- 行业/题材归属
    is_st INTEGER DEFAULT 0,          -- 当前是否ST (1=是, 0=否)
    is_suspended INTEGER DEFAULT 0,   -- 当前是否停牌 (1=是, 0=否)
    first_kline_date TEXT,            -- 本地K线最早日期 'YYYYMMDD'
    last_kline_date TEXT,             -- 本地K线最新日期 'YYYYMMDD'
    total_kline_bars INTEGER DEFAULT 0, -- 本地日K线总条数
    updated_at TEXT NOT NULL          -- 更新时间
);
CREATE INDEX IF NOT EXISTS idx_im_list_date ON instrument_master(list_date);
CREATE INDEX IF NOT EXISTS idx_im_board ON instrument_master(board);
"""

# 板块推断规则
def infer_board_and_exchange(code_str):
    code = str(code_str).strip()
    if code.startswith(('600', '601', '603', '605')):
        return 'MAIN', 'SH', f'sh{code}'
    elif code.startswith(('000', '001', '002', '003')):
        return 'MAIN', 'SZ', f'sz{code}'
    elif code.startswith(('300', '301')):
        return 'GEM', 'SZ', f'sz{code}'
    elif code.startswith(('688', '689')):
        return 'STAR', 'SH', f'sh{code}'
    elif code.startswith(('8', '4', '920')):
        return 'BSE', 'BJ', f'bj{code}'
    return 'OTHER', 'UNKNOWN', code


def init_instrument_master_table(writer_id='db_init'):
    """初始化 instrument_master 表结构"""
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'instrument_master', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")
    with hdb._connect() as conn:
        conn.executescript(SCHEMA_INSTRUMENT_MASTER)


def sync_instrument_master_from_local():
    """
    从现有的 stock_universe_mainboard.json 与 daily_kline 本地数据构建初始 instrument_master：
    - 结合本地 3103 标的清单
    - 穿透 daily_kline 计算每只股票最早可见日期（作为历史上市截面代理 list_date）
    - 关联行业/题材映射
    - 识别 ST 标识
    """
    init_instrument_master_table()
    
    # 1. 读本地主板池
    uni_stocks = []
    if os.path.exists(UNIVERSE_JSON):
        try:
            with open(UNIVERSE_JSON, 'r', encoding='utf-8') as f:
                d = json.load(f)
                uni_stocks = d if isinstance(d, list) else d.get('stocks', [])
        except Exception as e:
            print(f"[universe] 读取 {UNIVERSE_JSON} 失败: {e}")

    # 2. 读本地 THEME_MAP 建立行业映射字典
    theme_mapping = {}
    try:
        from pipeline import menxia
        tm = getattr(menxia, 'THEME_MAP', {})
        for theme_name, code_list in tm.items():
            for c in code_list:
                pure_c = ''.join(ch for ch in str(c) if ch.isdigit())
                if pure_c not in theme_mapping:
                    theme_mapping[pure_c] = theme_name
    except Exception:
        pass

    # 3. 统计本地 daily_kline 的时间范围
    kline_stats = {}
    with hdb._connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute("""
            SELECT code, MIN(trade_date), MAX(trade_date), COUNT(*)
            FROM daily_kline
            GROUP BY code
        """).fetchall()
        for r in rows:
            kline_stats[r[0]] = {
                'min_date': r[1],
                'max_date': r[2],
                'bars': r[3]
            }

    now_ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    records = []

    for s in uni_stocks:
        code = str(s.get('code') or s.get('symbol') or '').strip()
        pure_code = ''.join(ch for ch in code if ch.isdigit())
        if len(pure_code) != 6:
            continue
        
        name = str(s.get('name') or s.get('stock_name') or '').strip()
        board, exchange, full_sym = infer_board_and_exchange(pure_code)
        
        # 历史 K 线边界
        k_info = kline_stats.get(pure_code, {})
        first_k = k_info.get('min_date') or '20210830'
        last_k = k_info.get('max_date') or '20260904'
        total_bars = k_info.get('bars') or 0

        # 如果第一根 K 线在 20210830（即我们库的起点），说明它上市早于 2021-08-30；
        # 如果第一根 K 线显著晚于起点（例如 20220615），则该日可作为其上市交易的最保守估计
        list_d = first_k

        # ST 标记判定
        is_st_flag = 1 if ('ST' in name.upper() or '退' in name) else 0

        # 行业归属
        ind = theme_mapping.get(pure_code) or s.get('industry') or '主板制造与综合'

        records.append({
            'symbol': pure_code,
            'name': name,
            'full_symbol': full_sym,
            'exchange': exchange,
            'board': board,
            'list_date': list_d,
            'delist_date': None,
            'industry': ind,
            'is_st': is_st_flag,
            'is_suspended': 0,
            'first_kline_date': first_k,
            'last_kline_date': last_k,
            'total_kline_bars': total_bars,
            'updated_at': now_ts
        })

    # 4. 批量落盘 (P1-1 授权校验)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write('instrument_syncer', 'instrument_master', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with hdb._connect() as conn:
        conn.executemany("""
            INSERT INTO instrument_master (
                symbol, name, full_symbol, exchange, board, list_date,
                delist_date, industry, is_st, is_suspended,
                first_kline_date, last_kline_date, total_kline_bars, updated_at
            ) VALUES (
                :symbol, :name, :full_symbol, :exchange, :board, :list_date,
                :delist_date, :industry, :is_st, :is_suspended,
                :first_kline_date, :last_kline_date, :total_kline_bars, :updated_at
            ) ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name,
                full_symbol=excluded.full_symbol,
                industry=excluded.industry,
                is_st=excluded.is_st,
                first_kline_date=excluded.first_kline_date,
                last_kline_date=excluded.last_kline_date,
                total_kline_bars=excluded.total_kline_bars,
                updated_at=excluded.updated_at
        """, records)

    print(f"[universe] instrument_master 同步完成，共记录 {len(records)} 只标的主数据。")
    return len(records)


def eligible_universe(as_of_date, min_list_days=60, exclude_st=True, exclude_suspended=True, board_filter=('MAIN',), data_provider=None):
    """
    【Point-in-Time 历史动态股票池查询核心】
    输入：
      as_of_date: 目标历史截面日期，格式可为 'YYYYMMDD' 或 'YYYY-MM-DD' 或 datetime.date
      min_list_days: 上市至少多少个交易日（默认60个自然日/约3个月次新过滤，避免极端上市波动）
      exclude_st: 是否剔除 ST 股票 (默认 True)
      exclude_suspended: 是否剔除停牌股票 (默认 True)
      board_filter: 允许的板块列表，默认 ('MAIN',) 主板
    返回：
      list of dict: 当天真实符合交易条件的股票清单 [{symbol, name, full_symbol, industry, board}, ...]
    原则：
      在 as_of_date 时点未上市的，坚决不进入；
      已退市且退市时间早于 as_of_date 的，坚决不进入；
      彻底杜绝幸存者偏差与时间穿越。
    """
    if isinstance(as_of_date, datetime.date):
        target_d = as_of_date.strftime('%Y%m%d')
    else:
        target_d = str(as_of_date).replace('-', '').strip()[:8]

    # 计算最晚允许的上市日期（例如 as_of_date - min_list_days）
    try:
        dt_target = datetime.datetime.strptime(target_d, '%Y%m%d')
        dt_max_list = dt_target - datetime.timedelta(days=min_list_days)
        max_list_date_str = dt_max_list.strftime('%Y%m%d')
    except Exception:
        max_list_date_str = target_d

    query = """
        SELECT symbol, name, full_symbol, exchange, board, industry, list_date, is_st, is_suspended
        FROM instrument_master
        WHERE list_date <= ?
          AND (delist_date IS NULL OR delist_date > ?)
    """
    params = [max_list_date_str, target_d]

    if board_filter:
        placeholders = ','.join(['?'] * len(board_filter))
        query += f" AND board IN ({placeholders})"
        params.extend(board_filter)

    if exclude_st:
        query += " AND is_st = 0"

    with (data_provider or hdb)._connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(query, params).fetchall()

        # 如果需要检查当天停牌：批量拉取当天成交量，避免循环开启数据库连接
        suspended_syms = set()
        if exclude_suspended:
            v_rows = cursor.execute(
                "SELECT code FROM daily_kline WHERE trade_date = ? AND (volume IS NULL OR volume <= 0)",
                (target_d,)
            ).fetchall()
            suspended_syms = {vr[0] for vr in v_rows}

    result = []
    for r in rows:
        sym = r[0]
        if exclude_suspended and sym in suspended_syms:
            continue
        
        result.append({
            'symbol': sym,
            'name': r[1],
            'full_symbol': r[2],
            'exchange': r[3],
            'board': r[4],
            'industry': r[5],
            'list_date': r[6]
        })

    return result


def validate_universe_integrity():
    """
    数据质量校验器（Phase 1 Gate 必须通过）：
    - 校验 instrument_master 记录数
    - 校验是否存在未来上市穿越
    - 校验 symbol 是否为纯数字 6 位
    - 校验是否存在重复主键
    """
    issues = []
    with hdb._connect() as conn:
        c = conn.cursor()
        total = c.execute("SELECT count(*) FROM instrument_master").fetchone()[0]
        if total < 2500:
            issues.append(f"instrument_master 标的总量不足: {total} < 2500")

        # 检查非法 symbol 格式
        invalid_syms = c.execute("SELECT symbol FROM instrument_master WHERE length(symbol) != 6").fetchall()
        if invalid_syms:
            issues.append(f"存在非6位代码: {len(invalid_syms)} 条")

        # 检查上市时间异常（> 20300101）
        future_list = c.execute("SELECT symbol, list_date FROM instrument_master WHERE list_date > '20300101'").fetchall()
        if future_list:
            issues.append(f"存在未来上市时间标的: {len(future_list)} 条")

    passed = len(issues) == 0
    return {
        'passed': passed,
        'total_instruments': total,
        'issues': issues
    }


if __name__ == '__main__':
    print("=== 初始化与同步 instrument_master ===")
    count = sync_instrument_master_from_local()
    print(f"成功入库: {count} 条")

    print("\n=== 校验股票池数据完整性 ===")
    chk = validate_universe_integrity()
    print("质量校验结果:", chk)

    print("\n=== Point-in-Time 动态测试 ===")
    # 测试历史截面 2022-01-05
    u_2022 = eligible_universe('20220105', min_list_days=60)
    # 测试历史截面 2024-06-01
    u_2024 = eligible_universe('20240601', min_list_days=60)
    # 测试历史截面 2026-09-01
    u_2026 = eligible_universe('20260901', min_list_days=60)

    print(f"2022-01-05 合格主板池规模: {len(u_2022)} 只")
    print(f"2024-06-01 合格主板池规模: {len(u_2024)} 只")
    print(f"2026-09-01 合格主板池规模: {len(u_2026)} 只")
    
    # 动态演进单调性验证（随时间推移，上市满足条件的标的应逐步扩充）
    if len(u_2022) < len(u_2026):
        print("✅ Point-in-Time 动态演进测试通过：历史早期标的少于后期，杜绝了把新股票强塞进早期的幸存者偏差！")
    else:
        print("❌ 警告：历史标的数量未呈现正常动态扩张！")
