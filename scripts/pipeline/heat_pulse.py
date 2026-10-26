# -*- coding: utf-8 -*-
"""
scripts/pipeline/heat_pulse.py
新闻热度通道（v4.1，用户 2026-09-01）

信号时序铁律：
  - 捕获侧（领先信号，给新闻加分）：跨源同题共现 / 编辑加红 / 百度热搜形成期
  - 风控侧（滞后信号，绝不作买入理由）：东财个股人气榜 → 仅喂给卖出监控兑现提醒

每源独立 try/except；全挂时系统退回纯语义通道，绝不报错停摆。
"""
import os
import re
import json
import time
import datetime
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_DIR = os.path.join(BASE_DIR, 'state')
CACHE_PATH = os.path.join(STATE_DIR, 'heat_pulse.json')
CACHE_TTL_SEC = 1800  # 30 分钟内复用缓存

_LOCK = threading.Lock()

_BAIDU_KW = []         # 捕获侧：百度热搜词（领先）
_CROWDED = {}          # 风控侧：{code: rank}（滞后，**绝不参与新闻加分**）
_LAST_FETCH = 0        # 缓存时间戳

# ---------------- 接口调用（每源独立 try/except） ----------------
def _fetch_baidu():
    """百度股市热搜（A 股今日）→ 名称/代码列表。返回 [] 表示失败。"""
    try:
        import akshare as ak
        today = datetime.date.today().strftime('%Y%m%d')
        df = ak.stock_hot_search_baidu(symbol='A股', date=today, time='今日')
        if df is None or df.empty:
            return []
        kws = []
        for x in df['名称/代码'].tolist():
            s = str(x).strip()
            if s and s not in kws:
                kws.append(s)
        return kws[:12]
    except Exception:
        return []


def _fetch_crowded():
    """东财个股人气榜 TOP100 → {code: rank}。返回 {} 表示失败。"""
    try:
        import akshare as ak
        df = ak.stock_hot_rank_em()
        if df is None or df.empty:
            return {}
        out = {}
        for _, row in df.head(100).iterrows():
            code = str(row.get('代码') or '').strip()
            rank = int(row.get('当前排名') or 0)
            if code and rank:
                out[code] = rank
        return out
    except Exception:
        return {}


def _safe_load_cache():
    global _BAIDU_KW, _CROWDED, _LAST_FETCH
    try:
        if not os.path.exists(CACHE_PATH):
            return False
        d = json.load(open(CACHE_PATH, encoding='utf-8'))
        ts = d.get('ts', 0)
        if time.time() - ts > CACHE_TTL_SEC:
            return False
        _BAIDU_KW = d.get('baidu_kw', []) or []
        _CROWDED = d.get('crowded_stocks', {}) or {}
        _LAST_FETCH = ts
        return True
    except Exception:
        return False


def _save_cache():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump({
            'ts': _LAST_FETCH,
            'baidu_kw': _BAIDU_KW,
            'crowded_stocks': _CROWDED,
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }, open(CACHE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    except Exception:
        pass


def refresh():
    """刷新热度数据。优先读 30 分钟缓存；过期则逐源拉取，每源独立降级。"""
    global _BAIDU_KW, _CROWDED, _LAST_FETCH
    with _LOCK:
        if _safe_load_cache():
            return
        b = _fetch_baidu()
        c = _fetch_crowded()
        if not b and not c:
            # 全挂：保旧值（如果有），不更新时间戳，下一轮再试
            return
        if b:
            _BAIDU_KW = b
        if c:
            _CROWDED = c
        _LAST_FETCH = time.time()
        _save_cache()


def get_snapshot():
    """返回当前内存中的热度快照（不触发网络）。"""
    return {
        'baidu_kw': list(_BAIDU_KW),
        'crowded_stocks': dict(_CROWDED),
        'ts': _LAST_FETCH,
        'fresh': (time.time() - _LAST_FETCH) < CACHE_TTL_SEC if _LAST_FETCH else False,
    }


# ---------------- 新闻匹配（只读捕获侧） ----------------
def match_news(news, attention_count=1, source_list=None):
    """根据领先信号计算 heat_tier。**个股人气榜不参与**（v4.1 防追高红线）。"""
    if _LAST_FETCH == 0:
        refresh()
    title = (news.get('title') or '').strip()
    summary = (news.get('summary') or '')[:200]
    text = f'{title} {summary}'

    # 跨源共现：attention_count 视为"被几个源推过"（news_officer 去重层自然产物）
    co_source_n = max(int(attention_count or 1), 1)
    # 如果显式传入了 source_list，则以其去重数为准
    if source_list and isinstance(source_list, (list, set)):
        co_source_n = max(co_source_n, len(set(source_list)))

    # 编辑加红/加急标记（抓取层若有保留）
    editor_hot = bool(news.get('editor_hot') or news.get('red') or news.get('urgent'))

    # 百度热搜词包含匹配
    kw_hits = []
    for kw in _BAIDU_KW:
        if kw and kw in title:
            kw_hits.append(kw)

    # 热度定档（**只认领先信号**）
    heat_tier = None
    if editor_hot or co_source_n >= 3:
        heat_tier = 'HIGH'
    elif co_source_n == 2 or kw_hits:
        heat_tier = 'MID'

    return {
        'editor_hot': editor_hot,
        'co_source_n': co_source_n,
        'kw_hits': kw_hits,
        'heat_tier': heat_tier,
        'snapshot_ts': _LAST_FETCH,
    }


# ---------------- 风控侧：人气榜兑现提醒（不进新闻） ----------------
def crowded_alerts(positions, threshold_rank=20, threshold_pct=3.0):
    """持仓标的命中人气榜 TOP{threshold_rank} 且当日涨幅 ≥ {threshold_pct}% → 兑现提醒列表。
    返回 [{'code','name','rank','pct'}, ...]；positions 为 list[{code,name,current_price,open_price}]。"""
    if _LAST_FETCH == 0:
        refresh()
    if not _CROWDED or not positions:
        return []
    out = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        code = str(pos.get('code') or pos.get('symbol') or '').strip()
        # 兼容 6 位纯数字
        pure = ''.join(ch for ch in code if ch.isdigit())
        if len(pure) != 6:
            continue
        rank = _CROWDED.get(pure) or _CROWDED.get(code)
        if not rank or rank > threshold_rank:
            continue
        # 涨幅（如有）
        cp = pos.get('current_price')
        op = pos.get('open_price') or pos.get('cost')
        try:
            pct = (float(cp) - float(op)) / float(op) * 100 if cp and op else 0
        except Exception:
            pct = 0
        if pct >= threshold_pct:
            out.append({'code': pure, 'name': pos.get('name', ''), 'rank': rank, 'pct': round(pct, 2)})
    return out


# ---------------- 模块自检 ----------------
if __name__ == '__main__':
    refresh()
    s = get_snapshot()
    print('baidu_kw:', s['baidu_kw'][:5], '... 共', len(s['baidu_kw']))
    print('crowded_stocks:', list(s['crowded_stocks'].items())[:5], '... 共', len(s['crowded_stocks']))
    print('fresh:', s['fresh'])
