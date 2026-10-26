# -*- coding: utf-8 -*-
"""
scripts/analysis/event_factors.py
舆情因子计算（中泰/方正/quant67方法论实现）

- 从 SQLite 拉取 LLM 产出的事件因子；
- 指数衰减加权（半衰期由 LLM 自评）；
- 按"标的"或"事件类型"汇总得分，供选股/回测/看板消费；
- 附简版周频 IC 占位（完整 IC 序列留给 walk-forward 跑）。

参考来源：
  - 中泰证券《基于新闻舆情数据的选股因子》  - 标签与相关度过滤
  - 方正金工《热点反应因子》               - 情绪剧烈变化做反转
  - quant67《另类数据：新闻、舆情》         - 指数衰减 w_t = e^(-λt)
  - 建投金工《因子半衰期加权》               - 跨样本衰减监控
"""

import os
import sys
import math
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb


def _parse_ts(ts):
    """接受 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD'，回退 None。"""
    if not ts:
        return None
    ts = str(ts)[:19]
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def factor_snapshot(symbols=None, since_hours=72, event_types=None,
                    min_confidence=50):
    """
    返回该时间窗内的事件因子原始快照（已按 updated_at 过滤）。
    symbols=None → 全部；否则仅 primary_symbol 命中列表的因子。
    event_types=None → 全部；否则仅命中列表。
    """
    factors = hdb.get_factors(symbol=None, since_hours=since_hours)
    out = []
    for f in factors:
        if (event_types is not None) and (f['event_type'] not in event_types):
            continue
        if (symbols is not None) and f['primary_symbol'] not in symbols:
            continue
        if (f['confidence'] or 0) < min_confidence:
            continue
        out.append(f)
    return out


def symbol_sentiment(symbol, since_hours=72, min_confidence=50,
                     now=None, lambda_floor=0.005):
    """
    对单一标的计算舆情因子聚合得分（指数衰减加权 + confidence 加权 + side 校准）。

    score = Σ ( confidence/100 * exp(-λ·Δt) * impact_sign * sentiment )
    返回 dict: {symbol, score, events: N, pos_weight, neg_weight, neutral_weight}
    备注：empty 时 score=0、events=0
    """
    sym = str(symbol).zfill(6) if str(symbol).isdigit() else str(symbol)
    now = now or datetime.datetime.now()
    rows = hdb.get_factors(symbol=sym, since_hours=since_hours)
    score = 0.0
    pos = neg = neu = 0.0
    n = 0
    for r in rows:
        if (r['confidence'] or 0) < min_confidence:
            continue
        ts = _parse_ts(r.get('published_at') or r.get('updated_at', ''))
        if not ts:
            continue
        delta_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        lam = max(lambda_floor, r.get('decay_lambda') or 0.05)
        weight = (r['confidence'] or 50) / 100.0 * math.exp(-lam * delta_h)
        side_sign = {'POSITIVE': 1.0, 'NEGATIVE': -1.0, 'NEUTRAL': 0.0}.get(
            r.get('impact_side') or 'NEUTRAL', 0.0)
        s = float(r.get('sentiment') or 0.0) * side_sign * weight
        score += s
        if side_sign > 0:
            pos += weight
        elif side_sign < 0:
            neg += weight
        else:
            neu += weight
        n += 1
    return {
        'symbol': sym, 'score': round(score, 4), 'events': n,
        'pos_weight': round(pos, 4), 'neg_weight': round(neg, 4),
        'neutral_weight': round(neu, 4),
    }


def top_symbols(top_k=20, since_hours=72, min_confidence=50):
    """
    对全市场所有出现过的 primary_symbol 聚合舆情得分，
    按 |score| 排序返回 top_k（用于看板"舆情榜单"）。
    """
    factors = factor_snapshot(since_hours=since_hours, min_confidence=min_confidence)
    syms = sorted({f['primary_symbol'] for f in factors if f.get('primary_symbol')})
    res = []
    for s in syms:
        d = symbol_sentiment(s, since_hours=since_hours, min_confidence=min_confidence)
        if d['events'] > 0:
            res.append(d)
    res.sort(key=lambda x: abs(x['score']), reverse=True)
    return res[:top_k]


def rolling_ic_series(since_days=180, forward_days=5, min_confidence=50):
    """
    【阶段三 P3】LLM 事件感知舆情因子滚动 IC / ICIR / 胜率统计检验：
    - 对标券商金工（方正/中泰）标准：IC = RankCorr(衰减情绪得分, 标的未来N日收益)；
    - 年化 ICIR = (IC均值 / IC标准差) * sqrt(52)；
    - 胜率 = IC > 0 的周期占比；
    - 严格执行【统计红线】：有效配对样本 < 30 笔时明确返回 INSUFFICIENT_SAMPLE，绝不虚构结论。
    """
    import math
    from datasource.providers import get_daily_history
    factors = factor_snapshot(since_hours=since_days * 24, min_confidence=min_confidence)
    syms = sorted({f['primary_symbol'] for f in factors if f.get('primary_symbol') and len(f['primary_symbol']) == 6})

    if not syms:
        return {
            'status': 'INSUFFICIENT_SAMPLE',
            'sample_count': 0,
            'ic_mean': None,
            'icir': None,
            'ic_win_rate': None,
            'verdict': '⛔ 样本不足 (0/30 笔)',
            'note': '数据库中暂无带明确股票代码的事件因子，随流水线运行持续积累',
            'periods': []
        }

    ic_pairs = []
    for sym in syms:
        # 优先从本地 SQLite 获取，毫秒级且零网络依赖
        rows = hdb.get_kline(sym)
        if not rows or len(rows) < 30:
            try:
                klines = get_daily_history(sym)
                if klines and len(klines) >= 30:
                    rows = [(k['trade_date'], k['open'], k['high'], k['low'], k['close'], k['volume'], k['amount'], k.get('pct_chg', 0)) for k in klines]
            except Exception:
                continue
        if not rows or len(rows) < 30:
            continue

        for f in factors:
            if f['primary_symbol'] != sym:
                continue
            ts = _parse_ts(f.get('published_at') or '')
            if not ts:
                continue
            # 找到事件公布当日及未来 N 交易日
            pub_date = ts.strftime('%Y%m%d')
            base_idx = None
            for i, r in enumerate(rows):
                if str(r[0]) >= pub_date:
                    base_idx = i
                    break
            if base_idx is None or base_idx + forward_days >= len(rows):
                continue
            # 收益率 = (T+N 收盘价 / T 收盘价) - 1
            close_base = float(rows[base_idx][4])
            close_forward = float(rows[base_idx + forward_days][4])
            if close_base <= 0:
                continue
            ret = (close_forward / close_base) - 1.0
            side_sign = {'POSITIVE': 1.0, 'NEGATIVE': -1.0, 'NEUTRAL': 0.0}.get(f.get('impact_side', 'NEUTRAL'), 0.0)
            score = float(f.get('sentiment') or 0.0) * side_sign * (float(f.get('confidence') or 50) / 100.0)
            ic_pairs.append({
                'date': ts.strftime('%Y-%m-%d'),
                'week': ts.strftime('%Y-W%W'),
                'symbol': sym,
                'score': score,
                'ret': ret
            })

    total_n = len(ic_pairs)
    if total_n < 30:
        return {
            'status': 'INSUFFICIENT_SAMPLE',
            'sample_count': total_n,
            'ic_mean': None,
            'icir': None,
            'ic_win_rate': None,
            'verdict': f'⛔ 样本不足 ({total_n}/30 笔)',
            'note': f'当前有效配对样本仅 {total_n} 笔（未达最低 30 笔统计门槛），符合诚实协议，不输出有效性结论',
            'periods': []
        }

    # 按周计算周期 IC
    by_week = {}
    for p in ic_pairs:
        by_week.setdefault(p['week'], []).append((p['score'], p['ret']))

    weekly_ics = []
    for wk, pairs in by_week.items():
        if len(pairs) < 2:
            continue
        xs = [x[0] for x in pairs]
        ys = [x[1] for x in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var_x = sum((x - mx) ** 2 for x in xs)
        var_y = sum((y - my) ** 2 for y in ys)
        if var_x == 0 or var_y == 0:
            continue
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        ic = cov / math.sqrt(var_x * var_y)
        weekly_ics.append({'week': wk, 'ic': ic, 'count': len(pairs)})

    if not weekly_ics:
        return {
            'status': 'INSUFFICIENT_SAMPLE',
            'sample_count': total_n,
            'ic_mean': None,
            'icir': None,
            'ic_win_rate': None,
            'verdict': f'🟡 数据离散度不足 ({total_n} 笔)',
            'note': '周度截面方差不足以计算稳定 IC，待因子样本扩充',
            'periods': []
        }

    ic_vals = [w['ic'] for w in weekly_ics]
    mean_ic = sum(ic_vals) / len(ic_vals)
    std_ic = math.sqrt(sum((x - mean_ic) ** 2 for x in ic_vals) / len(ic_vals)) if len(ic_vals) > 1 else 0.001
    icir = (mean_ic / std_ic) * math.sqrt(52) if std_ic > 0 else 0.0
    win_rate = (sum(1 for x in ic_vals if x > 0) / len(ic_vals)) * 100.0

    verdict = '✅ 因子显著有效' if (mean_ic >= 0.03 and icir >= 1.5 and win_rate >= 60) else ('🟡 弱信号/待观察' if mean_ic > 0 else '❌ 信号衰减/无效')

    return {
        'status': 'ACTIVE',
        'sample_count': total_n,
        'period_count': len(weekly_ics),
        'ic_mean': round(mean_ic * 100, 2),        # 百分比形式
        'ic_std': round(std_ic * 100, 2),
        'icir': round(icir, 2),
        'ic_win_rate': round(win_rate, 1),
        'verdict': verdict,
        'note': f'基于 {total_n} 笔事件因子与未来 5 日收益配对，覆盖 {len(weekly_ics)} 个周度截面',
        'periods': weekly_ics
    }


def weekly_ic_preview(symbols=None, since_days=120):
    """向后兼容接口：直接调用 rolling_ic_series"""
    return rolling_ic_series(since_days=since_days, forward_days=5)




def pos_neg_diff(symbol=None, window_days=90):
    """阶段五F补：日度统计 (正面因子数-负面因子数)，返回 90 日滚动序列与当前均值。
    写法：按 published_at 切日窗口，sentiment>=0.5 算 +1、<=-0.5 算 -1；返回 (date_seq, current_mean, n_days)。
    端到端在 state/posneg_diff.json 落一份最新快照。"""
    import os, json, datetime
    from datasource import db as hdb
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=int(window_days))).strftime('%Y-%m-%d %H:%M:%S')
    rows = hdb.get_factors(symbol=symbol, since_hours=int(window_days) * 24) or []
    by_day = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pub = str(r.get('published_at') or '')[:10]
        if pub < cutoff[:10] or not pub:
            continue
        s = float(r.get('sentiment') or 0.0)
        by_day.setdefault(pub, 0)
        if s >= 0.5:
            by_day[pub] += 1
        elif s <= -0.5:
            by_day[pub] -= 1
    seq = sorted(by_day.items())
    n = max(1, len(seq))
    cur_mean = sum(v for _, v in seq) / n
    out = {
        'symbol': symbol or 'ALL',
        'window_days': int(window_days),
        'date_seq': [{'date': d, 'diff': v} for d, v in seq],
        'current_mean': round(cur_mean, 3),
        'n_days': n,
        'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        import os as _o
        _pf = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), '..', '..', 'state', 'posneg_diff.json')
        with open(_pf, 'w', encoding='utf-8') as _f:
            json.dump(out, _f, ensure_ascii=False)
    except Exception:
        pass
    return out

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    hdb.init_db()
    hdb.add_event_factor_columns()
    snap = factor_snapshot(since_hours=72)
    print(f'事件因子快照(72h内): {len(snap)} 条')
    top = top_symbols(top_k=10, since_hours=72)
    print('舆情榜单 Top10:')
    for r in top:
        print(f"  {r['symbol']}  score={r['score']:+.3f}  events={r['events']}  "
              f"+{r['pos_weight']:.2f} / -{r['neg_weight']:.2f} / ={r['neutral_weight']:.2f}")
    print('IC 占位:', weekly_ic_preview())
