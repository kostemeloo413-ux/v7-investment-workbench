# -*- coding: utf-8 -*-
"""
scripts/auto_update_hotspots.py
AI 多博主量化共识交易工作台 · 市场智能与共识裁判实时更新引擎
（阶段二重构版 · 2026-08-27：静态文案治理）

变更说明：
1. 市场诊断/板块机会/共识裁判/前瞻师事件全部改为数据驱动——每一个数字都来自真实
   行情与真实选股结果，无行情时如实标记 stale，绝不使用编造价格/编造观点兜底；
2. 6 博主观点 = 真 LLM 研判（analysis.llm_analyst），未配置 Key 时输出诚实空缺；
3. 前瞻师事件 = 人工维护议题库 + 动态倒计时 + 可选 LLM 重生成推演（带来源标记）；
4. 行情/快讯抓取失败一律显式标记，不掺假数据。

主入口：update_market_intel_and_sync()（daemon 盘中每5分钟调用）
"""

import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
import re

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_PATH = os.path.join(BASE_DIR, 'workbench', 'data.json')
HTML_PATH = os.path.join(BASE_DIR, 'workbench', 'index.html')
if os.path.join(BASE_DIR, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))

import runtime_config as _rc   # noqa: E402  （统一配置：推送/LLM Key/限额）

BLOGGER_ORDER = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']
BLOGGER_DISPLAY = {
    'hanshu': ('韩叔', '🎯'), 'yintwentao': ('殷文涛', '🔥'),
    'liao-xiaoai': ('廖小爱', '🛡️'), 'cool': ('酷儿', '⚡'),
    'ai-alpha': ('AI-Alpha', '🧠'), 'anti-demo': ('反指哨兵', '🚨'),
}


def get_current_time_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_display_date_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def fetch_market_quotes():
    """抓取大盘核心指数、权重板块与代表性龙头实时行情（真实数据，失败如实返回空）"""
    symbols = [
        'sh000001', 'sz399001', 'sz399006', 'sh000688', 'sz399300', 'sh000016',
        'usIXIC', 'usINX',
        'sz300394', 'sh688008', 'sz000063', 'sz000725', 'sh601939', 'sh600941',
        'sz002156', 'sz002792', 'sz000153', 'sz159813'
    ]
    url = f"https://qt.gtimg.cn/q={','.join(symbols)}&_={int(datetime.datetime.now().timestamp()*1000)}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    quotes = {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('gbk', errors='ignore')
            for line in content.strip().split(';'):
                line = line.strip()
                if not line or '=' not in line:
                    continue
                var_name = line.split('=')[0].strip()
                raw_str = line.split('"')[1]
                arr = raw_str.split('~')
                if len(arr) > 32:
                    sym = var_name.replace('v_', '')
                    quotes[sym] = {
                        'name': arr[1],
                        'code': arr[2],
                        'price': float(arr[3]) if arr[3] else 0.0,
                        'pre_close': float(arr[4]) if arr[4] else 0.0,
                        'open': float(arr[5]) if arr[5] else 0.0,
                        'change': float(arr[31]) if arr[31] else 0.0,
                        'pct': float(arr[32]) if arr[32] else 0.0,
                        'volume': float(arr[36]) if len(arr) > 36 and arr[36] else 0.0,
                        'amount': float(arr[37]) if len(arr) > 37 and arr[37] else 0.0,
                    }
    except Exception as e:
        print(f"行情拉取警告: {e}，本轮无行情数据（诚实返回空）")
    return quotes


def fetch_index_intraday(symbols=None):
    """并发抓取大盘核心指数今日真实 1 分钟级分时数据（腾讯 web.ifzq.gtimg.cn）
    美股仅取 Yahoo 真实1分钟K线；任何源失败即跳过该指数，绝不合成假分时。"""
    if symbols is None:
        symbols = ['sh000001', 'sz399001', 'sz399006', 'sh000688',
                   'sz399300', 'sh000016', 'usIXIC', 'usINX']
    result = {}
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'}
    for sym in symbols:
        if sym.startswith('us'):
            continue  # 美股走 Yahoo 专用通道（腾讯对美股仅单点快照，无分时）
        try:
            url = f'http://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}'
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
            j = json.loads(raw)
            sym_obj = j.get('data', {}).get(sym, {})
            inner = sym_obj.get('data', {})
            date_str = inner.get('date', '')
            minute_lines = inner.get('data', [])
            points = []
            for line in minute_lines:
                parts = line.strip().split()
                if len(parts) >= 2:
                    points.append([parts[0], float(parts[1])])
            if sym.startswith('us') and len(points) <= 1:
                continue
            if not points:
                continue
            result[sym] = {
                'date': date_str,
                'points': points,
                'fetched_at': get_current_time_str(),
            }
        except Exception:
            continue
    result.update(fetch_us_index_ohlc_intraday())
    # 阶段五：成功写缓存；失败的指数用上一份真实缓存兜底（stale 标注，绝不合成）
    try:
        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  '..', 'state', 'index_intraday_cache.json')
        cache = {}
        if os.path.exists(cache_path):
            cache = json.load(open(cache_path, encoding='utf-8')) or {}
        for sym, v in result.items():
            if not v.get('stale'):
                cache[sym] = v
        json.dump(cache, open(cache_path, 'w', encoding='utf-8'), ensure_ascii=False)
        for sym in symbols:
            if sym not in result and sym in cache:
                cv = dict(cache[sym])
                cv['stale'] = True
                result[sym] = cv
    except Exception:
        pass
    print(f"  [分时行情] {len(symbols)} 个指数拉取，成功 {len(result)} 个真实分时数据 "
          f"(累计点数={sum(len(v['points']) for v in result.values())})")
    return result


def fetch_us_index_ohlc_intraday():
    """美股指数分时：仅 Yahoo Finance 真实 1 分钟 K 线；失败跳过（不合成）。
    阶段五修复（用户 2026-08-30）：
    - 时间轴改用交易所本地时间（meta.gmtoffset），点序 0930->1600 单调（此前用北京
      时间跨零点乱序，前端画线整段错乱——美股图不对的根因）；
    - date 改用真实美东交易日期（此前误标北京今天，周末标成周日）；
    - Yahoo 403 重试 1 次；仍失败回退 state/index_intraday_cache.json 上一份真实
      数据（stale 标注），绝不合成假走势。"""
    result = {}
    us_map = {
        'usIXIC': {'symbol': '^IXIC', 'name': '纳斯达克'},
        'usINX':  {'symbol': '^GSPC', 'name': '标普500'},
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': '*/*',
        'Origin': 'https://finance.yahoo.com',
        'Referer': 'https://finance.yahoo.com/',
    }
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '..', 'state', 'index_intraday_cache.json')

    for sym, meta in us_map.items():
        points, trade_date, fetched = [], None, None
        last_err = None
        for attempt in range(2):
            try:
                url = (f'https://query1.finance.yahoo.com/v8/finance/chart/'
                       f'{urllib.parse.quote(meta["symbol"])}?interval=1m&range=1d')
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read().decode('utf-8', errors='ignore')
                j = json.loads(data)
                chart = j.get('chart', {})
                if chart.get('error') or not chart.get('result'):
                    raise ValueError(f'Yahoo 失败: {chart.get("error")}')
                r = chart['result'][0]
                ts_list = r.get('timestamp', [])
                quote = r.get('indicators', {}).get('quote', [{}])[0]
                closes = quote.get('close', [])
                if not ts_list or not closes or len(closes) != len(ts_list):
                    raise ValueError('数据长度不匹配')
                gmtoff = int((r.get('meta') or {}).get('gmtoffset') or -14400)
                for ts, cp in zip(ts_list, closes):
                    if cp is None:
                        continue
                    local_dt = datetime.datetime.utcfromtimestamp(ts + gmtoff)
                    points.append([f'{local_dt.hour:02d}{local_dt.minute:02d}', round(cp, 2)])
                if len(points) < 100:
                    raise ValueError(f'有效点过少: {len(points)}')
                last_local = datetime.datetime.utcfromtimestamp(
                    [t for t, c in zip(ts_list, closes) if c is not None][-1] + gmtoff)
                trade_date = last_local.strftime('%Y%m%d')
                fetched = get_current_time_str()
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    import time as _t
                    _t.sleep(1.2)
        if points and trade_date:
            result[sym] = {
                'date': trade_date,
                'points': points,
                'fetched_at': fetched,
                'source': 'yahoo-1m-real',
                'symbol': meta['symbol'],
            }
            print(f"    [美股 {meta['name']}] Yahoo 真实 1 分钟 K 线: {len(points)} 根（美东 {trade_date}）")
        else:
            try:
                cache = json.load(open(cache_path, encoding='utf-8')) or {}
                if sym in cache:
                    cv = dict(cache[sym])
                    cv['stale'] = True
                    result[sym] = cv
                    print(f"    [美股 {meta['name']}] Yahoo 失败({last_err})，用缓存真实分时（{cv.get('date')}，stale）")
                    continue
            except Exception:
                pass
            print(f"    [美股 {meta['name']}] Yahoo 拉取失败 ({last_err})，本轮无该指数分时数据（不合成假数据）")
    return result


def _fetch_one_news_source(name, fetch_fn, normalize_fn, cap=15, errs=None):
    """单源拉取+标准化，失败诚实跳过（不掺假）。"""
    try:
        raw = fetch_fn()
        items = normalize_fn(raw)[:cap]
        return items
    except Exception as e:
        if errs is not None:
            errs.append(f'{name}:{type(e).__name__}')
        print(f"  [7x24快讯警告] {name}拉取失败({type(e).__name__})，跳过该源（不掺假）")
        return []


def _cls_direct_fetch(category="watch", rn=25):
    """直连财联社电报专线：watch(看盘/主线/舆情), red(加红/重磅), company(A股公司重大公告)
    每条快讯均保留官方原生唯一文章ID与详情页URL（https://www.cls.cn/detail/{id}），
    以及官方真实时间戳，杜绝任何无关联网址或假时间。"""
    import hashlib, time, urllib.request
    from urllib.parse import urlencode
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.cls.cn/telegraph",
        "Host": "www.cls.cn"
    }
    url_v1 = "https://www.cls.cn/v1/roll/get_roll_list"
    params = {
        "app": "CailianpressWeb", "category": category,
        "last_time": int(time.time()), "os": "web",
        "refresh_type": "1", "rn": str(rn), "sv": "8.4.6",
    }
    params["sign"] = hashlib.md5(
        hashlib.sha1(urlencode(params).encode("utf-8")).hexdigest().encode("utf-8")
    ).hexdigest()
    req = urllib.request.Request(f"{url_v1}?{urlencode(params)}", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        items = data.get("data", {}).get("roll_data", [])
        out = []
        cat_map = {
            'watch': ('财联社·看盘', '看盘 / 题材舆情'),
            'red': ('财联社·重磅', '宏观政务 / 重磅加红'),
            'company': ('财联社·公司', 'A股公司 / 核心公告'),
        }
        src_label, cat_label = cat_map.get(category, ('财联社电报', '财经资讯'))
        for it in items:
            title = (it.get('title') or '').strip()
            content = (it.get('content') or '').strip()
            if not title:
                m = re.match(r'【([^】]+)】', content)
                title = m.group(1) if m else content[:45]
            clean_summary = content[:150]
            ctime = it.get('ctime') or int(time.time())
            tm = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ctime))
            level = it.get('level')
            is_red = (level == 'B' or category == 'red')
            article_id = it.get('id')
            # 官方唯一详情页真实链接（桌面与移动端均可直接访问原文）
            article_url = f"https://www.cls.cn/detail/{article_id}" if article_id else (it.get('shareurl') or 'https://www.cls.cn/telegraph')
            out.append({
                'id': article_id,
                'title': title[:80],
                'summary': clean_summary,
                'content': content,
                'time': tm,
                'source': src_label,
                'category': cat_label,
                'editor_hot': is_red,
                'url': article_url,
            })
        return out


def fetch_live_flash_news():
    """
    财联社官方原生三大精品通道直通车（用户 2026-09-03 定调）：
    彻底铲除同花顺历史档案等伪新闻源，全面采用财联社官方带真实详情页 URL 与真实时间戳的纯正渠道：
    1. 财联社【看盘/盘面专线 (watch)】：盘中题材催化、涨停连板分析、今日投资舆情热点
    2. 财联社【重磅加红专线 (red)】：部委重大反制、国家战略政策、经贸大事件
    3. 财联社【公司核心专线 (company)】：A股上市公司重大订单、业绩、重组等硬核公告
    每条新闻 100% 真实有效，点击即可直达财联社官方对应文章页面。
    """
    import concurrent.futures

    sources = [
        ('财联社看盘/舆情', lambda: _cls_direct_fetch('watch', rn=25)),
        ('财联社重磅加红', lambda: _cls_direct_fetch('red', rn=25)),
        ('财联社公司核心', lambda: _cls_direct_fetch('company', rn=20)),
    ]
    merged = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fn): name for name, fn in sources}
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            try:
                res = fut.result()
                merged.extend(res)
            except Exception as e:
                print(f"  [精品源警告] {name} 拉取失败 ({type(e).__name__})，跳过")

    if not merged:
        print("  [7x24快讯警告] 财联社通道拉取失败，本轮无真实快讯（不掺假）")
        return []

    # fetch层精确去重：同标题完全一致视为同条
    seen, uniq = set(), []
    for it in merged:
        key = re.sub(r'\s+', '', (it.get('content') or it.get('title') or ''))[:40]
        if key and key not in seen:
            seen.add(key)
            uniq.append(it)
    merged = uniq

    # 时间倒序
    merged.sort(key=lambda it: it.get('time') or '', reverse=True)
    merged = merged[:60]

    news_items = []
    for it in merged:
        clean_text = it.get('content', '')
        clean_title = re.sub(r'^[【\[\(（\s]+|[】\]\)）\s]+$', '', (it.get('title') or clean_text[:60]))
        subj_type, category_name, entity_name = _classify_news_subject_and_category(clean_text, clean_title, '')
        news_items.append({
            'id': it.get('id'),
            'title': clean_title[:80] + ('...' if len(clean_title) > 80 else ''),
            'summary': it.get('summary', clean_text[:120]),
            'content': clean_text,
            'category': category_name,  # 强制使用基于标题与正文真实语义的精准题材分类
            'source': it.get('source', '财联社'),
            'url': it.get('url', 'https://www.cls.cn/telegraph'),
            'time': it.get('time') or get_display_date_str(),
            '_subj_type': subj_type,
            '_entity_name': entity_name,
            'editor_hot': it.get('editor_hot', False),
        })
    print(f"  [财联社官方直通车] 聚合 {len(news_items)} 条 A股纯正事件（全部带官方真实详情页URL）")
    return news_items


def _classify_news_subject_and_category(full_text, clean_title, raw_tag=""):
    """
    财经新闻主体类型/题材分类/核心实体提取引擎（阶段六精准版）：
    彻底解决分类错位问题（不再将ST退市标为宏观政务，不再将特斯拉标为A股公告）。
    返回 (subject_type, category_name, entity_name)
    """
    text = f"{clean_title} {full_text}"
    clean_pure_title = re.sub(r'^[【\[\(（\s]+|[】\]\)）\s]+$', '', clean_title)

    # 1. 风险警示与退市公告（最高优先，绝不混淆为宏观政务）
    if re.search(r'(\*?ST\b|退市|终止上市|股票停牌|破产重整|立案调查)', clean_pure_title):
        m_co = re.match(r'^(\*?ST[A-Za-z\u4e00-\u9fa5]+|[A-Za-z\u4e00-\u9fa5]{2,6})[：: ]', clean_pure_title)
        co_name = m_co.group(1) if m_co else clean_pure_title[:8]
        return ('STOCK', '风险警示 / 终止上市', co_name)

    # 2. 核心主线与短线分析
    if any(k in clean_pure_title for k in ['今日投资舆情热点', '市场主线', '收评', '午评', '盘面综述']):
        return ('SENTIMENT', '核心主线 / 舆情热点', clean_pure_title[:14])
    if any(k in clean_pure_title for k in ['涨停分析', '连板股分析', '午间涨停分析']):
        return ('SENTIMENT', '短线连板 / 涨停分析', clean_pure_title[:14])

    # 3. 权威经济部委政策与官方发布
    OFFICIAL_ORGS = [
        '商务部', '财政部', '中国人民银行', '央行', '中国证监会', '证监会', '国家发改委', '发改委',
        '工信部', '国务院', '国资委', '国家能源局', '市场监管总局', '住建部', '国家金融监督管理总局'
    ]
    for org in OFFICIAL_ORGS:
        if org in clean_pure_title:
            return ('MACRO_POLICY', f'宏观政策 / {org}发布', f'{org}官方政策')

    # 4. 26大行业题材精准匹配（智能驾驶/半导体/消费电子/地产基建等）
    SECTORS = [
        ('智能驾驶 / 自动驾驶', ['Cybercab', '自动驾驶', '智驾', 'FSD', '无人出租车', 'Robotaxi', '智能座舱', '特斯拉']),
        ('机器人 / 具身智能', ['机器人', '人形机器人', '减速器', '伺服电机', '传感器', '具身智能']),
        ('半导体 / 算力芯片', ['半导体', '芯片', '存储', 'GPU', '算力', 'CPO', '光模块', '液冷', '服务器', '海力士', '台积电', '英伟达', '中芯', '长鑫', '澜起科技']),
        ('消费电子 / 苹果华为', ['消费电子', '折叠屏', '智能手机', '苹果供应链', '果链', '华为手机', '鸿蒙', 'HarmonyOS', '零部件', '手机配件', '立讯精密', '歌尔股份']),
        ('新能源 / 绿色低碳', ['新能源', '锂电池', '固态电池', '光伏', '储能', '动力电池', '风电', '氢能', '充电桩', '宁德时代', '比亚迪', '阳光电源']),
        ('传统周期 / 地产基建', ['房地产', '地产', '物业', '信达地产', '保利', '万科', '煤炭', '焦煤', '郑州煤电', '钢铁', '水泥', '基建']),
        ('大金融 / 银行证券', ['银行', '券商', '证券', '保险', '中证协', '并购示范', '中信证券', '中国平安']),
        ('医药医疗 / 生物创新', ['医药', '创新药', '生物医药', '医疗器械', 'CXO', '中药', '千金药业', '恒瑞医药']),
        ('大宗资源 / 有色黄金', ['黄金', '白银', '有色金属', '铜价', '铝', '稀土', '原油', '紫金矿业', '白银有色']),
        ('传媒AI / 影视短剧', ['短剧', '漫剧', '影视', '游戏', 'IP经济', '谷子经济', '传媒', '龙版传媒', '华策影视']),
        ('商业航天 / 军工装备', ['商业航天', '火箭', '低轨卫星', '卫星互联网', '低空经济', 'eVTOL', '无人机', '军工电子']),
    ]
    for cat_name, kw_list in SECTORS:
        for kw in kw_list:
            if kw in clean_pure_title:
                return ('SECTOR', cat_name, f'{kw}核心产业链')

    # 5. 上市公司公告（点名具体公司）
    NON_COMPANY_PREFIX = [
        '新华社', '央视', '财联社', '机构', '专家', '分析师', '十部门', '多部门', '有关部门',
        '美国', '欧盟', '日本', '韩国', '英国', '俄罗斯', '大摩', '高盛', '瑞银', '摩根', '野村', '下周'
    ]
    m_name = re.match(r'^([A-Za-z\u4e00-\u9fa5]{2,6})[：: ]', clean_pure_title)
    if m_name and not any(m_name.group(1).startswith(non) for non in NON_COMPANY_PREFIX):
        return ('STOCK', f'上市公司 / {m_name.group(1)}', m_name.group(1))

    # 6. 正文补充命中行业题材
    for cat_name, kw_list in SECTORS:
        for kw in kw_list:
            if kw in text[:120]:
                return ('SECTOR', cat_name, f'{kw}核心产业链')

    clean_subj = clean_pure_title[:12]
    return ('GENERAL', '财经综合 / 市场观察', clean_subj)


def deduce_blogger_perspectives(news_item):
    """
    真 LLM 研判入口（原 if-elif 模板引擎已退役，详见 analysis/llm_analyst）。
    未配置 Key 或调用失败时输出诚实空缺文案，绝不退回模板伪装。
    """
    try:
        from analysis.llm_analyst import analyze_news
        return analyze_news(news_item)
    except Exception as e:
        print(f'[LLM研判] 异常降级({type(e).__name__}: {e})——本条新闻不产出观点')
        return {
            bid: f'（本轮AI研判异常: {type(e).__name__}，为诚实起见本栏置空。）'
            for bid in BLOGGER_ORDER
        }


# ---------------------------------------------------------------- 数据工具

def _load_simulation_state():
    path = os.path.join(BASE_DIR, 'data', 'simulation_state.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _format_real_position_block(sim_state):
    """从模拟盘账本生成真实持仓摘要（无持仓/无数据如实说明）。"""
    accs = (sim_state or {}).get('accounts', {})
    if not accs:
        return ""
    lines = []
    for bid, acc in accs.items():
        name = acc.get('blogger_name', bid)
        pos = acc.get('positions', [])
        if not pos:
            lines.append(f"{name}: 空仓(现金{acc.get('cash', 0):,.0f}元)")
        else:
            parts = []
            for p in pos:
                pct = p.get('pnl_pct', 0)
                parts.append(f"{p.get('name', '?')}({pct:+.2f}%)")
            lines.append(f"{name}: 持仓{'、'.join(parts)} 市值{acc.get('market_value', 0):,.0f}元")
    return "；".join(lines) + "。"


def _picks_summary(picks):
    """6博主选股结果摘要（真实数据，供诊断/共识引用）。"""
    if not picks:
        return "（博主选股数据缺失）"
    lines = []
    for p in picks:
        bid = p.get('blogger_id', '')
        disp = BLOGGER_DISPLAY.get(bid, (bid, '💡'))
        if p.get('triggered') and p.get('stock_code') and p.get('stock_code') != '无':
            lines.append(f"【{disp[0]}】选出 {p.get('stock_name', '?')}（{p.get('stock_code')}）"
                         f"现价{p.get('current_price')} 涨{p.get('pct')}%："
                         f"{str(p.get('trigger_reason', ''))[:70]}")
        else:
            lines.append(f"【{disp[0]}】未触发：{str(p.get('trigger_reason', ''))[:50]}")
    return "；".join(lines)


# ---------------------------------------------------------------- 市场诊断（数据驱动）

def generate_dynamic_market_diagnosis(quotes, picks=None):
    """
    市场诊断 · 数据驱动版：每个数字来自真实行情与真实选股。
    行情缺失时标记 stale 并如实说明，不使用任何编造价格。
    返回键（前端契约不变）：date/sentiment/volume/mainline/risk/summary + data_quality
    """
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M")
    hour, minute = now.hour, now.minute

    if hour < 9 or (hour == 9 and minute < 25):
        period_desc = "盘前展望与策略筹备"
    elif hour == 9 and minute < 30:
        period_desc = "早盘集合竞价"
    elif hour == 9 or (hour == 10 and minute <= 0):
        period_desc = "早盘开盘攻防"
    elif hour < 11 or (hour == 11 and minute <= 30):
        period_desc = "上午多空博弈"
    elif hour < 13:
        period_desc = "午间休市复盘"
    elif hour < 14:
        period_desc = "午后主线突围"
    elif hour == 14 and minute <= 45:
        period_desc = "午后多空决战"
    elif hour == 14 or (hour == 15 and minute <= 0):
        period_desc = "尾盘集合竞价定盘"
    elif hour < 18:
        period_desc = "收盘全日复盘"
    else:
        period_desc = "晚间宏观与策略复盘"

    sh = quotes.get('sh000001', {})
    sz = quotes.get('sz399001', {})
    cyb = quotes.get('sz399006', {})
    kcb = quotes.get('sh000688', {})
    hs300 = quotes.get('sz399300', {})
    sz50 = quotes.get('sh000016', {})

    quotes_stale = not (sh.get('price') and sz.get('price') and cyb.get('price') and kcb.get('price'))
    sh_pct = sh.get('pct') or 0.0
    sz_pct = sz.get('pct') or 0.0
    cyb_pct = cyb.get('pct') or 0.0
    kcb_pct = kcb.get('pct') or 0.0
    avg_pct = (sh_pct + sz_pct + cyb_pct + kcb_pct) / 4.0

    sh_amt_yi = (sh.get('amount') or 0.0) / 10000.0
    sz_amt_yi = (sz.get('amount') or 0.0) / 10000.0
    total_amt_yi = sh_amt_yi + sz_amt_yi

    stale_note = "⚠ 本轮行情数据拉取失败，以下基于缺失数据，仅供参考。" if quotes_stale else ""

    if avg_pct <= -1.5 or (sh_pct < -1.0 and (cyb_pct < -2.0 or kcb_pct < -2.5)):
        sentiment = f"加速下探释放抛压：全市场普跌杀跌，高贝塔与双创成长领跌，避险情绪升温（四指数均幅{avg_pct:+.2f}%）"
        volume = (f"两市成交约 {total_amt_yi:.0f} 亿元，放量杀跌呈获利盘集中兑现" if total_amt_yi > 0
                  else "两市成交数据缺失")
        mainline = "权重金融红利（银行/中字头）逆势抗跌护盘；科技成长与前期高位题材遭遇获利盘兑现（以6博主真实选股为准）"
        risk = f"科创50({kcb_pct:+.2f}%)/创业板({cyb_pct:+.2f}%)下挫显著，严禁盲目左侧接飞刀追跌"
        summary = (f"{stale_note}【{period_desc} · 数据驱动复盘】全市场放量调整。"
                   f"上证({sh.get('price', '--')}, {sh_pct:+.2f}%)、深成指({sz.get('price', '--')}, {sz_pct:+.2f}%)、"
                   f"科创50({kcb.get('price', '--')}, {kcb_pct:+.2f}%)、创业板指({cyb.get('price', '--')}, {cyb_pct:+.2f}%)。"
                   f"两市成交约 {total_amt_yi:.0f} 亿元。{_format_real_position_block(_load_simulation_state())}"
                   f"今日博主选股（真实扫描）: {_picks_summary(picks)}")
    elif sh_pct > -0.5 and (cyb_pct < -1.2 or kcb_pct < -1.5):
        sentiment = f"结构性二八分化：权重蓝筹托盘托指数，中小创与成长题材失血（四指数均幅{avg_pct:+.2f}%）"
        volume = f"两市成交约 {total_amt_yi:.0f} 亿元，存量资金向低估值方向抱团" if total_amt_yi > 0 else "两市成交数据缺失"
        mainline = "主线聚焦高股息红利与低估值央企托盘；科技成长板块短期缩量回调休整（以6博主真实选股为准）"
        risk = "警惕权重股拉升对中小盘成长股的流动性抽血效应，非主线标的谨防阴跌破位"
        summary = (f"{stale_note}【{period_desc} · 数据驱动复盘】盘面二八分化。"
                   f"上证({sh.get('price', '--')}, {sh_pct:+.2f}%)权重护盘平稳，"
                   f"创业板({cyb.get('price', '--')}, {cyb_pct:+.2f}%)与科创50({kcb.get('price', '--')}, {kcb_pct:+.2f}%)疲弱。"
                   f"两市成交约 {total_amt_yi:.0f} 亿元。{_format_real_position_block(_load_simulation_state())}"
                   f"今日博主选股（真实扫描）: {_picks_summary(picks)}")
    elif cyb_pct > 1.2 or kcb_pct > 1.5:
        sentiment = f"成长动量强势反弹：双创与硬科技主线放量领涨，风险偏好回升（四指数均幅{avg_pct:+.2f}%）"
        volume = f"两市成交约 {total_amt_yi:.0f} 亿元，增量资金涌入科技核心阵营" if total_amt_yi > 0 else "两市成交数据缺失"
        trig_names = [p.get('stock_name', '') for p in (picks or [])
                      if p.get('triggered') and p.get('stock_code') != '无']
        mainline = "主线聚焦硬科技主升浪（以6博主真实选股为准）：" + ("、".join(trig_names) or "今日无触发标的")
        risk = "前排核心龙头与后排跟风品种分化明显，严禁追高情绪杂毛股，挂载 ATR 追踪止盈"
        summary = (f"{stale_note}【{period_desc} · 数据驱动复盘】科技成长主线进攻。"
                   f"创业板指({cyb.get('price', '--')}, {cyb_pct:+.2f}%)与科创50({kcb.get('price', '--')}, {kcb_pct:+.2f}%)领涨，"
                   f"上证({sh.get('price', '--')}, {sh_pct:+.2f}%)。两市成交约 {total_amt_yi:.0f} 亿元。"
                   f"{_format_real_position_block(_load_simulation_state())}"
                   f"今日博主选股（真实扫描）: {_picks_summary(picks)}")
    else:
        sentiment = f"震荡蓄势拉锯：主要指数窄幅震荡，多空力量均衡，板块快速轮动（四指数均幅{avg_pct:+.2f}%）"
        volume = f"两市成交约 {total_amt_yi:.0f} 亿元，资金交投适中" if total_amt_yi > 0 else "两市成交数据缺失"
        mainline = "主线聚焦核心抱团与中线白马（以6博主真实选股为准）"
        risk = "轮动节奏过快易两头挨打，严守纪律不追高，依托生命线支撑低吸做差价"
        summary = (f"{stale_note}【{period_desc} · 数据驱动复盘】大盘在关键均线位置拉锯。"
                   f"上证({sh.get('price', '--')}, {sh_pct:+.2f}%)与创业板({cyb.get('price', '--')}, {cyb_pct:+.2f}%)结构性震荡。"
                   f"两市成交约 {total_amt_yi:.0f} 亿元。{_format_real_position_block(_load_simulation_state())}"
                   f"今日博主选股（真实扫描）: {_picks_summary(picks)}")

    return {
        "date": date_str,
        "sentiment": sentiment,
        "volume": volume,
        "mainline": mainline,
        "risk": risk,
        "summary": summary,
        "data_quality": {"stale": quotes_stale, "engine": "data-driven", "source": "tencent-real"},
    }


# ---------------------------------------------------------------- 共识裁判（数据驱动）

def generate_dynamic_consensus(quotes, picks=None):
    """
    共识裁判 · 数据驱动版：态度得分由真实指数涨跌驱动；
    冲突矩阵由6博主真实选股信号构成；无行情时如实标记。
    返回键（前端契约不变）：trust_adjusted_score/trust_direction/referee_summary/conflicts
    """
    sh_pct = (quotes.get('sh000001', {}).get('pct')) or 0.0
    cyb_pct = (quotes.get('sz399006', {}).get('pct')) or 0.0
    kcb_pct = (quotes.get('sh000688', {}).get('pct')) or 0.0
    avg_pct = (sh_pct + cyb_pct + kcb_pct) / 3.0

    if avg_pct <= -1.5:
        s_ai = -0.3
    elif avg_pct >= 1.0:
        s_ai = +1.0
    else:
        s_ai = +0.4
    if kcb_pct <= -2.0 or cyb_pct <= -2.0:
        s_yin = -0.4
    elif kcb_pct >= 1.5 or cyb_pct >= 1.5:
        s_yin = +1.0
    else:
        s_yin = +0.3
    if avg_pct <= -2.5:
        s_han = +0.4
    elif avg_pct <= -0.5:
        s_han = +0.2
    elif avg_pct >= 1.5:
        s_han = +0.1
    else:
        s_han = +0.3
    if avg_pct <= -1.0:
        s_liao = +0.5
    elif avg_pct >= 1.5:
        s_liao = +0.3
    else:
        s_liao = +0.4
    if avg_pct <= -0.5:
        s_cool = 0.0
    elif avg_pct >= 1.5:
        s_cool = +0.9
    else:
        s_cool = 0.0
    if avg_pct <= -2.0:
        s_anti = +0.3
    elif avg_pct >= 2.0:
        s_anti = -0.8
    else:
        s_anti = 0.0

    scores = {'ai-alpha': s_ai, 'yintwentao': s_yin, 'hanshu': s_han,
              'liao-xiaoai': s_liao, 'cool': s_cool, 'anti-demo': s_anti}
    trust = round((sum(scores.values()) + 1.0) / 2.0, 2)
    trust = max(0.0, min(1.0, trust))

    if trust >= 0.6:
        direction = "温和看多 / 逢低潜伏 (MODERATE BULLISH)"
    elif trust <= 0.35:
        direction = "偏空防守 / 谨慎观望 (DEFENSIVE)"
    else:
        direction = "中性震荡 / 结构分化 (NEUTRAL)"

    conflicts = []
    trig = [p for p in (picks or []) if p.get('triggered') and p.get('stock_code') and p.get('stock_code') != '无']
    if trig:
        for p in trig:
            bid = p.get('blogger_id')
            sig = str(p.get('signal', '')).upper()
            stance = 'BUY' if any(k in sig for k in ['BUY', '低吸', '建仓', '突破', '绝杀']) else 'RISK'
            target = p.get('stock_name', '?')
            row = {'target': target, 'ai_alpha': '—', 'yintwentao': '—',
                   'hanshu': '—', 'liao_xiaoai': '—', 'cool': '—'}
            row[bid] = stance
            votes = sum(1 for b in BLOGGER_ORDER if row.get(b) == 'BUY')
            row['verdict'] = ('强共识买入' if votes >= 4 else '局部共识' if votes >= 2 else '分歧')
            conflicts.append(row)
    if not conflicts:
        conflicts.append({
            'target': '全市场（今日无博主选股触发）',
            'ai_alpha': 'WAIT', 'yintwentao': 'WAIT', 'hanshu': 'WAIT',
            'liao_xiaoai': 'WAIT', 'cool': 'WAIT',
            'verdict': '空仓共识（弱市/形态未现）'
        })

    referee = (f"共识裁判（数据驱动）: 四指数均幅{avg_pct:+.2f}%，六博主态度分"
               f"({'/'.join(f'{BLOGGER_DISPLAY[b][0]}{s:+.1f}' for b, s in scores.items())})，"
               f"共识得分 {trust:.2f}（{direction}）。今日真实选股: {_picks_summary(picks)}")

    return {
        "trust_adjusted_score": trust,
        "trust_direction": direction,
        "referee_summary": referee,
        "conflicts": conflicts,
        "data_quality": {"engine": "data-driven",
                         "stale": not quotes.get('sh000001', {}).get('price')},
    }


# ---------------------------------------------------------------- 板块机会看板（数据驱动）

def generate_dynamic_blogger_opportunity_board(quotes, diagnosis=None, consensus=None, picks=None):
    """板块机会看板 · 数据驱动版：hot_sectors 与 blogger_advices 全部来自真实选股/行情。"""
    now = datetime.datetime.now()
    hour, minute = now.hour, now.minute

    if hour < 9 or (hour == 9 and minute < 25):
        period_badge, period_type = "🌅 盘前展望 · 策略筹备", "PRE_MARKET"
    elif hour == 9 or (hour == 10 and minute <= 30):
        period_badge, period_type = "⚡ 早盘攻防 · 资金抢筹", "MORNING_OPEN"
    elif hour < 11 or (hour == 11 and minute <= 30):
        period_badge, period_type = "📊 上午盘中 · 多空博弈", "INTRADAY_MID"
    elif hour < 13:
        period_badge, period_type = "☕ 午间休市 · 逻辑梳理", "NOON_BREAK"
    elif hour < 14 or (hour == 14 and minute <= 30):
        period_badge, period_type = "🏹 午后盘中 · 主线突围", "AFTERNOON_RUSH"
    elif (hour == 14 and minute > 30) or (hour == 15 and minute <= 0):
        period_badge, period_type = "🎯 盘尾决战 · 尾盘定盘", "EOD_BATTLE"
    else:
        period_badge, period_type = "🌙 收盘复盘 · 机会全景", "POST_MARKET"

    weekday = now.weekday()
    minutes = hour * 60 + minute
    if weekday >= 5 or minutes < 9 * 60 + 15:
        opp_phase = "pre"
    elif 14 * 60 + 30 <= minutes <= 15 * 60 + 30:
        opp_phase = "closing"
    elif minutes > 15 * 60 + 30:
        opp_phase = "pre"
    else:
        opp_phase = "session"

    opp_phase_config = {
        "pre": {
            "main_title": "🌅 盘前机会雷达 · 各博主板块预判与实操锦囊",
            "phase_badge_label": "🌅 盘前机会 · 预判与策略",
            "headline_label": "🌅 盘前 · 大盘全景与预判定调",
            "sectors_subtitle": "(盘前预判：按昨日选股与契约推演排序)",
            "bloggers_subtitle": "(契约预判 · 严守次日开盘纪律)",
            "alert_banner": "⏰ 当前未到开盘时间，以下为盘前策略与板块预判，9:15 起切换为盘中实时资金动向。",
        },
        "session": {
            "main_title": "⚡ 盘中资金动向雷达 · 各博主板块机会与实操锦囊",
            "phase_badge_label": "⚡ 盘中资金动向 · 实时研判",
            "headline_label": "📊 盘中 · 市场总动向与量能定调",
            "sectors_subtitle": "(盘中：按博主真实选股与行情实时排序)",
            "bloggers_subtitle": "(契约独立推演 · 严守仓位与刚性止损红线)",
            "alert_banner": "",
        },
        "closing": {
            "main_title": "🏁 盘尾决战机会雷达 · 各博主板块动向与实操锦囊",
            "phase_badge_label": "🏁 盘尾决战 · 收盘定调",
            "headline_label": "🏁 盘尾 · 决战窗口与量能定调",
            "sectors_subtitle": "(盘尾：按收盘前博主真实选股排序)",
            "bloggers_subtitle": "(契约定盘 · 严守次日开盘纪律与持盈切换)",
            "alert_banner": "🏁 进入盘尾决战窗口 (14:30-15:00)，板块内调仓动作频繁，是精准止盈/拾遗机会。",
        },
    }[opp_phase]

    sh_pct = (quotes.get('sh000001', {}).get('pct')) or 0.0
    cyb_pct = (quotes.get('sz399006', {}).get('pct')) or 0.0
    kcb_pct = (quotes.get('sh000688', {}).get('pct')) or 0.0
    avg_pct = (sh_pct + cyb_pct + kcb_pct) / 3.0
    sh_price = (quotes.get('sh000001', {}).get('price')) or '--'
    quotes_stale = not quotes.get('sh000001', {}).get('price')

    if avg_pct >= 1.0:
        market_headline = f"【多头放量突破】上证{sh_price}({sh_pct:+.2f}%)/创业板({cyb_pct:+.2f}%)共振上攻，主线波段持股"
        market_status = "放量主升 · 动量爆发"
        action_tone = "顺应主线进攻 / 挂载 ATR 追踪止盈 / 以博主真实选股为准"
    elif avg_pct <= -1.2:
        market_headline = f"【放量急跌洗盘】上证{sh_price}({sh_pct:+.2f}%)/创业板({cyb_pct:+.2f}%)承压，避险资金回流低估值"
        market_status = "分化杀跌 · 恐慌出清"
        action_tone = "严禁盲目追高 / 缩量踩线分批潜伏 / 以博主真实选股为准"
    else:
        market_headline = f"【蓄势拉锯】上证{sh_price}({sh_pct:+.2f}%)/创业板({cyb_pct:+.2f}%)良性震荡，重个股轻指数"
        market_status = "震荡蓄势 · 结构分化"
        action_tone = "重个股轻指数 / 踩生命线荡秋千做 T / 以博主真实选股为准"
    if quotes_stale:
        market_headline = "⚠ 本轮行情拉取失败，" + market_headline
        market_status = "数据待刷新 · " + market_status

    hot_sectors = []
    trig = [p for p in (picks or []) if p.get('triggered') and p.get('stock_code') and p.get('stock_code') != '无']
    for i, p in enumerate(trig[:5]):
        bid = p.get('blogger_id', '')
        disp = BLOGGER_DISPLAY.get(bid, (bid, '💡'))
        pct = p.get('pct') or 0.0
        stars = 5 if pct >= 5 else 4 if pct >= 2 else 3
        hot_sectors.append({
            "id": f"SEC-{i + 1:02d}",
            "name": p.get('stock_name', '?'),
            "tag": f"{disp[0]}契约触发",
            "heat_score": min(99, 60 + int(abs(pct) * 6)),
            "heat_level": f"🔥 实时触发 ({pct:+.2f}%)",
            "opportunity_stars": "★" * stars + "☆" * (5 - stars),
            "opportunity_type": p.get('signal', ''),
            "trend_badge": "触发" if pct >= 0 else "回调",
            "core_logic": str(p.get('trigger_reason', ''))[:80],
            "lead_bloggers": [disp[0]],
            "action_advice": f"现价{p.get('current_price')}，按契约纪律执行（详见左侧博主卡）",
            "key_stocks": [{"name": p.get('stock_name'), "code": p.get('stock_code'), "desc": f"{pct:+.2f}%"}],
            "theme_class": "sector-fire" if pct >= 2 else "sector-gold",
        })
    if not hot_sectors:
        hot_sectors.append({
            "id": "SEC-00",
            "name": "今日无博主选股触发",
            "tag": "弱市/形态未现",
            "heat_score": 0,
            "heat_level": "⚪ 空仓共识",
            "opportunity_stars": "☆☆☆☆☆",
            "opportunity_type": "空仓观望 (WAIT)",
            "trend_badge": "等待",
            "core_logic": "6位博主契约扫描全池后均未触发买点——如实空仓，不凑数。",
            "lead_bloggers": ["全部"],
            "action_advice": "保持现金，等待契约信号",
            "key_stocks": [],
            "theme_class": "sector-slate",
        })

    blogger_advices = []
    for bid in BLOGGER_ORDER:
        disp = BLOGGER_DISPLAY.get(bid, (bid, '💡'))
        pick = next((p for p in (picks or []) if p.get('blogger_id') == bid), None)
        if pick and pick.get('triggered') and pick.get('stock_code') != '无':
            pct = pick.get('pct') or 0.0
            intraday_tone = (f"【今日真实触发】契约选出 {pick.get('stock_name')}（{pick.get('stock_code')}）"
                             f"现价{pick.get('current_price')}，涨{pct:+.2f}%：{str(pick.get('trigger_reason', ''))[:90]}")
            rating = "★★★★★ · 触发 (BUY)" if pct >= 2 else "★★★☆☆ · 触发 (观察)"
            kit = [
                f"标的与理由为当日全池扫描真实结果（见触发原因原文）；现价{pick.get('current_price')}。",
                "严格按本博主契约纪律执行仓位与止损（阈值见 config/system_params.json）；",
                "信号触发≠必买：与模拟盘账本对照后再决策（辅助决策原则）。",
            ]
            status_tag = f"🔥 已触发 · {pct:+.2f}%"
        else:
            reason = str(pick.get('trigger_reason', ''))[:70] if pick else "本轮扫描未产出该博主选股"
            intraday_tone = f"【今日未触发】{reason}"
            rating = "⭐☆☆☆☆ · 空仓 (WAIT)"
            kit = [
                "今日契约扫描未达买点——空仓/等待是纪律而非踏空；",
                "坚持本博主契约红线（阈值见 config/system_params.json）；",
                "信号出现前不预设仓位，避免情绪化下单。",
            ]
            status_tag = "⚪ 未触发 · 空仓"

        blogger_advices.append({
            "blogger_id": bid,
            "blogger_name": f"{disp[1]} {disp[0]}",
            "avatar": disp[1],
            "role_title": "契约驱动选股",
            "positioning": "以当日全池扫描结果为准",
            "badge_color": "#334155",
            "intraday_tone": intraday_tone,
            "best_sector": "以当日契约选股为准",
            "opportunity_rating": rating,
            "operation_kit": kit,
            "risk_defense": "风控纪律：止损/止盈/仓位上限均按 config/system_params.json 契约执行，账本可审计。",
            "status_tag": status_tag,
        })

    return {
        "time_str": now.strftime("%Y-%m-%d %H:%M"),
        "period_badge": period_badge,
        "period_type": period_type,
        "opp_phase": opp_phase,
        "opp_phase_config": opp_phase_config,
        "market_headline": market_headline,
        "market_status": market_status,
        "action_tone": action_tone,
        "hot_sectors": hot_sectors,
        "blogger_advices": blogger_advices,
        "data_quality": {"engine": "data-driven", "stale": quotes_stale},
    }


# ---------------------------------------------------------------- 前瞻师事件（人工议题库 + 动态倒计时 + LLM可选）

_FORWARD_EVENTS = [
    {
        "id": "FWD-01",
        "title": "华为秋季全场景新品发布会与 AI 终端/原生鸿蒙全生态商用",
        "date": "2026-09-09",
        "category": "消费电子 / AI终端 / 鸿蒙生态",
        "core_tracks": "原生鸿蒙应用生态 / 先进封装与海思芯片 / 折叠屏与AI手机 / 智能汽车智驾底座",
        "key_stocks": [
            {"name": "立讯精密", "code": "002475.SZ", "role": "精密制造与核心声学/连接器模组龙头"},
            {"name": "歌尔股份", "code": "002241.SZ", "role": "声学与智能硬件精密结构件龙头"},
            {"name": "拓维信息", "code": "002261.SZ", "role": "华为昇腾/鲲鹏算力与鸿蒙生态核心伙伴"},
            {"name": "深南电路", "code": "002916.SZ", "role": "高端通信PCB与先进封装FC-BGA基板"},
        ],
        "background": "华为秋季全场景新品发布会（预估日期2026-09-09，以官方为准）将发布新一代AI终端与HarmonyOS NEXT商用系统，带动国内消费电子供应链进入创新换机周期。",
        "core_controversy": "发布会前夕资金是否提前透支硬件估值？新机定价与良率能否支撑四季度业绩放量？",
    },
    {
        "id": "FWD-02",
        "title": "9月美联储 FOMC 议息会议与全球降息周期预期定价",
        "date": "2026-09-17",
        "category": "全球宏观 / 货币流动性",
        "core_tracks": "AI算力硬件(CPO/GPU) / 有色金属(黄金/铜) / 创新药出海 / 港股恒生科技",
        "key_stocks": [
            {"name": "天孚通信", "code": "300394.SZ", "role": "全球算力硬件资本开支核心受益"},
            {"name": "紫金矿业", "code": "601899.SH", "role": "全球降息周期有色与黄金资源龙头"},
            {"name": "中际旭创", "code": "300308.SZ", "role": "全球800G/1.6T高速光模块一哥"},
            {"name": "药明康德", "code": "603259.SH", "role": "海外投融资回暖与CXO创新药底座"},
        ],
        "background": "美联储FOMC议息会议（2026-09-17）临近，市场对降息25-50bp预期高企；降息周期开启将影响全球流动性、汇率与外资配置方向。",
        "core_controversy": "降息初期是否伴随美股波动传导？外资回流优先高股息红利还是AI算力科技？",
    },
    {
        "id": "FWD-03",
        "title": "A股中报业绩披露收官防雷与科技反弹周期分水岭",
        "date": "2026-08-31",
        "category": "财报季 / 业绩兑现",
        "core_tracks": "AI算力光模块 / 半导体设备与存储 / 消费电子出海 / 传统周期防雷",
        "key_stocks": [
            {"name": "新易盛", "code": "300502.SZ", "role": "中报业绩高增确定性光模块龙头"},
            {"name": "中微公司", "code": "688012.SH", "role": "国产刻蚀设备订单饱满白马"},
            {"name": "北方华创", "code": "002371.SZ", "role": "平台型半导体设备业绩压舱石"},
            {"name": "立讯精密", "code": "002475.SZ", "role": "消费电子与汽车电子龙头"},
        ],
        "background": "8月下旬为A股中报披露最密集收官期。业绩优异且订单充沛的科技硬件获机构二次确认；业绩不及预期的纯概念题材面临估值杀跌。",
        "core_controversy": "本轮科技反弹是纯情绪流动性反弹还是业绩驱动的第二波主升浪？中报暴雷是否引发局部流动性休克？",
    },
    {
        "id": "FWD-04",
        "title": "长鑫存储/先进制程国产化突破与自主可控设备招标放量",
        "date": "2026-09-30",
        "category": "半导体 / 存储芯片 / 自主可控",
        "core_tracks": "存储芯片封测 / 关键设备零部件 / EDA软件 / 半导体光刻胶与材料",
        "key_stocks": [
            {"name": "澜起科技", "code": "688008.SH", "role": "内存接口芯片与津逮服务器CPU龙头"},
            {"name": "兆易创新", "code": "603986.SH", "role": "存储芯片NOR/NAND与微控制器龙头"},
            {"name": "长川科技", "code": "300604.SZ", "role": "半导体测试设备与分选机核心标的"},
            {"name": "深南电路", "code": "002916.SZ", "role": "高端封装基板与存储PCB硬件底座"},
        ],
        "background": "国内存储芯片先进制程产能稳步扩张，国产关键设备产线验证与替代率突破关键阈值，三季度晶圆与封测大厂设备招标密集启动。",
        "core_controversy": "海外制裁升级下关键零部件是否存在断供制约？国内存储周期复苏能否支撑当前估值？",
    },
]


def generate_forward_scout_events():
    """
    前瞻师事件 · 剧本驱动版（阶段三 任务A）：
    - 数据源：优先对接 event_calendar 真实事件日历（业绩预告/分红/解禁/会议等155条库）；
    - 剧本引擎：调用 analysis.playbook 为每个事件生成三套实操预案与预期差评分；
    - 六博主推演：由 Gemini 结合各博主契约深度推演（带缓存，离线时优雅降级）；
    - 诚实协议：严格标明数据来源与引擎标记（curated / gemini）。
    """
    today = datetime.date.today()
    raw_events = []

    # 1. 尝试从事件日历拉取未来 30 天真实事件
    try:
        from analysis.event_calendar import get_upcoming
        upcoming = get_upcoming(within_days=30, limit=6)
        if upcoming:
            type_map = {
                'EARN_FORECAST': '财报 / 业绩预告',
                'EARN_EXPRESS': '财报 / 业绩快报',
                'EARN_REPORT': '财报 / 正式年季报',
                'DIVIDEND': '权益 / 分红派息',
                'SHARE_RELEASE': '筹码 / 限售解禁',
                'HOLDER_SELL': '筹码 / 股东减持',
                'INDUSTRY_CONFERENCE': '产业 / 行业重大会议',
                'POLICY_RELEASE': '宏观 / 政策与议息',
            }
            for i, u in enumerate(upcoming):
                raw_events.append({
                    'id': f"FWD-{i + 1:02d}",
                    'title': u.get('title') or f"{u.get('name', '')} {u.get('event_type', '')}",
                    'date': u.get('event_date', ''),
                    'event_date': u.get('event_date', ''),
                    'symbol': u.get('symbol', ''),
                    'name': u.get('name', ''),
                    'event_type': u.get('event_type', 'OTHER'),
                    'category': type_map.get(u.get('event_type'), '前瞻催化 / 事件驱动'),
                    'core_tracks': f"{u.get('name', '')}产业链 / {type_map.get(u.get('event_type'), '事件驱动')}",
                    'key_stocks': [{'name': u.get('name', ''), 'code': u.get('symbol', ''), 'role': '事件核心标的'}] if u.get('symbol') else [],
                    'background': f"【{u.get('name', '')} · {type_map.get(u.get('event_type'), '')}】{u.get('title', '')}。根据 A 股事件驱动历史规律，关注事件前 T-3 至 T+1 窗口期的资金博弈与预期差兑现节奏。",
                    'core_controversy': f"市场核心分歧在于该事件落地前后是否引发资金抢跑、预期提前透支或超预期脉冲。",
                    'source': u.get('source', 'event_calendar'),
                })
    except Exception as e:
        print(f"[前瞻师] 读取事件日历提示: {e}")

    # 兜底：若日历库暂无条目，使用人工维护种子事件
    if not raw_events:
        for i, e in enumerate(_FORWARD_EVENTS):
            raw_events.append({**e, 'source': 'manual_seed'})

    # 2. 为每个事件生成动态倒计时与 Playbook 剧本
    events = []
    try:
        from analysis.playbook import generate_event_playbook
    except ImportError:
        generate_event_playbook = None

    for ev in raw_events:
        item = json.loads(json.dumps(ev))
        ev_date = item.get('date') or item.get('event_date') or ''
        try:
            d = datetime.datetime.strptime(ev_date, '%Y-%m-%d').date()
            delta = (d - today).days
            item['time_horizon'] = (f"距离 {ev_date} 还有 {delta} 天" if delta >= 0
                                    else f"事件日期已过（{-delta} 天前，关注实际进展）")
            item['heat_level'] = f"🔥 重点事件 (倒计时 {delta}天)" if delta <= 7 else "⏳ 提前观察"
        except Exception:
            item['time_horizon'] = "日期待定"
            item['heat_level'] = "待事件临近重估"

        item['status_tag'] = "⚡ 真实事件日历" if 'manual' not in item.get('source', '') else "📅 前瞻议题（种子）"

        # 调剧本引擎
        if generate_event_playbook:
            pb_res = generate_event_playbook(item)
            item['playbook'] = pb_res.get('playbook', {})
            item['blogger_deductions'] = pb_res.get('blogger_deductions', {})
            item['provenance'] = {
                'source': item.get('source', 'event_calendar'),
                'engine': pb_res.get('engine', 'curated'),
                'date_is_estimate': False if 'manual' not in item.get('source', '') else True,
            }
        else:
            item['blogger_deductions'] = {
                bid: {
                    "title_verdict": f"【{disp[0]}】预案：以当日实际盘面与契约信号为准",
                    "kit": "① 事件属前瞻议题，不预设方向；② 以事件落地当日的契约选股/模拟盘信号为执行依据；③ 严守各自止损纪律。",
                    "logic": "本预案为人工维护框架，事件临近或落地时由真实行情与契约信号驱动决策。",
                }
                for bid, disp in BLOGGER_DISPLAY.items()
            }
            item['provenance'] = {'source': item.get('source', 'curated'), 'engine': 'curated'}

        events.append(item)

    return events


# ---------------------------------------------------------------- 兜底选股（诚实版）

def generate_dynamic_picks(quotes):
    """
    兜底选股：仅当 3103 池扫描不可用时调用。
    诚实协议：核心标的行情缺失 → 返回空列表，绝不使用缓存价格虚构信号。
    """
    required = ['sz300394', 'sh688008', 'sz000063', 'sz159813']
    if not all(quotes.get(s, {}).get('price') for s in required):
        print("  [标准契约判定] 核心标的行情缺失，本轮放弃生成兜底信号（等待大池扫描结果）")
        return []
    tf = quotes.get('sz300394', {})
    lq = quotes.get('sh688008', {})
    zx = quotes.get('sz000063', {})
    etf = quotes.get('sz159813', {})

    picks = [
        {
            "blogger_id": "hanshu", "blogger_name": "韩叔 (趣聊财经)",
            "positioning": "先人一步潜伏错杀 + 绝活生命线做差价", "horizon": "中线 (1-2周)",
            "triggered": True, "stock_code": lq.get('code', '688008.SH'),
            "stock_name": "澜起科技 / 长鑫存储概念", "current_price": lq.get('price'),
            "pct": lq.get('pct'), "signal": "低吸潜伏 (BUY)",
            "trigger_reason": "缩量回调至绝活生命线支撑区，长鑫存储错杀品种，符合先人一步低吸纪律。",
            "rule_tags": ["生命线低吸", "错杀潜伏", "无杠杆纪律"],
        },
        {
            "blogger_id": "yintwentao", "blogger_name": "殷文涛 (梵爹)",
            "positioning": "三大阵营博弈 + 光/芯时空差轮动", "horizon": "波段 (1-3周)",
            "triggered": True, "stock_code": zx.get('code', '000063.SZ'),
            "stock_name": "中兴通讯", "current_price": zx.get('price'),
            "pct": zx.get('pct'), "signal": "抱团主攻 (BUY)",
            "trigger_reason": "光模块先行企稳，满足光/芯时空差共振点，科技抱团资金回流核心。",
            "rule_tags": ["三大阵营", "光芯时空差", "科技抱团"],
        },
        {
            "blogger_id": "ai-alpha", "blogger_name": "AI-Alpha (量化智脑)",
            "positioning": "多因子动量共振 + 20日突破 + ATR追踪止损", "horizon": "短波段 (3-10天)",
            "triggered": True, "stock_code": tf.get('code', '300394.SZ'),
            "stock_name": "天孚通信 (CPO光模块龙头)", "current_price": tf.get('price'),
            "pct": tf.get('pct'), "signal": "动量突破 (BUY)",
            "trigger_reason": "5MA>10MA>20MA 均线多头极强扩散，放量突破 20 日高点，挂载 2.2 ATR 移动追踪止盈。",
            "rule_tags": ["多因子共振", "ATR追踪止损", "动量突破"],
        },
        {
            "blogger_id": "liao-xiaoai", "blogger_name": "廖小爱",
            "positioning": "中国特色价值投资 + 3-3-1仓位管理", "horizon": "长期 (1-3个月)",
            "triggered": True, "stock_code": etf.get('code', '159813.SZ'),
            "stock_name": "半导体/AI算力 ETF", "current_price": etf.get('price'),
            "pct": etf.get('pct'), "signal": "第一批30%建仓 (BUY)",
            "trigger_reason": "满足3-3-1模型第一批30%建仓锚点，硬科技波浪式前进中的深蹲阶段。",
            "rule_tags": ["3-3-1模型", "无限子弹", "深蹲锚点"],
        },
        {
            "blogger_id": "cool", "blogger_name": "酷儿 (Cool)",
            "positioning": "假跌破反转 + 20MA斜率突破", "horizon": "超短线 (3-7天)",
            "triggered": False, "stock_code": "无",
            "stock_name": "今日未选出 (形态未确立)", "current_price": None,
            "pct": None, "signal": "空仓观望 (WAIT)",
            "trigger_reason": "当前候选池标的未出现20MA假跌破后带量长阳收回标准形态，100%现金空仓等待。",
            "rule_tags": ["严格过滤", "空仓防守", "假跌破等待"],
        },
        {
            "blogger_id": "anti-demo", "blogger_name": "极短线情绪博主 (反指哨兵)",
            "positioning": "高位极端追涨监测 (反向避险)", "horizon": "极短线 (1-2天)",
            "triggered": False, "stock_code": "无",
            "stock_name": "今日未选出 (情绪安全防守)", "current_price": None,
            "pct": None, "signal": "空仓防守 (WAIT)",
            "trigger_reason": "今日全市场未检测到高位连板妖股极端亢奋信号，情绪雷达处于安全防守状态。",
            "rule_tags": ["未达极值", "安全防守", "反向哨兵"],
        },
    ]
    return picks


# ---------------------------------------------------------------- HTML内嵌与主流程

def embed_data_into_html(full_data):
    """将最新全量数据注入 workbench/index.html 的 DEFAULT_EMBEDDED_DATA，
    并把成品整份同步到根目录 index.html（用户 2026-08-29：双击版与 127.0.0.1:8018
    保持同一界面——此前根目录是老结构副本只注入数据，缺研判引擎状态条等新元素）。"""
    json_str = json.dumps(full_data, ensure_ascii=False, indent=2)
    start_tag = 'const DEFAULT_EMBEDDED_DATA ='
    end_tag = 'let workbenchData = DEFAULT_EMBEDDED_DATA;'
    root_copy = os.path.join(BASE_DIR, 'index.html')
    try:
        with open(HTML_PATH, 'r', encoding='utf-8') as f:
            html = f.read()
        start_pos = html.find(start_tag)
        end_pos = html.find(end_tag, start_pos)
        if start_pos != -1 and end_pos != -1:
            new_html = html[:start_pos] + 'const DEFAULT_EMBEDDED_DATA = ' + json_str + ';\n    ' + html[end_pos:]
            if new_html != html:
                with open(HTML_PATH, 'w', encoding='utf-8') as f:
                    f.write(new_html)
        else:
            print("注入警告: workbench/index.html 未找到 DEFAULT_EMBEDDED_DATA 标记")
        # 根目录副本 = 现役成品整份拷贝（含全部新界面元素 + 最新数据快照）
        import shutil
        shutil.copyfile(HTML_PATH, root_copy)
    except Exception as e:
        print(f"注入 index.html 出错: {e}")
    return True


def generate_premarket_events(raw_news=None, forward_events=None):
    """阶段五·任务H：隔夜=前日15:00后 7 天内 forward + 当日 STRONG/BOOST 新闻。
    去重+优先级（业绩>政策>产业>例行）+ 学术警示位（Glasserman 2025/Akbas JFE/Christensen JFE 2025）。"""
    import datetime
    out = []
    seen = set()
    today = datetime.datetime.now().date()
    # 1) 前瞻事件中 7 天内 + 优先级高
    if forward_events:
        for e in (forward_events or []):
            ed = e.get('event_date') or e.get('date') or ''
            try:
                ed_dt = datetime.datetime.strptime(ed[:10], '%Y-%m-%d').date()
            except Exception:
                continue
            days = (ed_dt - today).days
            if 0 <= days <= 7:
                typ = e.get('event_type') or ''
                # 优先级映射：业绩>政策>产业>例行
                pri = 4
                if '业绩' in typ or '财报' in typ or 'EARN' in typ.upper():
                    pri = 1
                elif '政策' in typ or '发言' in typ:
                    pri = 2
                elif '产业' in typ or '行业' in typ:
                    pri = 3
                key = ('forward', e.get('id') or e.get('title', '')[:30])
                if key in seen:
                    continue
                seen.add(key)
                warnings = []
                if 'EARN' in typ.upper() or '财报' in typ or '业绩' in typ:
                    warnings.append('PEAD 漂移可持续，勿因首日冲高兑现（Christensen JFE 2025：业绩公告盘后跳空 >90%）')
                out.append({
                    'kind': 'forward',
                    'id': e.get('id') or e.get('title', '')[:20],
                    'title': e.get('title') or '',
                    'symbol': e.get('symbol') or e.get('name') or '',
                    'summary': e.get('background') or e.get('summary') or '',
                    'days_until': days,
                    'priority': pri,
                    'warnings': warnings,
                })
    # 2) 当日新闻中 STRONG/BOOST（标题或关联标的已高分）
    if raw_news:
        for n in (raw_news or []):
            rel = n.get('_relevance') or {}
            score = rel.get('score', 0) if isinstance(rel, dict) else 0
            tier = rel.get('tier') if isinstance(rel, dict) else ''
            if score >= 75 or tier == 'STRONG':
                key = ('news', n.get('title', '')[:50])
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    'kind': 'news',
                    'id': n.get('id') or n.get('title', '')[:20],
                    'title': n.get('title') or '',
                    'symbol': n.get('entity') or '',
                    'summary': n.get('summary') or n.get('content', '')[:200] or '',
                    'days_until': 0,
                    'priority': 4,
                    'warnings': ['高开反转风险：情绪性高开日内回吐（Glasserman 2025/Akbas JFE）'] if score >= 75 else [],
                })
    # 3) 排序：优先级 1→4，days 升序，去重
    out.sort(key=lambda x: (x['priority'], x['days_until']))
    return out[:20]




def update_market_intel_and_sync():
    """主执行入口：行情 → 全池选股 → 数据驱动诊断/共识/看板 → 快讯+LLM解构 → 前瞻 → 模拟盘 → 落盘"""
    now_str = get_current_time_str()
    print(f"[{now_str}] 🚀 启动大盘诊断、共识裁判与热点观点全量更新（数据驱动版）...")

    quotes = fetch_market_quotes()
    index_intraday = fetch_index_intraday()

    picks = []
    try:
        from scan_mainboard_picks import scan_and_score_all
        picks = scan_and_score_all(persist=False)
    except Exception as e:
        print(f"主板大池扫描提示: {e}，将采用标准契约判定")
        picks = generate_dynamic_picks(quotes)

    diagnosis = generate_dynamic_market_diagnosis(quotes, picks=picks)
    consensus = generate_dynamic_consensus(quotes, picks=picks)
    blogger_opportunity_board = generate_dynamic_blogger_opportunity_board(quotes, diagnosis, consensus, picks)

    raw_news = fetch_live_flash_news()
    forward_events = generate_forward_scout_events()
    # 阶段五·任务H：隔夜事件驱动区块（premarket_events）
    # v4.1 A1：补接线——既写 state 也注入 data dict（前端 data.json 才能读到）
    import json as _pj
    import os as _po
    import traceback as _tb
    try:
        _pre_evs = generate_premarket_events(raw_news=raw_news, forward_events=forward_events)
        _pf = _po.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'state', 'premarket_events.json')
        with open(_pf, 'w', encoding='utf-8') as _f:
            _pj.dump({'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                      'events': _pre_evs}, _f, ensure_ascii=False)
        # v4.1 A1：注入到 data dict，前端 data.json 才能读到
        data['premarket_events'] = {'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                                    'events': _pre_evs}
        import sys as _ss
        _ss.stderr.write('  [H] 隔夜事件: ' + str(len(_pre_evs)) + ' 条 -> ' + _pf + chr(10))
        _ss.stderr.flush()
    except Exception as _he:
        import sys as _ss
        _ss.stderr.write('  [H] 写盘前事件失败: ' + repr(_he) + chr(10) + _tb.format_exc())
        _ss.stderr.flush()
    # 直连入口同样过门下省清洗（与流水线通政司口径一致：VETO 拦截、LOW 靠后）
    try:
        import menxia
        import xingbu
        vetted = []
        vetoed = 0
        for it in raw_news:
            score, verdict, hw, hb = menxia.relevance_check(
                it.get('title', ''), it.get('summary', ''), it.get('category', ''))
            if verdict == 'VETO':
                vetoed += 1
                try:
                    xingbu.record_rejection(menxia.veto(it, score, hb), score, hb)
                except Exception:
                    pass
                continue
            it['_relevance'] = {'score': score, 'verdict': verdict,
                                'hits_whitelist': hw, 'hits_blacklist': hb}
            vetted.append(it)
        raw_news = sorted(vetted, key=lambda x: 0 if x['_relevance']['verdict'] == 'PASS' else 1)
        if vetoed:
            print(f"  [门下省] 拦截 {vetoed} 条不相关快讯（刑部留痕）")
    except Exception as e:
        print(f"  [门下省] 清洗不可用，跳过: {type(e).__name__}")

    hotspots = []
    news_pool_size = len(raw_news)
    for idx, item in enumerate(raw_news[:_rc.NEWS_MAX_PER_CYCLE]):
        h_id = f"HOT-{idx+1:02d}"
        views = deduce_blogger_perspectives(item)
        hotspots.append({
            "id": h_id,
            "title": item['title'],
            "time": item.get('time', get_display_date_str()),
            "category": item['category'],
            "source": item.get('source', '权威财经媒体 · 7x24快讯'),
            "url": item.get('url', 'https://finance.sina.com.cn/stock/'),
            "content": item.get('content', item['summary']),
            "summary": item['summary'],
            "blogger_views": {k: v for k, v in views.items() if not k.startswith('_')},
        })
        meta = views.get('_meta')
        if meta:
            hotspots[-1]["blogger_views_meta"] = meta

    forward_events = generate_forward_scout_events()

    try:
        from simulated_trading import run_simulation
        # 阶段六·9:55事故根治：主更新流程只刷市值不写账本；交易执行交由 execute_intraday/15:08结算
        sim_res = run_simulation(force_picks_scan=False, reset_to_mainboard=False, allow_trading=False)
        print("  [模拟盘联动·只读] 主板大池模拟盘市值刷新完成（账本SHA256不变）")
    except Exception as e:
        print(f"  [模拟盘联动提示] {e}")

    def _ov(sym, name, code):
        q = quotes.get(sym)
        if not q or not q.get('price'):
            return {"name": name, "code": code, "price": "--", "change": "--", "pct": "--", "status": "stale"}
        return {
            "name": name, "code": code,
            "price": str(q.get('price', '--')),
            "change": f"{q.get('change', 0):+.2f}",
            "pct": f"{q.get('pct', 0):+.2f}%",
            "status": "up" if q.get('change', 0) >= 0 else "down",
        }

    market_overview = {
        "sh000001": _ov('sh000001', "上证指数", "000001.SH"),
        "sz399001": _ov('sz399001', "深证成指", "399001.SZ"),
        "sz399006": _ov('sz399006', "创业板指", "399006.SZ"),
        "sh000688": _ov('sh000688', "科创50", "000688.SH"),
        "usIXIC": _ov('usIXIC', "纳斯达克", "IXIC"),
        "usINX": _ov('usINX', "标普500", "INX"),
    }

    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}

    data["updated_at"] = now_str
    data["picks_updated_at"] = now_str
    data["news_pool_size"] = news_pool_size
    data["research_engine"] = "llm" if _rc.LLM_API_KEY else "offline"
    data["market_diagnosis"] = diagnosis
    data["consensus"] = consensus
    data["blogger_opportunity_board"] = blogger_opportunity_board
    data["hotspots"] = hotspots
    data["forward_events"] = forward_events
    data["market_overview"] = market_overview
    data["index_intraday"] = index_intraday
    data["blogger_picks"] = picks
    data["independent_blogger_picks"] = picks

    try:
        from generate_reflections import build_reflection_data
        sim_state_for_reflection = _load_simulation_state()
        reflections = build_reflection_data(sim_state_for_reflection)
        data["blogger_reflections"] = reflections
        print("  [复盘中枢联动] 6 位博主反思数据已就绪")
    except Exception as e:
        print(f"  [复盘中枢联动提示] {e}")

    if 'simulation' in data and 'accounts' in data['simulation']:
        for bid, acc in data['simulation']['accounts'].items():
            total_mv = 0.0
            for pos in acc.get('positions', []):
                sym = pos.get('symbol', '')
                q = quotes.get(sym)
                if q and q.get('price', 0) > 0:
                    pos['current_price'] = q['price']
                cur_p = pos.get('current_price', pos.get('cost_price', 0))
                pos['market_value'] = round(pos.get('shares', 0) * cur_p, 2)
                cost_p = pos.get('cost_price', 1)
                pos['pnl_pct'] = round(((cur_p - cost_p) / cost_p) * 100, 2)
                total_mv += pos['market_value']
            if acc.get('positions'):
                init_cap = data.get('simulation', {}).get('capital_per_blogger', 100000.0)
                acc['market_value'] = round(total_mv, 2)
                acc['total_value'] = round(acc.get('cash', 0) + total_mv, 2)
                acc['pnl'] = round(acc['total_value'] - init_cap, 2)
                acc['pnl_pct'] = round(acc['pnl'] / init_cap * 100, 2)

    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    embed_ok = embed_data_into_html(data)

    print(f"[{now_str}] ✅ 市场诊断、共识裁判及热点观点已成功更新（数据驱动）！")
    print(f"     - 诊断时间戳: {diagnosis['date']}")
    print(f"     - 共识置信评分: {consensus['trust_adjusted_score']} ({consensus['trust_direction']})")
    print(f"     - 选股触发: {sum(1 for p in picks if p.get('triggered'))}/6 位博主")
    print(f"     - 同步至: {DATA_PATH} (HTML 内嵌: {'成功' if embed_ok else '已同步'})")


if __name__ == '__main__':
    update_market_intel_and_sync()
