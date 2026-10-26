# -*- coding: utf-8 -*-
"""
scripts/pipeline/menxia.py
门下省 · Menxia Sheng（内容相关性风控 · 四维打分制 v2）

v2 升级（2026-08-28，用户《阶段四改造指南》）：
- 从二元 PASS/LOW/VETO 升级为 0-100 打分制：主体(①) + 事件(②) + 池内命中(③) + 影响广度(④)
- ≥75 STRONG（强相关，触发逐条深度研判）
- 45-74 MEDIUM（中等，进常规折叠区，无博主评论）
- <45 WEAK（含原黑名单=0分，强不相关）
- 30-70 灰区交给 llm_analyst.batch_relevance_scan 读正文裁决
- 影响广度(④)用 THEME_MAP 预生成的「池内受益股名单」常量，避免概念股全名单虚高

调用方约定：relevance_check() 保留原签名（score, verdict, hits_w, hits_b）→ verdict
统一为 STRONG/MEDIUM/WEAK/VETO；下游读 tier 时也读 score。
"""

import re
import os
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------- 主体权重（维度①） ----------------
# 格式：keyword → 分数（累加但封顶，单一维度最大 35）
SUBJECT_HIGH = {  # +30~35
    '大基金': 35, '汇金': 35, '社保': 30, '全国社保基金': 30, '国家队': 30, '证金': 30,
}
REGULATOR = {  # +30
    '央行': 30, '人民银行': 30, '证监会': 30, '财政部': 30, '发改委': 30, '国务院': 30,
    '交易所': 30, '统计局': 30, '银保监会': 30, '国家金融监督管理总局': 30, '国资委': 30,
}
# 全球产业链龙头（用户 2026-08-28 追加，正面反例：英伟达业绩不能再被打到 25 分）
GLOBAL_CATALYSTS = {  # +25
    '英伟达': 25, 'NVIDIA': 25, '苹果': 25, 'Apple': 25, '特斯拉': 25, 'Tesla': 25,
    '台积电': 25, 'TSMC': 25, 'AMD': 25, 'OpenAI': 25, '微软': 25, 'Microsoft': 25,
    '谷歌': 25, 'Google': 25, 'Meta': 25,
}
INTL_MACRO = {  # +25
    '美联储': 25, 'CPI': 25, '非农': 25, '加息': 25, '降息': 25, 'FOMC': 25,
    '鲍威尔': 25, '美债': 20, '美元指数': 20, '纳斯达克': 15, '标普': 15,
}
INDUSTRY_ASSOC = {  # +15~20
    '行业协会': 15, '产业链': 10, '央企': 15, '国资委': 15, '工信部': 20, '科技部': 15,
}
# 机构资金主体（用户 2026-08-28 晚校准：机构动向是波段交易主粮）
INSTITUTION = {  # +10~25
    '北向资金': 25, '外资': 25, '社保基金': 25, '社保': 25, '险资': 15, '公募': 15,
    '私募': 15, '长线资金': 15, 'ETF': 10, '公募基金': 15, '机构资金': 15,
}
A_SHARE_LEADER = {  # +10
    '龙头': 5, '白马': 5, '核心资产': 10,
}
# 算分时按集合并去重（同一段文本可能同时命中央行+财政部时取高+5，不再叠加）

# ---------------- 事件类型（维度②） ----------------
EVENT_TIER = {
    '货币政策': 28, '财政政策': 28, '产业政策': 28,
    '超预期': 25, '业绩超预期': 25, '财报超预期': 25, '大超预期': 25,
    '大幅增持': 25, '国家队增持': 25, '举牌': 25,
    '降准': 25, '降息': 25, '回购': 18,
    '重组': 20, '并购': 20, '借壳': 20, '重大资产重组': 22,
    '定增': 18, '回购股份': 18, '中标': 18, '签约': 15,
    '业绩预增': 20, '业绩预减': 15, '业绩快报': 15, '业绩预告': 15,
    '大幅减持': 12, '减持': 5, '解禁': 12, '大额解禁': 15,
    'IPO': 12, '上市': 8, '退市': 15, 'ST': 10,
    '举牌': 25, '要约收购': 22, '实控人变更': 20,
    '涨价': 15, '提价': 15, '限产': 12, '扩产': 12,
    '新规': 20, '新规发布': 20, '集采': 20, '集中采购': 20, '涨停': 8,
    # 机构资金行为（用户 2026-08-28 晚校准）
    '调仓': 18, '加仓': 15, '吸金': 15, '抱团': 12, '样本股': 15,
    '业绩': 10, '净赚': 12,
}

# 业绩硬数据正则：净赚/净利润/同比 + 大额增速（3位数%或N倍/翻番）→ 额外+15
_RE_EARNINGS_SHOCK = re.compile(
    r'(同比增长|净赚|净利润|同比).{0,18}(超?\d{3,}(\.\d+)?%|\d+(\.\d+)?倍|翻[一两三]番)')

# 行情异动（方案二，用户 2026-08-28）：大宗商品/汇率/贵金属 现价异动保底MEDIUM
# v4.1 扩容（用户 2026-09-01）：补 A 股板块涨跌停词族 + 群体性信号
_RE_MARKET_MOVE_VERB = re.compile(
    r'失守|跌破|上破|突破关键|暴涨|暴跌|新高|新低|'
    r'涨停|跌停|涨停潮|跌停潮|多股涨停|批量跌停|炸板|触及涨停|封板|大跌|大涨'
)
_RE_MARKET_MOVE_SUBJ = re.compile(r'黄金|金价|白银|期银|期金|原油|油价|铜价|美元|美债|人民币|铁矿|焦煤|焦炭')
# v4.1 A股板块主体（双通道第二通道）
_RE_ASHARE_BOARD = re.compile(
    r'房地产|地产|券商|银行|白酒|半导体|芯片|人工智能|AI|算力|'
    r'短剧|传媒|影视|游戏|机器人|低空|固态电池|光伏|锂电|'
    r'创新药|中药|军工|有色|煤炭|钢铁|化工|汽车|新能源车|'
    r'消费电子|储能|稀土|石油|低空经济|数据要素'
)
# 群体性信号（仅标题命中时触发 STRONG 加分）
_RE_BOARD_RALLY = re.compile(r'涨停潮|多股涨停|批量跌停|跌停潮|概念股集体|板块集体|全线上涨|全线下跌')
# 极端波动升级线：±3% 以上或"暴涨/暴跌"字样 → STRONG
_RE_EXTREME_MOVE = re.compile(r'(跌|涨|上行|下行)\s?超?\s?([3-9](\.\d+)?|1\d+(\.\d+)?)\s?%|暴涨|暴跌')

# 交锋词封顶（v4.1 R4）：仅人物八卦，政策/地缘/热榜均豁免
_RE_PERSONAL_SPAT = re.compile(r'开撕|回怼|隔空喊话|互怼|骂战')
_RE_POLICY_GEO_HIT = re.compile(r'关税|制裁|贸易战|反制|军事|海峡|战争|央行|财政部|白宫|国务院|联储|美联储|证监会')

# ST/退市降级（方案一，用户 2026-08-28）：不碰ST，除非持仓股
_RE_ST_CAP = re.compile(r'\*?ST\b|退市|终止上市|退市风险|停牌核查')

# 复合事件（命中"主体词+事件词"组合时再加分，单维度不封顶与 EVENT_TIER 共同求和）
COMPOUND_EVENTS = {
    # 大基金/国家队/汇金 + 任何投入/增持/布局 → 国家级大额资本动作 +20
    ('大基金', '投入'): 20, ('大基金', '增持'): 20, ('大基金', '成立'): 20, ('大基金', '布局'): 20, ('大基金', '投向'): 20,
    ('国家队', '增持'): 20, ('国家队', '买入'): 20,
    ('汇金', '增持'): 20, ('汇金', '买入'): 20,
    # 美联储/央行 + 降息/降准 + 具体数值 → 量化宽松事件 +25
    ('美联储', '降息'): 25, ('美联储', '降准'): 25, ('美联储', '加息'): 25,
    ('央行', '降准'): 25, ('央行', '降息'): 25, ('央行', '加息'): 25,
    # 监管主体 + 新规/办法 → 资本市场制度事件 +25
    ('证监会', '新规'): 25, ('证监会', '办法'): 25, ('证监会', '发布'): 18,
    # 全球龙头 + 业绩/财报/指引 → 全球科技周期事件 +20
    ('英伟达', '财报'): 20, ('NVIDIA', '财报'): 20, ('英伟达', '业绩'): 20,
    ('苹果', '业绩'): 18, ('Apple', '业绩'): 18,
    ('特斯拉', '业绩'): 18, ('Tesla', '业绩'): 18,
    ('台积电', '业绩'): 18, ('TSMC', '业绩'): 18,
    # 行业景气叙事型（无具体主体+无具体事件）→ 板块景气 +15（与"叙事型"区分）
    ('板块', '高增'): 15, ('板块', '兑现'): 12, ('板块', '景气'): 12,
    ('行业', '高增'): 15, ('行业', '兑现'): 12, ('行业', '景气'): 12,
    # 政策导向 + 行业 → 政策驱动型 +20
    ('政策', '投向'): 15, ('政策', '扶持'): 15, ('政策', '利好'): 12,
    # 行业景气叙事型（无具体主体+无具体公司）→ 板块景气 +18（区别于纯叙事）
    ('需求高增',): 18, ('景气持续',): 18, ('业绩兑现',): 15, ('业绩兑现期',): 15,
    ('步入兑现',): 15, ('加速兑现',): 15, ('景气延续',): 15,
    # 板块异动（盘中直接变化）→ 板块异动 +12（兼容"板块午后异动"等插入词）
    ('板块异动',): 12, ('板块', '异动'): 12, ('板块拉升',): 12, ('板块走强',): 8, ('板块走弱',): 8,
    # 机构资金行为复合（用户 2026-08-28 晚校准：8条压测后补全）
    ('社保', '调仓'): 25, ('社保', '布局'): 20, ('资金', '抱团'): 20,
    ('ETF', '吸金'): 18, ('指数', '调入'): 15, ('指数', '样本'): 15,
    ('资金', '借道'): 18, ('基金', '布局'): 12, ('基金', '加仓'): 15,
    ('资金', '动向'): 12, ('指数', '调整'): 12, ('基金', '挖掘'): 15,
}

# ---------------- 影响广度（维度④）：池内受益股名单 ----------------
# 题材传导映射：主题 → 池内受益股代码清单（仅主板池 000/001/002/003/600/601/603 开头；
# 创业板/科创板不在3103池——中际旭创/新易盛等 300 开头已剔除，避免概念全名单虚高）
THEME_MAP = {
    '算力链': ['000063', '000977', '000938', '000725', '600588', '600498', '600522',
              '600487', '600845', '600271', '000021', '002415', '002230', '000768',
              '600050', '600118', '000547', '002544', '002463', '002916', '600183',
              '002130', '600601', '603019', '600850', '002261', '000034', '600839',
              '601138', '002837', '002130', '600588'],
    '光通信': ['600498', '600487', '600522', '600050', '600118', '002544', '002281',
              '601869', '600105', '002491', '000586', '000070', '600345'],
    'PCB': ['600183', '002916', '002463', '002475', '002384', '600460', '002600',
           '002436', '600699'],
    '半导体': ['600584', '600460', '600171', '002371', '002129', '603501', '600360',
              '002049', '600667', '601982', '002156', '600661', '002185', '600747',
              '603986', '002077', '603160', '600460', '002518'],
    '新能源': ['002594', '002460', '002074', '600905', '601012', '601615', '002709',
              '600089', '002518', '601127', '600406'],
    '汽车整车': ['600104', '000625', '601238', '000800', '600166', '601127', '000957',
              '600741', '002594'],
    '消费电子': ['002475', '000725', '002241', '002384', '600745', '002036', '000049',
              '600260', '002416', '603160'],
    '白酒': ['600519', '000858', '000568', '000596', '600809', '600702', '000860',
            '600559', '603369', '600199', '600197', '603919', '002646', '000995'],
    '医药': ['600276', '000538', '600436', '000661', '600196', '002422', '000999',
            '600084', '002007', '600084'],
    '银行': ['601398', '601939', '601288', '600036', '601166', '600000', '600015',
            '601328', '600016', '601818', '600919', '601169', '601229'],
    '保险': ['601318', '601628', '601336', '601601'],
    '券商': ['600030', '600837', '601688', '600999', '601066', '601211', '002736',
            '600918', '601456', '601995'],
    '地产': ['000002', '001979', '600048', '600606', '000402', '600340', '600383'],
    '黄金': ['600547', '601899', '600489', '002155', '601020', '000975', '002716',
            '600988', '002237', '601069', '001337'],
    '有色': ['601600', '601899', '603799', '601137', '002460', '000831', '000060',
            '600362', '601212', '000807', '002203', '600961'],
    '钢铁': ['600019', '600010', '000708', '000932', '600808', '000898', '002110'],
    '化工': ['600309', '000301', '600346', '600028', '002493', '000792', '600688',
            '000830', '002648', '600486'],
    '军工': ['600760', '000768', '600316', '600435', '002025', '000547', '600893',
            '600765', '002179', '600118', '600862', '002414', '601989'],
    '机器人': ['000333', '002472', '002527', '002698', '603728', '600580', '002747',
              '603667', '002526'],
    '储能': ['002074', '002709', '002837', '600089', '002518', '601127'],
    '稀土': ['600111', '000831', '600259', '000970', '600392', '600549'],
    '煤炭': ['601088', '601225', '601898', '601001', '000983', '601666', '600188',
            '601898'],
    '石油': ['601857', '600028', '601808', '000703', '600938', '600583'],
    '数据要素': ['600588', '600271', '002230', '600845', '000977', '601138'],
    '低空经济': ['002097', '600316', '002025', '002526', '600760', '000768'],
}

# 标题关键词 → 主题（用于找广度）
TITLE_THEME_KEYWORDS = {
    '算力': '算力链', 'AI': '算力链', '人工智能': '算力链', '大模型': '算力链',
    'GPU': '算力链', '服务器': '算力链', '光模块': '光通信', 'CPO': '光通信',
    '光通信': '光通信', '800G': '光通信', '1.6T': '光通信',
    'PCB': 'PCB', '印制电路板': 'PCB', '覆铜板': 'PCB',
    '半导体': '半导体', '芯片': '半导体', '集成电路': '半导体', '晶圆': '半导体',
    '新能源': '新能源', '锂电池': '新能源', '锂电': '新能源', '电池': '新能源',
    '汽车': '汽车整车', '整车': '汽车整车', '新能源车': '汽车整车',
    '消费电子': '消费电子', '手机': '消费电子', '苹果链': '消费电子',
    '白酒': '白酒', '茅台': '白酒', '五粮液': '白酒',
    '医药': '医药', '创新药': '医药', 'CXO': '医药',
    '银行': '银行', '保险': '保险', '券商': '银行',
    '地产': '地产', '房地产': '地产',
    '黄金': '黄金', '金价': '黄金', '赤金': '黄金',
    '有色': '有色', '铜价': '有色', '铝': '有色', '铜': '有色',
    '钢铁': '钢铁', '焦煤': '钢铁', '焦炭': '钢铁',
    '化工': '化工', '石化': '化工', '化纤': '化工',
    '军工': '军工', '航空': '军工', '国防': '军工', '导弹': '军工',
    '机器人': '机器人', '人形机器人': '机器人',
    '储能': '储能', 'EPC': '储能',
    '稀土': '稀土', '磁材': '稀土',
    '煤炭': '煤炭', '动力煤': '煤炭',
    '石油': '石油', '原油': '石油', '油价': '石油',
    '数据要素': '数据要素', '数字经济': '数据要素',
    '低空经济': '低空经济', 'eVTOL': '低空经济', '无人机': '低空经济',
    '英伟达': '算力链', 'NVIDIA': '算力链',
    '苹果': '消费电子', 'Apple': '消费电子',
    '特斯拉': '汽车整车', 'Tesla': '汽车整车',
    '台积电': '半导体', 'TSMC': '半导体',
    'AMD': '算力链',
    'OpenAI': '算力链',
}

# ---------------- 池内股票名集合（维度③） ----------------
_POOL_NAMES = None  # 懒加载

def _load_pool_names():
    global _POOL_NAMES
    if _POOL_NAMES is not None:
        return _POOL_NAMES
    _POOL_NAMES = {}
    try:
        p = os.path.join(BASE_DIR, 'data', 'stock_universe_mainboard.json')
        if os.path.exists(p):
            d = json.load(open(p, encoding='utf-8'))
            items = d if isinstance(d, list) else (d.get('stocks', []) if isinstance(d, dict) else [])
            for it in items:
                if isinstance(it, dict):
                    name = it.get('name') or it.get('stock_name') or ''
                    code = it.get('code') or it.get('symbol') or ''
                    if name and len(name) >= 2:
                        _POOL_NAMES[name] = code
    except Exception:
        pass
    return _POOL_NAMES

# ---------------- 原黑名单保留（命中=0分） ----------------
BLACKLIST = [
    '明星', '演员', '歌手', '网红', '主播', '直播带货', '综艺', '电视剧', '电影票房',
    '粉丝', '娱乐圈', '八卦', '绯闻', '恋情', '演唱会', '专辑', '影视剧', '片酬',
    '离婚', '结婚', '怀孕', '出轨', '生子',
    '足球', '男足', '国足', '篮球', '中超', 'CBA', '世界杯', '世预赛', '奥运会', '夺冠',
    '球队', '球员', '电竞', '游戏赛事', '体育赛事', '联赛', '比分', '进球', '绝杀',
    '宠物', '美食', '旅游攻略', '天气预警', '地震', '台风', '交通拥堵', '社保卡',
    '养老金认证', '快递丢失', '消费维权',
    '核弹头', '核潜艇', '部署核武', '副国防部长', '国防部长', '外交部长', '外交部发言人',
    '大使馆', '领事馆', '使领馆', '军事演习', '军演', '导弹试射', '弹道导弹', '战机',
    '军舰', '边防', '边境冲突', '领土主权', '主权声明', '制裁名单', '驱逐舰', '航母编队',
    '停火协议', '战事', '战况', '前线', '士兵', '军队调动', '征兵', '军费开支',
    # 阶段六·新闻治理（用户 2026-09-03）：海外非核心经济体日常宏观与非经济部委社会治理
    '越南大米', '越南出口', '越南贸易', '韩国外汇', '韩国主权债', '韩国外汇储备', '国家体育总局', '体育总局',
    '饭圈', '乱象整治', '追星', '未成年人防沉迷',
]


# ---------------- 来源档位（A₀-4，system_params.categories.news_source 可覆盖） ----------------
SOURCE_TIERS = {'财联社': 5, '东方财富': 4, '东财': 4, '新浪财经': 3, '新浪': 3, '同花顺': 2, '富途': 2, '富途资讯': 2}
_SRC_CFG = None

def _source_weight(source):
    global _SRC_CFG
    if _SRC_CFG is None:
        try:
            p = os.path.join(BASE_DIR, 'config', 'system_params.json')
            d = json.load(open(p, encoding='utf-8'))
            _SRC_CFG = (d.get('categories', {}).get('news_source', {}).get('params', {}) or {})
        except Exception:
            _SRC_CFG = {}
    sw = (_SRC_CFG.get('source_weights') if isinstance(_SRC_CFG, dict) else None) or SOURCE_TIERS
    for k, v in sw.items():
        if k and k in (source or ''):
            return int(v)
    return 0


# ---------------- 模拟盘持仓（ST降级的例外保险丝） ----------------
_HOLDINGS = None

def _load_holdings():
    """读模拟盘持仓代码集合；解析失败返回空集（诚实降级，ST封顶照常生效）。"""
    global _HOLDINGS
    if _HOLDINGS is not None:
        return _HOLDINGS
    _HOLDINGS = set()
    try:
        p = os.path.join(BASE_DIR, 'data', 'simulation_state.json')
        d = json.load(open(p, encoding='utf-8'))
        for acct in (d.get('accounts') or {}).values():
            for pos in (acct.get('positions') or acct.get('holdings') or []):
                if isinstance(pos, dict):
                    code = str(pos.get('code') or pos.get('symbol') or '')
                    pure = ''.join(ch for ch in code if ch.isdigit())
                    if len(pure) == 6:
                        _HOLDINGS.add(pure)
                    name = pos.get('name') or pos.get('stock_name') or ''
                    if name:
                        _HOLDINGS.add(name)
    except Exception:
        pass
    return _HOLDINGS


# ---------------- 打分主函数 ----------------
def score_news(news):
    """四维打分。返回 {score, tier, reason, matched_symbols, hits_subject, hits_event}"""
    title = (news.get('title') or '').strip()
    summary = (news.get('summary') or '').strip()[:300]
    text = f'{title} {summary}'

    # 黑名单一票否决
    hits_b = [w for w in BLACKLIST if w in text]
    if hits_b:
        return {
            'score': 0,
            'tier': 'VETO',
            'reason': f'黑名单命中:{",".join(hits_b[:3])}',
            'matched_symbols': [],
            'hits_subject': [],
            'hits_event': [],
        }

    s1 = s2 = s3 = s4 = 0
    hits_subj = []
    hits_evt = []

    # ① 主体：取各类别最高分，不重复累计
    s1 = 0
    for k, v in SUBJECT_HIGH.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('subject_high', k, v))
    for k, v in REGULATOR.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('regulator', k, v))
    for k, v in GLOBAL_CATALYSTS.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('global_catalyst', k, v))
    for k, v in INTL_MACRO.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('intl_macro', k, v))
    for k, v in INDUSTRY_ASSOC.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('industry_assoc', k, v))
    for k, v in INSTITUTION.items():
        if k in text:
            s1 = max(s1, v)
            hits_subj.append(('institution', k, v))
    # A股龙头 / 普通公司
    if any(w in text for w in ['龙头', '白马', '核心资产']):
        s1 = max(s1, 10)
        hits_subj.append(('a_share_leader', '龙头/白马/核心资产', 10))
    # 普通公司：单命中不给主体分（避免膨胀）
    s1 = min(s1, 35)

    # ② 事件类型：复合事件各自独立累加（同一combo不重复），基础词被复合消耗则跳过，封顶35
    s2 = 0
    consumed = set()
    for combo, v in COMPOUND_EVENTS.items():
        if isinstance(combo, tuple) and len(combo) == 2:
            k1, k2 = combo
            if k1 in text and k2 in text:
                s2 += v
                hits_evt.append((f'{k1}+{k2}', v))
                consumed.add(k1); consumed.add(k2)
        elif isinstance(combo, tuple) and len(combo) == 1:
            if combo[0] in text:
                s2 += v
                hits_evt.append((combo[0], v))
                consumed.add(combo[0])
    for k, v in EVENT_TIER.items():
        if k in text and k not in consumed:
            s2 += v
            hits_evt.append((k, v))
    # 业绩硬数据（净赚/净利润+大额增速）：+15
    if _RE_EARNINGS_SHOCK.search(text):
        s2 += 15
        hits_evt.append(('业绩硬数据', 15))
    # 行情异动（大宗商品/汇率现价变动）：动词+主体齐备 → +30（保底MEDIUM）
    _market_move = bool(_RE_MARKET_MOVE_VERB.search(text) and _RE_MARKET_MOVE_SUBJ.search(text))
    if _market_move:
        s2 += 30
        hits_evt.append(('行情异动', 30))
    # v4.1 R1 第二通道：A 股板块 + 涨跌停动词 → 事件 +30，保底 MEDIUM
    _board_move = bool(_RE_MARKET_MOVE_VERB.search(title) and _RE_ASHARE_BOARD.search(text))
    if _board_move:
        s2 += 30
        hits_evt.append(('A股板块异动', 30))
    # 群体性信号（仅标题命中时触发，再 +15 → STRONG）
    if _RE_BOARD_RALLY.search(title) and _RE_ASHARE_BOARD.search(text):
        s2 += 15
        hits_evt.append(('群体性板块行情', 15))
    s2 = min(s2, 35)
    # 例行小额增减持强制清零
    if re.search(r'(例行|小额|0\.[0-3]\s?%|不超过\s?0\.[0-3]\s?%)', text) and '减持' in text:
        s2 = min(s2, 5)

    # ③ 池内直接命中：三档绑定强度（阶段五A₀-1：标题直击3 > 正文提及2 > 仅板块间接1）
    pool = _load_pool_names()
    matched = []
    s3 = 0
    binding_strength = 0
    title_hits = [(n, c) for n, c in pool.items() if n in title]
    body_hits = [(n, c) for n, c in pool.items() if n not in title and n in text]
    has_code = bool(re.search(r'\b(60[0-9]{4}|00[0-9]{4})\b', title))
    if title_hits or has_code:
        binding_strength = 3
        matched = [c or n for n, c in title_hits[:5]]
        s3 = min(10 * max(len(title_hits), 1) + (10 if has_code else 0), 20)
    elif body_hits:
        binding_strength = 2
        matched = [c or n for n, c in body_hits[:5]]
        s3 = 6
    s3 = min(s3, 20)

    # ④ 影响广度：根据标题主题找池内受益股名单
    themes = set()
    for kw, th in TITLE_THEME_KEYWORDS.items():
        if kw in text:
            themes.add(th)
    breadth_symbols = []
    for th in themes:
        for code in THEME_MAP.get(th, []):
            if code not in breadth_symbols:
                breadth_symbols.append(code)
    bn = len(breadth_symbols)
    if bn >= 30:
        s4 = 20
    elif bn >= 10:
        s4 = 15
    elif bn >= 3:
        s4 = 10
    elif bn >= 1:
        s4 = 5
    else:
        s4 = 0
    # 宏观/监管主体（s1>=25）的事件天然全市场传导：广度保底满分20
    if s1 >= 25:
        s4 = max(s4, 20)
    # v4.1 R1：A股板块群体性异动 / 板块异动事件命中 → 广度保底 20（板块级全池影响）
    if _board_move or _RE_BOARD_RALLY.search(title):
        s4 = max(s4, 20)
    if binding_strength == 0 and breadth_symbols:
        binding_strength = 1
    matched = list(dict.fromkeys(matched + breadth_symbols))[:20]

    s_src = _source_weight(news.get('source', ''))
    score = s1 + s2 + s3 + s4 + s_src
    score = max(0, min(100, score))
    if score >= 75:
        tier = 'STRONG'
    elif score >= 45:
        tier = 'MEDIUM'
    else:
        tier = 'WEAK'

    # 方案二升级线：行情异动且波动 ≥3% / 暴涨暴跌 → STRONG
    if _market_move and tier != 'STRONG' and _RE_EXTREME_MOVE.search(text):
        tier = 'STRONG'
        score = max(score, 75)
    # v4.1 R1：A 股板块群体性涨跌停 → STRONG（与"涨停潮"群体信号匹配）
    if _RE_BOARD_RALLY.search(title) and _RE_ASHARE_BOARD.search(text):
        if tier != 'STRONG':
            tier = 'STRONG'
            score = max(score, 75)

    # 方案一封顶：ST/退市类不进强展示（用户不碰ST）；持仓股例外
    tier, score = apply_st_cap(title, tier, score, matched)

    # 阶段六·精品源特权通道（用户 2026-09-03 定调）：
    # ① 财联社每日主线大卡特权置顶（95分）
    if '今日投资舆情热点' in title:
        score = max(score, 95)
        tier = 'STRONG'
    elif '涨停分析' in title or '午间涨停' in title:
        score = max(score, 92)
        tier = 'STRONG'
    # ② 财联社重磅加红直达 85分
    elif news.get('editor_hot') and ('财联社' in (news.get('source') or '')):
        score = max(score, 85)
        tier = 'STRONG'

    # v4.1 R4 交锋词封顶（三重条件 + 三重豁免）
    # 命中：标题含"开撕/回怼/隔空喊话/互怼/骂战"
    # 豁免（任一即放行）：①标题或正文含政策/地缘词；②热榜命中（HIGH/MID）；③已 STRONG（其他强信号已识别）
    _heat = news.get('_heat') or {}
    if (_RE_PERSONAL_SPAT.search(title)
            and tier != 'STRONG'
            and not _RE_POLICY_GEO_HIT.search(title)
            and not _RE_POLICY_GEO_HIT.search(summary)
            and not _heat.get('heat_tier')):
        tier = 'WEAK'
        score = min(score, 25)

    # reason 简短
    reason_parts = []
    if s1: reason_parts.append(f'主体{s1}')
    if s2: reason_parts.append(f'事件{s2}')
    if s3: reason_parts.append(f'命中{s3}')
    if s4: reason_parts.append(f'广度{s4}({bn}只)')
    reason = '+'.join(reason_parts) or '无显著特征'

    return {
        'score': score,
        'tier': tier,
        'reason': reason,
        'matched_symbols': matched,
        'binding_strength': binding_strength,
        'source_weight': s_src,
        'hits_subject': hits_subj[:5],
        'hits_event': hits_evt[:5],
    }


def apply_st_cap(title, tier, score, matched_symbols=None):
    """ST/退市封顶（可独立调用：规则层与LLM升档后都必须过这道闸）。
    用户不碰ST——非持仓的 ST/退市/终止上市类一律封顶 MEDIUM。"""
    if tier == 'STRONG' and _RE_ST_CAP.search(title or ''):
        hold = _load_holdings()
        hit_hold = any(str(m) in hold for m in (matched_symbols or []))
        if not hit_hold:
            return 'MEDIUM', min(score, 74)
    return tier, score


def veto(item, score, hits_b):
    """构造刑部留痕记录（v2：黑名单命中 tier=VETO 时调用）。"""
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return {
        'news_id': item.get('id', item.get('_id', item.get('_id2', ''))),
        'title': item.get('title', ''),
        'summary': item.get('summary', '')[:120],
        'category': item.get('category', ''),
        'source': item.get('source', '新浪财经 7x24 实时快讯'),
        'relevance_score': score,
        'hits_blacklist': hits_b or [],
        'verdict': 'VETO',
        'reason': f'门下省否决：黑名单命中（0分，A 股相关性不足）',
        'rejected_at': now,
    }


def relevance_check(title='', summary='', category=''):
    """
    旧签名保留（兼容 news_officer 调用点）。verdict 改为 STRONG/MEDIUM/WEAK/VETO。
    返回 (score, verdict, hits_whitelist, hits_blacklist)
    """
    news = {'title': title or '', 'summary': summary or '', 'category': category or ''}
    r = score_news(news)
    hits_w = [h[1] for h in (r['hits_subject'] or [])]
    hits_b = []
    if r['tier'] == 'VETO':
        # 从 reason 提取黑名单词
        for w in BLACKLIST:
            if w in title or w in summary:
                hits_b.append(w)
    return r['score'], r['tier'], hits_w, hits_b


# 灰区阈值常量，供 LLM 批量评分任务对齐
GRAY_LOW = 30
GRAY_HIGH = 70


if __name__ == '__main__':
    print('=== 门下省自检 · 四维打分 v2 ===')
    cases = [
        ('【韩安冉名下多家公司已注销】网红韩安冉高调宣布零片酬客串《济公4》', '', '财经综合'),
        ('央行宣布全面降准0.5个百分点，释放长期资金约1.2万亿', '', '宏观政策'),
        ('半导体板块午后异动拉升，中芯国际涨超5%', '', '板块异动'),
        ('中国男足世预赛2-1战胜对手，球迷沸腾', '', '体育新闻'),
        ('某券商上调宁德时代目标价至320元', '', '公司研报'),
        ('波兰副国防部长扎莱夫斯基表示，波兰不希望在本国领土部署核弹头', '', '财经综合'),
        ('美国攻击型核潜艇维护困境造成数十亿美元损失', '', '财经综合'),
        ('军工板块午后异动，中航沈飞涨停', '', '板块异动'),
        ('多国军事演习在南海举行，中方回应', '', '财经综合'),
        # 用户 2026-08-28 实测认可的两条
        ('英伟达财报超预期 AI算力景气仍处高位', '', '全球宏观'),
        ('AI算力设施扩张带动需求高增 光通信板块步入业绩兑现期', '', '行业动态'),
        ('北京科技大学雄安校区建设加速推进', '', '其他'),
        ('国家大基金三期成立 重点投向半导体与AI算力', '', '宏观政策'),
        ('某公司董事完成减持计划 累计减持0.3%股份', '', '公司公告'),
        ('美联储宣布降息50个基点 鲍威尔讲话偏鸽', '', '国际宏观'),
    ]
    for title, summary, cat in cases:
        s, v, hw, hb = relevance_check(title, summary, cat)
        r = score_news({'title': title, 'summary': summary, 'category': cat})
        print(f"  [{v:6s}] score={s:2d}  {r['reason'][:50]:50s} | {title[:32]}")
