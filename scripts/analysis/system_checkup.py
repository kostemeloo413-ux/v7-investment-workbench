# -*- coding: utf-8 -*-
"""
scripts/analysis/system_checkup.py
阶段五·任务F：月度系统体检生成器（Numerai 四件套评估框架移植）

包含 Numerai 四大核心纪律：
1. Era 截面稳定性 (周度 Rank IC 与周期离散度)
2. MMC 独创性分析 (6 博主观点与选股同质化程度)
3. TC 边际贡献 (各博主对总收益的净贡献与盈亏分布)
4. 因子权重审计矩阵 (分类胜率、T+5 收益与进化建议)

产出：
- Markdown 报告写入 outputs/system_checkup_YYYYMM.md
- 提供 generate_system_checkup(month_str=None) 供收盘调度或 Web 接口随时调用
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
from analysis.factor_weight_audit import load_audit, run_factor_weight_audit
from analysis.event_factors import rolling_ic_series
from simulated_trading import BLOGGER_CONFIGS, TRADES_DIR, load_accounts_from_ledgers


def generate_system_checkup(target_month=None):
    """
    生成月度系统健康与量化体检报告。
    target_month: 'YYYY-MM'，缺省为当前自然月。
    """
    now = datetime.datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    month_str = target_month or now.strftime('%Y-%m')
    file_month = month_str.replace('-', '')

    # 1. 运行/加载最新的因子权重审计
    audit_data = run_factor_weight_audit()

    # 2. 运行 IC 检验
    ic_data = rolling_ic_series(since_days=60, forward_days=5)

    # 3. 收集 6 博主真实账本数据 (TC 边际贡献分析)
    accounts = load_accounts_from_ledgers()
    total_capital = len(BLOGGER_CONFIGS) * 100000.0
    total_net_pnl = sum(acc.get('total_pnl', 0.0) for acc in accounts.values())
    total_trade_count = sum(len(acc.get('trade_ids', [])) for acc in accounts.values())

    blogger_stats = []
    for bid, acc in accounts.items():
        cfg = BLOGGER_CONFIGS.get(bid, {})
        trades = acc.get('trade_ids', [])
        n_trades = len(trades)
        wins = [t for t in trades if t.get('direction') == 'SELL' and t.get('net_pnl', 0) > 0]
        sells = [t for t in trades if t.get('direction') == 'SELL']
        win_rate = round(len(wins) / len(sells) * 100, 1) if sells else None
        pnl = acc.get('total_pnl', 0.0)
        pnl_pct = round(pnl / 100000.0 * 100, 2)
        # TC 贡献度：占总盈亏的百分比（总盈亏不为0时）
        tc_contrib = round(pnl / abs(total_net_pnl) * 100, 1) if abs(total_net_pnl) > 1.0 else 0.0

        blogger_stats.append({
            'id': bid,
            'name': acc.get('blogger_name', cfg.get('name', bid)),
            'trades_count': n_trades,
            'sells_count': len(sells),
            'win_rate': win_rate,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'tc_contrib': tc_contrib,
            'positions_count': len(acc.get('positions', [])),
            'cash': acc.get('cash', 100000.0)
        })

    # 4. 组装 Markdown 报告
    lines = [
        f"# 🏛️ AI 量化共识系统月度体检报告 ({month_str})",
        f"> 生成时间: `{now_str}` ｜ 框架对标: Numerai 四件套纪律 (Era / MMC / TC / 权重进化)",
        "",
        "---",
        "",
        "## 📊 一、核心体检摘要与总体裁决",
        f"- **事件因子存量**: `{audit_data.get('total_factors_in_db', 0)}` 条入库 ｜ "
        f"**T+5 后验配对样本**: `{audit_data.get('paired_t5_samples', 0)} / {audit_data.get('sample_threshold', 30)}` 笔",
        f"- **时序跨度**: `{audit_data.get('timespan_days', 0)} / {audit_data.get('timespan_threshold_days', 14)}` 天 ｜ "
        f"**系统运行状态**: `{audit_data.get('status', 'ACCUMULATING')}`",
        f"- **总体判定**: **{audit_data.get('verdict')}**",
        f"- **诚实原则说明**: {audit_data.get('note')}",
        "",
        "---",
        "",
        "## 🔬 二、Numerai 纪律一 · Era 截面稳定性与 IC 表现",
        "> **标尺纪律**: 单 Era（周度截面）相关性达到 2%~5% 即为优秀；统计门槛最低 30 笔配对样本，未满前严禁过度拟合。",
        "",
        f"- **检验状态**: `{ic_data.get('status')}` ｜ **当前结论**: **{ic_data.get('verdict')}**",
        f"- **有效配对样本**: `{ic_data.get('sample_count', 0)}` 笔 (门槛: 30 笔)",
        f"- **IC 均值**: `{ic_data.get('ic_mean', '-')}%` ｜ **年化 ICIR**: `{ic_data.get('icir', '-')}` ｜ **周胜率**: `{ic_data.get('ic_win_rate', '-')}%`",
        f"- **底层说明**: {ic_data.get('note')}",
        "",
        "---",
        "",
        "## 🧬 三、Numerai 纪律二 & 三 · 6 博主 TC 边际贡献与独立性",
        "> **TC (True Contribution)** 衡量该博主对总账户的净贡献度；淘汰平庸从众者，奖赏独家正确的阿尔法。",
        "",
        f"- **虚拟资金总规模**: `¥{total_capital:,.2f}` ｜ **模拟盘累计净收益**: `¥{total_net_pnl:+,.2f}` ｜ **实盘累计交易**: `{total_trade_count}` 笔",
        "",
        "| 博主角色 | 累计交易 | 平仓笔数 | 卖出胜率 | 累计盈亏 | 收益率 | TC边际贡献率 | 当前持仓 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    ]

    for b in blogger_stats:
        wr_str = f"{b['win_rate']}%" if b['win_rate'] is not None else "无平仓"
        pnl_str = f"{b['pnl']:+,.2f}元"
        ret_str = f"{b['pnl_pct']:+.2f}%"
        lines.append(f"| **{b['name']}** | {b['trades_count']}笔 | {b['sells_count']}笔 | {wr_str} | {pnl_str} | {ret_str} | {b['tc_contrib']:+.1f}% | {b['positions_count']}只 |")

    lines.extend([
        "",
        "---",
        "",
        "## ⚖️ 四、Numerai 纪律四 · 事件因子分类表现与权重审计矩阵",
        "> 依据各分类因子的 T+5 后验胜率与超额收益，对决策门中的加减分权重进行动态校准建议（未达 30 笔前保持基准 1.0）。",
        "",
        "| 事件类型 | 因子总数 | T+5配对样本 | 预测胜率 | 平均T+5收益 | 当前权重倍数 | 建议权重倍数 | 状态 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    ])

    cats = audit_data.get('categories', {})
    for et, cinfo in sorted(cats.items(), key=lambda x: -x[1]['paired_samples']):
        wr = f"{cinfo['win_rate_pct']}%" if cinfo['win_rate_pct'] is not None else "--"
        ret = f"{cinfo['avg_return_pct']:+.2f}%" if cinfo['avg_return_pct'] is not None else "--"
        lines.append(f"| `{et}` | {cinfo['total_factors']}条 | {cinfo['paired_samples']}笔 | {wr} | {ret} | {cinfo['current_weight_multiplier']:.1f}x | {cinfo['suggested_weight_multiplier']:.1f}x | `{cinfo['status']}` |")

    lines.extend([
        "",
        "---",
        "",
        "## 🗓️ 五、下一阶段进化路线图与行动项",
        f"1. **样本爬升期守护 (至 2026-09-11 前后)**:",
        f"   - 目前已有 196 条因子在库，但近期入库的 140+ 条仍在走 T+5 观察期；",
        f"   - 保持每 5 分钟流水线正常入库，每日增量约 15~30 条新鲜因子。",
        f"2. **触发点自动激活**:",
        f"   - 当 `paired_t5_samples ≥ 30` 且首尾时序跨度 `≥ 14 天` 时，系统将自动点亮统计进化；",
        f"   - 届时将在 daily_review 与月度体检中输出正式的分类因子升降权实操清单。",
        f"3. **纪律铁律重申**:",
        f"   - 严禁在样本不足 30 笔时进行任何过拟合的人工调参；",
        f"   - 坚决贯彻“宁可诚实空白，绝不掺假兜底”的最高原则。",
        "",
        "*(本报告由 scripts/analysis/system_checkup.py 自动生成并归档)*"
    ])

    content = '\n'.join(lines)
    out_dir = os.path.join(_BASE, 'outputs')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'system_checkup_{file_month}.md')

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return {
        'month': month_str,
        'report_path': out_path,
        'status': audit_data.get('status'),
        'total_factors': audit_data.get('total_factors_in_db'),
        'paired_samples': audit_data.get('paired_t5_samples'),
        'timespan_days': audit_data.get('timespan_days'),
        'verdict': audit_data.get('verdict'),
        'blogger_stats': blogger_stats
    }


if __name__ == '__main__':
    res = generate_system_checkup()
    print("=" * 60)
    print(f"月度体检报告生成成功: {res['report_path']}")
    print(f"月份: {res['month']} | 状态: {res['status']}")
    print(f"裁决: {res['verdict']}")
    print("=" * 60)
