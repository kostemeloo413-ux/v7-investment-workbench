# -*- coding: utf-8 -*-
"""
scripts/pipeline/blogger_workers.py
六部 · Blogger Workers（6 位博主工作单元）

职责：读取驿传行单（dispatch_log.json），执行 6 位博主板块任务：
  1. 复用 scan_mainboard_picks.scan_and_score_all(persist=False) 全池量化打分
     （6 博主共享一次 3100+ 主板全池扫描，耗时 ~0.3s）
  2. 题材关联增强：对照通政奏报当日热点题材，为每只选股标注
     matched_themes / theme_note（命中即标注联动，未命中如实声明，符合诚实协议）
  3. 产出六部奏本 data/pipeline/liubu_report.json

产出结构：
  {
    "schema_version": "1.0",
    "produced_at": "...",
    "dispatch_at": "...",
    "hot_themes": ["AI算力", ...],
    "picks": [
      { ...原博主选股结构...,
        "matched_themes": ["半导体"],       // 命中的当日题材
        "theme_note": "【题材联动】..."      // 题材关联说明
      }, ...
    ]
  }
"""

import os
import sys
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(BASE_DIR, 'scripts')
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, BASE_DIR)

LIUBU_PATH = os.path.join(BASE_DIR, 'data', 'pipeline', 'liubu_report.json')
SCHEMA_VERSION = '1.0'


def _build_theme_pool(news_list):
    """从通政奏报提取当日题材关键词池（themes + entity + 标题关键词）。"""
    pool = []
    for n in news_list:
        for t in n.get('themes', []):
            if len(t) >= 2 and t not in pool:
                pool.append(t)
        ent = n.get('entity', '')
        if len(ent) >= 2 and ent not in pool:
            pool.append(ent)
        title = n.get('title', '')
        # 标题中提取 2~6 字实义词段（简单窗口扫描，去常见虚词）
        for seg in title.split():
            seg = seg.strip('【】[]()（）《》,.，。')
            if 2 <= len(seg) <= 6 and seg not in pool and not seg.isdigit():
                pool.append(seg)
    return pool


def _match_stock_themes(stock_name, trigger_reason, theme_pool):
    """返回选股命中的当日题材（子串匹配）。"""
    text = f'{stock_name} {trigger_reason}'
    matched = []
    for kw in theme_pool:
        if kw and kw in text:
            matched.append(kw)
    return list(dict.fromkeys(matched))


def _build_theme_note(stock_name, matched, news_list):
    """构造题材联动说明；未命中如实声明。"""
    if not matched:
        return f'【题材提示】{stock_name} 与当日热点题材无直接关键词关联，本信号由量化契约独立成立，不依赖消息面。'
    srcs = []
    for n in news_list:
        n_themes = n.get('themes', [])
        if any(m in n_themes or m in n.get('entity', '') or m in n.get('title', '') for m in matched):
            srcs.append(f"{n.get('id')}「{n.get('title', '')[:20]}」")
    src_str = '、'.join(srcs[:2])
    return (f'【题材联动】{stock_name} 命中当日热点「{" / ".join(matched)}」'
            f'（通政奏报 {src_str}），属消息面+量化共振，注意情绪兑现风险。')


def execute_liubu(dispatch_log=None, brief=None, quiet=False):
    """
    六部主流程：全池扫描 + 题材关联增强。

    返回 (report_dict, path)
    """
    if dispatch_log is None:
        from dispatcher import load_dispatch
        dispatch_log = load_dispatch()
    if brief is None:
        from news_officer import load_brief
        brief = load_brief()

    news_list = (brief or {}).get('news', [])
    theme_pool = _build_theme_pool(news_list)
    hot_themes = (dispatch_log or {}).get('hot_themes', []) or theme_pool

    # 1. 全池扫描（六部模式：不写盘，由起居注统一落盘）
    from scan_mainboard_picks import scan_and_score_all
    picks = scan_and_score_all(persist=False)

    # 2. 题材关联增强（逐博主奏本）
    enhanced = []
    for p in picks:
        stock_name = p.get('stock_name', '')
        reason = p.get('trigger_reason', '')
        matched = _match_stock_themes(stock_name, reason, theme_pool)
        note = _build_theme_note(stock_name, matched, news_list)
        tags = list(p.get('rule_tags', []) or [])
        for m in matched[:2]:
            if m not in tags:
                tags.insert(0, f'题材:{m}')
        ep = dict(p)
        ep['matched_themes'] = matched
        ep['theme_note'] = note
        ep['rule_tags'] = tags
        enhanced.append(ep)

    report = {
        'schema_version': SCHEMA_VERSION,
        'produced_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'dispatch_at': (dispatch_log or {}).get('dispatched_at'),
        'news_count': len(news_list),
        'theme_pool_count': len(theme_pool),
        'hot_themes': hot_themes,
        'picks': enhanced,
    }

    os.makedirs(os.path.dirname(LIUBU_PATH), exist_ok=True)
    with open(LIUBU_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if not quiet:
        for p in enhanced:
            status = f"✅ {p['stock_name']} ({p['stock_code']})" if p.get('triggered') else f"⏸️ {p['stock_name']}"
            theme_txt = ' / '.join(p['matched_themes']) if p['matched_themes'] else '无直接关联'
            print(f"[六部] {p['blogger_name']:16s} {status} 题材:{theme_txt}")
    return report, LIUBU_PATH


def load_liubu():
    if os.path.exists(LIUBU_PATH):
        try:
            with open(LIUBU_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


if __name__ == '__main__':
    r, p = execute_liubu()
    print('六部奏本已落盘:', p)
