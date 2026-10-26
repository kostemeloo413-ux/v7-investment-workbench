# -*- coding: utf-8 -*-
"""
scripts/analysis/factor_weight_audit.py
阶段五·任务F：事件因子分类权重审计与统计进化前置引擎

功能定位：
1. 每日收盘后（挂 daily_review）对 SQLite 中积累的 event_factor 进行全量穿透；
2. 聚合各事件类型（财报超预期、行业政策、公司重大动态、资金异动等）的 T+5 后验收益与胜率；
3. 遵循【诚实协议与统计红线】：有效配对样本 < 30 笔时，严禁机器自动篡改交易权重，
   输出状态为 ACCUMULATING（积累中），保持初始基准权重并如实打印进度；
4. 当配对样本 ≥ 30 笔且时间跨度 ≥ 14 天时，激活统计进化画像，输出权重建议矩阵；
5. 产出落盘至 state/factor_weight_audit.json，供月度体检与决策门消费。
"""

import os
import sys
import json
import math
import sqlite3
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb

AUDIT_STATE_PATH = os.path.join(_BASE, 'state', 'factor_weight_audit.json')
DB_PATH = os.path.join(_BASE, 'data', 'market', 'market.db')
SAMPLE_THRESHOLD = 30    # 统计有效性最低门槛
TIMESPAN_DAYS_MIN = 14   # 最低时间跨度（2周）


def run_factor_weight_audit(forward_days=5):
    """
    运行事件因子权重与分类有效性审计。
    返回 dict 结构并自动持久化到 state/factor_weight_audit.json。
    """
    now = datetime.datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    if not os.path.exists(DB_PATH):
        res = {
            'status': 'NO_DATABASE',
            'updated_at': now_str,
            'total_factors': 0,
            'paired_samples': 0,
            'verdict': '⛔ 数据库不存在',
            'categories': {},
        }
        _save_audit(res)
        return res

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, published_at, primary_symbol, event_type,
               sentiment, confidence, impact_side, title
        FROM event_factor
        ORDER BY id ASC
    """)
    rows = c.fetchall()

    c.execute("SELECT min(published_at), max(published_at) FROM event_factor")
    min_time, max_time = c.fetchone()
    conn.close()

    total_factors = len(rows)
    if total_factors == 0:
        res = {
            'status': 'EMPTY',
            'updated_at': now_str,
            'total_factors': 0,
            'paired_samples': 0,
            'verdict': '⛔ 暂无事件因子样本',
            'categories': {},
        }
        _save_audit(res)
        return res

    # 1. 计算时间跨度
    span_days = 0
    if min_time and max_time:
        try:
            d0 = datetime.datetime.strptime(min_time[:10], '%Y-%m-%d')
            d1 = datetime.datetime.strptime(max_time[:10], '%Y-%m-%d')
            span_days = (d1 - d0).days + 1
        except Exception:
            span_days = 1

    # 2. 标的缓存与 T+N 收益后验匹配
    paired_samples = []
    category_buckets = {}

    for row in rows:
        fid, pub, sym, et, snt, conf, side, title = row
        et = et or 'UNCLASSIFIED'
        category_buckets.setdefault(et, {
            'total_count': 0,
            'with_symbol_count': 0,
            'paired_count': 0,
            'win_count': 0,
            'returns': [],
            'scores': []
        })
        category_buckets[et]['total_count'] += 1

        pure = ''.join(ch for ch in str(sym) if ch.isdigit())
        if len(pure) != 6:
            continue

        category_buckets[et]['with_symbol_count'] += 1

        klines = hdb.get_kline(pure)
        if not klines or len(klines) < 30:
            continue

        pub_date = str(pub)[:10].replace('-', '')
        base_idx = None
        for idx, k in enumerate(klines):
            if str(k[0]) >= pub_date:
                base_idx = idx
                break

        if base_idx is None or base_idx + forward_days >= len(klines):
            continue

        close_t0 = float(klines[base_idx][4])
        close_tN = float(klines[base_idx + forward_days][4])
        if close_t0 <= 0:
            continue

        ret_fwd = (close_tN / close_t0) - 1.0
        side_sign = {'POSITIVE': 1.0, 'NEGATIVE': -1.0, 'NEUTRAL': 0.0}.get(side, 0.0)
        eff_score = float(snt or 0.0) * side_sign * (float(conf or 50) / 100.0)

        # 预测判定是否正确
        # 正面信号(eff_score > 0)且未来收益 > 0，或负面信号(eff_score < 0)且未来收益 < 0 计为命中
        is_win = (eff_score > 0 and ret_fwd > 0) or (eff_score < 0 and ret_fwd < 0)

        pair_info = {
            'id': fid,
            'symbol': pure,
            'event_type': et,
            'date': str(pub)[:10],
            'eff_score': round(eff_score, 4),
            'ret_t5': round(ret_fwd * 100, 2),
            'is_win': is_win
        }
        paired_samples.append(pair_info)

        b = category_buckets[et]
        b['paired_count'] += 1
        if is_win:
            b['win_count'] += 1
        b['returns'].append(ret_fwd)
        b['scores'].append(eff_score)

    total_paired = len(paired_samples)

    # 3. 汇总各分类表现
    cat_summary = {}
    for et, b in category_buckets.items():
        p_cnt = b['paired_count']
        wr = round((b['win_count'] / p_cnt * 100), 1) if p_cnt > 0 else None
        avg_ret = round((sum(b['returns']) / p_cnt * 100), 2) if p_cnt > 0 else None

        # 权重乘数建议
        # 当某类因子有效样本 ≥ 10 且总样本 ≥ 30 时方可建议调整；否则保持 1.0
        if total_paired >= SAMPLE_THRESHOLD and p_cnt >= 10:
            if wr >= 65 and avg_ret > 1.5:
                suggested_multiplier = 1.2   # 表现优异推荐升权
            elif wr < 40 or avg_ret < -1.5:
                suggested_multiplier = 0.8   # 持续失效推荐降权
            else:
                suggested_multiplier = 1.0
        else:
            suggested_multiplier = 1.0

        cat_summary[et] = {
            'total_factors': b['total_count'],
            'with_symbol': b['with_symbol_count'],
            'paired_samples': p_cnt,
            'win_rate_pct': wr,
            'avg_return_pct': avg_ret,
            'current_weight_multiplier': 1.0,
            'suggested_weight_multiplier': suggested_multiplier,
            'status': 'ACTIVE' if (total_paired >= SAMPLE_THRESHOLD and p_cnt >= 10) else 'ACCUMULATING'
        }

    # 4. 统计进化整体裁决
    is_sample_ready = (total_paired >= SAMPLE_THRESHOLD)
    is_time_ready = (span_days >= TIMESPAN_DAYS_MIN)

    if not is_sample_ready or not is_time_ready:
        status = 'ACCUMULATING'
        left_samples = max(SAMPLE_THRESHOLD - total_paired, 0)
        left_days = max(TIMESPAN_DAYS_MIN - span_days, 0)
        verdict = f'🟡 统计积累期 (配对样本: {total_paired}/{SAMPLE_THRESHOLD} 笔，时间跨度: {span_days}/{TIMESPAN_DAYS_MIN} 天)'
        note = (f'当前配对样本还差 {left_samples} 笔，跨度还差 {left_days} 天；'
                f'根据【诚实协议与统计红线】，机器暂不自动调整博主权重，保持既定等权配置。预计 9 月中旬自然成熟。')
    else:
        status = 'READY_FOR_EVOLUTION'
        verdict = f'✅ 满足统计进化门槛 (配对样本: {total_paired} 笔，跨度: {span_days} 天)'
        note = '事件因子样本量与时序跨度已满足方正/中泰金工要求，可正式激活按分类胜率微调决策门权重。'

    result = {
        'status': status,
        'updated_at': now_str,
        'total_factors_in_db': total_factors,
        'paired_t5_samples': total_paired,
        'sample_threshold': SAMPLE_THRESHOLD,
        'timespan_days': span_days,
        'timespan_threshold_days': TIMESPAN_DAYS_MIN,
        'verdict': verdict,
        'note': note,
        'categories': cat_summary,
        'recent_pairs': paired_samples[-10:] if paired_samples else []
    }

    _save_audit(result)
    return result


def _save_audit(data):
    """保存审计快照到 state/factor_weight_audit.json"""
    try:
        os.makedirs(os.path.dirname(AUDIT_STATE_PATH), exist_ok=True)
        with open(AUDIT_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[factor_weight_audit] 保存审计文件失败: {e}')


def load_audit():
    """读取最新的审计快照，不存在则现场计算一次"""
    if os.path.exists(AUDIT_STATE_PATH):
        try:
            with open(AUDIT_STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return run_factor_weight_audit()


if __name__ == '__main__':
    res = run_factor_weight_audit()
    print("=" * 60)
    print(f"事件因子权重审计报告 [{res['updated_at']}]")
    print(f"裁决: {res['verdict']}")
    print(f"说明: {res['note']}")
    print("-" * 60)
    print(f"{'事件类型':<16} | {'总数':<5} | {'配对':<5} | {'胜率':<8} | {'平均T+5收益':<10} | {'状态'}")
    print("-" * 60)
    for et, info in sorted(res['categories'].items(), key=lambda x: -x[1]['paired_samples']):
        wr_str = f"{info['win_rate_pct']}%" if info['win_rate_pct'] is not None else "--"
        ret_str = f"{info['avg_return_pct']:+.2f}%" if info['avg_return_pct'] is not None else "--"
        print(f"{et:<16} | {info['total_factors']:<5} | {info['paired_samples']:<5} | {wr_str:<8} | {ret_str:<10} | {info['status']}")
    print("=" * 60)
