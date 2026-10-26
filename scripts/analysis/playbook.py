# -*- coding: utf-8 -*-
"""
scripts/analysis/playbook.py
前瞻事件剧本引擎与预期差分析（阶段三 任务A）

定位：
- 从"死板罗列4个静态议题"升级为"基于真实事件日历 + 结构化剧本 + LLM预期差"；
- 将前瞻事件转化为可执行的三套应对预案：
    1. pre_observation:  事件前观察点与时间窗口
    2. upside_plan:      超预期/利好兑现后的操作预案（买点/追击/止盈规则）
    3. downside_plan:    不及预期/暴雷后的风控预案（刚性止损/清仓线）
    4. neutral_plan:     符合预期/平淡无奇时的应对（做T/观望/资金换向）
    5. priced_in_score:  LLM评估市场对该事件已预期消化的程度 (0-100)
- 同时输出六博主各自专属的【💡 总结观点 / 🎯 应对锦囊 / 🔍 推演逻辑】，完美兼容现有前端弹窗。
"""

import os
import sys
import json
import hashlib
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

BLOGGER_IDS = ['liao-xiaoai', 'cool', 'yintwentao', 'hanshu', 'ai-alpha', 'anti-demo']
BLOGGER_NAMES = {
    'hanshu': '韩叔 (趣聊财经)',
    'yintwentao': '殷文涛 (梵爹)',
    'liao-xiaoai': '廖小爱',
    'cool': '酷儿 (Cool)',
    'ai-alpha': 'AI-Alpha (量化智脑)',
    'anti-demo': '极短线情绪反指哨兵',
}

_PLAYBOOK_SYSTEM_PROMPT = (
    '你是A股事件驱动量化交易策略专家兼多流派分析中枢。'
    '给定一个即将在未来30天内发生的A股重大事件（财报披露、解禁、分红、重大会议、政策催化等），'
    '请输出结构化的交易剧本与六位博主的契约推演。要求：\n'
    '1. 严禁编造具体的未来股票目标价格数字；\n'
    '2. 严格按JSON格式输出，结构：\n'
    '{\n'
    '  "playbook": {\n'
    '    "priced_in_score": 0到100的整数(市场对该事件预期已被提前消化的程度),\n'
    '    "pre_observation": ["事件前观察要点1", "观察要点2"],\n'
    '    "upside_plan": "若超预期或利好发酵的操作预案（仓位/追击/止盈红线）",\n'
    '    "downside_plan": "若不及预期或利空暴雷的止损防守预案",\n'
    '    "neutral_plan": "若符合预期/平淡无奇时的操作预案"\n'
    '  },\n'
    '  "blogger_deductions": {\n'
    '    "liao-xiaoai": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"},\n'
    '    "cool": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"},\n'
    '    "yintwentao": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"},\n'
    '    "hanshu": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"},\n'
    '    "ai-alpha": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"},\n'
    '    "anti-demo": {"title_verdict": "不超过28字观点", "kit": "①②③条操作锦囊(不超过120字)", "logic": "不超过90字推演"}\n'
    '  }\n'
    '}\n'
    '只输出JSON本体。'
)


def _cache_get(key):
    cache = rc._load_json(rc.LLM_CACHE_PATH, {})
    return cache.get(key)


def _cache_set(key, value):
    cache = rc._load_json(rc.LLM_CACHE_PATH, {})
    cache[key] = value
    rc._save_json(rc.LLM_CACHE_PATH, cache)


def _build_fallback_deductions(event):
    """当 LLM 离线或调用失败时，基于博主契约与事件类型生成的结构化降级预案"""
    title = event.get('title', '')
    symbol = event.get('symbol', '')
    name = event.get('name', '')
    ev_type = event.get('event_type', '')

    target_str = f"{name}({symbol})" if symbol else name
    return {
        "liao-xiaoai": {
            "title_verdict": f"【国家战略与价值底线】关注 {target_str} 是否具备长期护城河与合理估值黄金坑。",
            "kit": "① 严禁在事件前追涨无业绩题材炒作；② 恪守 3-3-1 模型，若因短期事件扰动出现深蹲黄金坑，分批打入底仓；③ 留足 70% 无限子弹穿越周期。",
            "logic": f"中国特色价值投资坚守内在价值与国家战略方向，{title} 的中长期价值取决于真实业绩与现金流壁垒。"
        },
        "cool": {
            "title_verdict": f"【技术形态右侧确认】不赌 {target_str} 消息面，等待 20MA 假跌破放量回收长阳。",
            "kit": "① 事件落地前后严禁冲动挂单；② 观察标的是否在 20MA 下方走出假摔诱空并在 24 小时内带量阳线反转；③ 严格锚定止损线，无形态保持 100% 空仓。",
            "logic": "80% 的时间属于垃圾震荡，事件催化极易引发主力流动性收割，只在确定性的假跌破技术形态右侧出手。"
        },
        "yintwentao": {
            "title_verdict": f"【三大阵营流动性博弈】跟踪增量资金在 {target_str} 相关赛道中的抱团意图。",
            "kit": "① 紧盯全市场 2.4T 量能平衡线；② 若科技先锋之矛阵营大资金持续流入则顺势波段跟进；③ 回避老登防守板块抽血，严格执行主升浪止盈纪律。",
            "logic": f"{title} 将加速场内资金在科技成长与避险阵营间的迁移，以机构大资金合力主线为唯一博弈方向。"
        },
        "hanshu": {
            "title_verdict": f"【生命线防守反击】大事件前绝不追冲高发力阳线，缩量踩线再荡秋千做差价。",
            "kit": "① 牢记韩叔铁律：大事件前不追涨，冲高必有获利砸盘；② 标记标的 20MA/30MA 绝活生命线；③ 缩量回踩生命线分批低吸，反弹即踢出做差价，把成本做为零。",
            "logic": "重大事件前夕资金分歧剧烈（荡秋千行情），只有在缩量回踩生命线的恐慌低点介入，安全边际才最高。"
        },
        "ai-alpha": {
            "title_verdict": f"【多因子量化扫描】事件催化需转化为量化动量突破方可触发 BUY 信号。",
            "kit": "① 模型动态监测标的 5MA>10MA>20MA 多头排列扩散度与成交量比；② 若突破 20 日高点触发 BUY，挂载 ATR(14)×2.2 移动追踪止盈；③ 跌破 -4.5% 刚性止损。",
            "logic": "纯因子驱动框架无主观情绪预期，仅当事件驱动转化为真实价量动量共振时执行全自动买卖。"
        },
        "anti-demo": {
            "title_verdict": f"【情绪过热防利好兑现】警惕 {target_str} 出现一致性亢奋抢跑与高位派发。",
            "kit": "① 监控题材连板高度与换手率；② 连板>3 或换手>25% 立即触发『禁买黑名单』；③ 遇散户一致性沸腾提示逢高分批减仓锁定胜果。",
            "logic": "金融市场经典陷阱在于『买预期、卖事实』，当全网情绪达到极值沸点时，主力兑现意愿达到峰值。"
        }
    }


def _build_fallback_playbook(event):
    """当 LLM 离线时的结构化降级剧本"""
    return {
        "priced_in_score": 50,
        "pre_observation": [
            "关注事件日前 3 个交易日主力资金净流入与量能变化",
            "核验标的估值水位与均线系统是否处于良性支撑带"
        ],
        "upside_plan": "若催化超预期且板块放量突破，可顺应主升浪轻仓介入，严格挂载 ATR 追踪止盈，不盲目满仓。",
        "downside_plan": "若不及预期或借利好砸盘，破位 20MA 或亏损触及 -3.5% 刚性无条件止损，绝不补仓死扛。",
        "neutral_plan": "若符合预期且走势平淡，以生命线高抛低吸荡秋千做 T 为主，控制总仓位在 50% 以内。"
    }


def generate_event_playbook(event, force_refresh=False):
    """
    针对单一前瞻事件生成结构化剧本与六博主深度推演。
    返回 dict: {
      "playbook": {...},
      "blogger_deductions": {...},
      "engine": "gemini" / "curated"
    }
    """
    ev_id = event.get('id', '')
    ev_date = event.get('event_date') or event.get('date') or ''
    ev_symbol = event.get('symbol', '')
    ev_title = event.get('title', '')
    ev_type = event.get('event_type', '')

    cache_key = f"pb_{ev_id}_{ev_type}_{ev_symbol}_{ev_date}"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached and cached.get('playbook') and cached.get('blogger_deductions'):
            return {**cached, 'engine': cached.get('engine', 'gemini')}

    if not rc.LLM_API_KEY:
        fb_ded = _build_fallback_deductions(event)
        fb_pb = _build_fallback_playbook(event)
        return {
            "playbook": fb_pb,
            "blogger_deductions": fb_ded,
            "engine": "curated"
        }

    # 构造 Prompt 调 LLM
    try:
        from analysis.llm_analyst import _call_llm, PERSONA_CARDS, _strip_code_fence
        user_prompt = (
            f"[前瞻事件基本信息]\n"
            f"事件ID: {ev_id}\n"
            f"事件标题: {ev_title}\n"
            f"事件日期: {ev_date}\n"
            f"关联标的: {event.get('name', '')} ({ev_symbol})\n"
            f"事件分类: {event.get('category', ev_type)}\n"
            f"事件背景与分歧: {event.get('background', '')} {event.get('core_controversy', '')}\n\n"
            f"--- 六位博主方法论与红线 ---\n"
            + '\n'.join(f"### {bid} | {PERSONA_CARDS[bid]['name']}\n{PERSONA_CARDS[bid]['card']}"
                        for bid in BLOGGER_IDS)
        )
        parsed = _call_llm(_PLAYBOOK_SYSTEM_PROMPT + '\n\n' + user_prompt)
        pb = parsed.get('playbook', {})
        deductions = parsed.get('blogger_deductions', {})

        # 校验必备字段
        valid_ded = {}
        for bid in BLOGGER_IDS:
            d = deductions.get(bid)
            if isinstance(d, dict) and all(k in d for k in ('title_verdict', 'kit', 'logic')):
                valid_ded[bid] = {
                    'title_verdict': str(d['title_verdict'])[:60],
                    'kit': str(d['kit'])[:200],
                    'logic': str(d['logic'])[:150],
                }

        if len(valid_ded) >= 5 and pb.get('upside_plan') and pb.get('downside_plan'):
            clean_pb = {
                'priced_in_score': max(0, min(100, int(pb.get('priced_in_score', 50)))),
                'pre_observation': pb.get('pre_observation', [])[:4],
                'upside_plan': str(pb.get('upside_plan', ''))[:200],
                'downside_plan': str(pb.get('downside_plan', ''))[:200],
                'neutral_plan': str(pb.get('neutral_plan', ''))[:200],
            }
            res = {
                "playbook": clean_pb,
                "blogger_deductions": valid_ded,
                "engine": rc.LLM_MODEL or "gemini"
            }
            _cache_set(cache_key, res)
            return res
    except Exception as e:
        print(f"[playbook] {ev_id} LLM 剧本生成异常: {type(e).__name__}: {str(e)[:80]}")

    # 降级
    fb_ded = _build_fallback_deductions(event)
    fb_pb = _build_fallback_playbook(event)
    return {
        "playbook": fb_pb,
        "blogger_deductions": fb_ded,
        "engine": "curated"
    }


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    demo_ev = {
        'id': 'FWD-DEMO',
        'title': '中芯国际 2026Q3 业绩预告与 14nm 订单放量',
        'event_date': '2026-09-15',
        'symbol': '688981',
        'name': '中芯国际',
        'category': '半导体 / 财报披露',
        'background': '公司预计三季度先进制程产能利用率达 95% 以上，在手订单充沛。',
        'core_controversy': '市场关注业绩超预期能否打破半导体板块周期筑底格局。'
    }
    pb_res = generate_event_playbook(demo_ev, force_refresh=True)
    print(f"=== 剧本引擎测试 (engine={pb_res['engine']}) ===")
    print("Playbook:", json.dumps(pb_res['playbook'], ensure_ascii=False, indent=2))
    print("\n韩叔推演:", json.dumps(pb_res['blogger_deductions']['hanshu'], ensure_ascii=False, indent=2))
