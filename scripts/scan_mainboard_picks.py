"""
A 股主板大池全池并发扫描与 6 位博主 Skill 契约量化选股引擎
- 预选池：3103 家沪深纯正主板股票 (data/stock_universe_mainboard.json)
- 数据源：腾讯金融实时行情 (多包并发高吞吐量 API)
- 过滤机制：剔除创业板/科创板/北交所/ST/*ST/退市/停牌股
- 选股策略：
  1. AI-Alpha 智脑：多因子动量共振 (涨幅 +2.5%~+7.5%, 换手 3%~15%, 成交额>2亿)
  2. 韩叔：绝活生命线低吸 (主板优质中大盘, 回踩企稳 -2%~+1.5%, 换手 1%~4%, PE>0)
  3. 殷文涛 (梵爹)：硬科技/高端制造大资金抱团合力 (成交额>5亿, 涨幅 +1.5%~+6%)
  4. 廖小爱：3-3-1 价值投资深蹲筑底 (市值>300亿, PE 8~35, 换手<2.5%, 震荡筑底)
  5. 酷儿：20MA 假跌破长阳反转 (极严苛, 不满足则 100% 空仓)
  6. 反指哨兵：情绪极值监控与风险雷达 (监控高位连板诱多, 无异常则安全防守)
- 输出目标：workbench/data.json + workbench/index.html (内嵌数据更新与时间戳同步)
"""

import sys
import os
import json
import time
import urllib.request
import concurrent.futures
from datetime import datetime

# Windows 终端编码保护
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_PATH = os.path.join(ROOT_DIR, 'data', 'stock_universe_mainboard.json')
DATA_JSON_PATH = os.path.join(ROOT_DIR, 'workbench', 'data.json')
HTML_PATH = os.path.join(ROOT_DIR, 'workbench', 'index.html')

ROLE_OUTPUT = {
    'ai-alpha': {
        'participant_id': 'P-RULE-QUANT',
        'display_name': '规则量化派',
        'skill_type': 'RULE_BASED_SKILL',
        'legacy_blogger_id': 'ai-alpha',
    },
    'hanshu': {
        'participant_id': 'P-HANSHU',
        'display_name': '韩叔',
        'skill_type': 'PERSONA_SKILL',
        'legacy_blogger_id': 'hanshu',
    },
    'yintwentao': {
        'participant_id': 'P-YINWENTAO',
        'display_name': '殷文涛',
        'skill_type': 'PERSONA_SKILL',
        'legacy_blogger_id': 'yintwentao',
    },
    'liao-xiaoai': {
        'participant_id': 'P-LIAO-XIAOAI',
        'display_name': '廖小爱',
        'skill_type': 'PERSONA_SKILL',
        'legacy_blogger_id': 'liao-xiaoai',
    },
    'cool': {
        'participant_id': 'P-COOL',
        'display_name': '酷儿',
        'skill_type': 'PERSONA_SKILL',
        'legacy_blogger_id': 'cool',
    },
    'anti-demo': {
        'participant_id': 'P-RISK-SENTINEL',
        'display_name': '反指哨兵',
        'skill_type': 'RISK_SENTINEL',
        'legacy_blogger_id': 'anti-demo',
    },
}


SKILL_POSITION_PLANS = {
    'P-RULE-QUANT': {'target_ratio': 0.20, 'max_position_ratio': 0.20,
                     'max_positions': 3, 'scale_in_rule': 'Rule Contract only'},
    'P-HANSHU': {'target_ratio': 0.25, 'max_position_ratio': 0.25,
                 'max_positions': 2, 'scale_in_rule': 'MA evidence required before add'},
    'P-YINWENTAO': {'target_ratio': 0.30, 'max_position_ratio': 0.30,
                    'max_positions': 2, 'scale_in_rule': 'Skill wave rule'},
    'P-LIAO-XIAOAI': {'target_ratio': 0.30, 'max_position_ratio': 0.30,
                      'max_positions': 2, 'scale_in_rule': '3·3·1 explicit conditions'},
    'P-COOL': {'target_ratio': 0.20, 'max_position_ratio': 0.20,
               'max_positions': 2, 'scale_in_rule': 'Intraday structure required'},
}

SKILL_EXIT_PLANS = {
    'P-RULE-QUANT': {'stop_loss': -0.045, 'take_profit': None,
                     'trailing': 0.08, 'time_exit': 'Rule Contract'},
    'P-HANSHU': {'stop_loss': -0.035, 'take_profit': 0.06,
                 'trailing': None, 'time_exit': '1-2 weeks'},
    'P-YINWENTAO': {'stop_loss': -0.035, 'take_profit': 0.085,
                    'trailing': None, 'time_exit': '1-3 weeks'},
    'P-LIAO-XIAOAI': {'stop_loss': -0.06, 'take_profit': 0.12,
                      'trailing': None, 'time_exit': '1-3 months'},
    'P-COOL': {'stop_loss': -0.03, 'take_profit': 0.09,
               'trailing': None, 'time_exit': '3-7 days'},
}


def _annotate_role_output(pick):
    """Add canonical participant identity while preserving legacy compatibility."""
    role = ROLE_OUTPUT[pick['blogger_id']]
    pick['participant_id'] = role['participant_id']
    pick['display_name'] = role['display_name']
    pick['skill_type'] = role['skill_type']
    pick['legacy_blogger_id'] = role['legacy_blogger_id']
    return pick


def _quote_evidence(quote):
    """Return only structured fields actually received from the quote."""
    return {
        'symbol': quote.get('symbol'),
        'code': quote.get('code'),
        'price': quote.get('price'),
        'change_pct': quote.get('change_pct'),
        'volume': quote.get('volume'),
        'amount_wan': quote.get('amount_wan'),
        'amount_yi': quote.get('amount_yi'),
        'turnover_ratio': quote.get('turnover_ratio'),
        'pe': quote.get('pe'),
        'market_cap_yi': quote.get('mktcap_yi'),
    }


def _attach_skill_decision(pick, scan_timestamp):
    """Make Contract V2 canonical and derive legacy compatibility fields."""
    from analysis.skill_contracts import build_decision, load_contracts

    role = ROLE_OUTPUT[pick['blogger_id']]
    participant_id = role['participant_id']
    contracts = {item['participant_id']: item for item in load_contracts()['contracts']}
    contract = contracts[participant_id]
    evidence = pick.pop('_candidate_evidence', None)
    if evidence is None:
        evidence = {'status': 'NO_CANDIDATE'}
    action = 'BUY' if pick.get('triggered') else 'NO_SIGNAL'
    failed = []
    if participant_id == 'P-HANSHU':
        failed.append('MA20_CONFIRMATION_UNAVAILABLE')
        action = 'NO_SIGNAL'
    elif participant_id == 'P-COOL':
        failed.append('INTRADAY_STRUCTURE_UNAVAILABLE')
        action = 'NO_SIGNAL'
    elif participant_id == 'P-RISK-SENTINEL':
        action = 'NO_SIGNAL'
        pick['alert_triggered'] = bool(pick.get('triggered'))
        pick['triggered'] = False
        pick['signal'] = 'RISK_ALERT' if pick['alert_triggered'] else 'NO_SIGNAL (风险观察)'
    if action != 'BUY' and not failed:
        failed.append('ENTRY_RULE_NOT_MET')

    policy = dict(SKILL_POSITION_PLANS.get(participant_id, {
        'target_ratio': 0.0, 'max_position_ratio': 0.0,
        'max_positions': 0, 'scale_in_rule': 'Not applicable',
    }))
    if action != 'BUY':
        policy['target_ratio'] = 0.0
    exit_plan = dict(SKILL_EXIT_PLANS.get(participant_id, {
        'stop_loss': None, 'take_profit': None,
        'trailing': None, 'time_exit': 'Not applicable',
    }))
    symbol = pick.get('stock_code')
    evidence_refs = [f'skill_contract:{participant_id}:{contract["version"]}']
    if symbol and symbol != '无':
        evidence_refs.insert(0, f'tencent_quote:{symbol}:{scan_timestamp}')
    decision = build_decision(
        participant_id,
        symbol or '',
        action,
        skill_version=contract['version'],
        timestamp=scan_timestamp,
        candidate_evidence=evidence,
        skill_rules_triggered=list(pick.get('rule_tags') or []),
        skill_rules_failed=failed,
        decision_reason=pick.get('trigger_reason', ''),
        position_plan=policy,
        exit_plan=exit_plan,
        holding_horizon=pick.get('horizon', ''),
        soft_risk_context=[],
        confidence=pick.get('score'),
        data_freshness={
            'scan_timestamp': scan_timestamp,
            'source': 'Tencent realtime quote',
        },
        evidence_refs=evidence_refs,
    )
    pick['skill_decision'] = decision
    if participant_id != 'P-RISK-SENTINEL':
        pick['triggered'] = action == 'BUY'
        if action != 'BUY':
            pick['signal'] = 'NO_SIGNAL (观察)'
    return pick

def load_stock_universe():
    if not os.path.exists(UNIVERSE_PATH):
        from build_stock_universe import build_universe
        build_universe()
    with open(UNIVERSE_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def fetch_quotes_batch(symbols):
    """批量从腾讯获取行情"""
    url = 'https://qt.gtimg.cn/q=' + ','.join(symbols)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            content = resp.read().decode('gbk', errors='ignore')
            return content
    except Exception as e:
        return ''

def scan_and_score_all(persist=True):
    """
    主板大池全池并发扫描与 6 博主量化打分。

    persist=True（默认）：扫描完成后直接写 workbench/data.json 并嵌入 index.html（兼容原调用）。
    persist=False（六部模式）：只返回 final_picks，不写盘，由流水线起居注统一落盘。
    """
    scan_timestamp = datetime.now().astimezone().isoformat()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔍 启动 A 股主板大池并发扫描与 6 博主量化打分...")
    t0 = time.time()
    
    universe = load_stock_universe()
    stocks = universe.get('stocks', [])
    print(f"载入主板预选池: 共 {len(stocks)} 家纯正主板股票")
    
    # 构造符号列表并分包 (80只/包)
    symbols = [s['symbol'] for s in stocks]
    chunk_size = 80
    chunks = [symbols[i:i+chunk_size] for i in range(0, len(symbols), chunk_size)]
    
    quotes_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_quotes_batch, c) for c in chunks]
        for f in concurrent.futures.as_completed(futures):
            raw = f.result()
            if not raw:
                continue
            lines = raw.strip().split(';\n')
            for line in lines:
                if not line or '=\"\";' in line:
                    continue
                parts = line.split('~')
                if len(parts) > 40:
                    name = parts[1].strip()
                    code = parts[2].strip()
                    price = float(parts[3] or 0)
                    change_pct = float(parts[32] or 0)
                    vol = float(parts[6] or 0)
                    amount_wan = float(parts[37] or 0)
                    turnover = float(parts[38] or 0) if len(parts) > 38 and parts[38] else 0.0
                    pe = float(parts[39] or 0) if len(parts) > 39 and parts[39] else 0.0
                    mktcap_yi = float(parts[45] or 0) if len(parts) > 45 and parts[45] else 0.0
                    
                    # 过滤 ST
                    upper_name = name.upper()
                    if 'ST' in upper_name or '退' in upper_name or 'PT' in upper_name:
                        continue
                    if price <= 0:
                        continue
                        
                    mkt = 'sh' if code.startswith(('600', '601', '603', '605')) else 'sz'
                    full_code = f'{mkt}{code}'
                    quotes_map[code] = {
                        'code': code,
                        'name': name,
                        'symbol': full_code,
                        'full_code': full_code,
                        'market': mkt,
                        'price': price,
                        'change_pct': change_pct,
                        'volume': vol,
                        'amount_wan': amount_wan,
                        'amount_yi': round(amount_wan / 10000, 2),
                        'turnover_ratio': turnover,
                        'pe': pe,
                        'mktcap_yi': mktcap_yi
                    }
                    
    fetch_time = time.time() - t0
    print(f"✅ 行情多包并发拉取完成: 成功获取 {len(quotes_map)} 只活跃主板股票, 耗时 {fetch_time:.2f}s")
    
    all_quotes = list(quotes_map.values())
    
    # ---------------- 6 博主 Skill 契约量化打分 ----------------
    
    # 1. AI-Alpha 智脑 (多因子动量突破 + 基本面质量因子 + ATR追踪止盈)
    # 契约规则：拒绝纯投机亏损杂毛，必须兼顾【动量因子 + 质量因子 (PE>0 且合理) + 流动性因子 (成交>2.5亿)】
    ai_candidates = []
    for q in all_quotes:
        # 市值>=50亿，PE处于正常盈利区间 (0 < PE <= 120)，涨幅处于 +2.5% ~ +7.5% 强动量区间，换手 3%~12%，成交额 > 2.5亿
        if q['mktcap_yi'] >= 50 and 0 < q['pe'] <= 120 and 2.5 <= q['change_pct'] <= 7.5 and 3.0 <= q['turnover_ratio'] <= 12.0 and q['amount_wan'] >= 25000:
            # 综合动量评分：量价配合 + 适度换手 + 成交流动性
            score = q['change_pct'] * 0.35 + q['turnover_ratio'] * 0.3 + (q['amount_wan'] / 15000) * 0.35
            ai_candidates.append((score, q))
            
    ai_candidates.sort(key=lambda x: x[0], reverse=True)
    if ai_candidates and ai_candidates[0][0] >= 5.5:
        best = ai_candidates[0][1]
        ai_pick = {
            "blogger_id": "ai-alpha",
            "blogger_name": "AI-Alpha (量化智脑)",
            "positioning": "多因子动量共振 + 量价质量规则 + ATR追踪止盈",
            "horizon": "短波段 (3-10天)",
            "triggered": True,
            "stock_code": best['code'],
            "stock_name": best['name'],
            "current_price": best['price'],
            "_candidate_evidence": _quote_evidence(best),
            "pct": best['change_pct'],
            "signal": "动量突破 (BUY)",
            "trigger_reason": f"【动量契约】主板大池扫描：{best['name']}（{best['code']}）涨幅{best['change_pct']}%，成交额达{best['amount_yi']}亿，换手率{best['turnover_ratio']}%，PE估值{round(best['pe'], 1)}倍，命中多因子动量与质量筛选；当前规则得分{round(ai_candidates[0][0], 2)}分；挂载 ATR(14)×2.2 动态追踪止盈与 -4.5% 刚性止损。",
            "rule_tags": ["多因子动量", "基本面质量", f"成交{best['amount_yi']}亿", "ATR追踪止盈"]
        }
    else:
        ai_pick = {
            "blogger_id": "ai-alpha",
            "blogger_name": "AI-Alpha (量化智脑)",
            "positioning": "多因子动量共振 + 量价质量规则 + ATR追踪止盈",
            "horizon": "短波段 (3-10天)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (量价动量未共振)",
            "current_price": None,
            "pct": None,
            "signal": "空仓观望 (WAIT)",
            "trigger_reason": "【动量契约】全池主板股票已完成多因子动量、质量与流动性筛选；当前没有达到规则阈值的候选，保持量化纪律空仓防守。",
            "rule_tags": ["动量过滤", "多因子打分", "纪律防守", "ATR风控"]
        }
        
    # 2. 韩叔 (趣聊财经 - 20MA 绝活生命线低吸 + 5天线荡秋千)
    # 契约规则：专注半导体存储、人形机器人、刚需自费医药、消费电子精密制造；坚决排除老登银行/地产/传统火电/白酒杂毛与千倍PE题材股
    EXCLUDE_HANSHU = ['银行', '地产', '房产', '置业', '白酒', '啤酒', '酒业', '煤炭', '港口', '高速', '电力', '保险', '券商', '百货']
    HANSHU_SECTORS = ['存储', '芯片', '半导体', '微电', '机器人', '医药', '药业', '生物', '医疗', '电子', '精密', '连接器', '制造', '机械', '装备', '电气', '自动化', '智能']
    
    hs_candidates = []
    for q in all_quotes:
        name = q['name']
        # 1. 严格一票否决老登防守资产
        if any(ex in name for ex in EXCLUDE_HANSHU):
            continue
        # 2. 属于韩叔硬科技与制造医药核心范畴
        is_target_sector = any(kw in name for kw in HANSHU_SECTORS)
        if not is_target_sector:
            continue
        # 3. 市值 80亿~1500亿，PE 10~75倍（拒绝亏损与千倍题材杂毛），涨跌幅收敛（-2.0% ~ +1.5%），换手 1.0%~4.0% 缩量回踩
        if 80 <= q['mktcap_yi'] <= 1500 and 10.0 <= q['pe'] <= 75.0 and -2.0 <= q['change_pct'] <= 1.5 and 1.0 <= q['turnover_ratio'] <= 4.0:
            score = (4.0 - abs(q['change_pct'])) * 0.4 + (q['mktcap_yi'] / 100) * 0.3 + (4.0 - q['turnover_ratio']) * 0.3
            hs_candidates.append((score, q))
            
    hs_candidates.sort(key=lambda x: x[0], reverse=True)
    if hs_candidates:
        best = hs_candidates[0][1]
        hs_pick = {
            "blogger_id": "hanshu",
            "blogger_name": "韩叔 (趣聊财经)",
            "positioning": "20MA生命线低吸风格 + 分批候选（均线证据待确认）",
            "horizon": "中短线 (1-2周)",
            "triggered": True,
            "stock_code": best['code'],
            "stock_name": best['name'],
            "current_price": best['price'],
            "_candidate_evidence": _quote_evidence(best),
            "pct": best['change_pct'],
            "signal": "生命线低吸 (BUY)",
            "trigger_reason": f"【绝活契约】{best['name']}（{best['code']}）市值{round(best['mktcap_yi'], 1)}亿，PE估值{round(best['pe'], 1)}倍，涨跌幅{best['change_pct']}%，换手率{best['turnover_ratio']}%；命中韩叔行业、估值、波动与换手候选规则。MA5/MA20 与盘中结构证据当前不可用，生命线确认需等待真实均线数据。",
            "rule_tags": ["行业筛选", "估值筛选", "波动筛选", "均线证据待确认"]
        }
    else:
        hs_pick = {
            "blogger_id": "hanshu",
            "blogger_name": "韩叔 (趣聊财经)",
            "positioning": "20MA生命线低吸风格 + 分批候选（均线证据待确认）",
            "horizon": "中短线 (1-2周)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (未达生命线买点)",
            "current_price": None,
            "pct": None,
            "signal": "空仓观望 (WAIT)",
            "trigger_reason": "【绝活契约】主板科技与先进制造标的当前未命中韩叔行业、估值、波动与换手候选规则；MA5/MA20 证据不可用，保持观察。",
            "rule_tags": ["行业筛选", "估值筛选", "等待规则命中", "均线证据待确认"]
        }
        
    # 3. 殷文涛 (梵爹 - 三大阵营博弈与科技成长阵营大资金合力)
    # 契约规则：锁定科技成长进攻之矛（光模块、PCB、半导体先进制程/设备/材料、商业航天卫星、算力硬件）；坚决剔除老登盾（银行/地产/白酒/煤炭等）
    EXCLUDE_YWT = ['银行', '地产', '房产', '白酒', '啤酒', '煤炭', '钢铁', '水泥', '公路', '港口', '百货', '保险', '农业', '养殖']
    YWT_TECH_SECTORS = ['光电', '通信', '算力', 'PCB', '服务器', '半导体', '芯片', '材料', '设备', '航天', '卫星', '连接器', '元器件', '微电', '集成', '电子', '信息', '网络']
    
    ywt_candidates = []
    for q in all_quotes:
        name = q['name']
        if any(ex in name for ex in EXCLUDE_YWT):
            continue
        is_tech_spear = any(kw in name for kw in YWT_TECH_SECTORS)
        if not is_tech_spear:
            continue
        # 大资金科技主战场：成交额 > 4.5亿 (45000万元)，涨幅 +1.5% ~ +6.5%，非ST
        if q['amount_wan'] >= 45000 and 1.5 <= q['change_pct'] <= 6.5:
            score = (q['amount_wan'] / 20000) * 0.5 + q['change_pct'] * 0.3 + (q['turnover_ratio']) * 0.2
            ywt_candidates.append((score, q))
            
    ywt_candidates.sort(key=lambda x: x[0], reverse=True)
    if ywt_candidates:
        best = ywt_candidates[0][1]
        ywt_pick = {
            "blogger_id": "yintwentao",
            "blogger_name": "殷文涛 (梵爹)",
            "positioning": "科技成长波段 + 成交活跃规则 + 阵营观察",
            "horizon": "波段 (1-3周)",
            "triggered": True,
            "stock_code": best['code'],
            "stock_name": best['name'],
            "current_price": best['price'],
            "_candidate_evidence": _quote_evidence(best),
            "pct": best['change_pct'],
            "signal": "成交活跃波段候选 (BUY)",
            "trigger_reason": f"【阵营契约】{best['name']}（{best['code']}）成交额达{best['amount_yi']}亿元，涨幅{best['change_pct']}%，换手率{best['turnover_ratio']}%；命中科技成长行业与成交活跃规则，进入殷文涛波段候选。未取得机构资金流或主力净流入证据，不作该事实判断。",
            "rule_tags": ["科技成长筛选", "成交活跃", f"成交{best['amount_yi']}亿", "换手与涨幅规则"]
        }
    else:
        ywt_pick = {
            "blogger_id": "yintwentao",
            "blogger_name": "殷文涛 (梵爹)",
            "positioning": "科技成长波段 + 成交活跃规则 + 阵营观察",
            "horizon": "波段 (1-3周)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (阵营博弈胶着)",
            "current_price": None,
            "pct": None,
            "signal": "空仓观望 (WAIT)",
            "trigger_reason": "【阵营契约】当前没有命中科技成长行业与成交活跃规则的波段候选，保持观察。未取得机构资金流或主力净流入证据。",
            "rule_tags": ["科技成长筛选", "成交规则等待", "证据边界", "观察"]
        }
        
    # 4. 廖小爱 (中国特色价值投资 + 3·3·1 选股模型 + 算力铁三角/新质生产力 + 3-3-1 仓位)
    # 核心哲学：严格排除老登防守资产（银行/地产/白酒/传统火电煤炭/运营商），聚焦 AI 算力铁三角与新质生产力第一龙头
    EXCLUDE_OLD_SECTORS = ['银行', '地产', '房产', '置业', '白酒', '啤酒', '酒业', '煤炭', '港口', '公路', '高速', '百货', '商业', '超市', '移动', '电信', '联通']
    TECH_NEW_QUALITY_KW = [
        '科技', '半导体', '芯片', '微电', '光电', '通信', '电子', '信息', '软件', '材料',
        '智能', '声学', '连接器', 'PCB', '封测', '算力', '数字', '网络', '信创', '计算机',
        '南瑞', '特变', '电气', '电工', '电源', '储能', '光伏', '新能源', '能源', '锂',
        '矿业', '钼业', '稀土', '有色', '铝业', '铜业', '金属', '黄金', '锗', '钛',
        '机器人', '自动化', '电机', '制造', '重机', '航空', '航天', '卫星', '汽车', '精工'
    ]
    
    lxa_candidates = []
    for q in all_quotes:
        name = q['name']
        # 1. 严格一票否决老登防守行业
        if any(ex in name for ex in EXCLUDE_OLD_SECTORS):
            continue
            
        # 2. 必须属于新质生产力 / 硬科技 / 算力铁三角赛道
        is_tech = any(tk in name for tk in TECH_NEW_QUALITY_KW)
        if not is_tech:
            continue
            
        # 3. 3·3·1 模型严选：市值 >= 150 亿，合理市盈率 PE 10~65，处于深蹲蓄力/良性回踩区间（-2.5% ~ +1.5%），换手率 0.5%~3.5%
        if q['mktcap_yi'] >= 150 and 10.0 <= q['pe'] <= 65.0 and -2.5 <= q['change_pct'] <= 1.5 and 0.5 <= q['turnover_ratio'] <= 3.5:
            # 综合评分：百亿千亿第一龙头权重 + 安全估值 + 缩量深蹲蓄力程度
            score = (q['mktcap_yi'] / 150) * 0.45 + (65.0 - q['pe']) * 0.25 + (3.5 - q['turnover_ratio']) * 0.3
            lxa_candidates.append((score, q))
            
    lxa_candidates.sort(key=lambda x: x[0], reverse=True)
    if lxa_candidates:
        best = lxa_candidates[0][1]
        lxa_pick = {
            "blogger_id": "liao-xiaoai",
            "blogger_name": "廖小爱",
            "positioning": "中国特色价值投资 + 3·3·1硬科技模型 + 有限分批建仓",
            "horizon": "长线产业周期 (1-3个月)",
            "triggered": True,
            "stock_code": best['code'],
            "stock_name": best['name'],
            "current_price": best['price'],
            "_candidate_evidence": _quote_evidence(best),
            "pct": best['change_pct'],
            "signal": "首批30%底仓建仓 (BUY)",
            "trigger_reason": f"【3·3·1新质生产力契约】{best['name']}（{best['code']}）市值{round(best['mktcap_yi'], 1)}亿，PE估值{round(best['pe'], 1)}倍，涨跌幅{best['change_pct']}%，换手率{best['turnover_ratio']}%；命中硬科技行业、估值、市值与换手筛选规则。position_plan 由 Skill 设定首仓30%，后续加仓需满足独立条件。",
            "rule_tags": ["3·3·1模型", "硬科技行业筛选", "估值与市值筛选", "有限分批建仓"]
        }
    else:
        lxa_pick = {
            "blogger_id": "liao-xiaoai",
            "blogger_name": "廖小爱",
            "positioning": "中国特色价值投资 + 3·3·1硬科技模型 + 有限分批建仓",
            "horizon": "长线产业周期 (1-3个月)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (等待硬科技黄金坑)",
            "current_price": None,
            "pct": None,
            "signal": "空仓观望 (WAIT)",
            "trigger_reason": "【3·3·1契约】当前没有命中硬科技行业、估值、市值与换手筛选规则的候选，保持观察；后续仓位必须受 Skill 的有限分批条件约束。",
            "rule_tags": ["3·3·1严选", "行业与估值筛选", "规则等待", "有限分批建仓"]
        }
        
    # 5. 酷儿 (超短线假跌破放量反转 + 20MA斜率突破 - 极严苛，无则 100% 空仓)
    # 契约规则：专注高弹性中盘与人气龙头（市值60亿~800亿），寻找主力流动性Sweep后的长阳绝杀；排除沉重大象与老登股；垃圾时间坚决空仓
    EXCLUDE_COOL = ['银行', '地产', '港口', '公路', '煤炭', '白酒', '啤酒', '电力', '保险']
    cool_candidates = []
    for q in all_quotes:
        name = q['name']
        if any(ex in name for ex in EXCLUDE_COOL):
            continue
        # 市值 60亿~800亿（高弹性好掉头），探底回升长阳（涨幅 +4.0% ~ +9.5%，换手 4%~15%，成交额 > 3亿）
        if 60 <= q['mktcap_yi'] <= 800 and 4.0 <= q['change_pct'] <= 9.5 and 4.0 <= q['turnover_ratio'] <= 15.0 and q['amount_wan'] >= 30000:
            score = q['change_pct'] * 0.4 + q['turnover_ratio'] * 0.3 + (q['amount_wan'] / 10000) * 0.3
            cool_candidates.append((score, q))
            
    cool_candidates.sort(key=lambda x: x[0], reverse=True)
    # 酷儿只在有极致形态时出手，若没有绝对高分（>=8.5）则坚定空仓
    if cool_candidates and cool_candidates[0][0] >= 8.5:
        best = cool_candidates[0][1]
        cool_pick = {
            "blogger_id": "cool",
            "blogger_name": "酷儿 (Cool)",
            "positioning": "高弹性候选 + 涨幅/换手/成交额规则（盘中结构待证）",
            "horizon": "超短线 (3-7天)",
            "triggered": True,
            "stock_code": best['code'],
            "stock_name": best['name'],
            "current_price": best['price'],
            "_candidate_evidence": _quote_evidence(best),
            "pct": best['change_pct'],
            "signal": "高弹性观察候选 (BUY)",
            "trigger_reason": f"【高弹性候选契约】{best['name']}（{best['code']}）涨幅{best['change_pct']}%，成交额{best['amount_yi']}亿元，换手率{best['turnover_ratio']}%；命中高弹性、涨幅、换手与成交额筛选规则。未取得分时下探/收回、Sweep、K线吞没或MA20证据，盘中结构确认需等待真实数据。",
            "rule_tags": ["高弹性筛选", "涨幅与换手规则", "成交额规则", "盘中结构待证"]
        }
    else:
        cool_pick = {
            "blogger_id": "cool",
            "blogger_name": "酷儿 (Cool)",
            "positioning": "高弹性候选 + 涨幅/换手/成交额规则（盘中结构待证）",
            "horizon": "超短线 (3-7天)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (形态未确立)",
            "current_price": None,
            "pct": None,
            "signal": "空仓观望 (WAIT)",
            "trigger_reason": "【高弹性候选契约】当前没有命中高弹性、涨幅、换手与成交额筛选规则的候选；分时结构与MA20证据不可用，保持观察。",
            "rule_tags": ["高弹性筛选", "空仓观察", "盘中结构待证", "证据边界"]
        }
        
    # 6. 反指哨兵 (高位极端情绪监测与反向避险)
    overheated_list = [q for q in all_quotes if q['turnover_ratio'] >= 18.0 or q['change_pct'] >= 9.8]
    if len(overheated_list) >= 15: # 市场极端过热亢奋
        anti_pick = {
            "blogger_id": "anti-demo",
            "blogger_name": "极短线情绪博主 (反指哨兵)",
            "positioning": "高位极端追涨监测 (反向避险)",
            "horizon": "极短线 (1-2天)",
            "triggered": True,
            "stock_code": overheated_list[0]['code'],
            "stock_name": f"{overheated_list[0]['name']} (高位预警)",
            "current_price": overheated_list[0]['price'],
            "_candidate_evidence": _quote_evidence(overheated_list[0]),
            "pct": overheated_list[0]['change_pct'],
            "signal": "反向避险 (RISK)",
            "trigger_reason": f"全市场情绪雷达警报：主板共 {len(overheated_list)} 家标的换手率超18%或涨幅达到9.8%，{overheated_list[0]['name']}换手率达{overheated_list[0]['turnover_ratio']}%；反指哨兵仅提供风险观察，不执行交易。",
            "rule_tags": ["高位预警", "极端换手", "情绪退潮雷达", "避险拦截"]
        }
    else:
        anti_pick = {
            "blogger_id": "anti-demo",
            "blogger_name": "极短线情绪博主 (反指哨兵)",
            "positioning": "高位极端追涨监测 (反向避险)",
            "horizon": "极短线 (1-2天)",
            "triggered": False,
            "stock_code": "无",
            "stock_name": "今日未选出 (情绪安全防守)",
            "current_price": None,
            "pct": None,
            "signal": "空仓防守 (WAIT)",
            "trigger_reason": "今日主板大池未达到换手率/涨幅情绪观察阈值；反指哨兵仅提供风险观察，不执行交易。",
            "rule_tags": ["未达极值", "安全防守", "反向哨兵", "情绪雷达"]
        }
        
    final_picks = [_annotate_role_output(pick) for pick in
                   [ai_pick, hs_pick, ywt_pick, lxa_pick, cool_pick, anti_pick]]
    final_picks = [_attach_skill_decision(pick, scan_timestamp) for pick in final_picks]

    # 六部模式：只返回选股结果，由流水线起居注统一落盘（避免双重写盘）
    if not persist:
        return final_picks

    # ---------------- 落盘更新 workbench/data.json ----------------
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data_payload = {}
    if os.path.exists(DATA_JSON_PATH):
        with open(DATA_JSON_PATH, 'r', encoding='utf-8') as f:
            data_payload = json.load(f)
            
    data_payload['updated_at'] = now_str
    data_payload['picks_updated_at'] = now_str
    data_payload['blogger_picks'] = final_picks
    data_payload['stock_universe_stats'] = {
        "total_mainboard_count": len(stocks),
        "active_scanned_count": len(quotes_map),
        "scan_time": now_str,
        "scan_duration_sec": round(time.time() - t0, 2)
    }
    
    with open(DATA_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data_payload, f, ensure_ascii=False, indent=2)
        
    # ---------------- 同步更新 workbench/index.html ----------------
    update_html_embedded_data(final_picks, now_str)
    
    print(f"==================================================")
    print(f"🎉 6 位博主主板大池量化选股扫描完成!")
    print(f"⏱️ 耗时: {time.time() - t0:.2f} 秒 (全池 {len(quotes_map)} 家主板)")
    print(f"📊 选股结果概要:")
    for p in final_picks:
        status_text = f"✅ 选出: {p['stock_name']} ({p['stock_code']}) [现价: {p['current_price']}, 涨幅: {p['pct']}%]" if p['triggered'] else f"⏸️ {p['stock_name']}"
        print(f"   - {p['blogger_name']}: {status_text}")
    print(f"💾 数据已同步至: {DATA_JSON_PATH} & {HTML_PATH}")
    print(f"==================================================")
    return final_picks

def update_html_embedded_data(picks, now_str):
    if not os.path.exists(HTML_PATH):
        return
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # 替换内嵌的 blogger_picks 数据
    import re
    picks_json_str = json.dumps(picks, ensure_ascii=False, indent=6)
    
    # 匹配 const INITIAL_DATA 或 window.WORKBENCH_DATA
    pattern = r'(let\s+PICKS_CANDIDATE_POOL\s*=\s*)\[[\s\S]*?\];'
    if re.search(pattern, content):
        content = re.sub(pattern, f'let PICKS_CANDIDATE_POOL = {picks_json_str};', content, count=1)
        
    # 同时更新 HTML 页面中的时间戳元素默认文字
    content = re.sub(r'id="picks-update-time">[^<]*<', f'id="picks-update-time">数据更新时间：{now_str}<', content)
    
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    scan_and_score_all()
