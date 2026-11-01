# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/strategy.py
V7 博主实验室策略草案、有限 JSON DSL、人工确认与确定性解释器 (阶段 3 核心实现)
严格遵守开发指南第 7 节与 NEXT_STAGE_3.md 规范：
- 严禁 eval / exec
- 字段与操作符严格白名单
- 缺失参数、未知字段、未确认草案刚性阻断不可执行
"""

from __future__ import annotations
import os
import sys
import json
import re
import hashlib
import uuid
from typing import Optional, Dict, Any, List, Tuple, Literal

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import (
    StrategyDraft,
    StrategyVersion,
    StrategyCondition,
    ExitRules,
    SizingRules,
    ParameterOrigin,
    KnowledgeVersion,
    KnowledgeItem,
    LabPosition,
    SCHEMA_VERSION
)
from blogger_lab.store import BloggerLabStore, current_time_iso


# ==========================================
# 1. 可执行字段白名单目录 (FIELD_CATALOG)
# 严格定义计算公式、所需最少历史天数、单位与支持的操作符
# ==========================================

FIELD_CATALOG: Dict[str, Dict[str, Any]] = {
    "last_price_to_ma20_pct": {
        "name": "现价相对20日均线偏离度",
        "formula": "(close - ma20) / ma20 * 100",
        "window_days": 20,
        "unit": "percent",
        "basis": "live_price_vs_completed_daily_ma",
        "allowed_ops": ["gt", "gte", "lt", "lte", "between"]
    },
    "last_price_to_ma5_pct": {
        "name": "现价相对5日均线偏离度",
        "formula": "(close - ma5) / ma5 * 100",
        "window_days": 5,
        "unit": "percent",
        "basis": "live_price_vs_completed_daily_ma",
        "allowed_ops": ["gt", "gte", "lt", "lte", "between"]
    },
    "ma5_to_ma10_pct": {
        "name": "5日均线相对10日均线比率",
        "formula": "(ma5 - ma10) / ma10 * 100",
        "window_days": 10,
        "unit": "percent",
        "basis": "completed_daily",
        "allowed_ops": ["gt", "gte", "lt", "lte", "between"]
    },
    "volume_ratio_5d": {
        "name": "当日成交量相对5日均量比值",
        "formula": "volume / volume_ma5",
        "window_days": 5,
        "unit": "ratio",
        "basis": "completed_daily",
        "allowed_ops": ["gt", "gte", "lt", "lte", "between"]
    },
    "pct_change_1d": {
        "name": "单日涨跌幅",
        "formula": "(close - prev_close) / prev_close * 100",
        "window_days": 2,
        "unit": "percent",
        "basis": "completed_daily",
        "allowed_ops": ["gt", "gte", "lt", "lte", "between"]
    },
    "close_gt_ma20": {
        "name": "收盘价位于20日均线上方",
        "formula": "close > ma20",
        "window_days": 20,
        "unit": "boolean",
        "basis": "completed_daily",
        "allowed_ops": ["eq"]
    },
    "ma5_gt_ma10": {
        "name": "5日均线金叉或位于10日均线上方",
        "formula": "ma5 > ma10",
        "window_days": 10,
        "unit": "boolean",
        "basis": "completed_daily",
        "allowed_ops": ["eq"]
    }
}


def compute_canonical_draft_hash(
    entry_rules: List[StrategyCondition],
    exit_rules: ExitRules,
    sizing_rules: SizingRules
) -> str:
    """计算草案规则确定性 SHA-256 哈希"""
    canonical_dict = {
        "entry": [
            {
                "field": r.field,
                "op": r.op,
                "value": r.value,
                "unit": r.unit,
                "basis": r.basis
            }
            for r in sorted(entry_rules, key=lambda x: x.field)
        ],
        "exit": {
            "stop_loss_pct": exit_rules.stop_loss_pct,
            "take_profit_pct": exit_rules.take_profit_pct,
            "max_holding_sessions": exit_rules.max_holding_sessions
        },
        "sizing": {
            "position_fraction": sizing_rules.position_fraction,
            "max_positions": sizing_rules.max_positions
        }
    }
    raw = json.dumps(canonical_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# ==========================================
# 2. 确定性条件解释器 (Deterministic Interpreter)
# 严禁 eval() / exec()，采用纯字典与三值逻辑
# ==========================================

EvalStatus = Literal["TRUE", "FALSE", "UNKNOWN"]


def evaluate_single_condition(
    cond: StrategyCondition,
    market_snapshot: Dict[str, Any]
) -> Tuple[EvalStatus, str]:
    """
    针对单条入场条件进行确定性求值：
    - 返回 (TRUE / FALSE / UNKNOWN, 判定说明)
    - 绝不使用 eval/exec，未知字段/缺失参数刚性返回 UNKNOWN
    """
    field = cond.field
    if field not in FIELD_CATALOG:
        return "UNKNOWN", f"未知指标字段: '{field}'，不在白名单目录中"

    if cond.value is None:
        return "UNKNOWN", f"指标 '{field}' 缺少有效参数值 (value 为 None)"

    if field not in market_snapshot or market_snapshot[field] is None:
        return "UNKNOWN", f"行情证据中缺失指标: '{field}'"

    actual = market_snapshot[field]
    target = cond.value
    op = cond.op

    try:
        if op == "gt":
            res = float(actual) > float(target)
            return ("TRUE", f"{actual} > {target}") if res else ("FALSE", f"{actual} 不大于 {target}")
        elif op == "gte":
            res = float(actual) >= float(target)
            return ("TRUE", f"{actual} >= {target}") if res else ("FALSE", f"{actual} 小于 {target}")
        elif op == "lt":
            res = float(actual) < float(target)
            return ("TRUE", f"{actual} < {target}") if res else ("FALSE", f"{actual} 不小于 {target}")
        elif op == "lte":
            res = float(actual) <= float(target)
            return ("TRUE", f"{actual} <= {target}") if res else ("FALSE", f"{actual} 大于 {target}")
        elif op == "eq":
            res = (actual == target)
            return ("TRUE", f"{actual} == {target}") if res else ("FALSE", f"{actual} != {target}")
        elif op == "between":
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                return "UNKNOWN", f"between 操作符参数必须为长度为2的列表/元组: {target}"
            low, high = target
            if low is None or high is None:
                return "UNKNOWN", f"between 上下界存在未决值: {target}"
            val = float(actual)
            res = (float(low) <= val <= float(high))
            return ("TRUE", f"{low} <= {val} <= {high}") if res else ("FALSE", f"{val} 不在区间 [{low}, {high}] 内")
        else:
            return "UNKNOWN", f"不支持的操作符: '{op}'"
    except Exception as e:
        return "UNKNOWN", f"指标求值异常 ({field} op={op}): {e}"


def evaluate_entry_rules(
    rules: List[StrategyCondition],
    market_snapshot: Dict[str, Any]
) -> Tuple[EvalStatus, List[Dict[str, Any]]]:
    """
    全部入场条件 AND 组合求值：
    - 任一条件为 FALSE -> 整体为 FALSE
    - 无 FALSE 但任一条件为 UNKNOWN -> 整体为 UNKNOWN (不触发买入)
    - 全部条件为 TRUE -> 整体为 TRUE
    """
    if not rules:
        return "UNKNOWN", [{"error": "无有效入场规则"}]

    details = []
    has_false = False
    has_unknown = False

    for cond in rules:
        status, reason = evaluate_single_condition(cond, market_snapshot)
        details.append({
            "field": cond.field,
            "op": cond.op,
            "target": cond.value,
            "actual": market_snapshot.get(cond.field),
            "status": status,
            "reason": reason
        })
        if status == "FALSE":
            has_false = True
        elif status == "UNKNOWN":
            has_unknown = True

    if has_false:
        return "FALSE", details
    elif has_unknown:
        return "UNKNOWN", details
    else:
        return "TRUE", details


def evaluate_exit_rules(
    exit_rules: ExitRules,
    position: LabPosition,
    current_price: float,
    current_sessions: int = 1
) -> Tuple[bool, Optional[str], float]:
    """
    退出规则求值：
    - 检查刚性止损 (stop_loss_pct)
    - 检查目标止盈 (take_profit_pct)
    - 检查最大持仓时限 (max_holding_sessions)
    - 返回 (should_exit, reason, gain_pct)
    """
    if position.entry_price <= 0:
        return False, None, 0.0

    gain_pct = (current_price - position.entry_price) / position.entry_price * 100.0

    # 1. 刚性止损
    if exit_rules.stop_loss_pct is not None:
        if gain_pct <= exit_rules.stop_loss_pct:
            return True, f"TRIGGER_STOP_LOSS ({gain_pct:.2f}% <= {exit_rules.stop_loss_pct}%)", gain_pct

    # 2. 目标止盈
    if exit_rules.take_profit_pct is not None:
        if gain_pct >= exit_rules.take_profit_pct:
            return True, f"TRIGGER_TAKE_PROFIT ({gain_pct:.2f}% >= {exit_rules.take_profit_pct}%)", gain_pct

    # 3. 持仓天数超时
    if exit_rules.max_holding_sessions is not None:
        if current_sessions >= exit_rules.max_holding_sessions:
            return True, f"TRIGGER_TIMEOUT ({current_sessions} >= {exit_rules.max_holding_sessions} sessions)", gain_pct

    return False, None, gain_pct


# ==========================================
# 3. 策略引擎 (BloggerLabStrategyEngine)
# 负责知识映射草案、草案编辑、不可变策略发布
# ==========================================

class BloggerLabStrategyEngine:
    """策略草案生成、参数确认与发布引擎"""

    def __init__(self, store: BloggerLabStore):
        self.store = store

    def generate_draft_from_knowledge(
        self,
        persona_id: str,
        knowledge_version_id: str
    ) -> StrategyDraft:
        """
        从指定分身的 READY 知识版本生成初始策略草案：
        - 知识版本非 READY 时刚性阻断；
        - 从知识条目中识别具体指标与参数；
        - 原文具备的参数标为 'source'；缺失的标为 'unresolved'（值填 None）；
        - 原文提及但指标目录不支持的条件记录在 unsupported_conditions；
        - 形成初始草案并落库。
        """
        kv = self.store.get_knowledge_version(knowledge_version_id, persona_id)
        if kv is None:
            raise ValueError(f"知识版本 '{knowledge_version_id}' 不存在")
        if kv.status != "READY":
            raise ValueError(f"知识版本状态为 '{kv.status}'，非 READY 状态禁止生成策略草案")

        items = self.store.get_knowledge_items(knowledge_version_id, persona_id)
        if not items:
            raise ValueError(f"知识版本 '{knowledge_version_id}' 中无任何知识项")

        entry_rules: List[StrategyCondition] = []
        exit_rules = ExitRules()
        sizing_rules = SizingRules()
        unresolved_fields: List[str] = []
        unsupported_conditions: List[str] = []
        all_source_refs: List[str] = []

        for it in items:
            all_source_refs.append(it.id)
            stmt = it.statement
            cond_texts = " ".join(it.conditions)
            full_context = f"{stmt} {cond_texts}"

            # --- 入场条件识别 ---
            if it.kind == "ENTRY_RULE":
                # 识别 MA20 相关
                if "20日均线" in full_context or "20MA" in full_context or "生命线" in full_context or "均线" in full_context:
                    # 检查是否包含突破/站上
                    if any(w in full_context for w in ["站上", "突破", "金叉", "上方"]):
                        entry_rules.append(StrategyCondition(
                            field="close_gt_ma20",
                            op="eq",
                            value=True,
                            unit="boolean",
                            basis="completed_daily",
                            source_refs=[it.id],
                            parameter_origin=ParameterOrigin.SOURCE
                        ))
                    else:
                        # 偏离度待确认
                        m = re.search(r'([-+]?\d+(?:\.\d+)?)\s*%', full_context)
                        if m:
                            entry_rules.append(StrategyCondition(
                                field="last_price_to_ma20_pct",
                                op="gte",
                                value=float(m.group(1)),
                                unit="percent",
                                basis="live_price_vs_completed_daily_ma",
                                source_refs=[it.id],
                                parameter_origin=ParameterOrigin.SOURCE
                            ))
                        else:
                            # 缺具体阈值，保留为待确认
                            unresolved_fields.append("entry.last_price_to_ma20_pct")
                            entry_rules.append(StrategyCondition(
                                field="last_price_to_ma20_pct",
                                op="gte",
                                value=None,
                                unit="percent",
                                basis="live_price_vs_completed_daily_ma",
                                source_refs=[it.id],
                                parameter_origin=ParameterOrigin.UNRESOLVED,
                                suggestion_note="博主提及均线偏离但未给出具体百分比，建议设置为 0.0%"
                            ))

                # 识别 5日/10日均线金叉
                if ("5日均线" in full_context and "10日均线" in full_context) or "5MA" in full_context:
                    if "金叉" in full_context or "多头" in full_context or "上方" in full_context:
                        entry_rules.append(StrategyCondition(
                            field="ma5_gt_ma10",
                            op="eq",
                            value=True,
                            unit="boolean",
                            basis="completed_daily",
                            source_refs=[it.id],
                            parameter_origin=ParameterOrigin.SOURCE
                        ))

                # 识别成交量倍数
                if any(w in full_context for w in ["成交量", "均量", "放量", "缩量", "量比", "倍"]):
                    m_vol = re.search(r'(\d+(?:\.\d+)?)\s*倍', full_context)
                    if m_vol:
                        vol_mult = float(m_vol.group(1))
                        # 取倍数前后局部上下文分析比较方向
                        start_idx = max(0, m_vol.start() - 20)
                        end_idx = min(len(full_context), m_vol.end() + 20)
                        local_ctx = full_context[start_idx:end_idx]

                        # 缩量/低于/小于/不超过方向词
                        is_less = any(w in local_ctx for w in ["低于", "小于", "少于", "不足", "不超过", "不高于", "缩量", "萎缩", "以内"])
                        # 放量/高于/大于/不低于方向词
                        is_greater = any(w in local_ctx for w in ["高于", "大于", "超过", "超", "放大", "放量", "不低于", "不小于", "以上"])

                        if is_less and not is_greater:
                            op = "lte" if any(w in local_ctx for w in ["不超过", "不高于", "以内"]) else "lt"
                            entry_rules.append(StrategyCondition(
                                field="volume_ratio_5d",
                                op=op,
                                value=vol_mult,
                                unit="ratio",
                                basis="completed_daily",
                                source_refs=[it.id],
                                parameter_origin=ParameterOrigin.SOURCE
                            ))
                        elif is_greater and not is_less:
                            op = "gte" if any(w in local_ctx for w in ["达到", "放量", "放大", "不低于", "以上"]) else "gt"
                            entry_rules.append(StrategyCondition(
                                field="volume_ratio_5d",
                                op=op,
                                value=vol_mult,
                                unit="ratio",
                                basis="completed_daily",
                                source_refs=[it.id],
                                parameter_origin=ParameterOrigin.SOURCE
                            ))
                        else:
                            # 方向冲突或方向不明确（例如“成交量为0.8倍左右”），保留为未决待确认
                            unresolved_fields.append("entry.volume_ratio_5d")
                            entry_rules.append(StrategyCondition(
                                field="volume_ratio_5d",
                                op="gte",
                                value=None,
                                unit="ratio",
                                basis="completed_daily",
                                source_refs=[it.id],
                                parameter_origin=ParameterOrigin.UNRESOLVED,
                                suggestion_note=f"检测到成交量倍数 {vol_mult}，但方向无法明确（缩量或放量），请手动确认操作符与阈值"
                            ))
                    else:
                        unresolved_fields.append("entry.volume_ratio_5d")
                        entry_rules.append(StrategyCondition(
                            field="volume_ratio_5d",
                            op="gte",
                            value=None,
                            unit="ratio",
                            basis="completed_daily",
                            source_refs=[it.id],
                            parameter_origin=ParameterOrigin.UNRESOLVED,
                            suggestion_note="原文仅提及成交量但未提供具体倍数，建议设定为 1.2~1.5 倍（放量）或 0.8 倍（缩量）"
                        ))

                # 识别其他超出白名单的未支持条件
                if any(kw in full_context for kw in ["筹码峰", "北向资金", "机构龙虎榜", "涨停封板率", "分时量比"]):
                    unsupported_conditions.append(f"[{it.id}] {stmt[:40]}...")

            # --- 退出条件识别 ---
            elif it.kind == "EXIT_RULE":
                # 止损识别
                if "止损" in full_context or "亏损" in full_context or "跌破" in full_context:
                    # 优先找关键字紧邻的百分比，避免截取到同一条文稿中的止盈百分比
                    m_loss = re.search(r'(?:止损|亏损|跌破|回撤)[^\d%]{0,10}(\d+(?:\.\d+)?)\s*%', full_context)
                    if not m_loss:
                        m_loss = re.search(r'(\d+(?:\.\d+)?)\s*%\s*[^\d%]{0,10}(?:止损|亏损|跌破|回撤)', full_context)
                    if not m_loss and not ("止盈" in full_context or "盈利" in full_context):
                        m_loss = re.search(r'(\d+(?:\.\d+)?)\s*%', full_context)

                    if m_loss:
                        val = float(m_loss.group(1))
                        # 止损统一记录为负数
                        exit_rules.stop_loss_pct = -abs(val)
                        exit_rules.stop_loss_source_refs.append(it.id)
                        exit_rules.stop_loss_origin = ParameterOrigin.SOURCE
                    elif exit_rules.stop_loss_pct is None:
                        unresolved_fields.append("exit.stop_loss_pct")
                        exit_rules.stop_loss_origin = ParameterOrigin.UNRESOLVED

                # 止盈识别
                if "止盈" in full_context or "上涨" in full_context or "锁定利润" in full_context or "盈利" in full_context:
                    # 优先找关键字紧邻的百分比，避免截取到同一条文稿中的止损百分比
                    m_prof = re.search(r'(?:止盈|盈利|上涨|锁定利润|获利)[^\d%]{0,10}(\d+(?:\.\d+)?)\s*%', full_context)
                    if not m_prof:
                        m_prof = re.search(r'(\d+(?:\.\d+)?)\s*%\s*[^\d%]{0,10}(?:止盈|盈利|上涨|锁定利润|获利)', full_context)
                    if not m_prof and not ("止损" in full_context or "亏损" in full_context):
                        m_prof = re.search(r'(\d+(?:\.\d+)?)\s*%', full_context)

                    if m_prof:
                        val = float(m_prof.group(1))
                        exit_rules.take_profit_pct = abs(val)
                        exit_rules.take_profit_source_refs.append(it.id)
                        exit_rules.take_profit_origin = ParameterOrigin.SOURCE
                    elif exit_rules.take_profit_pct is None:
                        unresolved_fields.append("exit.take_profit_pct")
                        exit_rules.take_profit_origin = ParameterOrigin.UNRESOLVED

                # 持仓时限识别
                m_days = re.search(r'(\d+)\s*(?:天|日|交易日)', full_context)
                if m_days and "持仓" in full_context:
                    exit_rules.max_holding_sessions = int(m_days.group(1))
                    exit_rules.max_holding_source_refs.append(it.id)
                    exit_rules.max_holding_origin = ParameterOrigin.SOURCE

            # --- 仓位风控识别 ---
            elif it.kind == "POSITION_RISK" or it.kind == "PRINCIPLE":
                # 区分【单只/单笔个股仓位】与【总仓位/组合仓位】
                # schema 中的 position_fraction 严格表示单笔/单只个股最大开仓比例
                def _parse_fraction(text: str) -> Optional[float]:
                    cn_map = {
                        "一": 0.1, "二": 0.2, "两": 0.2, "三": 0.3, "四": 0.4,
                        "五": 0.5, "六": 0.6, "七": 0.7, "八": 0.8, "九": 0.9,
                        "半": 0.5, "十": 1.0, "满": 1.0
                    }
                    m_cn = re.search(r'([一二两三四五六七八九半满])\s*成', text)
                    if m_cn:
                        return cn_map.get(m_cn.group(1))
                    m_digit = re.search(r'(\d+)\s*成', text)
                    if m_digit:
                        return round(float(m_digit.group(1)) / 10.0, 2)
                    if "半仓" in text:
                        return 0.5
                    if "满仓" in text:
                        return 1.0
                    m_p = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
                    if m_p:
                        return round(float(m_p.group(1)) / 100.0, 2)
                    return None

                # 切分子句（按逗号、分号、句号、换行）
                clauses = re.split(r'[,，;；.。\n\r]', full_context)
                clauses = [c.strip() for c in clauses if c.strip()]

                single_keywords = ["单只", "单笔", "每只", "个股", "单仓", "单票", "每笔"]
                total_keywords = ["总仓", "总体仓位", "总仓位", "整体仓位", "组合仓位", "大盘仓位"]

                # 1. 优先在明确包含“单只/单笔/个股”且不含“总仓”的子句中寻找单笔仓位
                single_frac = None
                for c in clauses:
                    if any(k in c for k in single_keywords) and not any(k in c for k in total_keywords):
                        f = _parse_fraction(c)
                        if f is not None:
                            single_frac = f
                            break

                # 2. 如果子句混在一起，如“总仓位不超过六成，单只个股最大仓位不超过两成”
                # 在包含单只关键词的片段中精细匹配：
                if single_frac is None:
                    m_single = re.search(r'(?:单只|单笔|每只|个股|单仓|每笔)[^，,；;。]{0,15}?([一二两三四五六七八九半满\d]+)\s*(?:成|%)', full_context)
                    if m_single:
                        raw_val = m_single.group(0)
                        single_frac = _parse_fraction(raw_val)

                if single_frac is not None:
                    sizing_rules.position_fraction = single_frac
                    sizing_rules.fraction_source_refs.append(it.id)
                    sizing_rules.fraction_origin = ParameterOrigin.SOURCE
                else:
                    # 如果只有“总仓”约束而无单只约束，或者虽然提了仓位但无法可靠区分为单只：
                    has_total = any(k in full_context for k in total_keywords)
                    if not has_total and any(w in full_context for w in ["仓位", "建仓", "开仓", "底仓"]):
                        # 仅在无“总仓”歧义且为明确开仓/建仓时，尝试通用解析
                        f = _parse_fraction(full_context)
                        if f is not None:
                            sizing_rules.position_fraction = f
                            sizing_rules.fraction_source_refs.append(it.id)
                            sizing_rules.fraction_origin = ParameterOrigin.SOURCE
                    # 否则（有总仓限制但无单只限制，或无法确定），严格保留为 None (UNRESOLVED) 让用户填写

        # 检查仓位是否缺失
        if sizing_rules.position_fraction is None:
            unresolved_fields.append("sizing.position_fraction")
            sizing_rules.fraction_origin = ParameterOrigin.UNRESOLVED

        # 检查退出规则是否完全为空
        if exit_rules.stop_loss_pct is None and exit_rules.take_profit_pct is None and exit_rules.max_holding_sessions is None:
            unresolved_fields.append("exit.at_least_one_exit_rule")

        # 状态判定
        if unsupported_conditions:
            status = "BLOCKED"
        elif unresolved_fields:
            status = "NEEDS_CONFIRMATION"
        else:
            status = "NEEDS_CONFIRMATION"  # 即使全齐，首发也需用户人工确认

        now_iso = current_time_iso()
        draft_id = f"draft_{persona_id}_{uuid.uuid4().hex[:8]}"
        draft_hash = compute_canonical_draft_hash(entry_rules, exit_rules, sizing_rules)

        draft = StrategyDraft(
            schema_version=SCHEMA_VERSION,
            id=draft_id,
            persona_id=persona_id,
            knowledge_version=knowledge_version_id,
            revision=1,
            status=status,
            created_at=now_iso,
            updated_at=now_iso,
            entry_rules=entry_rules,
            exit_rules=exit_rules,
            sizing_rules=sizing_rules,
            unresolved_fields=list(set(unresolved_fields)),
            unsupported_conditions=unsupported_conditions,
            source_refs=all_source_refs,
            draft_hash=draft_hash
        )

        self.store.save_strategy_draft(draft)
        return draft

    def update_strategy_draft(
        self,
        draft_id: str,
        persona_id: str,
        expected_revision: int,
        entry_rules: Optional[List[StrategyCondition]] = None,
        exit_rules: Optional[ExitRules] = None,
        sizing_rules: Optional[SizingRules] = None
    ) -> StrategyDraft:
        """
        编辑修改策略草案（支持用户补齐未决参数、微调阈值）：
        - 乐观并发控制：必须匹配 expected_revision；
        - 更新后递增 revision，重算 unresolved_fields 与 draft_hash。
        """
        draft = self.store.get_strategy_draft(draft_id, persona_id)
        if draft is None:
            raise ValueError(f"草案 '{draft_id}' 不存在")

        if draft.status == "CONFIRMED":
            raise ValueError("已确认的草案不可修改，如需调整请基于最新状态生成新修订或新草案")

        if draft.revision != expected_revision:
            raise ValueError(
                f"草案修订版本冲突 (Concurrency Conflict): 期望 revision={expected_revision}, "
                f"实际数据库最新为 revision={draft.revision}"
            )

        new_entry = entry_rules if entry_rules is not None else draft.entry_rules
        new_exit = exit_rules if exit_rules is not None else draft.exit_rules
        new_sizing = sizing_rules if sizing_rules is not None else draft.sizing_rules

        # 重新扫描未决字段
        unresolved: List[str] = []
        for r in new_entry:
            if r.value is None:
                unresolved.append(f"entry.{r.field}")
            elif isinstance(r.value, (list, tuple)) and any(v is None for v in r.value):
                unresolved.append(f"entry.{r.field}")

        if new_exit.stop_loss_pct is None and new_exit.take_profit_pct is None and new_exit.max_holding_sessions is None:
            unresolved.append("exit.at_least_one_exit_rule")

        if new_sizing.position_fraction is None:
            unresolved.append("sizing.position_fraction")

        now_iso = current_time_iso()
        new_hash = compute_canonical_draft_hash(new_entry, new_exit, new_sizing)

        updated_draft = StrategyDraft(
            schema_version=SCHEMA_VERSION,
            id=draft.id,
            persona_id=draft.persona_id,
            knowledge_version=draft.knowledge_version,
            revision=draft.revision + 1,
            status="BLOCKED" if draft.unsupported_conditions else "NEEDS_CONFIRMATION",
            created_at=draft.created_at,
            updated_at=now_iso,
            entry_rules=new_entry,
            exit_rules=new_exit,
            sizing_rules=new_sizing,
            unresolved_fields=list(set(unresolved)),
            unsupported_conditions=draft.unsupported_conditions,
            source_refs=draft.source_refs,
            draft_hash=new_hash
        )

        self.store.save_strategy_draft(updated_draft)
        return updated_draft

    def confirm_strategy_draft(
        self,
        draft_id: str,
        persona_id: str,
        expected_revision: int,
        expected_hash: str
    ) -> StrategyVersion:
        """
        确认发布策略草案为不可变 StrategyVersion：
        - 检查 draft_revision 与 draft_hash 一致性；
        - 严格门禁：
          1. 绝无未决字段 (unresolved_fields 为空)；
          2. 绝无阻断的未支持条件 (unsupported_conditions 为空)；
          3. 所有入场指标字段均在 FIELD_CATALOG 中；
          4. 至少存在一条有效退出规则 (止损或止盈)；
          5. 仓位比例在 (0.0, 1.0] 内；
        - 通过后固化不可变 StrategyVersion 并设置为分身 active_strategy_id。
        """
        draft = self.store.get_strategy_draft(draft_id, persona_id)
        if draft is None:
            raise ValueError(f"草案 '{draft_id}' 不存在")

        if draft.revision != expected_revision:
            raise ValueError(
                f"确认失败：草案修订版本不一致 (期望 {expected_revision}, 实际 {draft.revision})"
            )

        if draft.draft_hash != expected_hash:
            raise ValueError(
                f"确认失败：草案内容哈希不一致 (期望 {expected_hash}, 实际 {draft.draft_hash})"
            )

        # 门禁 1: 检查未支持条件
        if draft.unsupported_conditions:
            raise ValueError(
                f"确认受阻 (BLOCKED): 存在系统指标库不支持的必要条件: {draft.unsupported_conditions}，"
                "严禁私自删减必要条件以凑合执行"
            )

        # 门禁 2: 检查未决参数
        if draft.unresolved_fields:
            raise ValueError(
                f"确认受阻 (UNRESOLVED): 仍存在未确认或缺失的参数: {draft.unresolved_fields}，"
                "请先编辑草案补齐参数后再确认"
            )

        # 门禁 3: 检查入场规则
        if not draft.entry_rules:
            raise ValueError("确认受阻: 入场规则列表为空")
        for r in draft.entry_rules:
            if r.field not in FIELD_CATALOG:
                raise ValueError(f"确认受阻: 入场指标 '{r.field}' 不在可执行白名单目录中")
            if r.value is None:
                raise ValueError(f"确认受阻: 入场指标 '{r.field}' 参数值为空")

        # 门禁 4: 检查退出规则
        if draft.exit_rules.stop_loss_pct is None and draft.exit_rules.take_profit_pct is None:
            raise ValueError("确认受阻: 至少需要配置一项有效的止损或止盈退出规则")

        # 门禁 5: 检查仓位规则
        if draft.sizing_rules.position_fraction is None or not (0.0 < draft.sizing_rules.position_fraction <= 1.0):
            raise ValueError("确认受阻: 仓位比例必须在 (0.0, 1.0] 区间内")

        # 门禁通过，生成不可变 StrategyVersion
        existing_versions = self.store.list_strategy_versions(persona_id)
        next_ver_num = len(existing_versions) + 1
        version_id = f"strat_{persona_id}_{str(next_ver_num).zfill(3)}"
        now_iso = current_time_iso()

        strat_version = StrategyVersion(
            schema_version=SCHEMA_VERSION,
            id=version_id,
            persona_id=persona_id,
            draft_id=draft.id,
            draft_revision=draft.revision,
            draft_hash=draft.draft_hash,
            knowledge_version=draft.knowledge_version,
            status="CONFIRMED",
            confirmed_at=now_iso,
            entry_rules=draft.entry_rules,
            exit_rules=draft.exit_rules,
            sizing_rules=draft.sizing_rules,
            source_refs=draft.source_refs
        )

        # 1. 保存不可变策略版本
        self.store.save_strategy_version(strat_version)

        # 2. 更新草案状态为 CONFIRMED
        draft.status = "CONFIRMED"
        draft.updated_at = now_iso
        self.store.save_strategy_draft(draft)

        # 3. 设置为分身激活策略
        self.store.set_active_strategy(persona_id, strat_version.id)

        return strat_version
