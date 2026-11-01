# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/llm.py
V7 博主实验室 LLM 知识抽取与结构校验适配器 (R2 修订版)
负责将博主长文分块提取为带精确引用的结构化知识项，落实明确的失败返回契约。
"""

from __future__ import annotations
import json
import re
import os
import sys
from typing import Optional, Dict, Any, Tuple, List

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

import runtime_config as rc
from blogger_lab.schemas import (
    KnowledgeExtractionResult,
    KnowledgeItem,
    Citation,
    KnowledgeKind,
    SCHEMA_VERSION
)

PROMPT_VERSION = "BLOGGER_LAB_EXTRACT_V1.1"

_LAB_EXTRACTION_SYSTEM_PROMPT = """你是一个量化投资与知识工程专家。你的任务是从财经博主/交易专家的文稿片段中，系统性提取其投资体系的结构化知识。

提取分类（kind）必须且仅限以下七种之一：
1. PRINCIPLE: 核心投资哲学与总原则（例如仓位大框架、不加杠杆、无限子弹等）
2. SECTOR_PREFERENCE: 行业题材与选股偏好（例如只做科技龙头、回避高估值等）
3. ENTRY_RULE: 具体买入/开仓条件（例如触碰20MA放量反包、缩量回调支撑位等）
4. EXIT_RULE: 止损、止盈或卖出条件（例如跌破60MA三日不回、冲高5%分批兑现等）
5. POSITION_RISK: 仓位控制与单笔风控纪律（例如单笔最大20%、回撤控制等）
6. HISTORICAL_CASE: 历史案例与操作复盘（注意：历史案例中的具体股票绝不能直接当今日候选）
7. UNRESOLVED: 原文提及但参数模糊、前后矛盾或存在疑问的点

核心纪律红线：
- 严禁脑补或编造原文未提及的具体参数！若博主只说“缩量回调低吸”，必须将均线窗口、缩量阈值等标记在 conditions 中注明“原文未提供具体数值”或列入 UNRESOLVED。
- 每一条知识必须包含 citations 数组，其中的 quote 必须是直接从原文中原样复制的连续片段（10~80字），绝不允许修改任何字符！
- 严格输出合法 JSON，不包含任何 Markdown 代码块包裹，也不输出任何客套话。

JSON 结构规范：
{
  "schema_version": "V7_BLOGGER_LAB_V1.0",
  "items": [
    {
      "id": "ITEM_001",
      "kind": "ENTRY_RULE",
      "statement": "明确简洁的规则描述",
      "conditions": ["适用条件1", "适用条件2"],
      "exceptions": ["反例或失效场景"],
      "citations": [
        {
          "quote": "从文本中原样提取的文字片段"
        }
      ],
      "source_date": null
    }
  ],
  "unresolved_notes": ["整体模糊或冲突的说明"]
}
"""


class LLMConnectionError(RuntimeError):
    """LLM 外部服务连接异常"""
    pass


class LLMValidationError(ValueError):
    """LLM 返回内容 Schema 或原文引用校验异常"""
    pass


def _strip_code_fence(text: str) -> str:
    """剥除 Markdown 代码块包装"""
    s = text.strip()
    if s.startswith('```'):
        first_nl = s.find('\n')
        s = s[first_nl + 1:] if first_nl != -1 else s[3:]
        if s.rstrip().endswith('```'):
            cut = s.rfind('```')
            s = s[:cut].rstrip()
    return s.strip()


def verify_and_align_citations(
    raw_quote: str,
    chunk_text: str,
    source_id: str,
    chunk_id: str,
    chunk_offset_in_source: int = 0
) -> Optional[Citation]:
    """
    在分块原文（chunk_text）中查找引用片段并对齐字符偏移。
    若精确匹配成功，返回带 start/end（分块局部偏移）及 chunk_offset_in_source 的 Citation；
    若无法在原文中找到该引用，返回 None（判定为幻觉引用）。
    """
    clean_quote = raw_quote.strip()
    if not clean_quote:
        return None

    pos = chunk_text.find(clean_quote)
    if pos != -1:
        return Citation(
            source_id=source_id,
            chunk_id=chunk_id,
            start=pos,
            end=pos + len(clean_quote),
            chunk_offset_in_source=chunk_offset_in_source,
            quote=clean_quote
        )

    # 尝试忽略空白字符匹配（某些模型可能会把换行替换为空格）
    norm_quote = re.sub(r'\s+', '', clean_quote)
    if not norm_quote:
        return None

    pattern_chars = [re.escape(c) for c in norm_quote]
    regex_pattern = r'\s*'.join(pattern_chars)
    match = re.search(regex_pattern, chunk_text)
    if match:
        matched_text = match.group(0)
        return Citation(
            source_id=source_id,
            chunk_id=chunk_id,
            start=match.start(),
            end=match.end(),
            chunk_offset_in_source=chunk_offset_in_source,
            quote=matched_text
        )

    return None


def validate_extraction_payload(
    parsed: Any,
    chunk_text: str,
    source_id: str,
    chunk_id: str,
    persona_id: str,
    knowledge_version: str,
    chunk_offset_in_source: int = 0
) -> Tuple[KnowledgeExtractionResult, List[str]]:
    """
    对 LLM 返回的原始字典进行严格 Schema 校验和原文引用核对。
    若顶层非 dict、版本错误或严重结构损坏，直接抛出 LLMValidationError；
    返回 (KnowledgeExtractionResult, errors_list)。
    """
    if not isinstance(parsed, dict):
        raise LLMValidationError(f"顶层返回必须是 JSON 对象 (dict)，实际得到: {type(parsed).__name__}")

    # 1. 顶层 schema_version 严格校验
    version = parsed.get("schema_version")
    if version != SCHEMA_VERSION:
        raise LLMValidationError(
            f"不支持的 schema_version: '{version}'，必须为 '{SCHEMA_VERSION}'"
        )

    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        raise LLMValidationError("返回数据缺少合法的 'items' 列表")

    errors: List[str] = []
    clean_items: List[KnowledgeItem] = []
    valid_kinds = {
        "PRINCIPLE", "SECTOR_PREFERENCE", "ENTRY_RULE", "EXIT_RULE",
        "POSITION_RISK", "HISTORICAL_CASE", "UNRESOLVED"
    }

    raw_count = len(raw_items)

    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            errors.append(f"第 {idx+1} 项类型错误: 期望 dict，实际得到 {type(item).__name__}")
            continue

        raw_kind = item.get("kind")
        if not isinstance(raw_kind, str) or raw_kind.strip().upper() not in valid_kinds:
            errors.append(f"第 {idx+1} 项非法 kind: {raw_kind}")
            continue
        clean_kind = raw_kind.strip().upper()

        stmt = item.get("statement")
        if not isinstance(stmt, str) or len(stmt.strip()) < 2:
            errors.append(f"第 {idx+1} 项 statement 类型错误或长度过短: {stmt}")
            continue
        clean_stmt = stmt.strip()

        # conditions 强类型校验：必须是 list[str]
        raw_conds = item.get("conditions", [])
        if not isinstance(raw_conds, list) or not all(isinstance(c, str) for c in raw_conds):
            errors.append(f"第 {idx+1} 项 conditions 必须是字符串列表 (list[str])")
            continue
        clean_conditions = [c.strip() for c in raw_conds if c.strip()]

        # exceptions 强类型校验：必须是 list[str]
        raw_excs = item.get("exceptions", [])
        if not isinstance(raw_excs, list) or not all(isinstance(e, str) for e in raw_excs):
            errors.append(f"第 {idx+1} 项 exceptions 必须是字符串列表 (list[str])")
            continue
        clean_exceptions = [e.strip() for e in raw_excs if e.strip()]

        # citations 校验：必须是非空 list
        raw_citations = item.get("citations")
        if not isinstance(raw_citations, list) or len(raw_citations) == 0:
            errors.append(f"第 {idx+1} 项缺少有效 citations 列表")
            continue

        valid_citations: List[Citation] = []
        for c_idx, c in enumerate(raw_citations):
            if isinstance(c, dict):
                q = c.get("quote")
            elif isinstance(c, str):
                q = c
            else:
                q = None

            if not isinstance(q, str) or not q.strip():
                errors.append(f"第 {idx+1} 项的第 {c_idx+1} 条引用非有效文本")
                continue

            aligned = verify_and_align_citations(
                q, chunk_text, source_id, chunk_id, chunk_offset_in_source
            )
            if aligned:
                valid_citations.append(aligned)
            else:
                errors.append(f"第 {idx+1} 项的第 {c_idx+1} 条引用未在原文中找到(幻觉拦截): '{q[:30]}...'")

        if not valid_citations:
            errors.append(f"第 {idx+1} 项无任何通过验证的原文引用，判定无效并剔除")
            continue

        local_raw = str(item.get("id") or f"item_{idx+1}").strip()
        item_id = local_raw if local_raw.startswith(f"{chunk_id}_") else f"{chunk_id}_{local_raw}"
        source_date = item.get("source_date")
        if source_date is not None:
            if not isinstance(source_date, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', source_date):
                source_date = None

        knowledge_item = KnowledgeItem(
            id=item_id,
            persona_id=persona_id,
            knowledge_version=knowledge_version,
            kind=clean_kind,  # type: ignore
            statement=clean_stmt,
            conditions=clean_conditions,
            exceptions=clean_exceptions,
            citations=valid_citations,
            source_date=source_date
        )
        clean_items.append(knowledge_item)

    # 判定整体状态
    if raw_count == 0:
        overall_status = "SUCCESS"  # 合法空抽取
    elif len(clean_items) == 0:
        overall_status = "FAILED"   # 模型输出了知识，但全部损坏/幻觉
    elif len(clean_items) < raw_count:
        overall_status = "PARTIAL"  # 部分有效
    else:
        overall_status = "SUCCESS"  # 全部有效

    raw_notes = parsed.get("unresolved_notes", [])
    clean_notes = [str(n).strip() for n in raw_notes if isinstance(n, str) and str(n).strip()]

    result = KnowledgeExtractionResult(
        schema_version=SCHEMA_VERSION,
        status=overall_status,
        raw_items_count=raw_count,
        valid_items_count=len(clean_items),
        items=clean_items,
        unresolved_notes=clean_notes,
        validation_errors=errors
    )
    return result, errors


class BloggerLabLLMAdapter:
    """博主实验室 LLM 适配器"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        mock_provider=None
    ):
        self.base_url = base_url or rc.LLM_BASE_URL
        self.model = model or rc.LLM_MODEL
        self.api_key = api_key or rc.LLM_API_KEY
        self.mock_provider = mock_provider
        self.prompt_version = PROMPT_VERSION

    def extract_knowledge_from_chunk(
        self,
        chunk_text: str,
        source_id: str,
        chunk_id: str,
        persona_id: str,
        knowledge_version: str,
        chunk_offset_in_source: int = 0,
        timeout: int = 30
    ) -> Tuple[KnowledgeExtractionResult, Dict[str, Any]]:
        """
        核心接口：从单块文本抽取知识并进行本地 Schema 及引用验证。
        返回 (KnowledgeExtractionResult, meta_dict)。
        若顶层损坏或全条目校验失败，抛出 LLMValidationError。
        若外部连接异常，抛出 LLMConnectionError。
        """
        # 1. 测试替身分支（显式用于无网络确定性测试）
        if self.mock_provider is not None:
            raw_text, meta = self.mock_provider(chunk_text, source_id, chunk_id)
            meta["is_mock"] = True
            try:
                content = _strip_code_fence(raw_text)
                parsed = json.loads(content)
            except Exception as e:
                raise LLMValidationError(f"Mock 返回非合法 JSON: {e}")

            res, errs = validate_extraction_payload(
                parsed, chunk_text, source_id, chunk_id, persona_id,
                knowledge_version, chunk_offset_in_source
            )
            meta["validation_errors"] = errs
            meta["status"] = res.status
            # 若原始有条目但全部失效（status=="FAILED"），刚性抛出 LLMValidationError 阻断
            if res.status == "FAILED":
                raise LLMValidationError(f"抽取严重失败 (FAILED): 全部条目无效或未命中引用. 详情: {errs}")
            return res, meta

        # 2. 真实网络调用分支
        if not self.api_key:
            raise LLMConnectionError("未配置 API Key (DEEPSEEK_API_KEY / LLM_API_KEY 为空)")

        user_content = (
            f"[来源标识: {source_id} | 分块: {chunk_id}]\n"
            f"[待提取文本]:\n{chunk_text}"
        )

        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=timeout,
                max_retries=1
            )
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _LAB_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.2,
                max_tokens=2500,
                response_format={"type": "json_object"}
            )
        except Exception as e:
            raise LLMConnectionError(f"LLM 请求失败 (URL={self.base_url}, Model={self.model}): {e}")

        content_raw = resp.choices[0].message.content or ""
        clean_json_str = _strip_code_fence(content_raw)

        try:
            parsed = json.loads(clean_json_str)
        except Exception as e:
            raise LLMValidationError(f"模型输出非有效 JSON: {e}\n原始返回片段: {content_raw[:150]}")

        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                "total_tokens": getattr(resp.usage, "total_tokens", None)
            }

        meta = {
            "is_mock": False,
            "model_returned": getattr(resp, "model", self.model),
            "model_requested": self.model,
            "prompt_version": self.prompt_version,
            "finish_reason": resp.choices[0].finish_reason,
            "usage": usage,
            "base_url": self.base_url
        }

        res, errs = validate_extraction_payload(
            parsed, chunk_text, source_id, chunk_id, persona_id,
            knowledge_version, chunk_offset_in_source
        )
        meta["validation_errors"] = errs
        meta["status"] = res.status
        if res.status == "FAILED":
            raise LLMValidationError(f"抽取严重失败 (FAILED): 全部条目无效或未命中引用. 详情: {errs}")

        return res, meta
