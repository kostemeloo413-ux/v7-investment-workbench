# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/schemas.py
V7 博主实验室跨模块数据契约定义 (阶段 1 扩充版)
严格符合 V7_BLOGGER_LAB_DEVELOPMENT_GUIDE_V1.md 第 3 节规范
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional, Literal
from enum import Enum
from pydantic import BaseModel, Field


SCHEMA_VERSION = "V7_BLOGGER_LAB_V1.0"

# 知识项七大分类定义
KnowledgeKind = Literal[
    "PRINCIPLE",            # 投资原则
    "SECTOR_PREFERENCE",     # 选股偏好 / 题材偏好
    "ENTRY_RULE",           # 入场条件
    "EXIT_RULE",            # 退出条件
    "POSITION_RISK",        # 仓位 / 风险管理
    "HISTORICAL_CASE",      # 历史案例（不可直接作为今日候选）
    "UNRESOLVED"            # 待澄清 / 冲突 / 缺参数项
]


# ==========================================
# 阶段 1: 分身、资料批次、来源文件与分块模型
# ==========================================

class Persona(BaseModel):
    """博主策略数字分身"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="全局唯一分身ID，例如 persona_hanshu")
    name: str = Field(..., min_length=1, description="博主分身显示名称")
    description: str = Field(default="", description="分身投资风格简介")
    status: Literal["ACTIVE", "ARCHIVED"] = Field(default="ACTIVE")
    active_strategy_id: Optional[str] = Field(None, description="当前启用的策略版本ID")
    created_at: str = Field(..., description="创建时点 ISO 8601")
    updated_at: str = Field(..., description="更新时点 ISO 8601")


class Source(BaseModel):
    """单份导入资料文件元数据"""
    id: str = Field(..., description="全局唯一来源文件ID，例如 src_xxx")
    batch_id: str = Field(..., description="所属资料导入批次ID")
    persona_id: str = Field(..., description="所属分身ID")
    filename: str = Field(..., description="原始文件名")
    file_type: Literal["TXT", "MD", "DOCX", "UNSUPPORTED"] = Field(...)
    sha256: str = Field(..., description="原始文件字节 SHA256 哈希")
    file_size_bytes: int = Field(..., ge=0)
    status: Literal["READY", "NEEDS_CONVERSION", "FAILED"] = Field(...)
    raw_file_path: str = Field(..., description="保存的磁盘原文件绝对路径")
    normalized_text_path: Optional[str] = Field(None, description="提取出的规范文本路径")
    extracted_chars: int = Field(default=0, ge=0, description="提取的有效字符总数")
    unsupported_parts: List[str] = Field(default_factory=list, description="未支持或跳过的内容部件说明")
    error_message: Optional[str] = Field(None, description="解析失败错误信息")


class Chunk(BaseModel):
    """全区间覆盖规范分块"""
    id: str = Field(..., description="全局唯一分块ID，例如 chunk_srcxxx_001")
    source_id: str = Field(..., description="所属来源文件ID")
    persona_id: str = Field(..., description="所属分身ID")
    batch_id: str = Field(..., description="所属批次ID")
    chunk_index: int = Field(..., ge=0, description="分块序号，从 0 开始")
    start_char: int = Field(..., ge=0, description="规范文本中起始绝对字符偏移")
    end_char: int = Field(..., ge=0, description="规范文本中结束绝对字符偏移")
    char_length: int = Field(..., ge=0, description="当前块字符长度")
    text_sha256: str = Field(..., description="分块文本 SHA256")
    text: str = Field(default="", description="分块文本实际内容")
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "INTERRUPTED"] = Field(default="PENDING")
    extraction_key: Optional[str] = Field(None, description="提炼任务去重幂等键")


class CorpusBatch(BaseModel):
    """一次资料导入与分块任务批次"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="全局唯一批次ID，例如 batch_xxx")
    persona_id: str = Field(..., description="所属分身ID")
    created_at: str = Field(..., description="创建时点 ISO 8601")
    status: Literal["UPLOADED", "PARSING", "READY", "PARTIAL", "FAILED"] = Field(default="UPLOADED")
    parser_version: str = Field(default="PARSER_V1.0")
    total_files: int = Field(default=0, ge=0)
    ready_files: int = Field(default=0, ge=0)
    failed_files: int = Field(default=0, ge=0)
    total_chars: int = Field(default=0, ge=0)
    total_chunks: int = Field(default=0, ge=0)
    source_ids: List[str] = Field(default_factory=list)
    summary_report: Dict[str, Any] = Field(default_factory=dict)


# ==========================================
# 知识抽取、引用与市场/交易模型
# ==========================================

class Citation(BaseModel):
    """
    原文精准引用定位。
    start 和 end 针对当前分块局部文本（chunk_text）。
    chunk_offset_in_source 记录当前分块在源文本全局规范文本中的起始绝对偏移。
    全局绝对偏移 = chunk_offset_in_source + start 到 chunk_offset_in_source + end。
    """
    source_id: str = Field(..., description="来源文件标识")
    chunk_id: str = Field(..., description="所属分块标识")
    start: int = Field(..., ge=0, description="分块文本(chunk_text)内的起始字符局部偏移")
    end: int = Field(..., ge=0, description="分块文本(chunk_text)内的结束字符局部偏移")
    chunk_offset_in_source: int = Field(default=0, ge=0, description="分块在源文本全局中的起始绝对偏移")
    quote: str = Field(..., min_length=1, description="与分块文本完全一致的连续原文片段")


class KnowledgeItem(BaseModel):
    """从资料中抽取的单条结构化知识"""
    id: str = Field(..., description="全局唯一知识条目ID")
    persona_id: str = Field(..., description="所属分身ID")
    knowledge_version: str = Field(..., description="知识批次版本号")
    kind: KnowledgeKind = Field(..., description="七大分类之一")
    statement: str = Field(..., min_length=2, description="核心表述 / 规则陈述")
    conditions: List[str] = Field(default_factory=list, description="适用条件列表")
    exceptions: List[str] = Field(default_factory=list, description="例外或反例说明")
    citations: List[Citation] = Field(default_factory=list, description="原文引用依据，至少一条")
    source_date: Optional[str] = Field(None, description="来源日期(YYYY-MM-DD)，未知填None")


class KnowledgeExtractionResult(BaseModel):
    """LLM 单块抽取返回的顶层结构与验证状态契约"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    status: Literal["SUCCESS", "PARTIAL", "FAILED"] = Field(default="SUCCESS", description="抽取与校验综合状态")
    raw_items_count: int = Field(default=0, description="模型原始生成的条目数")
    valid_items_count: int = Field(default=0, description="通过严格校验的有效条目数")
    items: List[KnowledgeItem] = Field(default_factory=list, description="通过校验的结构化知识项列表")
    unresolved_notes: List[str] = Field(default_factory=list, description="无法解析或存在冲突的备忘")
    validation_errors: List[str] = Field(default_factory=list, description="校验错误或剔除明细")


class KnowledgeVersion(BaseModel):
    """博主分身知识版本聚合实体"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="知识版本唯一ID，例如 kv_persona1_001")
    persona_id: str = Field(..., description="所属分身ID")
    batch_id: str = Field(..., description="关联来源资料批次ID")
    version_number: int = Field(default=1, ge=1, description="版本序列号")
    status: Literal["BUILDING", "READY", "PARTIAL", "FAILED"] = Field(default="BUILDING")
    created_at: str = Field(..., description="创建时点 ISO 8601")
    updated_at: str = Field(..., description="更新时点 ISO 8601")
    total_chunks: int = Field(default=0, ge=0)
    succeeded_chunks: int = Field(default=0, ge=0)
    partial_chunks: int = Field(default=0, ge=0)
    failed_chunks: int = Field(default=0, ge=0)
    total_items: int = Field(default=0, ge=0)
    items_by_kind: Dict[str, int] = Field(default_factory=dict)
    unresolved_count: int = Field(default=0, ge=0)
    summary: str = Field(default="", description="版本提炼概括")


class ExtractionJob(BaseModel):
    """知识提炼后台任务调度与进度记录"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="提炼任务ID，例如 job_ext_xxx")
    persona_id: str = Field(..., description="所属分身ID")
    batch_id: str = Field(..., description="批次ID")
    knowledge_version_id: str = Field(..., description="关联知识版本ID")
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "INTERRUPTED"] = Field(default="PENDING")
    created_at: str = Field(..., description="创建时点")
    updated_at: str = Field(..., description="更新时点")
    total_chunks: int = Field(default=0, ge=0)
    processed_chunks: int = Field(default=0, ge=0)
    succeeded_chunks: int = Field(default=0, ge=0)
    partial_chunks: int = Field(default=0, ge=0)
    failed_chunks: int = Field(default=0, ge=0)
    error_message: Optional[str] = Field(None)
    chunk_errors: Dict[str, str] = Field(default_factory=dict, description="失败分块错误明细")


class MarketEvidence(BaseModel):
    """市场证据快照契约"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="证据唯一ID")
    symbol: str = Field(..., description="6位数字证券代码")
    observed_at: str = Field(..., description="观测/拉取时点 ISO 8601 (必须带时区)")
    data_cutoff: str = Field(..., description="数据截断交易日 (YYYY-MM-DD 或 YYYYMMDD)")
    source: str = Field(..., description="数据源标识，如 market_db_sqlite / baostock_qfq_cache / tencent_live_quote")
    units: Dict[str, str] = Field(default_factory=dict, description="各字段单位")
    adjustment: str = Field(..., description="复权口径：QFQ / RAW / NONE")
    status: Literal[
        "VALID",
        "STALE",
        "MISSING",
        "INVALID_FUTURE_DATA",
        "INVALID_TIMESTAMP",
        "INVALID_PRICE",
        "SUSPENDED",
        "REJECTED",
        "UNKNOWN"
    ] = Field(..., description="有效性状态")
    values: Dict[str, Any] = Field(default_factory=dict, description="实际指标或量价数值")


class LabPosition(BaseModel):
    """实验持仓快照"""
    position_id: str
    symbol: str
    name: str
    shares: int = Field(..., ge=0)
    available_shares: int = Field(..., ge=0, description="T+1 可卖股数")
    avg_cost: float = Field(..., ge=0.0)
    entry_date: str = Field(..., description="开仓交易日 YYYYMMDD")
    entry_price: float = Field(..., ge=0.0)
    entry_strategy_version: str = Field(..., description="开仓时锁定的策略版本")
    exit_snapshot: Dict[str, Any] = Field(default_factory=dict, description="开仓时固化的退出规则快照")


class LabFill(BaseModel):
    """实验成交凭据"""
    fill_id: str
    position_id: Optional[str] = None
    input_hash: Optional[str] = None
    direction: Literal["BUY", "SELL"]
    symbol: str
    name: str
    trade_time: str
    trade_date: str
    price: float
    shares: int
    gross_amount: float
    commission: float
    stamp_tax: float
    slippage_cost: float = 0.0
    cash_effect: float = Field(..., description="对现金的影响额：买入为负，卖出为正")
    quote_ref_id: Optional[str] = None
    reason: str = ""


class LabAccount(BaseModel):
    """实验研究账户"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    account_id: str
    persona_id: str
    mode: Literal["LIVE", "REPLAY"] = "REPLAY"
    initial_capital: float = 100000.0
    cash: float
    total_asset: float
    revision: int = 0
    positions: Dict[str, LabPosition] = Field(default_factory=dict)


class PaperCycle(BaseModel):
    """独立研究模拟周期执行结果"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    cycle_id: str
    persona_id: str
    mode: Literal["LIVE", "REPLAY"] = "REPLAY"
    request_key: str
    input_hash: str
    scan_run_id: Optional[str] = None
    candidate_id: Optional[str] = None
    status: Literal["COMPLETED", "NO_ACTION", "BLOCKED", "FAILED"]
    action: Literal["BUY", "SELL", "HOLD", "NONE"]
    fills_count: int = 0
    fills: List[LabFill] = Field(default_factory=list)
    rejection_reason: Optional[str] = None
    summary: str
    account_snapshot: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


# ==========================================
# 阶段 3: 策略草案、DSL规则与不可变策略版本
# ==========================================

class ParameterOrigin(str, Enum):
    SOURCE = "source"                        # 原文明确包含具体参数
    USER = "user"                            # 用户手工指定/覆盖
    SYSTEM_SUGGESTION = "system_suggestion"  # 系统默认建议值（需人工确认）
    UNRESOLVED = "unresolved"                # 未决参数（值为 None，不可执行）


class StrategyCondition(BaseModel):
    """策略入场单条规则契约（有限 JSON DSL）"""
    field: str = Field(..., description="指标字段名，必须在 FIELD_CATALOG 白名单中")
    op: Literal["gt", "gte", "lt", "lte", "between", "eq"] = Field(..., description="限定操作符")
    value: Any = Field(default=None, description="比较目标值，未决为 None")
    unit: str = Field(default="percent", description="指标单位")
    basis: str = Field(default="completed_daily", description="计算基准")
    source_refs: List[str] = Field(default_factory=list, description="关联的知识项 ID 列表")
    parameter_origin: ParameterOrigin = Field(default=ParameterOrigin.UNRESOLVED, description="参数来源")
    suggestion_note: Optional[str] = Field(default=None, description="系统建议说明")


class ExitRules(BaseModel):
    """策略退出规则契约"""
    stop_loss_pct: Optional[float] = Field(default=None, description="刚性止损百分比（如 -3.5 代表 -3.5%）")
    take_profit_pct: Optional[float] = Field(default=None, description="止盈百分比（如 8.5 代表 8.5%）")
    max_holding_sessions: Optional[int] = Field(default=None, ge=1, description="最大持仓交易日天数")
    stop_loss_source_refs: List[str] = Field(default_factory=list)
    take_profit_source_refs: List[str] = Field(default_factory=list)
    max_holding_source_refs: List[str] = Field(default_factory=list)
    stop_loss_origin: ParameterOrigin = Field(default=ParameterOrigin.UNRESOLVED)
    take_profit_origin: ParameterOrigin = Field(default=ParameterOrigin.UNRESOLVED)
    max_holding_origin: ParameterOrigin = Field(default=ParameterOrigin.UNRESOLVED)


class SizingRules(BaseModel):
    """仓位管理规则契约"""
    position_fraction: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="单笔开仓比例 (0~1.0)")
    max_positions: int = Field(default=1, ge=1, le=10, description="最大同时持仓数 (首版为1)")
    fraction_source_refs: List[str] = Field(default_factory=list)
    fraction_origin: ParameterOrigin = Field(default=ParameterOrigin.UNRESOLVED)


class StrategyDraft(BaseModel):
    """策略草案契约"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="草案唯一标识，如 draft_persona1_xxx")
    persona_id: str = Field(..., description="所属分身ID")
    knowledge_version: str = Field(..., description="关联的知识版本ID（必须为 READY）")
    revision: int = Field(default=1, ge=1, description="草案修订号")
    status: Literal["DRAFT", "NEEDS_CONFIRMATION", "CONFIRMED", "BLOCKED"] = Field(default="NEEDS_CONFIRMATION")
    created_at: str = Field(..., description="创建时间 ISO 8601")
    updated_at: str = Field(..., description="更新时间 ISO 8601")
    entry_rules: List[StrategyCondition] = Field(default_factory=list, description="入场条件列表（默认全部 AND 组合）")
    exit_rules: ExitRules = Field(default_factory=ExitRules)
    sizing_rules: SizingRules = Field(default_factory=SizingRules)
    unresolved_fields: List[str] = Field(default_factory=list, description="未决或缺失数值的字段路径")
    unsupported_conditions: List[str] = Field(default_factory=list, description="超出白名单目录但原文提及的必要条件")
    source_refs: List[str] = Field(default_factory=list, description="引用的知识项ID全集")
    draft_hash: str = Field(..., description="草案内容确定性 SHA256")


class StrategyVersion(BaseModel):
    """不可变确认策略版本契约"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="策略版本唯一标识，如 strat_persona1_001")
    persona_id: str = Field(..., description="所属分身ID")
    draft_id: str = Field(..., description="来源草案ID")
    draft_revision: int = Field(..., description="确认时的草案修订号")
    draft_hash: str = Field(..., description="确认时的草案内容哈希")
    knowledge_version: str = Field(..., description="锁定的知识版本ID")
    status: Literal["CONFIRMED"] = Field(default="CONFIRMED")
    confirmed_at: str = Field(..., description="确认发布时间 ISO 8601")
    entry_rules: List[StrategyCondition] = Field(..., description="确认生效的入场规则")
    exit_rules: ExitRules = Field(..., description="确认生效的退出规则")
    sizing_rules: SizingRules = Field(..., description="确认生效的仓位规则")
    source_refs: List[str] = Field(default_factory=list)


# ==========================================
# 阶段 4: 机会发现与证据解释契约
# ==========================================

class StockEvaluation(BaseModel):
    """单只股票规则求值结果"""
    symbol: str = Field(..., description="股票代码，如 000001")
    name: str = Field(default="", description="股票简称")
    status: Literal["TRUE", "FALSE", "UNKNOWN"] = Field(..., description="规则评估状态")
    checks: List[Dict[str, Any]] = Field(default_factory=list, description="逐条件判定证据明细")
    market_snapshot: Dict[str, Any] = Field(default_factory=dict, description="指标计算行情证据快照")
    rejection_reason: Optional[str] = Field(default=None, description="未入选或阻断原因")


class ScanCandidate(BaseModel):
    """全部入场条件为 TRUE 的合格候选股票"""
    symbol: str = Field(..., description="股票代码")
    name: str = Field(default="", description="股票简称")
    strategy_version_id: str = Field(..., description="应用的确认策略版本ID")
    checks: List[Dict[str, Any]] = Field(default_factory=list, description="逐条件判定证据")
    market_snapshot: Dict[str, Any] = Field(default_factory=dict, description="行情指标快照")
    quote_time: str = Field(..., description="行情报价时点")
    data_cutoff: str = Field(..., description="历史日线截止点")
    rank: int = Field(default=1, ge=1, description="候选稳定排序名次 (按代码稳定升序)")
    decision: Literal["QUALIFIED"] = Field(default="QUALIFIED")


class ScanRun(BaseModel):
    """一次完整的今日机会发现扫描执行记录"""
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(..., description="扫描任务唯一标识，如 scan_persona1_xxx")
    persona_id: str = Field(..., description="所属分身ID")
    strategy_version_id: str = Field(..., description="执行扫描的不可变策略版本ID")
    mode: Literal["LIVE", "REPLAY"] = Field(default="REPLAY", description="扫描模式: LIVE(当下研究) 或 REPLAY(固定回放)")
    status: Literal["COMPLETED", "FAILED", "NO_CANDIDATES"] = Field(..., description="扫描状态")
    as_of_date: str = Field(..., description="行情/日线截止交易日 (YYYYMMDD 或 YYYY-MM-DD)")
    universe_target_count: int = Field(default=0, ge=0, description="目标扫描标的数")
    universe_evaluated_count: int = Field(default=0, ge=0, description="实际完成评估标的数")
    universe_excluded_count: int = Field(default=0, ge=0, description="受限/缺数据排除标的数")
    candidates_count: int = Field(default=0, ge=0, description="命中候选总数")
    candidates: List[ScanCandidate] = Field(default_factory=list, description="命中候选股票清单")
    evaluations: List[StockEvaluation] = Field(default_factory=list, description="全部标的评估明细")
    exclusion_reasons: Dict[str, int] = Field(default_factory=dict, description="排除原因分类统计")
    summary: str = Field(default="", description="执行摘要")
    execution_ms: float = Field(default=0.0, ge=0.0, description="执行耗时毫秒数")
    created_at: str = Field(..., description="扫描启动时点 ISO 8601")


