# -*- coding: utf-8 -*-
"""
scripts/pipeline/xingbu.py
刑部 · Xing Bu（违规留痕审计）

定位：被门下省否决的内容（不相关新闻）记录归档，形成审计链，
     并定期「反哺」门下省黑名单词典——同样的污染第二次自动被拦。

产物：data/pipeline/routing_mistakes.json
  {
    "schema_version": "1.0",
    "total": 3,
    "records": [
      {
        "news_id": "HOT-01",
        "title": "【韩安冉名下多家公司已注销】...",
        "category": "财经综合/市场观察",
        "source": "新浪财经 7x24 实时快讯",
        "relevance_score": -6,
        "hits_blacklist": ["网红", "片酬"],
        "verdict": "VETO",
        "reason": "门下省否决：...",
        "rejected_at": "2026-08-25 17:18:00"
      }
    ],
    "fed_back_blacklist": ["网红", "片酬", "男足", "世预赛"]
  }
"""

import os
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MISTAKES_PATH = os.path.join(BASE_DIR, 'data', 'pipeline', 'routing_mistakes.json')
SCHEMA_VERSION = '1.0'
MAX_RECORDS = 100  # 留痕上限，超限滚动淘汰最旧的


def _load():
    if os.path.exists(MISTAKES_PATH):
        try:
            with open(MISTAKES_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'schema_version': SCHEMA_VERSION, 'total': 0, 'records': [], 'fed_back_blacklist': []}


def record_rejection(item, score, hits_blacklist):
    """
    刑部留痕：记录一条被门下省否决的新闻。
    item: 门下省 veto() 构造好的记录（或原始新闻 dict）
    """
    db = _load()
    records = db.get('records', [])

    # 去重：同标题不重复记录
    title = item.get('title', '')
    if any(r.get('title') == title for r in records):
        return db

    records.append(item)
    # 滚动淘汰
    if len(records) > MAX_RECORDS:
        records = records[-MAX_RECORDS:]

    db['records'] = records
    db['total'] = len(records)
    os.makedirs(os.path.dirname(MISTAKES_PATH), exist_ok=True)
    with open(MISTAKES_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    return db


def feedback_blacklist(menxia_module):
    """
    刑部反哺：统计留痕记录里高频命中黑名单词，反馈给门下省词典。
    频率 >= 2 的词若不在词典中，自动并入（下次同类新闻直接拦）。
    返回新并入的词列表。
    """
    db = _load()
    records = db.get('records', [])
    if not records:
        return []

    # 统计黑名单命中词频
    from collections import Counter
    freq = Counter()
    for r in records:
        for w in r.get('hits_blacklist', []):
            freq[w] += 1

    existing = set(menxia_module.BLACKLIST)
    new_words = [w for w, c in freq.items() if c >= 2 and w not in existing]
    if new_words:
        menxia_module.BLACKLIST.extend(new_words)
        db['fed_back_blacklist'] = sorted(set(db.get('fed_back_blacklist', []) + new_words))
        with open(MISTAKES_PATH, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    return new_words


def list_mistakes(limit=20):
    """查看最新留痕记录（供都察院/前端展示）。"""
    db = _load()
    return db.get('records', [])[-limit:]


if __name__ == '__main__':
    print('=== 刑部自检 ===')
    print('留痕记录数:', len(list_mistakes()))
    db = _load()
    print('已反哺黑名单词:', db.get('fed_back_blacklist', []))
    for r in list_mistakes(5):
        print(f"  {r.get('rejected_at','')} [{r.get('relevance_score','')}] {r.get('title','')[:40]}")
