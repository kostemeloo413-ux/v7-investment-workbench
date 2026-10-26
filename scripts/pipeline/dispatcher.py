# -*- coding: utf-8 -*-
"""
scripts/pipeline/dispatcher.py
驿传 · Dispatcher（分发官）

职责：读取通政司奏报（news_brief.json），按 6 位博主画像路由情报，
     为每位博主生成任务单（关注题材 / 相关情报 / 执行指令）。

画像路由规则（贴合各博主 Skill 契约）：
  - ai-alpha    多因子动量 / 量价共振 / 突破 / 算力 / 半导体
  - hanshu      低吸 / 生命线 / 洗盘 / 错杀 / 防守板块 / 大盘生命线
  - yintwentao  科技阵营 / 抱团 / 光模块 / 芯片 / 算力 / 主力资金
  - liao-xiaoai 价值 / 估值 / 宏观 / 政策 / 战略 / 长期 / 核心资产
  - cool        超短 / 量价 / 换手 / 连板 / 情绪 / 形态 / 假跌破
  - anti-demo   情绪极值 / 连板 / 换手 / 追涨 / 风险 / 派发 / 熔断

产出：data/pipeline/dispatch_log.json（驿传行单）
  {
    "schema_version": "1.0",
    "dispatched_at": "...",
    "brief_fetched_at": "...",
    "news_count": 4,
    "blogger_orders": [
      {
        "blogger_id": "cool",
        "blogger_name": "酷儿 (Cool)",
        "profile": "假跌破反转 + 20MA斜率突破（超短线）",
        "mission": "select_picks",
        "attention_keywords": [...],
        "relevant_news": [{"id", "title", "category", "why"}],
        "hot_themes": ["半导体", "存储芯片"],
        "orders": ["按 Skill 契约执行超短线假跌破扫描", ...]
      }, ...
    ]
  }
"""

import os
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DISPATCH_PATH = os.path.join(BASE_DIR, 'data', 'pipeline', 'dispatch_log.json')
SCHEMA_VERSION = '1.0'

# 6 位博主画像（关键词路由表）
BLOGGER_PROFILES = [
    {
        'blogger_id': 'ai-alpha',
        'blogger_name': 'AI-Alpha (量化智脑)',
        'profile': '多因子动量突破 + 20日新高 + ATR动态追踪',
        'mission': 'select_picks',
        'attention_keywords': ['动量', '突破', '量价', '共振', '算力', '半导体', '存储', '芯片', 'AI', '算力资本开支', '新高'],
        'orders': ['按多因子动量契约扫描主板大池 20 日新高突破标的',
                   '关注通政奏报中算力/半导体/存储题材的量价共振信号'],
    },
    {
        'blogger_id': 'hanshu',
        'blogger_name': '韩叔 (趣聊财经)',
        'profile': '20MA绝活生命线低吸 + 5天线荡秋千做差价',
        'mission': 'select_picks',
        'attention_keywords': ['低吸', '生命线', '洗盘', '错杀', '缩量', '防守', '企稳', '水网', '基建', '大盘'],
        'orders': ['按绝活生命线契约扫描回踩 20MA 企稳的低吸标的',
                   '关注通政奏报中防守板块/错杀标的的生命线机会'],
    },
    {
        'blogger_id': 'yintwentao',
        'blogger_name': '殷文涛 (梵爹)',
        'profile': '三大阵营博弈 + 光/芯时空差轮动',
        'mission': 'select_picks',
        'attention_keywords': ['算力', '光模块', 'CPO', '芯片', '半导体', '存储', '抱团', '主力', '大资金', '阵营', '科技'],
        'orders': ['按三大阵营契约扫描科技成长进攻阵营的抱团合力标的',
                   '结合通政奏报光/芯题材判断阵营资金流向'],
    },
    {
        'blogger_id': 'liao-xiaoai',
        'blogger_name': '廖小爱',
        'profile': '中国特色价值投资 + 3-3-1 仓位模型 + 无限子弹',
        'mission': 'select_picks',
        'attention_keywords': ['价值', '估值', '宏观', '政策', '战略', '核心资产', '长期', '经济', '基建', '水网', '国企'],
        'orders': ['按 3-3-1 模型扫描深蹲筑底安全边际区核心资产',
                   '关注通政奏报中宏观政策/国家战略方向的底仓机会'],
    },
    {
        'blogger_id': 'cool',
        'blogger_name': '酷儿 (Cool)',
        'profile': '假跌破反转 + 20MA斜率突破（超短线）',
        'mission': 'select_picks',
        'attention_keywords': ['换手', '连板', '情绪', '形态', '假跌破', '反包', '长阳', '量价', '妖股', '冲高', '回落'],
        'orders': ['按假跌破契约扫描流动性Sweep + 放量吞没长阳的反转买点',
                   '警惕通政奏报中高位连板/极端换手的冲高风险标的'],
    },
    {
        'blogger_id': 'anti-demo',
        'blogger_name': '极短线情绪博主 (反指哨兵)',
        'profile': '高位极端追涨监测（反向避险）',
        'mission': 'risk_guard',
        'attention_keywords': ['连板', '换手', '追涨', '风险', '派发', '熔断', '情绪', '见顶', '妖股', '极端', '预警'],
        'orders': ['全市场情绪雷达扫描 3+ 连板/换手>25%/BIAS20>18% 高危标的',
                   '对通政奏报中情绪极值题材触发黑名单拦截与共识扣分'],
    },
]


def _match_relevance(news_item, keywords):
    """判断一条情报是否命中某博主画像关键词，返回 (命中, 原因)。"""
    text = ' '.join([
        str(news_item.get('title', '')),
        str(news_item.get('category', '')),
        str(news_item.get('entity', '')),
        str(news_item.get('summary', '')),
    ])
    hit = []
    for kw in keywords:
        if kw and kw.lower() in text.lower():
            hit.append(kw)
    return hit


def dispatch(brief=None, quiet=False):
    """
    驿传主流程：读取通政奏报，按画像路由生成驿传行单。

    返回 (dispatch_dict, path)。
    """
    if brief is None:
        from news_officer import load_brief
        brief = load_brief()
    if not brief:
        raise RuntimeError('驿传：未找到通政奏报 news_brief.json，请先运行通政司')

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    news_list = brief.get('news', [])

    # 汇总全市场当日热点题材（供六部做题材关联）
    hot_themes = []
    for n in news_list:
        for t in n.get('themes', []):
            if t and t not in hot_themes:
                hot_themes.append(t)

    blogger_orders = []
    for prof in BLOGGER_PROFILES:
        relevant = []
        for n in news_list:
            hits = _match_relevance(n, prof['attention_keywords'])
            if hits:
                relevant.append({
                    'id': n.get('id'),
                    'title': n.get('title', ''),
                    'category': n.get('category', ''),
                    'entity': n.get('entity', ''),
                    'why': '命中关键词：' + '、'.join(hits[:3]),
                })
        blogger_orders.append({
            'blogger_id': prof['blogger_id'],
            'blogger_name': prof['blogger_name'],
            'profile': prof['profile'],
            'mission': prof['mission'],
            'attention_keywords': prof['attention_keywords'],
            'relevant_news': relevant,
            'hot_themes': hot_themes,
            'orders': prof['orders'],
        })

    dispatch_log = {
        'schema_version': SCHEMA_VERSION,
        'dispatched_at': now,
        'brief_fetched_at': brief.get('fetched_at'),
        'news_count': len(news_list),
        'hot_themes': hot_themes,
        'blogger_orders': blogger_orders,
    }

    os.makedirs(os.path.dirname(DISPATCH_PATH), exist_ok=True)
    with open(DISPATCH_PATH, 'w', encoding='utf-8') as f:
        json.dump(dispatch_log, f, ensure_ascii=False, indent=2)

    if not quiet:
        for bo in blogger_orders:
            print(f"[驿传] {bo['blogger_name']:16s} 获情报 {len(bo['relevant_news'])} 条 / 关注题材 {len(bo['hot_themes'])} 个")
    return dispatch_log, DISPATCH_PATH


def load_dispatch():
    if os.path.exists(DISPATCH_PATH):
        try:
            with open(DISPATCH_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


if __name__ == '__main__':
    d, p = dispatch()
    print('驿传行单已落盘:', p)
    print('全市场热点题材:', d['hot_themes'])
