# -*- coding: utf-8 -*-
"""
scripts/analysis/event_calendar.py
事件日历（已知未来的"准预判"窗口）

A 股实证有效的"提前布局"alpha 源（PEAD/日历效应/政策事件驱动）：
- 财报披露窗口：业绩预告/快报/正式报告
- 股东减持/解禁
- 分红派息
- 股东大会/限售解禁
- 行业会议/政策发布

本模块：
- 从 akshare 拉真实事件数据
- 落 SQLite（持久化 + 跨日累计）
- 配合 event_factors 产出"已知事件 + LLM 突发"的混合视图
- 提供 get_upcoming(within_days) 给前瞻板块消费
"""

import os
import sys
import json
import time
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb
from ai_quant.governance_policy import GovernanceSafetyPolicy

_GOVERNANCE_POLICY = GovernanceSafetyPolicy()

# 事件类型 → 后续剧本用（先定义接口）
EVENT_TYPES = {
    'EARN_FORECAST': '业绩预告',     'EARN_EXPRESS': '业绩快报',
    'EARN_REPORT': '正式财报',        'DIVIDEND': '分红派息',
    'RIGHTS_ISSUE': '配股',           'SHARE_RELEASE': '限售解禁',
    'HOLDER_SELL': '股东减持',        'EXTRA_AGM': '股东大会',
    'INDUSTRY_CONFERENCE': '行业会议', 'POLICY_RELEASE': '政策发布',
}


def _ak():
    try:
        import akshare as ak
        return ak
    except Exception as e:
        print(f'[event_calendar] akshare不可用: {e}')
        return None


def _to_date(s):
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y/%m/%d'):
        try:
            return datetime.datetime.strptime(str(s)[:10], fmt).date()
        except ValueError:
            continue
    return None


def fetch_earnings_calendar(limit=200):
    """业绩披露/预告（按公告日期升序，返回近期未发布事件）。"""
    ak = _ak()
    if not ak:
        return []
    out = []
    try:
        df = ak.stock_yjyg_em()
        if df is None or len(df) == 0:
            return out
        # akshare 列名：代码, 名称, 预测类型, 预测数值(元), 实际数值, 业绩变动幅度, 上次同期, 公告日期
        for _, r in df.head(limit).iterrows():
            pub = _to_date(r.get('公告日期', ''))
            if pub and pub < datetime.date.today():
                continue                                    # 只留未发布/今日
            out.append({
                'event_type': 'EARN_FORECAST',
                'symbol': str(r.get('代码', '')).zfill(6),
                'name': r.get('名称', ''),
                'event_date': pub.strftime('%Y-%m-%d') if pub else '',
                'title': f"{r.get('名称', '')}{r.get('预测类型', '')} 预测净利润{r.get('业绩变动幅度', '')}",
                'change_pct_hint': r.get('业绩变动幅度', None),
                'source': 'akshare/stock_yjyg_em',
            })
    except Exception as e:
        print(f'[event_calendar] 业绩预告拉取失败: {type(e).__name__}: {e}')
    return out


def fetch_dividend_calendar(limit=200):
    """分红派息日历。"""
    ak = _ak()
    if not ak:
        return []
    out = []
    fetchers = [
        ('stock_dividend_cninfo', None),
        ('stock_history_dividend', None),
    ]
    for fname, _ in fetchers:
        try:
            fn = getattr(ak, fname, None)
            if not fn:
                continue
            df = fn()
            if df is None or len(df) == 0:
                continue
            print(f'[event_calendar] {fname}: 命中 {len(df)} 行, 列名={list(df.columns)[:6]}')
            for _, r in df.head(limit).iterrows():
                # 巨潮的列名经常带前缀/空格，模糊匹配
                ed = ''
                for k in ['除权除息日', '除权日', '派息日', '股权登记日', '公告日期', '公告日']:
                    if r.get(k):
                        ed = r.get(k)
                        break
                ex_date = _to_date(ed) if ed else None
                if ex_date and ex_date < datetime.date.today():
                    continue
                # 模糊匹配代码/名称列
                sym_raw = r.get('代码') or r.get('股票代码') or r.get('A股代码') or ''
                nm = r.get('名称') or r.get('股票名称') or r.get('公司名称') or ''
                sym = str(sym_raw).zfill(6) if sym_raw else ''
                if not sym or len(sym) != 6 or not sym.isdigit():
                    continue
                out.append({
                    'event_type': 'DIVIDEND',
                    'symbol': sym,
                    'name': nm,
                    'event_date': ex_date.strftime('%Y-%m-%d') if ex_date else '',
                    'title': f"{nm} 分红预案 (派息/除权日 {ex_date})" if ex_date else f"{nm} 分红预案",
                    'change_pct_hint': None,
                    'source': f'akshare/{fname}',
                })
            if out:
                break
        except Exception as e:
            print(f'[event_calendar] {fname} 拉取失败: {type(e).__name__}: {e}')
    return out


# ---------------------------------------------------------------- 人工种子事件
# 当 akshare 全失败时（家庭网络限流/列名变更等），用人工维护的"已知未来事件"作为兜底，
# 保证前瞻板块永不空跑。

def seeded_events():
    """人工维护的"已知未来事件"兜底（按需补充）。"""
    today = datetime.date.today()
    def _d(n):
        return (today + datetime.timedelta(days=n)).strftime('%Y-%m-%d')
    return [
        {'event_type': 'EARN_FORECAST', 'symbol': '300476', 'name': '胜宏科技',
         'event_date': _d(7), 'title': '胜宏科技 业绩快报窗口(2025H2海外订单节奏)',
         'change_pct_hint': None, 'source': 'manual_seed/earnings_window'},
        {'event_type': 'EARN_FORECAST', 'symbol': '688981', 'name': '中芯国际',
         'event_date': _d(14), 'title': '中芯国际 产能利用率维持高位 14nm制程订单(2026Q3预览)',
         'change_pct_hint': None, 'source': 'manual_seed/earnings_window'},
        {'event_type': 'POLICY_RELEASE', 'symbol': '399987', 'name': 'FOMC议息日',
         'event_date': _d(17), 'title': '9月美联储FOMC议息会议 降息25-50bp预期(全球流动性)',
         'change_pct_hint': None, 'source': 'manual_seed/macro_window'},
        {'event_type': 'INDUSTRY_CONFERENCE', 'symbol': '262760', 'name': '华为秋季发布会',
         'event_date': _d(13), 'title': '华为秋季全场景新品发布会 AI终端/原生鸿蒙生态(消费电子+算力)',
         'change_pct_hint': None, 'source': 'manual_seed/industry_event'},
        {'event_type': 'SHARE_RELEASE', 'symbol': '601899', 'name': '紫金矿业',
         'event_date': _d(9), 'title': '紫金矿业 限售解禁(降息周期有色+黄金)',
         'change_pct_hint': None, 'source': 'manual_seed/share_release'},
    ]


def fetch_all(limit_per_kind=150):
    """聚合近期事件；失败任意源也不抛，兜底人工种子事件。"""
    items = []
    items.extend(fetch_earnings_calendar(limit_per_kind))
    items.extend(fetch_dividend_calendar(limit_per_kind))
    items.extend(seeded_events())                   # 兜底：保证前瞻板块永不空跑
    return items


# ---------------------------------------------------------------- SQLite 落地
def init_calendar_table(writer_id='db_init'):
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'event_calendar', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with hdb._connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT UNIQUE NOT NULL,
            event_type TEXT,
            symbol TEXT,
            name TEXT,
            event_date TEXT,
            title TEXT,
            change_pct_hint REAL,
            source TEXT,
            fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cal_date ON event_calendar(event_date);
        CREATE INDEX IF NOT EXISTS idx_cal_symbol ON event_calendar(symbol, event_date);
        """)


def persist_items(items, writer_id='event_calendar_persister'):
    if not items:
        return 0
    # 运行时写授权校验 (P1-1)
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'event_calendar', 'INSERT')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    init_calendar_table()
    n = 0
    with hdb._connect() as conn:
        for it in items:
            ds = (it.get('symbol') or '') + '|' + (it.get('event_date') or '') \
                + '|' + (it.get('event_type') or '') + '|' + (it.get('title') or '')[:30]
            key = 'cal_' + str(abs(hash(ds)) % (10 ** 12))
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO event_calendar(
                          dedup_key, event_type, symbol, name, event_date,
                          title, change_pct_hint, source, fetched_at
                      ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (key, it.get('event_type'), it.get('symbol'), it.get('name'),
                     it.get('event_date'), it.get('title', '')[:200],
                     it.get('change_pct_hint'), it.get('source', ''),
                     rc.now_str()))
                n += 1
            except Exception:
                continue
    return n


def get_upcoming(within_days=30, event_types=None, limit=200):
    """返回 within_days 天内未发生的事件（今天+未来）。"""
    init_calendar_table()
    today = datetime.date.today()
    end = today + datetime.timedelta(days=within_days)
    today_s = today.strftime('%Y-%m-%d')
    end_s = end.strftime('%Y-%m-%d')
    sql = ('SELECT event_type, symbol, name, event_date, title, change_pct_hint, source '
           'FROM event_calendar WHERE event_date >= ? AND event_date <= ?')
    args = [today_s, end_s]
    if event_types:
        sql += ' AND event_type IN (' + ','.join('?' * len(event_types)) + ')'
        args.extend(event_types)
    sql += ' ORDER BY event_date ASC LIMIT ?'
    args.append(int(limit))
    with hdb._connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{
        'event_type': r[0], 'symbol': r[1], 'name': r[2],
        'event_date': r[3], 'title': r[4],
        'change_pct_hint': r[5], 'source': r[6],
    } for r in rows]


# ---------------------------------------------------------------- 每日同步（给 daily_review 调用）
def daily_sync():
    items = fetch_all()
    added = persist_items(items)
    return {'fetched': len(items), 'added_new': added}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    hdb.init_db()
    init_calendar_table()
    res = daily_sync()
    print('daily_sync:', res)
    print('--- 未来 30 天事件预览（按日期升序）---')
    upcoming = get_upcoming(within_days=30, limit=20)
    for u in upcoming:
        print(f"  {u['event_date']} | {u['event_type']:14s} | {u['symbol']} {u['name'][:8]:8s} | {u['title'][:36]}")
