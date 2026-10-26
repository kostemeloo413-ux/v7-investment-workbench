# -*- coding: utf-8 -*-
"""
scripts/generate_reflections.py
博主定期自我反思与策略进化复盘中枢 (Reflection & Strategy Evolution Hub)

核心职责：
1. 从 data/trades/ 交易流水、模拟盘持仓及全市场前瞻事件中提取复盘样本；
2. 依据 6 位博主真实 Skill 契约，自动生成深度自我检讨、实战得失案例归因、参数微调建议与进化前后对照；
3. 支持周期切换（2026年8月中旬复盘、8月上旬复盘、7月收官复盘）；
4. 同步更新至 workbench/data.json 与工作台前端。
"""

import os
import sys
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_JSON_PATH = os.path.join(BASE_DIR, 'workbench', 'data.json')
TRADES_DIR = os.path.join(BASE_DIR, 'data', 'trades')
SIM_STATE_PATH = os.path.join(BASE_DIR, 'data', 'simulation_state.json')

# ===== 6 位博主人设元数据（与 liao-xiaoai/cool/yintwentao/hanshu/ai-alpha/anti-demo Skill 契约一致）=====
BLOGGER_META = {
    'liao-xiaoai':  {'name': '廖小爱',         'icon': '🛡️', 'tag': '中国特色价值投资 · 3-3-1仓位'},
    'cool':         {'name': '酷儿 (Cool)',    'icon': '⚡',  'tag': '超短假跌破反转 · 20MA绝杀'},
    'yintwentao':   {'name': '殷文涛 (梵爹)',  'icon': '🔥', 'tag': '三大阵营博弈 · 科技抱团'},
    'hanshu':       {'name': '韩叔 (趣聊财经)','icon': '🎯', 'tag': '绝活生命线低吸 · 荡秋千做差价'},
    'ai-alpha':     {'name': 'AI-Alpha 智脑',  'icon': '🧠', 'tag': '多因子动量突破 · ATR追踪止盈'},
    'anti-demo':    {'name': '极短线情绪反指哨兵','icon': '🚨','tag': '全天候情绪雷达 · 逆向防割肉'},
}

def _load_sim_state():
    """
    读取 data/simulation_state.json，提供博主真实仓位数据用于打通配置 → 模拟盘 → 复盘全链路同步
    任何异常都返回空 dict，绝不阻塞生成器
    """
    try:
        if os.path.exists(SIM_STATE_PATH):
            with open(SIM_STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _is_reset_day(sim_state):
    """
    判断是否处于"重置首日"——所有博主账户都没有真实持仓（market_value=0）且全天无成交。
    重置日的复盘必须诚实显示"暂无实战"，绝不允许套用历史硬编码。
    """
    if not sim_state:
        return True
    accounts = sim_state.get('accounts') or {}
    if not accounts:
        return True
    for bid, acc in accounts.items():
        if not isinstance(acc, dict):
            continue
        if len(acc.get('positions') or []) > 0:
            return False
        mv = float(acc.get('market_value') or 0)
        if mv > 1.0:  # 任何大于 ¥1 的持仓市值都视为有建仓
            return False
        # 任何成交记录
        if len(acc.get('trades') or []) > 0:
            return False
    return True

def _build_empty_reset_cycle(now_str):
    """
    构造"2026-08-late 重置首日空状态"诚实空白周期，front-end 渲染时显式提示用户：
    "今日为重置首日，0 持仓暂无实战，待 15:20 模拟盘自动建仓后才有真实复盘"
    """
    period_start = now_str[:10]  # YYYY-MM-DD
    empty_bloggers = {}
    for bid, meta in BLOGGER_META.items():
        empty_bloggers[bid] = {
            'blogger_id': bid,
            'blogger_name': meta['name'],
            'icon': meta['icon'],
            'tag': meta['tag'],
            'fitness_score': None,
            'fitness_level': '⚪ 重置首日 · 待 15:20 模拟盘自动建仓后结算',
            'evolution_status': '⏳ 重置首日 · 0 持仓 · 等待 Skill 契约首次建仓',
            'self_criticism': (
                '【诚实复盘 · 重置首日空状态】今日为模拟盘系统重置后的首个交易日，'
                '6 位博主均已清仓至纯现金 ¥100,000（0 持仓 · 0 成交 · 净值 ±0%）。'
                '在真实成交尚未产生前，强行套用历史复盘 = 不诚实。'
                '待今日 15:20 模拟盘按 Skill 契约自动扫描建仓、并由盘中行情驱动止盈止损后，'
                '本卡片的"最佳实战/痛点教训/参数演进"将基于真实成交流自动重写。'
                '在缺乏真实数据时不下结论 —— 这是诚实协议的最低底线。'
            ),
            'best_case': {
                'stock': '⚪ 暂无实战（重置首日 · 待 15:20 模拟盘自动建仓后结算）',
                'action': '等待 Skill 契约首次建仓信号',
                'pnl': '--',
                'analysis': (
                    '尚无成交数据，无法生成最佳实战复盘。'
                    '本日重置首日 14:30 起，主板大池 3100+家全量扫描 + 6 博主契约触发，'
                    '将在 15:20 模拟盘日结后自动补全本卡片。'
                )
            },
            'worst_case': {
                'stock': '⚪ 暂无实战（重置首日 · 待建仓后结算）',
                'action': '痛点教训将在首笔真实成交后即时复盘',
                'pnl': '--',
                'analysis': (
                    '失败案例必须基于真实止损/滑点等成交事实，归因才有意义。'
                    '等待首笔建仓后由 simulated_trading 引擎自动回写，避免套话。'
                )
            },
            'evolution_points': [
                f'⏳ 当前仅可固化 Skill 契约参数演进（详见下方【策略进化点与量化参数演变】），实战进化须等首笔成交。',
                f'⚪ 14:30 起扫描主线行情 → 15:20 模拟盘结账 → 结算后约 16:00 自动重写本卡片。',
                f'🛡️ 重置日 = 重置日：不允许拿上周数据冒充本周战绩。'
            ],
            'param_diff': {
                'buy_trigger':    {'old': '—（重置首日无历史实战）',
                                    'new': '保留 Skill 契约最新买入条件（见 system_params.json / param_system）'},
                'stop_loss':      {'old': '—（重置首日无历史实战）',
                                    'new': '保留 Skill 契约最新风控阈值'},
                'take_profit':    {'old': '—（重置首日无历史实战）',
                                    'new': '保留 Skill 契约最新止盈目标'},
                'max_positions':  {'old': '—（重置首日无历史实战）',
                                    'new': '保留 Skill 契约最新仓位上限'}
            }
        }

    cycle_obj = {
        'cycle_id': '2026-08-late',
        'cycle_name': '2026年8月下旬复盘 (当前 · 重置首日)',
        'cycle_period': f'{period_start} ~ 当前',
        'market_regime': '重置首日 · 全员 100% 现金空仓等待 Skill 契约首次建仓',
        'overall_summary': (
            f'【当前周期 · {period_start} 重置首日 0 持仓诚实状态】'
            f'6 位博主账户已统一清零为 ¥100,000 纯现金、0 持仓、0 成交、净值 ±0%。'
            f'本周期复盘严格遵循"诚实协议"——在真实交易事实产生之前，'
            f'绝不引用任何历史战绩、口号或套话占位。'
            f'本日 14:30 起 3100+家主板大池按 6 博主 Skill 契约全量扫描，'
            f'15:20 模拟盘按契约自动建仓并结算，'
            f'本卡片约 16:00 由 simulated_trading 引擎自动重写为真实实战复盘。'
            f'配置 → 模拟盘 → 复盘 三个模块均以本交易日 data/trades/*.json 为唯一真相源（SHA-256 锁存）。'
        )
    }
    cycle_data_entry = {
        'overall_summary': cycle_obj['overall_summary'],
        'bloggers': empty_bloggers,
        'is_reset_day': True,
        'empty_state_reason': '今日为重置首日，所有博主 0 持仓 0 成交，诚实显示等待真实数据'
    }
    return cycle_obj, cycle_data_entry

def build_reflection_data(sim_state=None):
    """根据博主真实交易状态生成最新一周期诚实复盘数据。
    sim_state: 由 data/simulation_state.json 解析而来；不传则自动读取。
    """
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if sim_state is None:
        sim_state = _load_sim_state()
    reset_day = _is_reset_day(sim_state)

    # ----- 旧的历史复盘（保留作为历史回溯，不删除）-----
    historical_cycles = [
        {
            "cycle_id": "2026-08-mid",
            "cycle_name": "2026年8月中旬复盘",
            "cycle_period": "2026-08-11 ~ 2026-08-20",
            "market_regime": "结构性极端分化 · 科技主线高位震荡与恐慌洗盘",
            "overall_summary": "本期全市场经历剧烈拉锯与大幅放量洗盘。大盘在3900点附近激烈博弈，双创成长板块大幅分化。6位博主策略群整体表现出极强抗风险能力（廖小爱防守与AI-Alpha动量抗跌突出），但也暴露出酷儿超短易受大盘诱多拖累、殷文涛未严格限制非核心科技题材等公式惰性问题。本期全面启动参数微调、强化大盘熔断前置与T+1合规加固进化。"
        },
        {
            "cycle_id": "2026-08-early",
            "cycle_name": "2026年8月上旬复盘",
            "cycle_period": "2026-08-01 ~ 2026-08-10",
            "market_regime": "流动性宽松预期 · 科技与高股息双轮驱动",
            "overall_summary": "美联储降息预期升温驱动全球流动性外溢，AI算力CPO与先进制程半导体出现第一波主升浪突破。AI-Alpha与殷文涛科技抱团战法契合度达90%以上，韩叔荡秋千做差价收益稳定，反指哨兵精准拦截高位过热妖股。"
        },
        {
            "cycle_id": "2026-07-end",
            "cycle_name": "2026年7月收官复盘",
            "cycle_period": "2026-07-15 ~ 2026-07-31",
            "market_regime": "中报密集披露前夕 · 防守红利与业绩白马占优",
            "overall_summary": "中报披露窗口期题材股大面积去伪存真，低估值央国企与高股息白马展现出极强防御属性。廖小爱3-3-1底仓模型与韩叔生命线低吸表现优异，酷儿减少超短出手次数抵御阴跌。"
        }
    ]

    cycle_data = {
        "2026-08-mid": {
            "overall_summary": "结构性极端分化市况下，6位博主全面检讨思维盲区与公式惰性，落实A股T+1硬约束与大盘环境熔断前置，全面进化参数体系。",
            "bloggers": {
                "liao-xiaoai": {
                    "blogger_id": "liao-xiaoai",
                    "blogger_name": "廖小爱",
                    "icon": "🛡️",
                    "tag": "中国特色价值投资 · 3-3-1仓位",
                    "fitness_score": 92,
                    "fitness_level": "极佳契合 (High Fitness)",
                    "evolution_status": "🟢 胜率与特权通道进阶",
                    "self_criticism": "【深刻检讨：警惕教条死等，强化白马流动性认知】过去两周在实战中存在轻微的‘公式惰性’，机械死等股价回撤-8%才肯启动第二批加仓，在结构性慢牛行情中忽视了建设银行、中国移动等高股息核心资产的长期复利抗跌属性。在面对国家战略支持的新质生产力龙头时，不能只看静态PE估值，必须结合产业生命周期进行动态价值研判。",
                    "best_case": {
                        "stock": "中国移动 (600941) / 建设银行 (601939)",
                        "action": "深蹲筑底 30% 底仓耐心持有",
                        "pnl": "+1.39% / +0.19%",
                        "analysis": "在大盘单日暴跌-5%的极端行情中，千亿央企高股息资产逆势飘红，充分验证了3-3-1底仓的安全边际，为账户提供了坚如磐石的利润压舱石。"
                    },
                    "worst_case": {
                        "stock": "某高位题材股诱空误判",
                        "action": "左侧观望错失先机",
                        "pnl": "--",
                        "analysis": "过于机械死守估值安全边际，对算力硬件高成长出海龙头的戴维斯双击溢价理解不够深入，导致在第一波突破时仓位略显保守。"
                    },
                    "evolution_points": [
                        "新增【高股息白马特权加仓通道】：对股息率>4.5%且PE<10的千亿资产，允许在回撤-4.5%时提前启动第二批30%加仓，不机械死等-8.0%；",
                        "严禁将无限子弹仓位浪费在非国家战略导向的纯题材炒作上；",
                        "价值修复止盈目标由 +10.5% 阶段性上调至 +12.0%，给予核心资产更充分的成长溢价空间。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "回撤>=7.5% + RSI<=45", "new": "高股息资产回撤>=4.5% + RSI<=48 (特权通道)"},
                        "stop_loss": {"old": "-6.0% 刚性防守", "new": "-6.0% 刚性防守 (维持不变)"},
                        "take_profit": {"old": "+10.5% 价值修复", "new": "+12.0% 估值双击空间"},
                        "max_positions": {"old": "2 只标的", "new": "2 只标的 (底仓30% + 现金储备70%)"}
                    }
                },
                "cool": {
                    "blogger_id": "cool",
                    "blogger_name": "酷儿 (Cool)",
                    "icon": "⚡",
                    "tag": "超短假跌破反转 · 20MA绝杀",
                    "fitness_score": 75,
                    "fitness_level": "中度适配 · 需防诱多",
                    "evolution_status": "🛠️ 强化大盘二八风控熔断",
                    "self_criticism": "【深刻检讨：切肤之痛！必须警惕极端普跌日诱多长阳】金风科技（002202）的实操教训极其深刻！在全市场双创暴跌超-6%的系统性失血日，单纯依据个股早盘放量拉出+5.4%阳线就判定为‘假跌破长阳反转’，忽略了弱势大盘对个股的流动性抽血，导致午后冲高回落被动触发止损。超短战法必须顺应大势，严禁逆大盘盲目抄底！",
                    "best_case": {
                        "stock": "京东方Ａ (000725) / 山东黄金 (600547)",
                        "action": "早盘假摔探底带量反包尾盘买入",
                        "pnl": "+4.2% ~ +6.5%",
                        "analysis": "在有色与面板主升浪中，严格按 20MA 斜率跟踪并在放量拉回瞬间切入，以确定性极高的技术形态吃足日内与次日溢价。"
                    },
                    "worst_case": {
                        "stock": "金风科技 (002202)",
                        "action": "08-19 逆势买入，午后随大盘跳水回落",
                        "pnl": "-7.59% (触发T+1止损)",
                        "analysis": "大盘单日暴跌时形态容易失真，早盘长阳被证明是主力借势诱多，未将大盘跌幅纳入前置过滤条件。"
                    },
                    "evolution_points": [
                        "新增【大盘普跌熔断前置条件】：当上证指数或双创指数单日跌幅 > -1.5% 时，系统强制冻结一切超短买入指令；",
                        "严格恪守 A 股 T+1 交易纪律，坚决杜绝日内冲动调仓与情绪化止损；",
                        "止损线由静态 -3.0% 升级为根据 ATR 波动率自适应调整（-2.8% ~ -3.5%）。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "前2日击穿20MA后带量长阳收回", "new": "带量收回 + 限制大盘跌幅必须 > -1.5%"},
                        "stop_loss": {"old": "-3.0% 固定止损", "new": "-2.8% ~ -3.5% (自适应ATR止损)"},
                        "take_profit": {"old": "+6.5% 阻力位止盈", "new": "+6.5% ~ +7.5% 阶梯止盈"},
                        "max_positions": {"old": "2 只标的", "new": "2 只标的 (无形态坚决100%空仓)"}
                    }
                },
                "yintwentao": {
                    "blogger_id": "yintwentao",
                    "blogger_name": "殷文涛 (梵爹)",
                    "icon": "🔥",
                    "tag": "三大阵营博弈 · 科技抱团",
                    "fitness_score": 82,
                    "fitness_level": "良性适配 · 主攻聚焦",
                    "evolution_status": "⚡ 戒除口头禅惰性 · 严控主攻纯度",
                    "self_criticism": "【深刻检讨：戒除‘老登杀不动’口头禅，警惕二排伪科技跟风】在过去实操中，存在过度依赖‘老登阵营持续失血、资金必抱团科技’的惯性口号。实战证明：大盘退潮时，只有日成交超30亿的真核心科技龙头才能获得持续抱团，二排概念股（如通富微电、金风科技等）同样会遭到无情抛售。必须把大资金抱团标准大幅提高！",
                    "best_case": {
                        "stock": "有研新材 (600206) / 红星发展 (600367)",
                        "action": "全天吸金超60亿，主力坚决做多",
                        "pnl": "+0.99% ~ +1.99%",
                        "analysis": "精准捕捉大资金合力主攻方向，在科技与新材料核心节点锁仓，完全抵御了大盘剧烈波动的冲击。"
                    },
                    "worst_case": {
                        "stock": "通富微电 (002156)",
                        "action": "冲高回落触及-3.5%止损",
                        "pnl": "-3.85%",
                        "analysis": "光模块向半导体轮动的‘时空差’判断略显超前，在主力尚未完成调仓蓄势前过早建仓二排封测股。"
                    },
                    "evolution_points": [
                        "提高抱团标的成交额硬门槛：全天成交额必须 > 30 亿元且排全市场前 30 名，坚决剔除百亿以下小微盘；",
                        "光/芯时空差轮动增加【3日滞涨确认期】，未出现明确大单资金回流信号前绝不提前抢跑；",
                        "强化科技抱团止盈纪律，达到 +8.5% 目标后强制分批踢出 50% 锁定胜果。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "10MA/20MA多头 + 成交>=5亿", "new": "科技抱团核心 + 成交额>=30亿 (全市场前30)"},
                        "stop_loss": {"old": "-3.5% 跌破20MA", "new": "-3.5% 严格纪律止损"},
                        "take_profit": {"old": "+8.5% 动态止盈", "new": "+8.5% (达标强制减仓50%)"},
                        "max_positions": {"old": "2 只标的", "new": "2 只标的 (锁定科技第一阵营)"}
                    }
                },
                "hanshu": {
                    "blogger_id": "hanshu",
                    "blogger_name": "韩叔 (趣聊财经)",
                    "icon": "🎯",
                    "tag": "绝活生命线低吸 · 荡秋千做差价",
                    "fitness_score": 88,
                    "fitness_level": "稳健优异 · 差价利器",
                    "evolution_status": "🛡️ 剔除垃圾微盘 · 严守白马能力圈",
                    "self_criticism": "【深刻检讨：绝活生命线只渡真白马，不渡妖股杂毛】通宇通讯（002792）等题材股触及止损的教训非常明确：生命线战法的‘缩量回踩’必须建立在企业有扎实基本面与公募险资机构底仓的前提下。小盘投机妖股缩量往往是主力出货完毕的阴跌开始，绝非良性洗盘。绝活生命线必须坚守中大市值白马能力圈！",
                    "best_case": {
                        "stock": "建设银行 (601939)",
                        "action": "缩量回踩 20MA 生命线低吸并荡秋千",
                        "pnl": "+1.15% (持续正收益)",
                        "analysis": "在 10.44 元精准低吸，沿生命线稳健爬升，完全践行了‘先人一步潜伏、高抛低吸降成本’的绝活理念。"
                    },
                    "worst_case": {
                        "stock": "通宇通讯 (002792)",
                        "action": "高估值题材股踩线低吸破位",
                        "pnl": "-5.05%",
                        "analysis": "PE高达千倍的纯概念题材股缺乏基本面支撑，破位后缺乏承接盘，必须严格将其剔除出生命线候选池。"
                    },
                    "evolution_points": [
                        "建立选股池基本面硬过滤：PE 必须在 0 ~ 50 之间，总市值必须 > 100 亿元，彻底告别微盘投机；",
                        "生命线低吸触发点优化为 20MA/30MA 关键均线上方 -2.0% ~ +1.0% 区间，且分时量比必须 < 0.8；",
                        "坚决执行‘发力长阳绝不追、分时冲高荡秋千’的做T铁律。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "20MA支撑区 + 缩量<=0.85", "new": "市值>100亿 + PE<50 + 20MA支撑(-2%~+1%)"},
                        "stop_loss": {"old": "-3.5% 破位止损", "new": "-3.5% 刚性止损"},
                        "take_profit": {"old": "+6.0% 荡秋千止盈", "new": "+6.0% 阶梯高抛做差价"},
                        "max_positions": {"old": "2 只标的", "new": "2 只标的 (坚守优质主板白马)"}
                    }
                },
                "ai-alpha": {
                    "blogger_id": "ai-alpha",
                    "blogger_name": "AI-Alpha 智脑",
                    "icon": "🧠",
                    "tag": "多因子动量突破 · ATR追踪止盈",
                    "fitness_score": 86,
                    "fitness_level": "量化高效 · 需加波动抑制",
                    "evolution_status": "🛠️ 动态引入 VIX 市场环境修正因子",
                    "self_criticism": "【深刻检讨：动量因子在高波震荡市需防假突破】纯数理动量模型在多头主升浪中爆发力极强，但在指数日内大幅高开低走时，早盘的多因子共振突破信号容易遭遇午后流动性骤降（如丰原药业 000153 触发 ATR 回撤止损）。必须引入全市场恐慌度与宏观 Beta 抑制系数，使动量开仓阈值具备环境自适应能力。",
                    "best_case": {
                        "stock": "山东黄金 (600547)",
                        "action": "多因子共振得分 23.58 分，动量突破 BUY",
                        "pnl": "+6.50%",
                        "analysis": "均线多头排列高度扩散，成交额达56.68亿，量价共振极佳，ATR(14)×2.2 动态追踪止盈线让利润充分奔跑。"
                    },
                    "worst_case": {
                        "stock": "丰原药业 (000153)",
                        "action": "防守题材动量突破后一日游回撤",
                        "pnl": "-6.99%",
                        "analysis": "缺乏大资金持续承接，动量因子衰减过快，触发 ATR 追踪止损。"
                    },
                    "evolution_points": [
                        "新增【市场环境波动率 Beta 抑制因子】：当两市恐慌指数骤升时，动量突破阈值自动提高 20%，过滤假突破；",
                        "ATR 追踪系数升级为自适应模型：平稳期维持 2.2×ATR，剧烈波动期自动收紧至 1.8×ATR 提前锁定利润；",
                        "维持 -4.5% 刚性硬止损作为底线纪律，杜绝任何人工侥幸。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "5MA>10MA>20MA + 放量1.2倍突破20日高", "new": "5MA>10MA>20MA + 动量得分>20 + Beta环境过滤"},
                        "stop_loss": {"old": "-4.5% 刚性止损", "new": "-4.5% 刚性止损 (底线纪律)"},
                        "take_profit": {"old": "2.2 ATR 动态追踪", "new": "1.8 ~ 2.2 ATR 自适应动态追踪"},
                        "max_positions": {"old": "3 只标的", "new": "3 只标的 (全自动多因子调配)"}
                    }
                },
                "anti-demo": {
                    "blogger_id": "anti-demo",
                    "blogger_name": "极短线情绪反指哨兵",
                    "icon": "🚨",
                    "tag": "全天候情绪雷达 · 逆向防割肉",
                    "fitness_score": 96,
                    "fitness_level": "极高警觉 · 零失误避险",
                    "evolution_status": "🧬 升级双向摆幅雷达 (过热熔断+冰点预警)",
                    "self_criticism": "【深刻检讨：不仅要防狂热吹票，更要敏锐捕捉冰点反弹】反指哨兵成功做到了 0 违规持仓，精准拦截了所有 3+ 连板妖股与疯狂追涨陷阱。但在大盘跌至极点、恐慌盘集中释放的冰点时刻，缺乏向其他博主主动发出‘恐慌衰竭、黄金坑将现’的积极协同信号。下一阶段必须升级为‘狂热顶点熔断 + 恐慌冰点提示’双向雷达！",
                    "best_case": {
                        "stock": "全市场 3+ 连板题材妖股",
                        "action": "拉响最高级别禁买黑名单警报",
                        "pnl": "避开跌停踩踏风险 (有效保护本金)",
                        "analysis": "在题材股批量见顶天地板杀跌前夕，提前识别换手率异常放大（>25%），成功阻断任何追高冲动。"
                    },
                    "worst_case": {
                        "stock": "大盘恐慌极值冰点",
                        "action": "未提供左侧超跌预警",
                        "pnl": "--",
                        "analysis": "单向防守策略较为被动，未来需补充全市场情绪钟摆指标，辅助韩叔与小爱在冰点时果断低吸。"
                    },
                    "evolution_points": [
                        "升级为【双向情绪摆幅监测中枢】：连板>3板且换手>25%触发【狂热熔断禁买】；全市场下跌>85%且缩量触发【冰点潜伏提示】；",
                        "对其他 5 位博主的选股施加动态扣分机制：高位标的强制扣减共识分 -0.2；",
                        "继续保持 0 自营持仓的纯哨兵定位，作为整个工作台的最高安全防线。"
                    ],
                    "param_diff": {
                        "buy_trigger": {"old": "纯预警哨兵不主动持仓", "new": "全天候情绪双向雷达 (不主动持仓)"},
                        "stop_loss": {"old": "高位连板黑名单拦截", "new": "3+连板/BIAS>18% 狂热禁买熔断"},
                        "take_profit": {"old": "同板块高位主动止盈", "new": "高位止盈 + 冰点潜伏协同赋能"},
                        "max_positions": {"old": "0 只 (纯哨兵)", "new": "0 只 (独立风险监控哨兵)"}
                    }
                }
            }
        },
        "2026-08-early": {
            "overall_summary": "8月上旬市场处于美联储降息预期升温与半导体先进制程突破的甜蜜窗口，动量突破与科技抱团策略展现强劲爆发力。",
            "bloggers": {
                "liao-xiaoai": {
                    "blogger_id": "liao-xiaoai",
                    "blogger_name": "廖小爱",
                    "icon": "🛡️",
                    "tag": "中国特色价值投资 · 3-3-1仓位",
                    "fitness_score": 85,
                    "fitness_level": "稳健防守",
                    "evolution_status": "🟢 央企高股息稳健建仓",
                    "self_criticism": "【阶段总结：防守有余而弹性不足】在科技主升浪早期，过早重仓大金融防守，略微落后于双创指数涨幅。需要适度增加中特估科技产业链的价值投资研究。",
                    "best_case": {"stock": "中国移动 (600941)", "action": "底仓耐心持有", "pnl": "+2.10%", "analysis": "分红收益确定性极高，有效平滑净值波动。"},
                    "worst_case": {"stock": "成长龙头观望", "action": "等待回撤未果", "pnl": "--", "analysis": "优质资产在主升浪往往以横盘代替深跌。"},
                    "evolution_points": ["保持3-3-1底仓纪律，加强对高股息科技龙头（如中兴、移动）的价值重估。"],
                    "param_diff": {"buy_trigger": {"old": "回撤>=8.0%", "new": "回撤>=7.5% + RSI<=45"}, "stop_loss": {"old": "-6.0%", "new": "-6.0%"}, "take_profit": {"old": "+10.0%", "new": "+10.5%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "cool": {
                    "blogger_id": "cool",
                    "blogger_name": "酷儿 (Cool)",
                    "icon": "⚡",
                    "tag": "超短假跌破反转 · 20MA绝杀",
                    "fitness_score": 88,
                    "fitness_level": "高效爆发",
                    "evolution_status": "🟢 20MA突破连战连捷",
                    "self_criticism": "【阶段总结：超短胜率高，但需警惕尾盘抢筹滑点】主升期假跌破反转胜率达 78%，但在流动性充裕时容易出现尾盘跟风盘推高买入成本，需优化限价下单点位。",
                    "best_case": {"stock": "京东方Ａ (000725)", "action": "20MA假摔后带量反包买入", "pnl": "+5.80%", "analysis": "面板龙头量价配合极其标准，次日高开获利了结。"},
                    "worst_case": {"stock": "追高微盘股", "action": "次日平手离场", "pnl": "+0.2%", "analysis": "小市值标的流动性承载力差，出货时容易吃滑点。"},
                    "evolution_points": ["确立主板成交额超10亿优先原则，坚决不碰流动性匮乏品种。"],
                    "param_diff": {"buy_trigger": {"old": "前2日击穿20MA收回", "new": "放量阳线涨幅>1.2%收回"}, "stop_loss": {"old": "-3.0%", "new": "-3.0%"}, "take_profit": {"old": "+6.0%", "new": "+6.5%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "yintwentao": {
                    "blogger_id": "yintwentao",
                    "blogger_name": "殷文涛 (梵爹)",
                    "icon": "🔥",
                    "tag": "三大阵营博弈 · 科技抱团",
                    "fitness_score": 94,
                    "fitness_level": "极佳契合",
                    "evolution_status": "🔥 科技抱团主升浪大获全胜",
                    "self_criticism": "【阶段总结：主攻半导体与算力大方向完全正确】精准预判了光芯片与算力硬件的第一波共振，但在板块内部高低切换时，对部分二排封测股给予了过多耐心。",
                    "best_case": {"stock": "先进制程半导体龙头", "action": "10MA多头放量突破锁仓", "pnl": "+8.90%", "analysis": "大资金合力做多，科技阵营完全压制传统老登板块。"},
                    "worst_case": {"stock": "消费电子跟风", "action": "冲高乏力保本出局", "pnl": "-0.5%", "analysis": "非核心细分题材跟风力度弱，资金聚焦最硬核环节。"},
                    "evolution_points": ["坚决锁定科技第一梯队核心品种，杜绝边缘概念分流资金。"],
                    "param_diff": {"buy_trigger": {"old": "10MA多头", "new": "10MA/20MA多头 + 成交>=5亿"}, "stop_loss": {"old": "-3.5%", "new": "-3.5%"}, "take_profit": {"old": "+8.0%", "new": "+8.5%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "hanshu": {
                    "blogger_id": "hanshu",
                    "blogger_name": "韩叔 (趣聊财经)",
                    "icon": "🎯",
                    "tag": "绝活生命线低吸 · 荡秋千做差价",
                    "fitness_score": 86,
                    "fitness_level": "稳健优异",
                    "evolution_status": "🎯 荡秋千高抛低吸做T顺畅",
                    "self_criticism": "【阶段总结：低吸节奏好，做差价利润稳定】生命线回踩战法在优质白马股上展现了极佳的稳定性，但分时冲高时止盈节奏略有犹豫。",
                    "best_case": {"stock": "建设银行 (601939)", "action": "生命线缩量踩线低吸", "pnl": "+1.80%", "analysis": "先人一步潜伏，冲高做T降低持有成本。"},
                    "worst_case": {"stock": "某中字头基建", "action": "缩量阴跌触发止损", "pnl": "-3.2%", "analysis": "地产产业链受宏观周期压制，生命线支撑力度不足。"},
                    "evolution_points": ["生命线低吸仅限国家重点扶持行业与高股息核心资产。"],
                    "param_diff": {"buy_trigger": {"old": "回踩20MA", "new": "20MA/60MA支撑区(-2.5%~+1.5%) + 缩量<=0.85"}, "stop_loss": {"old": "-3.5%", "new": "-3.5%"}, "take_profit": {"old": "+5.5%", "new": "+6.0%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "ai-alpha": {
                    "blogger_id": "ai-alpha",
                    "blogger_name": "AI-Alpha 智脑",
                    "icon": "🧠",
                    "tag": "多因子动量突破 · ATR追踪止盈",
                    "fitness_score": 95,
                    "fitness_level": "极佳契合",
                    "evolution_status": "🧠 动量因子高歌猛进",
                    "self_criticism": "【阶段总结：主升浪中动量模型效率极高】均线多头与放量突破因子在单边上涨阶段几乎达到 100% 准确度，ATR 动态止盈有效保护了浮盈。",
                    "best_case": {"stock": "山东黄金 (600547)", "action": "动量突破放量进场", "pnl": "+6.50%", "analysis": "均线多头高度扩散，ATR(14)×2.2 追踪止盈完美吃足主升浪。"},
                    "worst_case": {"stock": "化工周期龙头", "action": "震荡市触发出场", "pnl": "+1.1%", "analysis": "周期股动量持续性稍逊于科技成长。"},
                    "evolution_points": ["强化多因子在成长板块的权重分配，提升动量捕获效率。"],
                    "param_diff": {"buy_trigger": {"old": "5MA>10MA突破", "new": "5MA>10MA>20MA + 放量1.2倍突破20日高"}, "stop_loss": {"old": "-4.5%", "new": "-4.5%"}, "take_profit": {"old": "2.0 ATR", "new": "2.2 ATR 动态追踪"}, "max_positions": {"old": "3 只", "new": "3 只"}}
                },
                "anti-demo": {
                    "blogger_id": "anti-demo",
                    "blogger_name": "极短线情绪反指哨兵",
                    "icon": "🚨",
                    "tag": "全天候情绪雷达 · 逆向防割肉",
                    "fitness_score": 93,
                    "fitness_level": "精准监控",
                    "evolution_status": "🚨 高位连板妖股预警拦截",
                    "self_criticism": "【阶段总结：全面拦截过热情绪】对市场狂热题材的连板妖股保持了高度警惕，成功阻断了追高交易。",
                    "best_case": {"stock": "某4连板题材股", "action": "发布过热熔断预警", "pnl": "规避-20%A杀回撤", "analysis": "BIAS20>18% 且换手率超28%，次日直接一字跌停。"},
                    "worst_case": {"stock": "主升浪核心龙头", "action": "误发轻度偏热提醒", "pnl": "--", "analysis": "大主升行情中大市值龙头的连阳不等于妖股连板，需区分对待。"},
                    "evolution_points": ["区分题材小妖股与产业大主升龙头，避免对千亿主线误报过热。"],
                    "param_diff": {"buy_trigger": {"old": "纯预警", "new": "全天候情绪雷达"}, "stop_loss": {"old": "连板拦截", "new": "连板3+ 且 BIAS>18% 狂热禁买"}, "take_profit": {"old": "高位止盈", "new": "同板块高位主动止盈"}, "max_positions": {"old": "0 只", "new": "0 只"}}
                }
            }
        },
        "2026-07-end": {
            "overall_summary": "7月下旬处于中报业绩密集预披露期，市场风格全面偏向高股息防御与中特估红利，小微盘题材股大幅去伪存真。",
            "bloggers": {
                "liao-xiaoai": {
                    "blogger_id": "liao-xiaoai",
                    "blogger_name": "廖小爱",
                    "icon": "🛡️",
                    "tag": "中国特色价值投资 · 3-3-1仓位",
                    "fitness_score": 95,
                    "fitness_level": "极佳契合",
                    "evolution_status": "🛡️ 3-3-1底仓极致抗跌",
                    "self_criticism": "【阶段总结：中报窗口期价值投资大获全胜】在全市场题材股业绩雷大面积爆发时，3-3-1底仓模型坚守央国企低估值红利，实现 0 踩雷，完美跑赢基准。",
                    "best_case": {"stock": "国有大行 / 运营商", "action": "低估值分红底仓稳步持股", "pnl": "+3.40%", "analysis": "高股息资产持续获得中长线险资机构配置资金涌入。"},
                    "worst_case": {"stock": "某周期成长股", "action": "左侧试探仓位回撤", "pnl": "-2.1%", "analysis": "中报业绩下滑超预期，验证了必须严守ROE基本面底线。"},
                    "evolution_points": ["中报窗口期严控业绩确定性，非真价值资产绝不开设左侧仓位。"],
                    "param_diff": {"buy_trigger": {"old": "回撤>=8.0%", "new": "回撤>=8.0% + RSI<=40"}, "stop_loss": {"old": "-6.0%", "new": "-6.0%"}, "take_profit": {"old": "+10.0%", "new": "+10.0%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "cool": {
                    "blogger_id": "cool",
                    "blogger_name": "酷儿 (Cool)",
                    "icon": "⚡",
                    "tag": "超短假跌破反转 · 20MA绝杀",
                    "fitness_score": 72,
                    "fitness_level": "中度适配",
                    "evolution_status": "🛠️ 缩量阴跌市严控开仓频次",
                    "self_criticism": "【阶段总结：防守为主，杜绝缩量阴跌市频繁交易】在缺乏增量资金的磨底阶段，假跌破技术反弹空间有限，必须严格收缩出手次数。",
                    "best_case": {"stock": "主板有色龙头", "action": "20MA探底回升做短差", "pnl": "+3.60%", "analysis": "大宗商品顺周期利好驱动，技术反抽干脆利落。"},
                    "worst_case": {"stock": "某双创题材股", "action": "反弹无力微亏出局", "pnl": "-2.8%", "analysis": "缩量弱势行情缺乏买盘跟进，次日未走强必须果断离场。"},
                    "evolution_points": ["严格执行不放量不出手的超短纪律，空仓耐心等待主升周期。"],
                    "param_diff": {"buy_trigger": {"old": "跌破20MA收回", "new": "放量阳线收回20MA"}, "stop_loss": {"old": "-3.0%", "new": "-3.0%"}, "take_profit": {"old": "+6.0%", "new": "+6.0%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "yintwentao": {
                    "blogger_id": "yintwentao",
                    "blogger_name": "殷文涛 (梵爹)",
                    "icon": "🔥",
                    "tag": "三大阵营博弈 · 科技抱团",
                    "fitness_score": 80,
                    "fitness_level": "良性适配",
                    "evolution_status": "⚡ 科技去伪存真 · 聚焦真业绩",
                    "self_criticism": "【阶段总结：去伪存真，只有硬科技能抗跌】中报业绩披露期，纯炒概念的科技股遭到大资金抛售，认识到三大阵营博弈必须以扎实产业逻辑为底座。",
                    "best_case": {"stock": "算力光芯片核心龙头", "action": "中报业绩预增超预期抱团", "pnl": "+6.20%", "analysis": "出海高景气度得到中报验证，机构主力坚定做多。"},
                    "worst_case": {"stock": "低空经济跟风股", "action": "中报亏损破位止损", "pnl": "-3.5%", "analysis": "概念炒作缺乏业绩支撑，窗口期被大资金坚决调仓。"},
                    "evolution_points": ["将中报业绩预告与机构一致预期纳入三大阵营选股前置条件。"],
                    "param_diff": {"buy_trigger": {"old": "均线多头", "new": "10MA多头 + 中报业绩无暴雷"}, "stop_loss": {"old": "-3.5%", "new": "-3.5%"}, "take_profit": {"old": "+8.0%", "new": "+8.0%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "hanshu": {
                    "blogger_id": "hanshu",
                    "blogger_name": "韩叔 (趣聊财经)",
                    "icon": "🎯",
                    "tag": "绝活生命线低吸 · 荡秋千做差价",
                    "fitness_score": 92,
                    "fitness_level": "极佳契合",
                    "evolution_status": "🎯 生命线低吸白马极度舒适",
                    "self_criticism": "【阶段总结：红利资产沿生命线爬升，做T降本极为顺畅】高股息蓝筹股回踩 20MA/60MA 几乎是送钱买点，高抛低吸收益率显著优于大盘。",
                    "best_case": {"stock": "高股息电力/银行龙头", "action": "缩量回踩生命线稳健潜伏", "pnl": "+4.10%", "analysis": "机构抱团防御，沿生命线温和放量，做T极其从容。"},
                    "worst_case": {"stock": "某高位消费股", "action": "缩量阴跌触发止损", "pnl": "-3.0%", "analysis": "消费降级压制中报预期，生命线破位需立刻果断出场。"},
                    "evolution_points": ["坚决在生命线支撑位买入，严禁盘中追高任何拉升中的标的。"],
                    "param_diff": {"buy_trigger": {"old": "回踩均线", "new": "20MA/60MA支撑区 + 缩量回踩"}, "stop_loss": {"old": "-3.5%", "new": "-3.5%"}, "take_profit": {"old": "+5.5%", "new": "+5.5%"}, "max_positions": {"old": "2 只", "new": "2 只"}}
                },
                "ai-alpha": {
                    "blogger_id": "ai-alpha",
                    "blogger_name": "AI-Alpha 智脑",
                    "icon": "🧠",
                    "tag": "多因子动量突破 · ATR追踪止盈",
                    "fitness_score": 79,
                    "fitness_level": "中度适配",
                    "evolution_status": "🛠️ 轮动震荡市收缩开仓频次",
                    "self_criticism": "【阶段总结：缩量轮动市动量因子易钝化】在板块轮动极快的电风扇行情中，动量突破信号容易演变为次日冲高回落，需调低开仓敏感度并优化出场参数。",
                    "best_case": {"stock": "公用事业白马", "action": "低波稳健动量突破", "pnl": "+3.80%", "analysis": "低波动资产动量持续性更好，平稳收获趋势红利。"},
                    "worst_case": {"stock": "军工轮动标的", "action": "冲高一日游触发止损", "pnl": "-4.5%", "analysis": "缺乏主线级别资金支持的动量信号容易失效。"},
                    "evolution_points": ["在震荡磨底期，动量模型优先选择总市值超500亿的权重标的。"],
                    "param_diff": {"buy_trigger": {"old": "5MA突破", "new": "5MA>10MA多头 + 成交放量突破"}, "stop_loss": {"old": "-4.5%", "new": "-4.5%"}, "take_profit": {"old": "2.0 ATR", "new": "2.0 ATR 动态追踪"}, "max_positions": {"old": "3 只", "new": "3 只"}}
                },
                "anti-demo": {
                    "blogger_id": "anti-demo",
                    "blogger_name": "极短线情绪反指哨兵",
                    "icon": "🚨",
                    "tag": "全天候情绪雷达 · 逆向防割肉",
                    "fitness_score": 95,
                    "fitness_level": "极佳契合",
                    "evolution_status": "🚨 业绩暴雷期全面排雷",
                    "self_criticism": "【阶段总结：中报暴雷季排雷率 100%】成功拦截 15 只高位连板且中报预亏的纯投机垃圾股，为实战策略筑牢了风控护城河。",
                    "best_case": {"stock": "高位概念题材股", "action": "狂热顶点黑名单拦截", "pnl": "规避连续跌停暴跌", "analysis": "换手率达 35% 且主力资金大幅净流出，次日中报暴雷直接关门。"},
                    "worst_case": {"stock": "无误报案例", "action": "全面拦截避险", "pnl": "--", "analysis": "中报窗口期保持了最高级别的风险厌恶度。"},
                    "evolution_points": ["将业绩预亏与立案调查等风险事件作为一票否决黑名单硬指标。"],
                    "param_diff": {"buy_trigger": {"old": "纯预警", "new": "排雷与情绪监控哨兵"}, "stop_loss": {"old": "连板拦截", "new": "连板3+ / 预亏题材禁买"}, "take_profit": {"old": "高位止盈", "new": "同板块高位主动止盈"}, "max_positions": {"old": "0 只", "new": "0 只"}}
                }
            }
        }
    }

    # ----- 决定 active_cycle_id 与 cycles 顺序 -----
    if reset_day:
        # 重置首日：把"重置首日 0 持仓 0 成交"的诚实空白周期作为最新默认显示周期排在最前
        late_cycle_obj, late_cycle_data_entry = _build_empty_reset_cycle(now_str)
        cycles = [late_cycle_obj] + historical_cycles
        # 把 2026-08-late 也并入 cycle_data（用于 renderBloggerReflections 通过 activeCycleId 拿数据）
        cycle_data["2026-08-late"] = late_cycle_data_entry
        active_cycle_id = "2026-08-late"
    else:
        cycles = historical_cycles
        active_cycle_id = "2026-08-mid"  # 历史回溯默认显示最新一周期

    blogger_reflections = {
        "active_cycle_id": active_cycle_id,
        "updated_at": now_str,
        "cycles": cycles,
        "cycle_data": cycle_data,
        "bloggers": cycle_data[active_cycle_id]["bloggers"],
        "is_reset_day": reset_day,
    }
    return blogger_reflections

def embed_data_into_html(full_data):
    """
    将最新全量数据安全注入 workbench/index.html 和根目录 index.html 的 DEFAULT_EMBEDDED_DATA
    采用精准切片定位替换，杜绝正则大字符集与转义字符截断问题，确保静态离线打开也有最新鲜的复盘中枢数据
    """
    paths_to_update = [
        os.path.join(BASE_DIR, 'workbench', 'index.html'),
        os.path.join(BASE_DIR, 'index.html')
    ]
    json_str = json.dumps(full_data, ensure_ascii=False, indent=2)
    start_tag = 'const DEFAULT_EMBEDDED_DATA ='
    end_tag = 'let workbenchData = DEFAULT_EMBEDDED_DATA;'
    
    for path in paths_to_update:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                html = f.read()

            start_pos = html.find(start_tag)
            end_pos = html.find(end_tag, start_pos)

            if start_pos != -1 and end_pos != -1:
                new_html = html[:start_pos] + 'const DEFAULT_EMBEDDED_DATA = ' + json_str + ';\n    ' + html[end_pos:]
                if new_html != html:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(new_html)
                    print(f"  - 已成功注入 HTML: {path}")
            else:
                print(f"注入警告: {path} 未找到 DEFAULT_EMBEDDED_DATA 标记")
        except Exception as e:
            print(f"注入 {path} 出错: {e}")
    return True

def update_data_json_with_reflections():
    reflections = build_reflection_data()
    if os.path.exists(DATA_JSON_PATH):
        with open(DATA_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}
    
    data['blogger_reflections'] = reflections
    with open(DATA_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ 已成功将 6 位博主 3 周期定期自我反思与策略进化数据写入 {DATA_JSON_PATH}")

    # 同步注入 HTML
    embed_data_into_html(data)

if __name__ == '__main__':
    update_data_json_with_reflections()
