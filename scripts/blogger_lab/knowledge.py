# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/knowledge.py
V7 博主实验室知识提炼、聚合与版本化管理引擎 (R2 整改版)
负责逐块提炼调度、原文引用校验、Map-Reduce 聚合、冲突/未决项保留及失败断点续跑。
严格落实：
1. 知识项 ID 命名空间隔离与复合主键防覆盖；
2. 只要有部分引用无效 (PARTIAL) 绝不误报 READY 发布。
"""

from __future__ import annotations
import os
import sys
import uuid
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import (
    KnowledgeVersion,
    ExtractionJob,
    KnowledgeItem,
    KnowledgeExtractionResult,
    Chunk,
    CorpusBatch,
    SCHEMA_VERSION
)
from blogger_lab.store import BloggerLabStore, current_time_iso
from blogger_lab.llm import BloggerLabLLMAdapter, LLMValidationError, LLMConnectionError


class BloggerLabKnowledgeEngine:
    """博主实验室知识提炼与聚合引擎"""

    def __init__(self, store: BloggerLabStore):
        self.store = store

    def start_batch_extraction(
        self,
        persona_id: str,
        batch_id: str,
        llm_adapter: BloggerLabLLMAdapter,
        version_id: Optional[str] = None,
        job_id: Optional[str] = None
    ) -> Tuple[KnowledgeVersion, ExtractionJob]:
        """
        启动资料批次的全量知识提炼。
        严格逐块持久化状态，失败或部分失败不计作完成；若存在失败块或部分有效块，批次与版本绝不误报 READY。
        """
        # 1. 校验分身与批次
        persona = self.store.get_persona(persona_id)
        if persona is None:
            raise ValueError(f"分身 '{persona_id}' 不存在")

        batch = self.store.get_corpus_batch(batch_id, persona_id)
        if batch is None:
            raise ValueError(f"批次 '{batch_id}' 不存在或不属于分身 '{persona_id}'")

        if batch.status != "READY":
            raise ValueError(f"批次状态为 '{batch.status}'，必须为 READY 才能启动知识提炼")

        chunks = self.store.get_chunks_by_batch(batch_id, persona_id)
        if not chunks:
            raise ValueError(f"批次 '{batch_id}' 下未发现任何可提炼有效分块")

        # 2. 初始化知识版本与任务对象
        now_iso = current_time_iso()
        existing_versions = self.store.list_knowledge_versions(persona_id)
        next_ver_num = len(existing_versions) + 1
        kv_id = version_id or f"kv_{persona_id}_{str(next_ver_num).zfill(3)}"
        j_id = job_id or f"job_ext_{batch_id}_{uuid.uuid4().hex[:6]}"

        kv = KnowledgeVersion(
            schema_version=SCHEMA_VERSION,
            id=kv_id,
            persona_id=persona_id,
            batch_id=batch_id,
            version_number=next_ver_num,
            status="BUILDING",
            created_at=now_iso,
            updated_at=now_iso,
            total_chunks=len(chunks),
            succeeded_chunks=0,
            partial_chunks=0,
            failed_chunks=0,
            total_items=0,
            items_by_kind={},
            unresolved_count=0,
            summary=""
        )
        self.store.save_knowledge_version(kv)

        job = ExtractionJob(
            schema_version=SCHEMA_VERSION,
            id=j_id,
            persona_id=persona_id,
            batch_id=batch_id,
            knowledge_version_id=kv_id,
            status="RUNNING",
            created_at=now_iso,
            updated_at=now_iso,
            total_chunks=len(chunks),
            processed_chunks=0,
            succeeded_chunks=0,
            partial_chunks=0,
            failed_chunks=0,
            error_message=None,
            chunk_errors={}
        )
        self.store.save_extraction_job(job)

        # 3. 逐块调度提炼
        all_extracted_items: List[KnowledgeItem] = []
        chunk_errors: Dict[str, str] = {}
        succeeded_count = 0
        partial_count = 0
        failed_count = 0

        for chunk in chunks:
            # 标记当前分块为 RUNNING
            self.store.update_chunk_status(chunk.id, persona_id, "RUNNING")
            
            # 尝试提炼（带 1 次自动重试）
            res, err_msg = self._extract_single_chunk_with_retry(
                llm_adapter=llm_adapter,
                chunk=chunk,
                persona_id=persona_id,
                knowledge_version_id=kv_id
            )

            if res is not None:
                if res.status == "SUCCESS":
                    # 全部条目与引用严格有效
                    self.store.update_chunk_status(chunk.id, persona_id, "SUCCEEDED", chunk.extraction_key)
                    succeeded_count += 1
                    all_extracted_items.extend(res.items)
                elif res.status == "PARTIAL":
                    # 部分有效、部分引用无效
                    self.store.update_chunk_status(chunk.id, persona_id, "PARTIAL", chunk.extraction_key)
                    partial_count += 1
                    err_desc = f"部分条目提取失败 ({res.valid_items_count}/{res.raw_items_count} 有效): {'; '.join(res.validation_errors)}"
                    chunk_errors[chunk.id] = err_desc
                    all_extracted_items.extend(res.items)
                else:
                    # 全部条目提取失败
                    self.store.update_chunk_status(chunk.id, persona_id, "FAILED")
                    failed_count += 1
                    chunk_errors[chunk.id] = f"全部条目提取失败: {'; '.join(res.validation_errors)}"
            else:
                # 提炼调用异常
                self.store.update_chunk_status(chunk.id, persona_id, "FAILED")
                failed_count += 1
                chunk_errors[chunk.id] = err_msg or "未知提炼错误"

            # 刷新任务进度
            job.processed_chunks = succeeded_count + partial_count + failed_count
            job.succeeded_chunks = succeeded_count
            job.partial_chunks = partial_count
            job.failed_chunks = failed_count
            job.chunk_errors = chunk_errors
            job.updated_at = current_time_iso()
            self.store.save_extraction_job(job)

        # 4. Map-Reduce 聚合与状态裁决
        return self._finalize_extraction(kv=kv, job=job, items=all_extracted_items)

    def retry_failed_chunks(
        self,
        persona_id: str,
        batch_id: str,
        knowledge_version_id: str,
        llm_adapter: BloggerLabLLMAdapter
    ) -> Tuple[KnowledgeVersion, ExtractionJob]:
        """
        断点续跑：对批次下此前标记为 FAILED 或 PARTIAL 的分块进行重新提炼，
        成功后与原有已成功知识条目合并，实现版本晋升。
        """
        kv = self.store.get_knowledge_version(knowledge_version_id, persona_id)
        if kv is None:
            raise ValueError(f"知识版本 '{knowledge_version_id}' 不存在")

        chunks = self.store.get_chunks_by_batch(batch_id, persona_id)
        retry_target_chunks = [c for c in chunks if c.status in ("FAILED", "PARTIAL")]
        
        if not retry_target_chunks:
            # 没有需要重试的分块
            existing_items = self.store.get_knowledge_items(knowledge_version_id, persona_id)
            job = ExtractionJob(
                schema_version=SCHEMA_VERSION,
                id=f"job_retry_{uuid.uuid4().hex[:6]}",
                persona_id=persona_id,
                batch_id=batch_id,
                knowledge_version_id=knowledge_version_id,
                status="SUCCEEDED",
                created_at=current_time_iso(),
                updated_at=current_time_iso(),
                total_chunks=len(chunks),
                processed_chunks=len(chunks),
                succeeded_chunks=len(chunks),
                partial_chunks=0,
                failed_chunks=0,
                chunk_errors={}
            )
            return kv, job

        # 创建续跑 Job
        now_iso = current_time_iso()
        job = ExtractionJob(
            schema_version=SCHEMA_VERSION,
            id=f"job_retry_{batch_id}_{uuid.uuid4().hex[:6]}",
            persona_id=persona_id,
            batch_id=batch_id,
            knowledge_version_id=knowledge_version_id,
            status="RUNNING",
            created_at=now_iso,
            updated_at=now_iso,
            total_chunks=len(retry_target_chunks),
            processed_chunks=0,
            succeeded_chunks=0,
            partial_chunks=0,
            failed_chunks=0,
            chunk_errors={}
        )

        newly_extracted_items: List[KnowledgeItem] = []
        chunk_errors: Dict[str, str] = {}
        succeeded_now = 0
        partial_now = 0
        failed_now = 0

        for chunk in retry_target_chunks:
            self.store.update_chunk_status(chunk.id, persona_id, "RUNNING")
            res, err_msg = self._extract_single_chunk_with_retry(
                llm_adapter=llm_adapter,
                chunk=chunk,
                persona_id=persona_id,
                knowledge_version_id=knowledge_version_id
            )

            if res is not None:
                if res.status == "SUCCESS":
                    self.store.update_chunk_status(chunk.id, persona_id, "SUCCEEDED", chunk.extraction_key)
                    succeeded_now += 1
                    newly_extracted_items.extend(res.items)
                elif res.status == "PARTIAL":
                    self.store.update_chunk_status(chunk.id, persona_id, "PARTIAL", chunk.extraction_key)
                    partial_now += 1
                    chunk_errors[chunk.id] = f"重试后仍有部分条目失败: {'; '.join(res.validation_errors)}"
                    newly_extracted_items.extend(res.items)
                else:
                    self.store.update_chunk_status(chunk.id, persona_id, "FAILED")
                    failed_now += 1
                    chunk_errors[chunk.id] = f"重试后全部条目失败: {'; '.join(res.validation_errors)}"
            else:
                self.store.update_chunk_status(chunk.id, persona_id, "FAILED")
                failed_now += 1
                chunk_errors[chunk.id] = err_msg or "重试请求失败"

            job.processed_chunks = succeeded_now + partial_now + failed_now
            job.succeeded_chunks = succeeded_now
            job.partial_chunks = partial_now
            job.failed_chunks = failed_now
            job.chunk_errors = chunk_errors
            job.updated_at = current_time_iso()
            self.store.save_extraction_job(job)

        # 重新读取当前全部 chunks 的实际最终状态
        all_chunks_now = self.store.get_chunks_by_batch(batch_id, persona_id)
        total_succeeded = sum(1 for c in all_chunks_now if c.status == "SUCCEEDED")
        total_partial = sum(1 for c in all_chunks_now if c.status == "PARTIAL")
        total_failed = sum(1 for c in all_chunks_now if c.status == "FAILED")

        # 持久化新抽取的条目（通过复合主键更新/插入）
        if newly_extracted_items:
            self.store.save_knowledge_items(newly_extracted_items, knowledge_version_id)

        # 读取该版本全部最新的知识条目
        combined_items = self.store.get_knowledge_items(knowledge_version_id, persona_id)

        # 重新评估并更新 KnowledgeVersion 整体状态
        kv.succeeded_chunks = total_succeeded
        kv.partial_chunks = total_partial
        kv.failed_chunks = total_failed
        kv.total_items = len(combined_items)
        kv.items_by_kind = self._count_items_by_kind(combined_items)
        kv.unresolved_count = kv.items_by_kind.get("UNRESOLVED", 0)
        kv.updated_at = current_time_iso()

        # 判定发布状态：只有 0 失败 0 部分失败且全部成功，才发布 READY！
        if total_failed == 0 and total_partial == 0 and total_succeeded == len(all_chunks_now):
            kv.status = "READY"
            job.status = "SUCCEEDED"
            kv.summary = f"全部 {len(all_chunks_now)} 个分块提炼完成，共汇总 {len(combined_items)} 项有效投资知识。"
        elif total_succeeded > 0 or total_partial > 0:
            kv.status = "PARTIAL"
            job.status = "PARTIAL"
            kv.summary = (
                f"提炼未全部完成：完全成功 {total_succeeded}/{len(all_chunks_now)} 块，"
                f"部分失败 {total_partial} 块，完全失败 {total_failed} 块。未达到 READY 发布条件。"
            )
        else:
            kv.status = "FAILED"
            job.status = "FAILED"
            kv.summary = "提炼完全失败。"

        self.store.save_knowledge_version(kv)
        self.store.save_extraction_job(job)
        return kv, job

    def _extract_single_chunk_with_retry(
        self,
        llm_adapter: BloggerLabLLMAdapter,
        chunk: Chunk,
        persona_id: str,
        knowledge_version_id: str
    ) -> Tuple[Optional[KnowledgeExtractionResult], Optional[str]]:
        """执行单块提炼，若异常或部分失败允许 1 次重试"""
        last_error = ""
        best_res: Optional[KnowledgeExtractionResult] = None
        for attempt in range(2):
            try:
                res, _ = llm_adapter.extract_knowledge_from_chunk(
                    chunk_text=chunk.text,
                    source_id=chunk.source_id,
                    chunk_id=chunk.id,
                    persona_id=persona_id,
                    knowledge_version=knowledge_version_id,
                    chunk_offset_in_source=chunk.start_char
                )
                if res.status == "SUCCESS":
                    return res, None
                best_res = res
            except (LLMValidationError, LLMConnectionError, Exception) as e:
                last_error = f"第 {attempt+1} 次抽取异常: {str(e)}"

        if best_res is not None:
            return best_res, None
        return None, last_error

    def _finalize_extraction(
        self,
        kv: KnowledgeVersion,
        job: ExtractionJob,
        items: List[KnowledgeItem]
    ) -> Tuple[KnowledgeVersion, ExtractionJob]:
        """聚合最终状态并持久化"""
        now_iso = current_time_iso()
        kv.updated_at = now_iso
        kv.succeeded_chunks = job.succeeded_chunks
        kv.partial_chunks = job.partial_chunks
        kv.failed_chunks = job.failed_chunks
        kv.total_items = len(items)
        kv.items_by_kind = self._count_items_by_kind(items)
        kv.unresolved_count = kv.items_by_kind.get("UNRESOLVED", 0)

        # 保存抽取的有效条目
        if items:
            self.store.save_knowledge_items(items, kv.id)

        # 状态判定：只有在完全 0 失败、0 部分失败时，才发布 READY
        if job.failed_chunks == 0 and job.partial_chunks == 0 and job.succeeded_chunks == job.total_chunks:
            kv.status = "READY"
            job.status = "SUCCEEDED"
            kv.summary = f"全量提炼完成：共覆盖 {job.total_chunks} 个分块，提取有效知识 {len(items)} 条。"
        elif job.succeeded_chunks > 0 or job.partial_chunks > 0:
            kv.status = "PARTIAL"
            job.status = "PARTIAL"
            kv.summary = (
                f"提炼未全部完成：完全成功 {job.succeeded_chunks} 块，部分失败 {job.partial_chunks} 块，"
                f"完全失败 {job.failed_chunks} 块。存在未通过引用条目，不符合 READY 发布条件。"
            )
        else:
            kv.status = "FAILED"
            job.status = "FAILED"
            kv.summary = f"全部分块提炼失败，未产出有效知识。"

        self.store.save_knowledge_version(kv)
        self.store.save_extraction_job(job)
        return kv, job

    @staticmethod
    def _count_items_by_kind(items: List[KnowledgeItem]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for it in items:
            counts[it.kind] = counts.get(it.kind, 0) + 1
        return counts
