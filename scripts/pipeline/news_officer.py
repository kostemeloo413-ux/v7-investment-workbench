# -*- coding: utf-8 -*-
"""
scripts/pipeline/news_officer.py
通政司 · News Officer（总新闻官）

职责：定时检索全市场情报（新闻快讯 / 大盘行情 / 指数分时），
     并调用 6 位博主认知模型对每条新闻做多视角解构。

产出：data/pipeline/news_brief.json（通政奏报）
  {
    "schema_version": "1.0",
    "fetched_at": "...",
    "market_quotes": {...},       // 大盘行情快照
    "index_intraday": {...},      // 指数分时（可为空）
    "news": [
      {
        "id": "HOT-01",
        "title/summary/content/source/url/time/category",
        "subj_type": "SECTOR|MACRO_DATA|...",
        "entity": "核心实体",
        "themes": ["半导体", "存储芯片"],   // 题材标签（从 category 拆分）
        "blogger_views": {"hanshu": "...", "cool": "...", ...}
      }
    ]
  }

阶段 2 实现：复用 auto_update_hotspots.py 的成熟检索与解构函数，
不重写业务逻辑，只做「独立落盘 + 题材标签结构化」。
"""

import os
import sys
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(BASE_DIR, 'scripts')
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, BASE_DIR)

import runtime_config as _rc   # noqa: E402 （LLM成本护栏等统一配置）

BRIEF_PATH = os.path.join(BASE_DIR, 'data', 'pipeline', 'news_brief.json')
SCHEMA_VERSION = '1.1'
_BLOGGER_IDS = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']


def _split_themes(category):
    """把题材分类字符串拆成标签数组：'半导体 / 存储芯片' → ['半导体', '存储芯片']。
    用户 2026-08-29：补 `·` 分隔符（新浪快讯常以"财经综合·市场观察"这种点分形式给两个分类）。"""
    import re
    if not category:
        return []
    parts = re.split(r'[/／|·、,，\s]+', str(category))
    return [p.strip() for p in parts if p.strip()]


def _title_similarity(a, b):
    """标题字符二元组重叠系数（交集/较短方）：判定"短稿是长稿的摘要"型同事件重复。
    用重叠系数而非 Jaccard——长短悬殊时 Jaccard 会被长稿稀释（欧洲央行长短两稿实测仅0.08）。"""
    import re as _re
    clean = lambda s: _re.sub(r'[^一-鿿A-Za-z0-9]', '', s or '')
    a, b = clean(a), clean(b)
    if len(a) < 6 or len(b) < 6:
        return 0.0
    ba = {a[i:i + 2] for i in range(len(a) - 1)}
    bb = {b[i:i + 2] for i in range(len(b) - 1)}
    inter = len(ba & bb)
    shorter = min(len(ba), len(bb))
    if shorter == 0 or inter < 4:   # 绝对交集下限：防短标题偶合误判
        return 0.0
    return inter / shorter


def gather_intelligence(quiet=False):
    """
    通政司主流程：检索新闻 + 大盘行情 + 指数分时，组装通政奏报。

    返回 (brief_dict, brief_path)；任何单点失败不阻断整体（try/except 兜底）。
    """
    import auto_update_hotspots as au

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 1. 大盘行情快照
    market_quotes = {}
    try:
        market_quotes = au.fetch_market_quotes()
    except Exception as e:
        if not quiet:
            print(f'[通政司] 行情检索提示：{e}')

    # 2. 指数分时（8 指数）
    index_intraday = {}
    try:
        index_intraday = au.fetch_index_intraday()
    except Exception as e:
        if not quiet:
            print(f'[通政司] 分时检索提示：{e}')

    # 3. 新闻快讯 + 门下省事前拦截 + 题材标签 + 6 博主解构
    news = []
    vetted_count = 0
    vetoed_count = 0
    try:
        import menxia
        import xingbu
    except ImportError:
        menxia = xingbu = None

    brief_news_tiers = {'strong': [], 'medium': [], 'weak_count': 0, 'dup_count': 0, 'llm_deep_used': 0}
    strong_ids, medium_ids, weak_count = [], [], 0
    llm_used = 0
    try:
        raw_news = au.fetch_live_flash_news()
        # v4.1 R0：刷新热度缓存（捕获侧领先信号：共现/编辑标记/百度热搜）
        try:
            from pipeline import heat_pulse
            heat_pulse.refresh()
        except Exception:
            heat_pulse = None
        # ---- 第一遍：门下省四维打分 + VETO 拦截 + 热度附加 ----
        survivors = []
        for idx, item in enumerate(raw_news):
            h_id = f'HOT-{idx + 1:02d}'
            title = item.get('title', '')
            summary = item.get('summary', '')
            category = item.get('category', '')
            # v4.1 R0：计算 heat（捕获侧，只用领先信号）
            heat_info = None
            if heat_pulse is not None:
                try:
                    heat_info = heat_pulse.match_news(item, attention_count=1, source_list=[item.get('source', '')])
                    item['_heat'] = heat_info
                except Exception:
                    heat_info = None
            if menxia is not None:
                # 透传 _heat 给 menxia（用于 R4 交锋封顶豁免）
                payload = {'title': title, 'summary': summary, 'category': category, '_heat': heat_info or {}}
                rule = menxia.score_news(payload)
                if rule['tier'] == 'VETO':
                    if xingbu is not None:
                        veto_rec = menxia.veto({**item, 'id': h_id}, rule['score'], [])
                        try:
                            xingbu.record_rejection(veto_rec, rule['score'], [])
                        except Exception:
                            pass
                    vetoed_count += 1
                    continue
                item['_rule'] = rule
                item['_id'] = h_id
                survivors.append(item)
                vetted_count += 1
            else:
                item['_rule'] = {'score': 50, 'tier': 'MEDIUM', 'reason': '门下省缺席'}
                item['_id'] = h_id
                survivors.append(item)

        # ---- 第二遍：v4.1 R2 全量裁决 —— 送评条件 score>=15 或有热度命中；上限 30 条 ----
        _EVAL_LIMIT = 30
        def _heat_tier(it):
            h = it.get('_heat') or {}
            return h.get('heat_tier')

        def _should_eval(it):
            if it['_rule']['tier'] in ('VETO', 'STRONG'):
                return False
            score = it['_rule']['score']
            heat = _heat_tier(it)
            return score >= 15 or bool(heat)

        candidates = [it for it in survivors if _should_eval(it)]

        # 排序：热度 HIGH > 热度 MID > 规则分降序
        def _sort_key(it):
            h = _heat_tier(it) or ''
            tier_rank = 2 if h == 'HIGH' else 1 if h == 'MID' else 0
            return (tier_rank, it['_rule']['score'])
        candidates.sort(key=_sort_key, reverse=True)

        truncated = len(candidates) > _EVAL_LIMIT
        if truncated:
            over = candidates[_EVAL_LIMIT:]
            candidates = candidates[:_EVAL_LIMIT]
            over_map = {it['_id']: {'score': it['_rule']['score'],
                                    'tier': 'MEDIUM' if it['_rule']['score'] >= 45 else 'WEAK',
                                    'reason': '超出送评额度', 'llm_used': False} for it in over}
        else:
            over_map = {}

        scan_map = {}
        if candidates and menxia is not None:
            try:
                from analysis.llm_analyst import batch_relevance_scan
                scan_map = batch_relevance_scan([
                    {'id': it['_id'], 'title': it.get('title', ''),
                     'summary': it.get('summary') or it.get('content') or '',
                     'content': it.get('content', ''),
                     'time': it.get('time', ''),
                     'rule_score': it['_rule']['score'],
                     'heat_tier': _heat_tier(it) or '',
                     'heat_kw_hits': (it.get('_heat') or {}).get('kw_hits', [])}
                    for it in candidates
                ])
                if truncated and not quiet:
                    print(f'[通政司] 送评额度截断: {len(candidates)}/{len(candidates)+len(over_map)} 条')
            except Exception as e:
                if not quiet:
                    print(f'[通政司] 灰区批量评分提示：{e}')
        scan_map.update(over_map)

        # ---- 事件去重（用户 2026-08-28 反馈：同事件长短两稿都上版面）----
        # 同相似度阈值内保留信息量大的（标题+摘要更长），其余标 DUP：不上版面、仍存档
        dup_ids = set()
        attention_map = {}
        try:
            ranked = sorted(survivors, key=lambda it: -len((it.get('title') or '') + (it.get('summary') or '')))
            kept_titles = []
            for it in ranked:
                t = it.get('title') or ''
                keeper_id = None
                for kt, kid in kept_titles:
                    if _title_similarity(t, kt) >= 0.35:
                        keeper_id = kid
                        break
                if keeper_id:
                    dup_ids.add(it.get('_id'))
                    attention_map[keeper_id] = attention_map.get(keeper_id, 1) + 1
                else:
                    kept_titles.append((t, it.get('_id')))
        except Exception:
            dup_ids = set()
            attention_map = {}

        # ---- 第三遍：分层消费——只有 STRONG 消耗深度研判预算 ----
        # v4.1 R0 消费侧：HIGH 保底 55，MID +10（让博主参与第一批博弈）
        llm_used = 0
        strong_ids, medium_ids, weak_count = [], [], 0
        dup_count = 0
        for item in survivors:
            h_id = item['_id']
            title = item.get('title', '')
            summary = item.get('summary', '')
            category = item.get('category', '')
            rule = item['_rule']
            scanned = scan_map.get(h_id) or {}
            score = int(scanned.get('score') or rule['score'])
            tier = scanned.get('tier') or rule['tier']
            # v4.1 R0：捕获侧热度分加成（**只读领先信号**）
            _heat = item.get('_heat') or {}
            _ht = _heat.get('heat_tier')
            if _ht == 'HIGH':
                # HIGH 保底 55（MEDIUM 中段）；若 LLM 评 ≥60 升 STRONG
                if score < 55:
                    score = max(score, 55)
                if tier == 'WEAK' and score >= 45:
                    tier = 'MEDIUM'
            elif _ht == 'MID':
                score = min(100, score + 10)
                # 保证进入 LLM 裁决（已在 _should_eval 命中）
            item['_heat_applied'] = _ht  # 留痕供前端展示
            # ST/退市封顶闸：LLM 升档后也必须过（用户不碰ST，持仓例外）
            if tier == 'STRONG':
                tier, score = menxia.apply_st_cap(title, tier, score,
                                                  rule.get('matched_symbols') or [])
            reason = scanned.get('reason') or rule.get('reason', '')
            item['_relevance'] = {'score': score, 'tier': tier, 'verdict': tier,
                                  'reason': reason,
                                  'binding_strength': rule.get('binding_strength'),
                                  'hits_subject': rule.get('hits_subject', []),
                                  'hits_event': rule.get('hits_event', []),
                                  'heat_tier': _ht,
                                  'co_source_n': _heat.get('co_source_n', 1),
                                  'editor_hot': _heat.get('editor_hot', False),
                                  'kw_hits': _heat.get('kw_hits', [])}
            # 挂到 item 顶层：persist_factor 从 item 读（阶段五A₀）
            item['binding_strength'] = rule.get('binding_strength')
            item['attention_count'] = attention_map.get(h_id, 1)

            if h_id in dup_ids:
                # 同事件重复稿：不上版面不占预算，仍存档（诚实计数）
                dup_count += 1
                item['_relevance']['tier'] = 'DUP'
                item['_relevance']['verdict'] = 'DUP'
                item['_relevance']['reason'] = (item['_relevance'].get('reason') or '') + ' | 同事件重复稿已合并'
                tier = 'DUP'
                views, meta = {}, None
            elif tier == 'STRONG':
                strong_ids.append(h_id)
                # 深度研判：只有 STRONG 消耗 NEWS_MAX_PER_CYCLE 预算；
                # LLM 闸门待机时（daemon 降频控制）排队下轮，诚实标注
                gate_closed = os.environ.get('NEWS_LLM_GATE', 'open') == 'closed'
                if gate_closed:
                    views = {bid: '（LLM闸门待机中：本轮无强信号降频期，本条强相关已排队下轮深度研判——'
                                  '诚实空缺优于凑数模板。）' for bid in _BLOGGER_IDS}
                elif llm_used < _rc.NEWS_MAX_PER_CYCLE:
                    views = au.deduce_blogger_perspectives(item)
                    llm_used += 1
                else:
                    views = {bid: '（本周期强相关AI研判条数已达上限，本条排队下轮——'
                                  '诚实空缺优于凑数模板。）' for bid in _BLOGGER_IDS}
                meta = (views or {}).get('_meta')
            elif tier == 'MEDIUM':
                medium_ids.append(h_id)
                # 中等相关：不触发博主评论（省下的预算给强相关），诚实标注
                views = {}
                meta = None
            else:  # WEAK：存档不上前端
                weak_count += 1
                views = {}
                meta = None

            clean_views = {k: v for k, v in (views or {}).items()
                           if not str(k).startswith('_')}
            news.append({
                'id': h_id,
                'title': title,
                'summary': summary,
                'content': item.get('content', ''),
                'category': category,
                'source': item.get('source', '新浪财经 7x24 实时快讯'),
                'url': item.get('url', 'https://finance.sina.com.cn/stock/'),
                'time': item.get('time', now),
                'subj_type': item.get('_subj_type', ''),
                'entity': item.get('_entity_name', ''),
                'themes': _split_themes(category),
                'blogger_views': clean_views,
                'blogger_views_meta': meta,
                'relevance': item.get('_relevance', {}),
                'attention_count': attention_map.get(h_id, 1),
                'binding_strength': (item.get('_relevance') or {}).get('binding_strength'),
            })
    except Exception as e:
        if not quiet:
            print(f'[通政司] 新闻检索提示：{e}')

    # 排序：分数高在前（DUP 沉底），同档按分数
    news.sort(key=lambda n: ((n.get('relevance') or {}).get('tier') == 'DUP',
                             -((n.get('relevance') or {}).get('score') or 0)))
    brief_news_tiers = {'strong': strong_ids, 'medium': medium_ids, 'weak_count': weak_count,
                        'dup_count': dup_count, 'llm_deep_used': llm_used}
    # 深度研判真实发生 → 刷新 LLM 闸门时间戳（daemon 降频判据）
    if llm_used > 0:
        try:
            _rc._save_json(os.path.join(BASE_DIR, 'state', 'llm_gate.json'),
                           {'last_llm_run': now, 'deep_used': llm_used})
        except Exception:
            pass

    brief = {
        'schema_version': SCHEMA_VERSION,
        'fetched_at': now,
        'market_quotes': market_quotes,
        'index_intraday': index_intraday,
        'news_count': len(news),
        'vetted_count': vetted_count,
        'vetoed_count': vetoed_count,
        'news_tiers': brief_news_tiers,
        'news': news,
    }

    # 4. 阶段六·蓄水池与全天双层置顶（用户 2026-09-03 定调）：
    # 从“单次快照覆盖”升级为“当日全天热点池”（早上的大事全天常驻，下午的动态倒序折叠）
    try:
        pool_path = os.path.join(BASE_DIR, 'state', 'today_news_pool.json')
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        daily_pool = []
        if os.path.exists(pool_path):
            try:
                pd = json.load(open(pool_path, encoding='utf-8'))
                if pd.get('date') == today_str and isinstance(pd.get('pool'), list):
                    daily_pool = pd['pool']
            except Exception:
                daily_pool = []

        # 将本轮新产出的 STRONG+MEDIUM 项目合并入池（同标题相似度 >= 0.70 视为同事件去重）
        current_items = [
            {
                'id': n['id'],
                'title': n['title'],
                'time': n['time'],
                'category': n['category'],
                'source': n['source'],
                'url': n['url'],
                'content': n['content'],
                'summary': n['summary'],
                'relevance_score': (n.get('relevance') or {}).get('score', 0),
                'relevance_tier': (n.get('relevance') or {}).get('tier', ''),
                'relevance_reason': (n.get('relevance') or {}).get('reason', ''),
                'blogger_views': n['blogger_views'],
                'blogger_views_meta': n.get('blogger_views_meta'),
                'engine': ('llm' if (n.get('blogger_views_meta') or {}).get('model')
                           else ('tiered_no_comment' if (n.get('relevance') or {}).get('tier') == 'MEDIUM'
                                 else 'skipped_or_offline')),
            }
            for n in news
            if (n.get('relevance') or {}).get('tier') in ('STRONG', 'MEDIUM')
        ]

        for it in current_items:
            t = it['title']
            match_idx = None
            for i, old in enumerate(daily_pool):
                if _title_similarity(t, old.get('title', '')) >= 0.70 or t == old.get('title'):
                    match_idx = i
                    break
            if match_idx is not None:
                # 已存在：若新记录有观点或分数更高，更新
                if it.get('blogger_views') and not daily_pool[match_idx].get('blogger_views'):
                    daily_pool[match_idx]['blogger_views'] = it['blogger_views']
                    daily_pool[match_idx]['blogger_views_meta'] = it.get('blogger_views_meta')
                if it.get('relevance_score', 0) > daily_pool[match_idx].get('relevance_score', 0):
                    daily_pool[match_idx]['relevance_score'] = it['relevance_score']
                    daily_pool[match_idx]['relevance_tier'] = it['relevance_tier']
            else:
                daily_pool.append(it)

        # 保存蓄水池
        try:
            os.makedirs(os.path.dirname(pool_path), exist_ok=True)
            json.dump({'date': today_str, 'pool': daily_pool}, open(pool_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        except Exception:
            pass

        # 全天双层排序：
        # 1. 大卡置顶层：取全天最重磅 TOP 2（《今日投资舆情热点》绝对第1，全天最高分紧随其后）
        # 2. 折叠流水层：其余全部项目按时间最新倒序排列
        strong_cand = []
        other_cand = []
        for it in daily_pool:
            sc = it.get('relevance_score', 0)
            tr = it.get('relevance_tier', '')
            if tr == 'STRONG' or sc >= 75:
                strong_cand.append(it)
            else:
                other_cand.append(it)

        def _top_card_sort(it):
            t = it.get('title', '')
            if '今日投资舆情热点' in t: return (100, 999, it.get('time', ''))
            if '涨停分析' in t: return (90, it.get('relevance_score', 0), it.get('time', ''))
            return (10, it.get('relevance_score', 0), it.get('time', ''))

        strong_cand.sort(key=_top_card_sort, reverse=True)
        top_cards = strong_cand[:2]  # 最多 2 张大卡，保证版面紧凑有力
        remaining_strong = strong_cand[2:]

        # 折叠区：其余项目按时间最新倒序
        collapsed = remaining_strong + other_cand
        for it in collapsed:
            it['relevance_tier'] = 'MEDIUM'
        collapsed.sort(key=lambda x: x.get('time', ''), reverse=True)

        # 重新编排 HOT-01, HOT-02... 编号
        for i, it in enumerate(top_cards + collapsed):
            it['id'] = f"HOT-{i+1:02d}"

        brief['hotspots'] = top_cards + collapsed
        brief['news_tiers']['strong'] = [c['id'] for c in top_cards]
        brief['news_tiers']['medium'] = [c['id'] for c in collapsed]
    except Exception as e:
        brief['hotspots'] = []
        if not quiet:
            print(f'[通政司] 热点组装提示：{e}')

    try:
        brief['forward_events'] = au.generate_forward_scout_events()
    except Exception as e:
        brief['forward_events'] = []
        if not quiet:
            print(f'[通政司] 前瞻事件提示：{e}')

    os.makedirs(os.path.dirname(BRIEF_PATH), exist_ok=True)
    with open(BRIEF_PATH, 'w', encoding='utf-8') as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)

    if not quiet:
        print(f'[通政司] 通政奏报已落盘：{BRIEF_PATH}（新闻 {len(news)} 条 / 热点 {len(brief["hotspots"])} 条 / 前瞻 {len(brief["forward_events"])} 条）')
    return brief, BRIEF_PATH


def load_brief():
    """读取最新通政奏报；不存在返回 None。"""
    if os.path.exists(BRIEF_PATH):
        try:
            with open(BRIEF_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


if __name__ == '__main__':
    b, p = gather_intelligence(quiet=False)
    print('新闻条数:', b['news_count'])
    for n in b['news']:
        print(f"  {n['id']} [{n['category']}] {n['title'][:30]} 主题:{n['themes']}")
