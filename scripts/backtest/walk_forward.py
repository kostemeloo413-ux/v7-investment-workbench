# -*- coding: utf-8 -*-
"""
scripts/backtest/walk_forward.py
全池横截面滚动验证（阶段一改造 · 堵住幸存者偏差）

旧做法的问题：每个策略只测 1 只"手工挑的明星股"250 根K线，就得出
"胜率90%+"——典型的幸存者偏差 + 无样本外验证。

本脚本的做法：
1. 从 SQLite 历史库取股票池样本（当前DB覆盖不足时如实降低样本并声明，
   随 daemon daily_review 每天补齐后重跑样本自动扩大）；
2. 把每只股票的历史切成 K 个时间折(fold)，策略在每折上独立运行——
   同一规则必须在不同时间段都成立，才算真有效（样本外思想）；
3. 汇总各策略 × 各折的交易级指标，输出 markdown 对照报告到 outputs/。
   结论分级：trades<30 → "样本不足不下结论"；30~100 → "初步观察"；
   >100 → "统计可读"(仍非投资建议)。

用法：
    python -m scripts.backtest.walk_forward               # 默认: DB内抽样≤150只
    python -m scripts.backtest.walk_forward --sample 60   # 快速试跑
    python -m scripts.backtest.walk_forward --folds 4     # 自定义折数
"""

import os
import sys
import json
import time
import random
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts'),
           os.path.join(_BASE, 'scripts', 'pipeline')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

STRATEGIES = [
    ('hanshu', '韩叔·生命线低吸', 'strategy_hanshu_optimized', dict(is_gem_star=False)),
    ('yintwentao', '殷文涛·抱团突破', 'strategy_yintwentao_optimized', dict(is_gem_star=False)),
    ('liao-xiaoai', '廖小爱·深蹲企稳', 'strategy_liao_xiaoai_optimized', dict(is_gem_star=False)),
    ('cool', '酷儿·假跌破反包', 'strategy_cool_optimized', dict(is_gem_star=False)),
    ('ai-alpha', 'AI-Alpha·多因子动量', 'strategy_ai_alpha_quant', dict(is_gem_star=False)),
]

FOLD_COLUMNS = ['open', 'high', 'low', 'close', 'volume']


def load_sample(sample_n=150, min_bars=260):
    """从SQLite取有足够历史的主板样本代码（按成交额排序取流动性头部，固定随机打乱）。"""
    from datasource import db as history_db
    history_db.init_db()
    with history_db._connect() as conn:
        rows = conn.execute(
            """SELECT code, COUNT(*) AS n, AVG(amount) AS amt
               FROM daily_kline GROUP BY code HAVING n >= ? ORDER BY amt DESC""",
            (min_bars,)).fetchall()
    if not rows:
        return [], f'库中无满足≥{min_bars}根历史的标的'
    codes = [r[0] for r in rows]
    random.Random(2026).shuffle(codes)          # 固定种子保证可复现
    picked = codes[:sample_n]
    note = (f'DB覆盖{len(rows)}只→抽样{len(picked)}只(目标{sample_n})'
            if len(rows) > sample_n else
            f'DB仅覆盖{len(rows)}只(不足目标{sample_n},如实缩样;daemon每日增量后重跑自动扩大)')
    return picked, note


def kfold_slices(total_len, folds, warmup=65):
    """把 [warmup, total_len) 切成 folds 段；每段独立评估。"""
    usable_start, end = warmup, total_len
    seg = max((end - usable_start) // folds, 40)
    out = []
    s = usable_start
    for i in range(folds):
        e = end if i == folds - 1 else s + seg
        if e - s < 30:
            break
        out.append((s, e))
        s = e
    return out


def run():
    import pandas as pd
    from backtest_engine import AShareBacktestEngine
    from datasource import db as history_db

    args = sys.argv[1:]
    sample_n = int(args[args.index('--sample') + 1]) if '--sample' in args else 150
    folds = int(args[args.index('--folds') + 1]) if '--folds' in args else 3

    codes, note = load_sample(sample_n)
    print(f'[walk-forward] {note}')
    if not codes:
        print('[walk-forward] 请先等 daily_review 补齐数据或手动跑一次全池同步。')
        return

    engine = AShareBacktestEngine(initial_capital=100000)
    results = {sid: {'name': name, 'folds': [dict(trades=0, wins=0, pnl_sum=0.0,
                                                  ret_sum=0.0, dd_max=0.0)
                                     for _ in range(folds)]}
               for sid, name, _, _ in STRATEGIES}

    t0 = time.time()
    done = 0
    err_seen = {}
    # 阶段六·B3：读博主契约的 stop_loss（行内 dict）注入引擎 _stop_loss_pct
    from simulated_trading import BLOGGER_CONFIGS

    for code in codes:
        rows = history_db.get_kline(code)
        if len(rows) < 260:
            continue
        df = pd.DataFrame([{'date': datetime.datetime.strptime(r[0], '%Y%m%d'),
                            'open': r[1], 'high': r[2], 'low': r[3],
                            'close': r[4], 'volume': r[5]} for r in rows])
        slices = kfold_slices(len(df), folds)
        for sid, _, fn_name, kw in STRATEGIES:
            fn = getattr(__import__('backtest_engine'), fn_name)
            stop_pct = (BLOGGER_CONFIGS.get(sid) or {}).get('stop_loss', -0.05)
            engine._stop_loss_pct = stop_pct  # 阶段六·B3：注入止损线

            def passthrough(d, _sig=None, _exit=None):
                # 信号已预计算；执行层用"次日开盘"消除前视偏差
                _df = d.copy()
                _df['signal'] = _sig.reindex(_df.index).fillna(0).astype(int)
                _df['exit_signal'] = _exit.reindex(_df.index).fillna(False).astype(bool)
                return _df

            for fi, (a, b) in enumerate(slices):
                sub = df.iloc[max(0, a - 65):b].reset_index(drop=True)
                try:
                    sig_df = fn(sub.copy())
                    sig = sig_df['signal'].shift(1).fillna(0)
                    exsig = sig_df['exit_signal'].shift(1).fillna(False)
                    res = engine.run_strategy(sig_df,
                                              lambda d: passthrough(d, sig, exsig),
                                              **kw)
                except Exception as e:
                    # 不静默：记录每类异常首个信息，报告中如实说明
                    key = f'{sid}:{type(e).__name__}'
                    if key not in err_seen:
                        err_seen[key] = str(e)[:120]
                    continue
                slot = results[sid]['folds'][fi]
                trades = res.get('trades', [])
                slot['trades'] += len(trades)
                slot['wins'] += sum(1 for t in trades if t['is_win'])
                slot['pnl_sum'] += sum(t['pnl_amt'] for t in trades)
                slot['ret_sum'] += sum(t['pnl_pct'] for t in trades)
                slot['dd_max'] = max(slot['dd_max'], res.get('max_drawdown_pct', 0))
                slot['intraday_stop_fills'] = slot.get('intraday_stop_fills', 0) + sum(1 for t in trades if t.get('fill') == 'intraday_stop')
        done += 1
        if done % 25 == 0:
            print(f'  …进度 {done}/{len(codes)} ({time.time() - t0:.0f}s)')

    # ---------------- 报告生成 ----------------
    def grade(n):
        return ('✅ 统计可读' if n > 100 else
                '🟡 初步观察' if n >= 30 else '⛔ 样本不足,不下结论')

    # 计算事件感知舆情因子 IC / ICIR / 胜率统计
    try:
        from analysis.event_factors import rolling_ic_series
        ic_res = rolling_ic_series(since_days=180, forward_days=5)
    except Exception as e:
        ic_res = {'status': 'ERROR', 'sample_count': 0, 'verdict': f'异常: {e}',
                  'ic_mean': None, 'icir': None, 'ic_win_rate': None, 'note': str(e)}

    lines = [f'# 全池横截面 Walk-Forward 对照报告 (口径v2·触线即成交)',
             f'- 生成时间: {rc.now_str()}',
             f'- 样本说明: {note}',
             f'- 时间折数: {folds} | 引擎: T+1/涨跌停/真实费率(backtest_engine)',
             f'- 成本口径: 佣金万三(最低5元)+卖出印花税0.05%+双边滑点0.1%，下列全部收益均为费后净口径（AIEQ教训）',
             f'- 撮合口径: **v2 盘中触线即成交**（阶段六·2026-09生效：日内 low 触止损线按契约线价离场，与实盘账本同向）',
             '',
             '## 一、6 博主规则契约全池滚动对照',
             '',
             '| 策略 | 折 | 笔数 | 胜率 | 单笔均收益 | 合计盈亏额 | 最大回撤 | 盘中触线单 | 结论 |',
             '|---|---|---|---|---|---|---|---|---|']
    tot = {}
    for sid, name, _, _ in STRATEGIES:
        agg_t = agg_w = 0
        agg_ret = 0.0
        agg_stops = 0
        for fi, slot in enumerate(results[sid]['folds']):
            n, w = slot['trades'], slot['wins']
            wr = round(w / n * 100, 1) if n else 0.0
            avg_ret = round(slot['ret_sum'] / n, 2) if n else 0.0
            nstops = slot.get('intraday_stop_fills', 0)
            agg_stops += nstops
            lines.append(f'| {name} | F{fi + 1} | {n} | {wr}% | {avg_ret}% '
                         f'| {slot["pnl_sum"]:.0f}元 | {slot["dd_max"]}% | {nstops}笔 | {grade(n)} |')
            agg_t += n
            agg_w += w
            agg_ret += slot['ret_sum']
        wr_all = round(agg_w / agg_t * 100, 1) if agg_t else 0.0
        tot[sid] = {'trades': agg_t, 'win_rate': wr_all,
                    'avg_ret_per_trade': round(agg_ret / agg_t, 2) if agg_t else 0.0,
                    'intraday_stops': agg_stops}
        lines.append(f'| **{name} 合计** | — | **{agg_t}** | **{wr_all}%** '
                     f'| **{tot[sid]["avg_ret_per_trade"]}%** | — | — | **{agg_stops}笔** | **{grade(agg_t)}** |')

    # 二、事件感知舆情因子检验
    lines += ['',
              '## 二、LLM 事件感知舆情因子有效性检验 (IC / ICIR / 胜率)',
              '',
              f"- 因子构建: LLM 多标签事件分类 + 情绪分 [-1, +1] + 指数衰减加权 ($w_t = e^{{-\\lambda \\Delta t}}$)",
              f"- 检验窗口: 滚动周度截面 vs 标的未来 5 日收益率 (T+5)",
              f"- **统计判定: {ic_res.get('verdict')}**",
              f"- 有效配对样本数: {ic_res.get('sample_count', 0)} 笔 (统计最低门槛: 30 笔)",
              f"- IC 均值: {ic_res.get('ic_mean', '-')}% | 年化 ICIR: {ic_res.get('icir', '-')} | IC 周胜率: {ic_res.get('ic_win_rate', '-')}%",
              f"- 说明: {ic_res.get('note', '')}"]

    # 三、实盘账本与回测近似对照（阶段六新增）
    # 诚实协议：账本样本 < 30 笔时如实声明，不虚构统计
    try:
        from simulated_trading import TRADES_DIR, BLOGGER_CONFIGS
        live_trades = []
        for bid in BLOGGER_CONFIGS:
            p = os.path.join(TRADES_DIR, bid + '.json')
            if os.path.exists(p):
                d = json.load(open(p, encoding='utf-8'))
                live_trades.extend(d.get('trades', []))
        n_live = len(live_trades)
        intraday_n = sum(1 for t in live_trades if t.get('price_type') == 'intraday')
        lines += ['',
                  '## 三、实盘账本 vs 回测近似对照',
                  '',
                  f"- 实盘累计成交: {n_live} 笔（其中盘中成交: {intraday_n} 笔）",
                  f"- **对账判定: {'⛔ 样本不足(<30笔),不下统计结论' if n_live < 30 else '✅ 样本达到对照门槛'}**",
                  f"- 说明: 当前实盘账本处于初期积累阶段；每笔成交带 `price_type` 与时间戳 `ts`，"
                  f"累计 ≥30 笔后输出真实成交价 vs 回测模拟价的偏差分布。"]
    except Exception:
        pass

    lines += ['', '## 四、阅读须知', '',
              '- 本表为【规则契约与事件因子的统计画像】而非收益预测；单笔均收益为交易级算术平均，'
              '不等于账户复利收益。',
              '- 执行时序已做反前视处理：信号按当日收盘判定、次日开盘成交（保守口径）。',
              '- 样本随 daily_review 每日增长。任何策略或因子在笔数<30 的样本上都不具备结论效力。',
              '- ⛔本文件为个人研究记录,不构成任何投资建议。']

    if err_seen:
        lines += ['', '## 执行异常明细（这些样本被跳过，不是零交易）', '']
        lines += [f'- `{k}`: {v}' for k, v in list(err_seen.items())[:10]]

    md = '\n'.join(lines)
    out_md = os.path.join(_BASE, 'outputs', 'walk_forward_report.md')
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write(md)

    summary_json = {'generated_at': rc.now_str(), 'sample_note': note,
                    'folds': folds, 'strategies': tot,
                    'event_factors_ic': ic_res}
    with open(os.path.join(rc.DATA_DIR, 'walk_forward_summary.json'), 'w',
              encoding='utf-8') as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)

    print(md[:1200])
    print('\n[walk-forward] 报告已写入:', out_md)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    run()
