# -*- coding: utf-8 -*-
"""
scripts/analysis/llm_analyst.py
真 LLM 研判层（阶段一改造核心 · 治"死板重复"）

替换旧 deduce_blogger_perspectives 的 if-elif 模板引擎：
- 一次调用让模型以六位博主各自【蒸馏契约】为约束，对同一条真实快讯
  产出六份差异化观点（严格 JSON）；
- 防幻觉锚定：每条观点必须引用新闻原文片段，服务端校验引用确实存在于原文，
  校验不过打回重生成（≤2次），仍不过则该博主观点如实标记不可用；
- 反思闭环：prompt 中注入该博主近期真实盈亏教训（reflection_memory）；
- 缓存：同一新闻（title+content哈希）不重复调用，盘中5分钟节奏零浪费。

旧模板函数保留在 auto_update_hotspots._deduce_blogger_perspectives_template_legacy，
仅供评测集做新旧输出对比，不再进入生产链路。
"""

import os
import sys
import json
import re
import hashlib

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

import runtime_config as rc

BLOGGER_IDS = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']
VALID_STANCES = {'BUY', 'REDUCE', 'WAIT', 'RISK'}

# 六位博主契约精要卡：蒸馏自 skills/bloggers/*/SKILL.md 五模块契约 +
# config/system_params.json 策略参数。保持紧凑是为控制 token 成本。
PERSONA_CARDS = {
    'hanshu': {
        'name': '韩叔(趣聊财经)·绝活生命线低吸',
        'card': ('核心方法:20MA/30MA"绝活生命线"缩量回踩低吸,反弹4%~7%坚决踢出做差价;'
                 '大事件前绝不追冲高发力阳线。铁律:不追发力阳线;跌破60MA且3日收不回必须止损。'
                 '红线:绝不追高,只在别人恐慌的踩线点先人一步。仓位:底仓50%+做T仓25%+现金25%。'),
    },
    'yintwentao': {
        'name': '殷文涛(梵爹)·三大阵营博弈',
        'card': ('核心方法:把资金分为"科技先锋之矛/老登避险停机坪/游资情绪"三大阵营,'
                 '跟踪增量资金抱团方向与"光/芯时空差轮动"(CPO冲高滞涨→芯片低位承接)。'
                 '铁律:两市成交额跌破2.2万亿必须减仓观望;回避四大行等老登防守股。'),
    },
    'liao-xiaoai': {
        'name': '廖小爱·中国特色价值投资',
        'card': ('核心方法:只买国家战略方向+深厚护城河+合理估值的龙头,3-3-1仓位模型'
                 '(30%底仓/30%深蹲黄金坑加仓/10%机动/30%场外无限子弹),波浪式前进中分批布局,穿越周期。'
                 '铁律:绝不用杠杆;纯概念无业绩题材股一律剔除;PE>45或市值<80亿的回避。'),
    },
    'cool': {
        'name': '酷儿(Cool)·假跌破绝杀',
        'card': ('核心方法:80%时间是无序震荡垃圾时间→空仓;只打一种形态——价格跌破20MA诱空后'
                 '≤3根K线内放量长阳收回支撑位(流动性扫割"假跌破"),尾盘介入;止损=假摔最低点下方1.5%,'
                 '盈亏比<3:1不做。铁律:禁止盘前利好冲动交易;禁止真破位后死扛补仓。'),
    },
    'ai-alpha': {
        'name': 'AI-Alpha·多因子量化智脑',
        'card': ('核心方法:纯因子驱动无叙事。触发条件全部满足才BUY:①5MA>10MA>20MA多头排列扩散'
                 '②收盘突破20日新高③量比>1.2④流动性和基本面过滤通过。持仓用ATR(14)×2.2移动追踪止盈,'
                 '-4.5%刚性止损。不给感性判断,信号之外只有WAIT。'),
    },
    'anti-demo': {
        'name': '反指哨兵·情绪逆向熔断',
        'card': ('角色是全市场情绪反向指标:监测连板高度/换手率异动/散户一致性。'
                 '3+连板或单日换手>25%或BIAS20>18%→标的进禁买黑名单并提示止盈(情绪沸点=风险);'
                 '散户恐慌割肉高潮→提示逆向冰点机会。默认输出RISK/WAIT视角而非顺势买入。'),
    },
}

STANCE_ICON = {'BUY': '🔴看多/买', 'REDUCE': '🟡减/兑现', 'WAIT': '⚪观望',
               'RISK': '🟠风险警示'}

_SYSTEM_PROMPT = (
    '你是A股多流派投研框架的主持分析官。给出一条真实财经快讯，请严格按用户提供的六位分析师'
    '各自方法论与纪律红线，分别独立给出对这条新闻的研判。要求：\n'
    '1. 六人观点必须体现各自框架差异,禁止雷同套话；每人引用新闻中真实的文字片段作为依据；\n'
    '2. 严格JSON输出,结构:{"views":[{"blogger":"ID","headline":"不超过26字观点标题",'
    '"stance":"BUY|REDUCE|WAIT|RISK","confidence":整数1-99,'
    '"logic":"不超过110字核心理由","quote_excerpt":"从新闻原文原样复制的连续片段"}],'
    '"factors":{"event_type":"EARN_SURPRISE|POLICY|MACRO|SECTOR|'
    'COMPANY_NEWS|FUND_FLOW|SENTIMENT|REGULATION|OTHER",'
    '"primary_symbol":"6位代码或空",'
    '"impact_side":"POSITIVE|NEGATIVE|NEUTRAL",'
    '"sentiment":-1.0到+1.0之间的一位小数,'
    '"half_life_h":1/3/24/72/168,'
    '"confidence":整数1-99}}\n'
    '3. quote_excerpt 必须逐字来自新闻原文(选最具依据性的10~40字)，不许改写；\n'
    '4. 与己方框架无关的新闻,观点应为WAIT并说明为何不触发本框架；'
    'factors中情绪分与半衰期必须基于新闻实际可被价格定价的速度(急文1-3h，'
    '政策/数据公告24-72h，宏观周期168h+)；\n'
    '只输出JSON本体。')


def _norm(s):
    return re.sub(r'\s+', '', str(s or ''))


def _cache_get(key):
    cache = rc._load_json(rc.LLM_CACHE_PATH, {})
    return cache.get(key)


def _cache_set(key, value):
    cache = rc._load_json(rc.LLM_CACHE_PATH, {})
    cache[key] = value
    if len(cache) > 600:
        for k in list(cache.keys())[:200]:
            cache.pop(k, None)
    rc._save_json(rc.LLM_CACHE_PATH, cache)


def _build_user_prompt(news_item, market_snapshot=None):
    title = news_item.get('title', '')
    content = news_item.get('content') or news_item.get('summary', '')
    cat = news_item.get('category', '')
    source = news_item.get('source', '')
    t = news_item.get('time', '')
    mk = ''
    if market_snapshot:
        mk = f'\n[市场快照参考]{json.dumps(market_snapshot, ensure_ascii=False)[:400]}'

    persona_block = []
    lessons_by = {}
    try:
        from analysis.reflection_memory import get_lessons_for_prompt
        for bid in BLOGGER_IDS:
            lessons_by[bid] = get_lessons_for_prompt(bid)
    except Exception:
        pass

    for bid in BLOGGER_IDS:
        p = PERSONA_CARDS[bid]
        lines = [f'### {bid} | {p["name"]}', p['card']]
        ls = lessons_by.get(bid) or []
        if ls:
            lines.append('近期真实战绩教训(必须在研判语气中体现):')
            lines += [f'- {x}' for x in ls[:4]]
        persona_block.append('\n'.join(lines))

    return (f'[待研判快讯] 时间:{t} 来源:{source} 分类:{cat}\n'
            f'标题:{title}\n正文:{content}{mk}\n\n--- 六位分析师契约卡 ---\n'
            + '\n'.join(persona_block))


def _validate_views(parsed, news_text_norm):
    """校验返回结构；返回 (clean_dict, errors_list)。"""
    views, errors = {}, []
    seen_blogger = set()
    items = parsed.get('views') if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {}, ['返回缺少 views 数组']
    for v in items:
        bid = str(v.get('blogger', '')).strip()
        if bid not in BLOGGER_IDS:
            continue
        if bid in seen_blogger:
            continue
        stance = str(v.get('stance', '')).strip().upper()
        quote = str(v.get('quote_excerpt', '')).strip()
        if stance not in VALID_STANCES:
            errors.append(f'{bid}: 非法stance={stance}')
            continue
        # 幻觉锚定校验：引用片段必须存在于原文
        if _norm(quote) and _norm(quote) in news_text_norm:
            anchor_ok = True
        else:
            anchor_ok = False
            errors.append(f'{bid}: 引用片段未命中原文(疑似幻觉)')
            continue
        conf = v.get('confidence')
        try:
            conf = max(1, min(99, int(conf)))
        except Exception:
            conf = 50
        headline = re.sub(r'\s+', ' ', str(v.get('headline', '')))[:40]
        logic = re.sub(r'\s+', ' ', str(v.get('logic', '')))[:150]
        views[bid] = {
            'headline': headline, 'stance': stance, 'confidence': conf,
            'logic': logic, 'quote_excerpt': quote[:60], 'anchor_ok': True,
        }
        seen_blogger.add(bid)
    missing = [b for b in BLOGGER_IDS if b not in views]
    for m in missing:
        errors.append(f'{m}: 缺失')
    return views, errors


def _strip_code_fence(text):
    """Gemini 等模型常把 JSON 包在 ```json ... ``` 标记里，剥掉便于 json.loads。
    不剥/不匹配则原样返回（让上层 JSONDecodeError 走标准提示）。"""
    s = text.strip()
    if s.startswith('```'):
        # 去掉开头的 ```json 或 ``` 行
        first_nl = s.find('\n')
        s = s[first_nl + 1:] if first_nl != -1 else s[3:]
        # 去掉结尾的 ``` 行
        if s.rstrip().endswith('```'):
            cut = s.rfind('```')
            s = s[:cut].rstrip()
    return s


def _call_llm(user_prompt, system_prompt=None, temperature=0.65, max_tokens=1600):
    from openai import OpenAI
    client = OpenAI(api_key=rc.LLM_API_KEY, base_url=rc.LLM_BASE_URL,
                    timeout=75, max_retries=1)
    resp = client.chat.completions.create(
        model=rc.LLM_MODEL,
        messages=[{'role': 'system', 'content': system_prompt or _SYSTEM_PROMPT},
                  {'role': 'user', 'content': user_prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={'type': 'json_object'},
    )
    content = _strip_code_fence(resp.choices[0].message.content or '')
    parsed = json.loads(content)
    # 留痕：把客户端侧 model/finish_reason 等元信息注入，调用方 meta.model 才能正确显示
    try:
        parsed['__meta'] = {
            'model': getattr(resp, 'model', None) or rc.LLM_MODEL,
            'finish_reason': resp.choices[0].finish_reason,
            'usage_total': getattr(resp.usage, 'total_tokens', None) if resp.usage else None,
        }
    except Exception:
        pass
    return parsed


def analyze_news(news_item, market_snapshot=None, force_refresh=False):
    """
    主入口：输入一条新闻 dict → 返回与旧接口兼容的 {blogger_id: "展示文本"}，
    并在结果里带 '_meta' 结构化数据与 '_engine' 标识。
    供 pipeline / confirm_page / 评测脚本共用。
    """
    title = news_item.get('title', '')
    content = news_item.get('content') or news_item.get('summary', '')

    if not rc.LLM_API_KEY:
        return {bid: '（AI研判离线：.env 未配置 DEEPSEEK_API_KEY。模板引擎已退役，'
                     '本栏不再生成任何伪装观点。）' for bid in BLOGGER_IDS}

    key = hashlib.sha256((title + content).encode('utf-8')).hexdigest()[:24]
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return _format(cached)

    user_prompt = _build_user_prompt(news_item, market_snapshot)
    text_norm = _norm(title + content)

    last_errors = []
    result = None
    for attempt in range(3):                       # 初次 + 最多2次打回重生成
        parsed = _call_llm(user_prompt + (
            '\n\n上次输出被驳回的原因,务必修正:\n- ' + '\n- '.join(last_errors))
            if last_errors else user_prompt)
        views, errs = _validate_views(parsed, text_norm)
        missing = [e for e in errs if e.endswith('缺失')]
        bad_anchor = [e for e in errs if not e.endswith('缺失')]
        if not bad_anchor and len(missing) <= 2 and views:
            # 小缺失接受，标记不可用即可；引用幻觉则必须重试
            result = views
            break
        last_errors = errs
    else:
        result = views or None

    if not result:
        return {bid: '（本轮AI研判未能通过引用真实性校验，为诚实起见弃用本轮观点。）'
                for bid in BLOGGER_IDS}

    payload = {'views': result, 'model': rc.LLM_MODEL,
               'ts': rc.now_str(), 'rejected': len(last_errors),
               'factors': _extract_factors_from_raw(parsed),
               '_client_meta': parsed.get('__meta') or {}}
    _cache_set(key, payload)
    persist_factor(news_item, payload)        # 因子入 SQLite（失败不抛）
    return _format(payload)


def _extract_factors_from_raw(parsed):
    """从LLM原始返回中稳健提取factors：None/dict/字段缺失都安全降级。"""
    if not isinstance(parsed, dict):
        return {}
    f = parsed.get('factors')
    if not isinstance(f, dict):
        return {}
    return f


def _format(payload):
    """把结构化结果转成旧管线兼容的 {blogger_id: 文本} 形式，并暴露 factors。"""
    out = {}
    cm = payload.get('_client_meta') or {}
    meta = {'model': cm.get('model') or payload.get('model'),
            'ts': payload.get('ts'),
            'views': {},
            'factors': payload.get('factors') if isinstance(payload.get('factors'), dict) else {},
            'usage': cm.get('usage_total'),
            'finish_reason': cm.get('finish_reason')}
    for bid in BLOGGER_IDS:
        v = payload['views'].get(bid)
        if not v:
            out[bid] = '（该条新闻未产出本框架有效观点/缺失，弃用而非凑数。）'
            continue
        icon = STANCE_ICON.get(v['stance'], v['stance'])
        out[bid] = (f'【{icon}·{v["confidence"]}】{v["headline"]}。{v["logic"]}'
                    f'｜依据:"{v["quote_excerpt"]}"')
        meta['views'][bid] = v
    out['_meta'] = meta
    return out


def persist_factor(news_item, payload):
    """把LLM产出的结构化因子入SQLite（舆情alpha原料）。失败不抛、不影响主流程。"""
    f = (payload.get('factors') or {})
    if not f:
        return False
    try:
        from datasource import db as hdb
        hdb.add_event_factor_columns()            # 幂等建表
        title = news_item.get('title') or ''
        content = (news_item.get('content') or '')[:200]
        # 标识：发布时间 > 新闻id > 标题+时间 哈希
        import hashlib
        seed = f"{title}|{news_item.get('time', '')}|{content[:80]}"
        key = hashlib.sha1(seed.encode('utf-8')).hexdigest()[:24]
        # 阶段五A0：绑定强度/注意力/分歧度随因子入库（缺失时诚实留空）
        stance_num = {'BUY': 1.0, 'REDUCE': -1.0, 'WAIT': 0.0, 'RISK': -1.0}
        _vals = [stance_num.get((v or {}).get('stance')) for v in (payload.get('views') or [])
                 if isinstance(v, dict)]
        _vals = [v for v in _vals if v is not None]
        _spread = None
        if len(_vals) >= 2:
            _mean = sum(_vals) / len(_vals)
            _spread = round((sum((x - _mean) ** 2 for x in _vals) / len(_vals)) ** 0.5, 3)
        hdb.upsert_event_factor(key, {
            'title': title,
            'published_at': news_item.get('time') or payload.get('ts') or '',
            **f,
            'binding_strength': news_item.get('binding_strength'),
            'attention_count': news_item.get('attention_count'),
            'sentiment_spread': _spread,
        }, source='llm', model=payload.get('model') or '')
        return True
    except Exception as ex:
        print(f'[LLM研判] 因子入SQLite失败: {type(ex).__name__}: {str(ex)[:80]}')
        return False


# 在 analyze_news 末尾接一下持久化钩子（保持兼容：失败不影响返回）


# ---------------------------------------------------------------- 前瞻师事件推演重生成

_FWD_SYSTEM_PROMPT = (
    '你是A股多流派投研框架的主持分析官。给定一个【前瞻事件】的议题描述与市场分歧点，'
    '请让六位分析师按各自方法论给出该事件的应对预案。要求：\n'
    '1. 六人观点必须体现各自框架差异，禁止雷同套话；\n'
    '2. 严格JSON输出: {"deductions": {"博主ID": {"title_verdict": "不超过30字观点标题",'
    '"kit": "不超过120字的3条操作要点，用①②③分隔", "logic": "不超过90字推演逻辑"}}}\n'
    '3. 博主ID必须为: hanshu/yintwentao/liao-xiaoai/cool/ai-alpha/anti-demo 之一；\n'
    '4. 这是前瞻预案而非实盘信号：可以谈条件与观察点，但不得编造具体价格或已发生事实。\n'
    '只输出JSON本体。')


def regenerate_forward_deductions(events, force_refresh=False):
    """
    对前瞻事件生成6博主推演预案（一次调用一个事件，按 事件id+日期 缓存）。
    返回 {event_id: {blogger_id: {title_verdict, kit, logic}}}；
    任一事件失败则该事件返回 None（调用方保留人工预案）。
    """
    if not rc.LLM_API_KEY:
        return None
    result = {}
    for ev in events:
        key = f"fwd_{ev['id']}_{ev.get('date', '')}"
        if not force_refresh:
            cached = _cache_get(key)
            if cached:
                result[ev['id']] = cached.get('deductions')
                continue
        user = (f'[前瞻事件] 标题:{ev["title"]}\n背景:{ev.get("background", "")}\n'
                f'市场核心分歧:{ev.get("core_controversy", "")}\n'
                f'关注主线:{ev.get("core_tracks", "")}\n\n'
                f'--- 六位分析师契约卡 ---\n'
                + '\n'.join(f'### {bid} | {PERSONA_CARDS[bid]["name"]}\n{PERSONA_CARDS[bid]["card"]}'
                            for bid in BLOGGER_IDS))
        try:
            parsed = _call_llm(user)
            deductions = (parsed or {}).get('deductions', {})
            # 校验结构：六位都在且三键齐全
            valid = {}
            for bid in BLOGGER_IDS:
                d = deductions.get(bid)
                if isinstance(d, dict) and all(k in d for k in ('title_verdict', 'kit', 'logic')):
                    valid[bid] = {
                        'title_verdict': str(d['title_verdict'])[:60],
                        'kit': str(d['kit'])[:200],
                        'logic': str(d['logic'])[:150],
                    }
            if len(valid) >= 5:
                result[ev['id']] = valid
                _cache_set(key, {'deductions': valid})
            else:
                result[ev['id']] = None
        except Exception as e:
            print(f'[前瞻师] {ev["id"]} LLM推演失败: {type(e).__name__} {str(e)[:80]}')
            result[ev['id']] = None
    return result


# ============================================================================
# 阶段四新增：批量相关性评分（治"新闻太多强制评论")
# ============================================================================
import datetime as _dt

def _content_age_days(news):
    """从新闻 time 字段解析发布距今天数，用于旧闻降级。"""
    if not news:
        return None
    t = (news.get('time') or news.get('published_at') or news.get('ts') or '').strip()
    if not t:
        return None
    # 兼容多种格式与尾部长度
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = _dt.datetime.strptime(t[:19], fmt)
            return (_dt.datetime.now() - dt).days
        except ValueError:
            continue
        except Exception:
            return None
    return None


def _stale_news_downgrade(rule_score, news):
    """旧闻降级：事件发布 ≥3 天且规则分 <75 → 直接落 MEDIUM，不交 LLM 浪费预算。"""
    age = _content_age_days(news)
    if age is None:
        return rule_score, None
    if age >= 3 and rule_score < 75:
        return 50, f'旧闻降级(发布{age}天)'
    return rule_score, None


def batch_relevance_scan(news_list, force_refresh=False):
    """
    批量相关性评分：给灰区（v4.1 升级为"送什么由 news_officer 决定"）的新闻一次性让 LLM 读正文裁决 tier。
    输入: news_list 每条须含 id/title/summary(>=200字正文)/time/rule_score
    输出: dict[id] = {score, tier, reason, llm_used: bool}
    失败降级: LLM 不可用 → 直接用 rule_score 推断 tier, llm_used=False。
    缓存: rl_{news_id} 与现有 pb_/研判缓存同库。
    """
    out = {}
    if not news_list:
        return out

    # 1) 先做旧闻降级（不动 LLM 预算）
    pending = []
    for n in news_list:
        nid = n.get('id') or n.get('news_id') or n.get('title', '')[:30]
        rs = int(n.get('rule_score') or 0)
        rs, note = _stale_news_downgrade(rs, n)
        if note:
            out[nid] = {'score': rs, 'tier': 'MEDIUM', 'reason': note, 'llm_used': False}
            continue
        # v4.1 R2：news_officer 已经按 score>=15 或 heat_tier 过滤了送评名单；
        # 这里不再做"30-70 免评"硬切，而是全部交给 LLM。LLM 仍按 prompt 出 STRONG/MEDIUM/WEAK。
        pending.append((nid, n, rs))

    if not pending:
        return out

    # 2) 命中 LLM 调用的：拼 prompt（含原文摘要 ≥200 字 + 时间戳 + 热度上下文）
    # v4.1 R3 双向锚例（用户 2026-09-01 全部实测案例入库）
    sys_prompt = (
        '你是 A 股相关性评分员，遵循以下评分准则：\n'
        '- ≥75 STRONG（强相关，逐条深度研判候选）\n'
        '- 45-74 MEDIUM（中等，常规展示，无博主评论）\n'
        '- <45 WEAK（弱相关，存档不上前端）\n'
        '\n【正向锚例（应给高分）】\n'
        '- A股板块/概念群体行情（涨停潮/多股涨停/概念暴跌/批量跌停）→ ≥60；\n'
        '- 地缘政治/贸易战/关税/制裁/反制/军事冲突等宏观冲击（无论用词是否含"反击/回应"）→ ≥60，'
        '涉及 A股直接受损/受益链 → ≥75；\n'
        '- 国内政策/产业事件点名 A股标的或板块 → 按事件硬度 45~90。\n'
        '\n【反向锚例（应给低分）】\n'
        '- 海外政要/投资人物的观点交锋、八卦互怼（开撕/反击/回怼），'
        '未涉及 A股标的、A股政策、关税制裁等实质政策动作 → ≤30；\n'
        '- 旧闻回顾/盘点（>3 天无新增信息）→ ≤40。\n'
        '\n【特别规则】\n'
        '标题点名具体公司/事件主体 + 硬数据事件（订单/财报/政策数字）→ 优先 STRONG；\n'
        '若事件发布已 >3 天且无新增信息（"回顾/盘点/影响"类），降为 MEDIUM。'
    )
    items_payload = []
    for nid, n, rs in pending:
        summary = (n.get('summary') or n.get('content') or '')[:300]
        items_payload.append({
            'id': nid,
            'rule_score': rs,
            'title': (n.get('title') or '')[:80],
            'summary': summary,
            'time': n.get('time') or n.get('published_at') or '',
            # v4.1 R0：透传热度上下文（捕获侧领先信号）
            'heat_tier': n.get('heat_tier', ''),
            'co_source_n': n.get('co_source_n', 1),
            'kw_hits': n.get('kw_hits', []),
        })
    user_prompt = (
        '请对以下新闻逐条评分，输出 JSON：\n'
        '{"items": [{"id": "...", "score": 0-100, "tier": "STRONG|MEDIUM|WEAK", "reason": "≤20字"}]}\n'
        '【待评新闻】\n' + json.dumps(items_payload, ensure_ascii=False)
    )

    cache_key = 'rl_' + hashlib.md5(user_prompt.encode('utf-8')).hexdigest()[:12]
    cached = _cache_get(cache_key) if not force_refresh else None
    parsed = None
    if cached and isinstance(cached, dict):
        parsed = cached.get('payload')
    if parsed is None:
        try:
            # v4.1 R2：max_tokens 3000（v4.1 送评上限 30 条 × 每条 ~60 token 输出）
            parsed = _call_llm(user_prompt, system_prompt=sys_prompt,
                               temperature=0.2, max_tokens=3000)
            _cache_set(cache_key, {'payload': parsed, 'ts': rc.now_str()})
        except Exception as e:
            # 失败降级：LLM 不可用 → 用 rule_score 推断 tier
            for nid, _, rs in pending:
                tier = 'STRONG' if rs >= 75 else 'MEDIUM' if rs >= 45 else 'WEAK'
                out[nid] = {'score': rs, 'tier': tier, 'reason': f'LLM不可用降级({type(e).__name__})', 'llm_used': False}
            return out

    # 3) 解析 LLM 输出
    items = parsed.get('items') if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        for nid, _, rs in pending:
            tier = 'STRONG' if rs >= 75 else 'MEDIUM' if rs >= 45 else 'WEAK'
            out[nid] = {'score': rs, 'tier': tier, 'reason': 'LLM输出格式错误降级', 'llm_used': False}
        return out
    for it in items:
        nid = it.get('id') or ''
        s = int(it.get('score') or 50)
        t = it.get('tier') or ('STRONG' if s >= 75 else 'MEDIUM' if s >= 45 else 'WEAK')
        if t not in ('STRONG', 'MEDIUM', 'WEAK'):
            t = 'STRONG' if s >= 75 else 'MEDIUM' if s >= 45 else 'WEAK'
        out[nid] = {'score': s, 'tier': t, 'reason': it.get('reason', '')[:30], 'llm_used': True}
    return out


# ============================================================================
# 阶段五·任务C：多空辩论（反方风控员，TradingAgents 机制移植）
# ============================================================================
_DEBATE_SYS = (
    '你是A股买入决策的反方风控员。给你一个候选买入的决策快照，你的职责是专门反对这笔买入：'
    '必须找出最多3条实质性的反对理由（技术位差/拥挤度高/事件已定价/逻辑脆弱/流动性差等），'
    '并对该买入的信心给出0-30的罚分（0=无法反驳，30=强烈反对）。严格JSON输出。'
)


def debate_quota_state():
    today = rc.now_str()[:10]
    st = rc._load_json(os.path.join(rc.STATE_DIR, 'debate_quota.json'), {}) or {}
    if st.get('date') != today:
        st = {'date': today, 'used': 0}
    return st


def bear_debate(snapshot):
    """
    反方辩论：对候选买入找3条反对理由+信心罚分。
    每日限3次（成本护栏）；LLM不可用时诚实返回罚分0+缺席标注（不拦单）。
    """
    quota_path = os.path.join(rc.STATE_DIR, 'debate_quota.json')
    st = debate_quota_state()
    if st.get('used', 0) >= 3:
        return {'objections': [], 'confidence_penalty': 0,
                'debate_absent': True, 'note': '辩论配额用尽(3/日)'}
    user_prompt = ('请对以下候选买入做反方风控分析，输出JSON：\n'
                   '{"objections": ["理由1", "理由2", "理由3"], '
                   '"confidence_penalty": 0-30}\n'
                   '【决策快照】\n' + json.dumps(snapshot, ensure_ascii=False, default=str)[:1500])
    cache_key = 'debate_' + hashlib.md5(user_prompt.encode('utf-8')).hexdigest()[:12]
    cached = _cache_get(cache_key)
    parsed = None
    if cached and isinstance(cached, dict):
        parsed = cached.get('payload')
    if parsed is None:
        try:
            parsed = _call_llm(user_prompt, system_prompt=_DEBATE_SYS,
                               temperature=0.3, max_tokens=500)
            _cache_set(cache_key, {'payload': parsed, 'ts': rc.now_str()})
        except Exception as e:
            return {'objections': [], 'confidence_penalty': 0,
                    'debate_absent': True, 'note': f'LLM不可用({type(e).__name__})'}
    obj = [str(o)[:60] for o in (parsed.get('objections') or [])][:3]
    try:
        pen = max(0, min(30, float(parsed.get('confidence_penalty') or 0)))
    except Exception:
        pen = 0
    st['used'] = st.get('used', 0) + 1
    rc._save_json(quota_path, st)
    return {'objections': obj, 'confidence_penalty': pen, 'debate_absent': False}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='LLM研判自测')
    ap.add_argument('--text', default='国家统计局发布数据：全国早稻总产量2817万吨，比上年增产6.8万吨，国际知名度提升明显。',
                    help='用自定义文本测试(含历史上的"国际知名度"陷阱)')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    demo = {'title': args.text[:80], 'content': args.text,
            'time': rc.now_str(), 'category': '测试', 'source': 'CLI测试'}
    print(json.dumps(analyze_news(demo), ensure_ascii=False, indent=2))
