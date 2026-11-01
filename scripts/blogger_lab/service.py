# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/service.py
V7 博主实验室业务服务层门面 (阶段 1 核心实现)
提供分身注册、资料批次导入、解析分块调度与状态查询服务，实现严格的多分身隔离。
"""

from __future__ import annotations
import os
import sys
import uuid
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import (
    Persona,
    CorpusBatch,
    Source,
    Chunk,
    KnowledgeVersion,
    ExtractionJob,
    KnowledgeItem,
    StrategyDraft,
    StrategyVersion,
    StrategyCondition,
    ExitRules,
    SizingRules,
    ScanRun,
    ScanCandidate,
    StockEvaluation,
    LabAccount,
    LabPosition,
    LabFill,
    PaperCycle
)
from blogger_lab.store import BloggerLabStore, current_time_iso
from blogger_lab.ingest import (
    compute_file_sha256,
    extract_text_from_file,
    chunk_normalized_text
)
from blogger_lab.llm import BloggerLabLLMAdapter
from blogger_lab.knowledge import BloggerLabKnowledgeEngine
from blogger_lab.market import BloggerLabMarketAdapter
from blogger_lab.strategy import BloggerLabStrategyEngine
from blogger_lab.scan import BloggerLabOpportunityScanner


class BloggerLabService:
    """博主实验室业务服务门面"""

    def __init__(
        self,
        lab_root: Optional[str] = None,
        market_adapter: Optional[Any] = None,
        paper_adapter: Optional[Any] = None,
        clock: Optional[Any] = None
    ):
        self.store = BloggerLabStore(lab_root=lab_root)
        self.knowledge_engine = BloggerLabKnowledgeEngine(store=self.store)
        self.strategy_engine = BloggerLabStrategyEngine(store=self.store)
        
        effective_clock = clock or (market_adapter.clock if (market_adapter and hasattr(market_adapter, 'clock')) else None)
        if market_adapter is None and clock is not None:
            market_adapter = BloggerLabMarketAdapter(clock=clock)
            
        self.scanner = BloggerLabOpportunityScanner(store=self.store, market_adapter=market_adapter)
        from blogger_lab.paper import BloggerLabPaperAdapter
        self.paper_adapter = paper_adapter or BloggerLabPaperAdapter(
            lab_root=lab_root,
            clock=effective_clock,
            store=self.store,
            market_adapter=self.scanner.market_adapter
        )

    # ==========================================
    # 分身管理
    # ==========================================

    def create_persona(self, persona_id: str, name: str, description: str = "") -> Persona:
        """注册新分身"""
        existing = self.store.get_persona(persona_id)
        if existing is not None:
            raise ValueError(f"分身 ID '{persona_id}' 已存在，不可重复注册")
        return self.store.create_persona(persona_id=persona_id, name=name, description=description)

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        """获取分身详情"""
        return self.store.get_persona(persona_id)

    def list_personas(self) -> List[Persona]:
        """列出所有分身"""
        return self.store.list_personas()

    # ==========================================
    # 资料导入与分块任务
    # ==========================================

    def import_corpus_files(
        self,
        persona_id: str,
        files: List[Path],
        batch_id: Optional[str] = None,
        target_chunk_chars: int = 6000,
        overlap_chars: int = 300
    ) -> CorpusBatch:
        """
        向指定分身导入一组资料文件，执行正文/表格提取与全区间覆盖分块。
        """
        persona = self.store.get_persona(persona_id)
        if persona is None:
            raise ValueError(f"分身 '{persona_id}' 不存在，请先注册分身")

        if not files:
            raise ValueError("导入文件列表为空")

        b_id = batch_id or f"batch_{persona_id}_{uuid.uuid4().hex[:8]}"
        batch_raw_dir = self.store.raw_files_dir / persona_id / b_id
        batch_norm_dir = self.store.normalized_dir / persona_id / b_id
        batch_raw_dir.mkdir(parents=True, exist_ok=True)
        batch_norm_dir.mkdir(parents=True, exist_ok=True)

        source_ids: List[str] = []
        ready_count = 0
        failed_count = 0
        total_extracted_chars = 0
        total_chunks_created = 0
        file_reports: List[Dict[str, Any]] = []

        now_iso = current_time_iso()

        for idx, src_path in enumerate(files):
            p = Path(src_path)
            if not p.exists() or not p.is_file():
                continue

            src_id = f"src_{b_id}_{str(idx).zfill(2)}"
            source_ids.append(src_id)

            # 1. 保存原文件副本（增加 src_id 前缀，杜绝同批次同名文件相互覆盖）
            dest_raw = batch_raw_dir / f"{src_id}_{p.name}"
            shutil.copy2(p, dest_raw)
            sha = compute_file_sha256(dest_raw)
            file_size = dest_raw.stat().st_size

            # 2. 抽取正文与表格
            norm_text, file_type, unsupported, err_msg = extract_text_from_file(dest_raw)

            if err_msg or file_type == "UNSUPPORTED":
                status = "NEEDS_CONVERSION" if "需格式转换" in (err_msg or "") else "FAILED"
                failed_count += 1
                source_obj = Source(
                    id=src_id,
                    batch_id=b_id,
                    persona_id=persona_id,
                    filename=p.name,
                    file_type=file_type,  # type: ignore
                    sha256=sha,
                    file_size_bytes=file_size,
                    status=status,
                    raw_file_path=str(dest_raw.resolve()),
                    normalized_text_path=None,
                    extracted_chars=0,
                    unsupported_parts=unsupported,
                    error_message=err_msg
                )
                self.store.save_source(source_obj)
                file_reports.append({
                    "filename": p.name,
                    "status": status,
                    "error": err_msg,
                    "unsupported_parts": unsupported
                })
                continue

            # 3. 保存规范文本
            dest_norm = batch_norm_dir / f"{src_id}_norm.txt"
            dest_norm.write_text(norm_text, encoding="utf-8")
            char_count = len(norm_text)
            total_extracted_chars += char_count

            # 4. 全区间严格覆盖分块
            chunks = chunk_normalized_text(
                full_text=norm_text,
                source_id=src_id,
                persona_id=persona_id,
                batch_id=b_id,
                target_chunk_chars=target_chunk_chars,
                overlap_chars=overlap_chars
            )
            total_chunks_created += len(chunks)

            # 5. 持久化 Source 与 Chunks
            source_obj = Source(
                id=src_id,
                batch_id=b_id,
                persona_id=persona_id,
                filename=p.name,
                file_type=file_type,  # type: ignore
                sha256=sha,
                file_size_bytes=file_size,
                status="READY",
                raw_file_path=str(dest_raw.resolve()),
                normalized_text_path=str(dest_norm.resolve()),
                extracted_chars=char_count,
                unsupported_parts=unsupported,
                error_message=None
            )
            self.store.save_source(source_obj)
            self.store.save_chunks(chunks)

            ready_count += 1
            file_reports.append({
                "filename": p.name,
                "status": "READY",
                "extracted_chars": char_count,
                "chunks_count": len(chunks),
                "unsupported_parts": unsupported
            })

        # 6. 确定批次最终状态
        if ready_count == len(files):
            batch_status = "READY"
        elif ready_count > 0:
            batch_status = "PARTIAL"
        else:
            batch_status = "FAILED"

        batch_obj = CorpusBatch(
            id=b_id,
            persona_id=persona_id,
            created_at=now_iso,
            status=batch_status,
            parser_version="PARSER_V1.0",
            total_files=len(files),
            ready_files=ready_count,
            failed_files=failed_count,
            total_chars=total_extracted_chars,
            total_chunks=total_chunks_created,
            source_ids=source_ids,
            summary_report={
                "files": file_reports,
                "completed": (batch_status == "READY")
            }
        )

        self.store.save_corpus_batch(batch_obj)
        return batch_obj

    def get_batch_details(self, batch_id: str, persona_id: str) -> Optional[Dict[str, Any]]:
        """获取批次详情及关联的来源和分块统计"""
        batch = self.store.get_corpus_batch(batch_id, persona_id)
        if batch is None:
            return None
        sources = self.store.get_sources_by_batch(batch_id, persona_id)
        return {
            "batch": batch.model_dump(),
            "sources": [s.model_dump() for s in sources]
        }

    # ==========================================
    # 阶段 2: 知识提炼门面接口
    # ==========================================

    def extract_knowledge(
        self,
        persona_id: str,
        batch_id: str,
        llm_adapter: BloggerLabLLMAdapter,
        version_id: Optional[str] = None
    ) -> Tuple[KnowledgeVersion, ExtractionJob]:
        """为分身指定资料批次启动知识提炼并生成知识版本"""
        return self.knowledge_engine.start_batch_extraction(
            persona_id=persona_id,
            batch_id=batch_id,
            llm_adapter=llm_adapter,
            version_id=version_id
        )

    def retry_knowledge_extraction(
        self,
        persona_id: str,
        batch_id: str,
        knowledge_version_id: str,
        llm_adapter: BloggerLabLLMAdapter
    ) -> Tuple[KnowledgeVersion, ExtractionJob]:
        """针对失败分块执行断点续跑重试"""
        return self.knowledge_engine.retry_failed_chunks(
            persona_id=persona_id,
            batch_id=batch_id,
            knowledge_version_id=knowledge_version_id,
            llm_adapter=llm_adapter
        )

    def get_extraction_job(self, job_id: str, persona_id: str) -> Optional[ExtractionJob]:
        """获取指定分身名下的知识提炼任务状态"""
        return self.store.get_extraction_job(job_id, persona_id)

    def list_batches(self, persona_id: str) -> List[CorpusBatch]:
        """获取分身名下的所有资料批次"""
        return self.store.list_corpus_batches(persona_id)

    def list_extraction_jobs(self, persona_id: str) -> List[ExtractionJob]:
        """获取分身名下的所有提炼任务"""
        return self.store.list_extraction_jobs(persona_id)

    def get_persona_overview(self, persona_id: str) -> Dict[str, Any]:
        """
        获取分身全景概览状态，供前端页面在加载、刷新或切换分身时
        从服务端无损恢复批次、任务、知识、策略、扫描与账户。
        """
        persona = self.get_persona(persona_id)
        if not persona:
            raise ValueError(f"分身 '{persona_id}' 不存在")

        batches = self.list_batches(persona_id)
        jobs = self.list_extraction_jobs(persona_id)
        kvs = self.store.list_knowledge_versions(persona_id)
        active_strat = self.get_active_strategy(persona_id)
        drafts = self.store.list_strategy_drafts(persona_id)
        scans = self.store.list_scan_runs(persona_id, limit=5)
        account = self.get_paper_account(persona_id, mode="REPLAY")

        latest_batch = batches[0] if batches else None
        latest_job = jobs[0] if jobs else None
        latest_draft = drafts[0] if drafts else None
        latest_scan = scans[0] if scans else None

        # 如果存在知识版本，附带最新版本的知识条目
        latest_kv_items = []
        latest_kv = None
        if kvs:
            latest_kv = kvs[0]
            items = self.store.get_knowledge_items(latest_kv.id, persona_id)
            latest_kv_items = [it.model_dump() for it in items]

        return {
            "persona": persona.model_dump(),
            "batches": [b.model_dump() for b in batches],
            "latest_batch": latest_batch.model_dump() if latest_batch else None,
            "jobs": [j.model_dump() for j in jobs],
            "latest_job": latest_job.model_dump() if latest_job else None,
            "knowledge_versions": [kv.model_dump() for kv in kvs],
            "latest_knowledge": {
                "version": latest_kv.model_dump() if latest_kv else None,
                "items": latest_kv_items
            },
            "latest_draft": latest_draft.model_dump() if latest_draft else None,
            "active_strategy": active_strat.model_dump() if active_strat else None,
            "latest_scan": latest_scan.model_dump() if latest_scan else None,
            "account": account.model_dump()
        }

    def get_source(self, source_id: str, persona_id: str) -> Optional[Source]:
        """获取指定分身名下的单份来源文件元数据"""
        return self.store.get_source(source_id, persona_id)

    def get_source_text_excerpt(
        self,
        source_id: str,
        persona_id: str,
        start_char: int,
        end_char: int
    ) -> Dict[str, Any]:
        """
        按来源 ID 与允许字符范围读取原文切片。
        严格核对分身归属，绝不透传或接受任意磁盘路径。
        """
        source = self.store.get_source(source_id, persona_id)
        if source is None:
            raise ValueError(f"来源文件 '{source_id}' 不存在或不属于分身 '{persona_id}'")
        if start_char < 0 or end_char < start_char:
            raise ValueError(f"非法的字符起止范围: [{start_char}, {end_char}]")
        if not source.normalized_text_path:
            raise ValueError(f"来源文件 '{source_id}' 尚未生成规范化文本")
        p = Path(source.normalized_text_path)
        if not p.exists():
            raise ValueError(f"规范化文本存储文件丢失: {source_id}")
        full_text = p.read_text(encoding="utf-8")
        clamped_end = min(end_char, len(full_text))
        excerpt = full_text[start_char:clamped_end]
        return {
            "source_id": source_id,
            "filename": source.filename,
            "start_char": start_char,
            "end_char": clamped_end,
            "total_chars": len(full_text),
            "excerpt": excerpt
        }

    def get_knowledge_version(self, version_id: str, persona_id: str) -> Optional[Dict[str, Any]]:
        """获取知识版本详情及其包含的结构化知识项"""
        kv = self.store.get_knowledge_version(version_id, persona_id)
        if kv is None:
            return None
        items = self.store.get_knowledge_items(version_id, persona_id)
        return {
            "version": kv.model_dump(),
            "items": [it.model_dump() for it in items]
        }

    def list_knowledge_versions(self, persona_id: str) -> List[KnowledgeVersion]:
        """列出分身名下的所有知识版本"""
        return self.store.list_knowledge_versions(persona_id)

    def get_knowledge_items(
        self,
        version_id: str,
        persona_id: str,
        kind: Optional[str] = None
    ) -> List[KnowledgeItem]:
        """获取指定知识版本下的知识项列表"""
        return self.store.get_knowledge_items(version_id, persona_id, kind=kind)

    # ==========================================
    # 阶段 3: 策略草案与不可变策略版本门面接口
    # ==========================================

    def generate_strategy_draft(
        self,
        persona_id: str,
        knowledge_version_id: str
    ) -> StrategyDraft:
        """从分身的 READY 知识版本生成初始策略草案"""
        return self.strategy_engine.generate_draft_from_knowledge(
            persona_id=persona_id,
            knowledge_version_id=knowledge_version_id
        )

    def get_strategy_draft(self, draft_id: str, persona_id: str) -> Optional[StrategyDraft]:
        """获取指定策略草案详情"""
        return self.store.get_strategy_draft(draft_id, persona_id)

    def list_strategy_drafts(self, persona_id: str) -> List[StrategyDraft]:
        """列出分身名下的所有策略草案"""
        return self.store.list_strategy_drafts(persona_id)

    def update_strategy_draft(
        self,
        draft_id: str,
        persona_id: str,
        expected_revision: int,
        entry_rules: Optional[List[StrategyCondition]] = None,
        exit_rules: Optional[ExitRules] = None,
        sizing_rules: Optional[SizingRules] = None
    ) -> StrategyDraft:
        """编辑策略草案（填补参数、微调阈值，自动版本并发控制）"""
        return self.strategy_engine.update_strategy_draft(
            draft_id=draft_id,
            persona_id=persona_id,
            expected_revision=expected_revision,
            entry_rules=entry_rules,
            exit_rules=exit_rules,
            sizing_rules=sizing_rules
        )

    def confirm_strategy_draft(
        self,
        draft_id: str,
        persona_id: str,
        expected_revision: int,
        expected_hash: str
    ) -> StrategyVersion:
        """确认发布策略草案为不可变 StrategyVersion 并激活"""
        return self.strategy_engine.confirm_strategy_draft(
            draft_id=draft_id,
            persona_id=persona_id,
            expected_revision=expected_revision,
            expected_hash=expected_hash
        )

    def get_strategy_version(self, version_id: str, persona_id: str) -> Optional[StrategyVersion]:
        """获取已确认发布的策略版本"""
        return self.store.get_strategy_version(version_id, persona_id)

    def list_strategy_versions(self, persona_id: str) -> List[StrategyVersion]:
        """列出分身名下的所有已确认策略版本"""
        return self.store.list_strategy_versions(persona_id)

    def get_active_strategy(self, persona_id: str) -> Optional[StrategyVersion]:
        """获取分身当前激活生效的策略版本"""
        persona = self.store.get_persona(persona_id)
        if persona is None or not persona.active_strategy_id:
            return None
        return self.store.get_strategy_version(persona.active_strategy_id, persona_id)

    # ==========================================
    # 今日机会发现扫描门面 (Stage 4)
    # ==========================================

    def run_opportunity_scan(
        self,
        persona_id: str,
        strategy_version_id: Optional[str] = None,
        mode: str = "REPLAY",
        as_of_date: Optional[str] = None,
        universe_symbols: Optional[List[str]] = None,
        universe_limit: int = 50,
        custom_quotes: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> ScanRun:
        """执行今日机会发现扫描"""
        return self.scanner.run_scan(
            persona_id=persona_id,
            strategy_version_id=strategy_version_id,
            mode=mode,
            as_of_date=as_of_date,
            universe_symbols=universe_symbols,
            universe_limit=universe_limit,
            custom_quotes=custom_quotes
        )

    def get_scan_run(self, run_id: str, persona_id: str) -> Optional[ScanRun]:
        """查询指定的扫描执行记录"""
        return self.store.get_scan_run(run_id, persona_id)

    def list_scan_runs(self, persona_id: str, limit: int = 50) -> List[ScanRun]:
        """列出指定分身名下的扫描记录"""
        return self.store.list_scan_runs(persona_id, limit=limit)

    def get_latest_scan_run(self, persona_id: str) -> Optional[ScanRun]:
        """获取分身最近一次扫描记录"""
        return self.store.get_latest_scan_run(persona_id)

    # ==========================================
    # 独立模拟与周期服务 (Stage 5)
    # ==========================================

    def run_paper_cycle(
        self,
        persona_id: str,
        request_key: str,
        mode: str = "REPLAY",
        scan_run_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        as_of_date: Optional[str] = None,
        quote_override: Optional[Dict[str, Any]] = None,
        failure_injection_hook: Optional[Any] = None
    ) -> PaperCycle:
        """执行独立模拟研究周期（先检查退出、后候选入场、严格原子落账与幂等保障）"""
        return self.paper_adapter.run_paper_cycle(
            persona_id=persona_id,
            request_key=request_key,
            mode=mode,
            scan_run_id=scan_run_id,
            candidate_id=candidate_id,
            as_of_date=as_of_date,
            quote_override=quote_override,
            failure_injection_hook=failure_injection_hook
        )

    def get_paper_account(self, persona_id: str, mode: str = "REPLAY") -> LabAccount:
        """获取分身指定模式的研究账户状态"""
        return self.paper_adapter.get_or_create_account(persona_id=persona_id, mode=mode)

    def get_paper_cycle(self, cycle_id: str, persona_id: str) -> Optional[PaperCycle]:
        """获取指定周期记录"""
        return self.paper_adapter.get_paper_cycle(cycle_id=cycle_id, persona_id=persona_id)

    def list_paper_cycles(self, persona_id: str, mode: Optional[str] = None, limit: int = 50) -> List[PaperCycle]:
        """列出分身的模拟周期执行历史"""
        return self.paper_adapter.list_paper_cycles(persona_id=persona_id, mode=mode, limit=limit)

    def list_paper_fills(self, persona_id: str, mode: Optional[str] = None, limit: int = 50) -> List[LabFill]:
        """列出分身的模拟成交流水"""
        return self.paper_adapter.list_paper_fills(persona_id=persona_id, mode=mode, limit=limit)



