# -*- coding: utf-8 -*-
"""
scripts/datasource/db.py
SQLite 历史数据库（阶段一改造新增 · 量化的地基）

终结三件旧账：
1. 旧的腾讯接口"现拉现用、从不落盘"，kline_cache / daily_snapshots 目录全空，
   错过的时点永久丢失 → 现在日线全量入库、每日收盘增量；
2. 7x24 快讯每次只留最新 10 条，历史即焚 → 现在永久存档；
3. 选股/研判信号无留痕，无法事后复盘模型质量 → 现在 signals 流水留痕。

单文件库 data/market/market.db 也是阶段二上云迁移的最小单位。

被 daemon 每日调用（sync_daily_klines），回测模块直接读表。
"""

import os
import sys
import json
import sqlite3
import datetime
import hashlib

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import runtime_config as rc
from ai_quant.governance_policy import GovernanceSafetyPolicy

_GOVERNANCE_POLICY = GovernanceSafetyPolicy()

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_kline (
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL,
    source TEXT DEFAULT 'akshare',
    updated_at TEXT,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(trade_date);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE,
    publish_time TEXT,
    title TEXT,
    content TEXT,
    category TEXT,
    source TEXT,
    url TEXT,
    subj_type TEXT,
    entity TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(publish_time);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    blogger_id TEXT,
    stock_code TEXT,
    stock_name TEXT,
    action TEXT,
    score REAL,
    confidence REAL,
    reason TEXT,
    snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect():
    os.makedirs(os.path.dirname(rc.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(rc.DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn


def init_db(writer_id='db_init'):
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'daily_kline', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")
    with _connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- 日线
def upsert_klines(rows, code, source='akshare', writer_id='kline_writer'):
    """rows: list of dict(open/high/low/close/volume/amount/pct_chg/trade_date)"""
    # 运行时写入授权检查 (G0C-R1-P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'daily_kline', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    now = rc.now_str()
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO daily_kline(code, trade_date, open, high, low, close,
                                       volume, amount, pct_chg, source, updated_at)
               VALUES (:code, :trade_date, :open, :high, :low, :close,
                       :volume, :amount, :pct_chg, :source, :updated_at)
               ON CONFLICT(code, trade_date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                 pct_chg=excluded.pct_chg, source=excluded.source, updated_at=excluded.updated_at""",
            [{**r, 'code': code, 'source': source, 'updated_at': now} for r in rows])


def get_kline(code, start=None, end=None, include_source=False):
    columns = 'trade_date, open, high, low, close, volume, amount, pct_chg'
    if include_source:
        columns += ', source, updated_at'
    q = (f'SELECT {columns} '
         'FROM daily_kline WHERE code=?')
    args = [code]
    if start:
        q += ' AND trade_date >= ?'
        args.append(start)
    if end:
        q += ' AND trade_date <= ?'
        args.append(end)
    q += ' ORDER BY trade_date'
    with _connect() as conn:
        return conn.execute(q, args).fetchall()


def kline_coverage():
    """返回 (股票数, 最早日期, 最新日期)，用于健康面板与回测前检查。"""
    with _connect() as conn:
        n = conn.execute('SELECT COUNT(DISTINCT code) FROM daily_kline').fetchone()[0]
        lo = conn.execute('SELECT MIN(trade_date) FROM daily_kline').fetchone()[0]
        hi = conn.execute('SELECT MAX(trade_date) FROM daily_kline').fetchone()[0]
    return n, lo, hi


# ---------------------------------------------------------------- 新闻
def insert_news(items, writer_id='news_persister'):
    """items: list of dict(title/content/time/source/url/category/subj_type/entity)
    返回新插入条数。dedup 用 title+time 哈希，天然幂等可重复跑。"""
    # 运行时写授权校验 (P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'news', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    now = rc.now_str()
    inserted = 0
    with _connect() as conn:
        for it in items:
            key = hashlib.sha256(
                f"{it.get('time','')}|{it.get('title','')}".encode('utf-8')).hexdigest()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO news(dedup_key, publish_time, title, content,
                       category, source, url, subj_type, entity, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (key, it.get('time', ''), it.get('title', ''), it.get('content', ''),
                     it.get('category', ''), it.get('source', ''), it.get('url', ''),
                     it.get('subj_type', ''), it.get('entity', ''), now))
                inserted += 1
            except Exception:
                continue
    return len(items), inserted


def recent_news(limit=50):
    with _connect() as conn:
        rows = conn.execute(
            'SELECT publish_time, title, content, category, source, url '
            'FROM news ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [{'publish_time': r[0], 'title': r[1], 'content': r[2], 'category': r[3],
             'source': r[4], 'url': r[5]} for r in rows]


# ---------------------------------------------------------------- 信号流水
def insert_signal(blogger_id, action, stock_code='', stock_name='', score=None,
                  confidence=None, reason='', snapshot=None, writer_id='signal_logger'):
    # 运行时写入授权检查 (G0C-R1-P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'signals', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with _connect() as conn:
        conn.execute(
            'INSERT INTO signals(ts, blogger_id, stock_code, stock_name, action, '
            'score, confidence, reason, snapshot_json) VALUES (?,?,?,?,?,?,?,?,?)',
            (rc.now_str(), blogger_id, stock_code, stock_name, action, score,
             confidence, reason, json.dumps(snapshot or {}, ensure_ascii=False)))


# ---------------------------------------------------------------- LLM 事件因子（舆情 alpha）
# 单条新闻/事件的 LLM 结构化输出：
#   event_type    事件类型(EARN_SURPRISE/POLICY/MACRO/SECTOR/...)
#   primary_symbol 主要受益/受损标的(6位代码)
#   impact_side   利好/利空/中性
#   sentiment     情绪分 [-1, +1]
#   half_life_h   半衰期(小时)：1 急/3 当日/24 日级/72+ 周级
#   confidence    LLM 自评确定性 1-99
#   decay_lambda  指数衰减系数(由半衰期推导：λ = ln2 / half_life_h)
# 写入后供"舆情因子(指数衰减加权)"与"事件日历"消费

def upsert_event_factor(dedup_key, payload, source='llm', model='', writer_id='event_factor_writer'):
    """
    payload 必备键：news_id(可选), title, event_type, primary_symbol,
                   impact_side, sentiment, half_life_h, confidence
    可选键（阶段五A₀）：binding_strength, attention_count, sentiment_spread
    返回写入或覆盖后的sqlite rowid
    """
    # 运行时写授权校验 (P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'event_factor', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    from math import log
    hl = max(1.0, float(payload.get('half_life_h') or 24.0))
    lam = log(2.0) / hl
    now = rc.now_str()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO event_factor(
                  dedup_key, source, model, event_type, primary_symbol,
                  impact_side, sentiment, half_life_h, decay_lambda,
                  confidence, title, raw_json, published_at, updated_at,
                  binding_strength, attention_count, sentiment_spread
              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(dedup_key) DO UPDATE SET
                  source=excluded.source, model=excluded.model,
                  event_type=excluded.event_type,
                  primary_symbol=excluded.primary_symbol,
                  impact_side=excluded.impact_side,
                  sentiment=excluded.sentiment,
                  half_life_h=excluded.half_life_h,
                  decay_lambda=excluded.decay_lambda,
                  confidence=excluded.confidence,
                  title=excluded.title,
                  raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at,
                  binding_strength=COALESCE(excluded.binding_strength, event_factor.binding_strength),
                  attention_count=COALESCE(excluded.attention_count, event_factor.attention_count),
                  sentiment_spread=COALESCE(excluded.sentiment_spread, event_factor.sentiment_spread)""",
            (dedup_key, source, model,
             payload.get('event_type') or 'OTHER',
             payload.get('primary_symbol') or '',
             payload.get('impact_side') or 'NEUTRAL',
             float(payload.get('sentiment') or 0.0),
             hl, lam,
             int(payload.get('confidence') or 0),
             (payload.get('title') or '')[:200],
             json.dumps(payload, ensure_ascii=False),
             payload.get('published_at') or now, now,
             payload.get('binding_strength'),
             payload.get('attention_count'),
             payload.get('sentiment_spread')))
        row = conn.execute(
            "SELECT id FROM event_factor WHERE dedup_key=?", (dedup_key,)).fetchone()
        return row[0] if row else None

def get_factors(symbol=None, event_type=None, since_hours=72, limit=200):
    """按时间窗口取事件因子（衰减权重将在调用方计算）。"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(hours=since_hours)).strftime('%Y-%m-%d %H:%M:%S')
    sql = ("SELECT id, event_type, primary_symbol, impact_side, sentiment, "
           "half_life_h, decay_lambda, confidence, title, published_at, "
           "binding_strength, attention_count, sentiment_spread "
           "FROM event_factor WHERE updated_at >= ?")
    args = [cutoff]
    if symbol:
        sql += " AND primary_symbol = ?"
        args.append(str(symbol).zfill(6) if str(symbol).isdigit() else symbol)
    if event_type:
        sql += " AND event_type = ?"
        args.append(event_type)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{
        'id': r[0], 'event_type': r[1], 'primary_symbol': r[2],
        'impact_side': r[3], 'sentiment': r[4], 'half_life_h': r[5],
        'decay_lambda': r[6], 'confidence': r[7], 'title': r[8],
        'published_at': r[9],
        'binding_strength': r[10] if len(r) > 10 else None,
        'attention_count': r[11] if len(r) > 11 else None,
        'sentiment_spread': r[12] if len(r) > 12 else None,
    } for r in rows]


def add_event_factor_columns(writer_id='db_init'):
    """一次性建表（首次调用时），含 event_factor 表与必要索引。"""
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'event_factor', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_factor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT UNIQUE NOT NULL,
            source TEXT DEFAULT 'llm',
            model TEXT,
            event_type TEXT,
            primary_symbol TEXT,
            impact_side TEXT,
            sentiment REAL,
            half_life_h REAL,
            decay_lambda REAL,
            confidence INTEGER,
            title TEXT,
            raw_json TEXT,
            published_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_factor_symbol ON event_factor(primary_symbol, updated_at);
        CREATE INDEX IF NOT EXISTS idx_factor_type ON event_factor(event_type, updated_at);
        """)
        # 阶段五A0：原料三列（幂等追加，老库自动升级）
        for _col in ('binding_strength INTEGER', 'attention_count INTEGER', 'sentiment_spread REAL'):
            try:
                is_m_auth, m_msg = _GOVERNANCE_POLICY.validate_db_write('schema_migration', 'event_factor', 'ALTER')
                if not is_m_auth:
                    _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', m_msg)
                    raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {m_msg}")
                conn.execute(f'ALTER TABLE event_factor ADD COLUMN {_col}')
            except Exception as e:
                if isinstance(e, PermissionError):
                    raise
                pass  # 列已存在


# ---------------------------------------------------------------- 元信息
def set_meta(key, value, writer_id='meta_syncer'):
    # 运行时写授权校验 (P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'sync_meta', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with _connect() as conn:
        conn.execute('INSERT INTO sync_meta(key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def get_meta(key, default=''):
    with _connect() as conn:
        row = conn.execute('SELECT value FROM sync_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


if __name__ == '__main__':
    init_db()
    print('数据库初始化完成:', rc.DB_PATH)
    print('覆盖情况(股票数/最早/最新):', kline_coverage())


# -------------- 阶段五·任务E：决策快照留痕 --------------

def init_decision_trace(writer_id='db_init'):
    """决策快照表（下单事务内同步写入；事后补写=归因失真，禁止）。"""
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'decision_trace', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, trade_date TEXT, blogger_id TEXT, symbol TEXT, name TEXT,
            action TEXT, contract_score REAL, gate_verdict TEXT,
            gate_adjustments TEXT, factor_snapshot TEXT, regime TEXT,
            priced_in REAL, debate_penalty REAL, price REAL, ratio REAL,
            reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trace_symbol ON decision_trace(symbol, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trace_blogger ON decision_trace(blogger_id, trade_date);
        """)
        # 阶段五·任务E：平仓归因三列（幂等追加）
        for _col in ('closed_ts TEXT', 'pnl_pct REAL', 'attribution TEXT'):
            try:
                is_m_auth, m_msg = _GOVERNANCE_POLICY.validate_db_write('schema_migration', 'decision_trace', 'ALTER')
                if not is_m_auth:
                    _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', m_msg)
                    raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {m_msg}")
                conn.execute(f'ALTER TABLE decision_trace ADD COLUMN {_col}')
            except Exception as e:
                if isinstance(e, PermissionError):
                    raise
                pass  # 列已存在


def insert_decision_trace(row, writer_id='decision_trace_logger'):
    """row: dict，键见建表列。返回 rowid。"""
    # 运行时写授权校验 (P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'decision_trace', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    init_decision_trace()
    cols = ['ts', 'trade_date', 'blogger_id', 'symbol', 'name', 'action',
            'contract_score', 'gate_verdict', 'gate_adjustments',
            'factor_snapshot', 'regime', 'priced_in', 'debate_penalty',
            'price', 'ratio', 'reason']
    vals = [row.get(c) for c in cols]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO decision_trace({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)
        return cur.lastrowid


# -------------- 阶段五·任务E：平仓归因查询 --------------

def get_open_trace(blogger_id, symbol):
    """取该博主该标的最近一笔未平仓的 BUY 快照；无则 None。"""
    init_decision_trace()
    sym6 = ''.join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, factor_snapshot, contract_score, gate_verdict, price "
            "FROM decision_trace WHERE blogger_id=? AND symbol LIKE ? AND action='BUY' "
            "AND (closed_ts IS NULL OR closed_ts='') ORDER BY id DESC LIMIT 1",
            (blogger_id, f'%{sym6}')).fetchone()
    if not rows:
        return None
    return {'id': rows[0], 'ts': rows[1], 'factor_snapshot': rows[2],
            'contract_score': rows[3], 'gate_verdict': rows[4], 'price': rows[5]}


def link_close_trace(blogger_id, symbol, pnl_pct, attribution, close_ts, writer_id='decision_trace_logger'):
    """把平仓信息回写到最近一笔未平仓 BUY 快照。返回关联的 trace id（无则 None）。"""
    # 运行时写授权校验 (G0C-FINAL-FIX)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'decision_trace', 'UPDATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    tr = get_open_trace(blogger_id, symbol)
    if not tr:
        return None
    with _connect() as conn:
        conn.execute(
            "UPDATE decision_trace SET closed_ts=?, pnl_pct=?, attribution=? WHERE id=?",
            (close_ts, round(float(pnl_pct), 3), attribution, tr['id']))
    return tr['id']


def recent_traces(limit=15):
    """最近决策快照（前端面板用）。"""
    init_decision_trace()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, blogger_id, symbol, name, action, gate_verdict, "
            "gate_adjustments, attribution, pnl_pct FROM decision_trace "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [{'ts': r[0], 'blogger_id': r[1], 'symbol': r[2], 'name': r[3],
             'action': r[4], 'verdict': r[5], 'adjustments': r[6],
             'attribution': r[7], 'pnl_pct': r[8]} for r in rows]
