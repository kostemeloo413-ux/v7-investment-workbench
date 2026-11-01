# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/store.py
V7 博主实验室独立元数据与存储管理器
负责分身、资料批次、来源文件及分块在独立 SQLite (lab.sqlite) 与文件系统中的落盘管理。
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import datetime
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
    Citation,
    StrategyDraft,
    StrategyVersion,
    StrategyCondition,
    ExitRules,
    SizingRules,
    StockEvaluation,
    ScanCandidate,
    ScanRun,
    SCHEMA_VERSION
)

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def current_time_iso() -> str:
    return datetime.datetime.now(SHANGHAI).isoformat()


class BloggerLabStore:
    """博主实验室元数据与磁盘存储底座"""

    def __init__(self, lab_root: Optional[str] = None):
        if lab_root is None:
            self.lab_root = Path(_BASE) / "data" / "blogger_lab"
        else:
            self.lab_root = Path(lab_root)

        self.db_path = self.lab_root / "lab.sqlite"
        self.raw_files_dir = self.lab_root / "raw_files"
        self.normalized_dir = self.lab_root / "normalized_texts"

        # 确保目录存在
        self.raw_files_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()

    def _init_sqlite(self):
        """初始化分身、批次、来源及分块数据表"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS personas (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    active_strategy_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS corpus_batches (
                    id TEXT PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    total_files INTEGER NOT NULL,
                    ready_files INTEGER NOT NULL,
                    failed_files INTEGER NOT NULL,
                    total_chars INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    summary_report_json TEXT NOT NULL,
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    raw_file_path TEXT NOT NULL,
                    normalized_text_path TEXT,
                    extracted_chars INTEGER NOT NULL,
                    unsupported_parts_json TEXT NOT NULL,
                    error_message TEXT,
                    FOREIGN KEY(batch_id) REFERENCES corpus_batches(id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    char_length INTEGER NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    extraction_key TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id TEXT PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    succeeded_chunks INTEGER NOT NULL,
                    partial_chunks INTEGER NOT NULL DEFAULT 0,
                    failed_chunks INTEGER NOT NULL,
                    total_items INTEGER NOT NULL,
                    items_by_kind_json TEXT NOT NULL,
                    unresolved_count INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    FOREIGN KEY(persona_id) REFERENCES personas(id),
                    FOREIGN KEY(batch_id) REFERENCES corpus_batches(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    knowledge_version_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    exceptions_json TEXT NOT NULL,
                    citations_json TEXT NOT NULL,
                    source_date TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(persona_id, knowledge_version_id, id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id),
                    FOREIGN KEY(knowledge_version_id) REFERENCES knowledge_versions(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS extraction_jobs (
                    id TEXT PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    knowledge_version_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    processed_chunks INTEGER NOT NULL,
                    succeeded_chunks INTEGER NOT NULL,
                    partial_chunks INTEGER NOT NULL DEFAULT 0,
                    failed_chunks INTEGER NOT NULL,
                    error_message TEXT,
                    chunk_errors_json TEXT NOT NULL,
                    FOREIGN KEY(persona_id) REFERENCES personas(id),
                    FOREIGN KEY(batch_id) REFERENCES corpus_batches(id),
                    FOREIGN KEY(knowledge_version_id) REFERENCES knowledge_versions(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_drafts (
                    id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    knowledge_version TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    draft_hash TEXT NOT NULL,
                    entry_rules_json TEXT NOT NULL,
                    exit_rules_json TEXT NOT NULL,
                    sizing_rules_json TEXT NOT NULL,
                    unresolved_fields_json TEXT NOT NULL,
                    unsupported_conditions_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(persona_id, id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    draft_revision INTEGER NOT NULL,
                    draft_hash TEXT NOT NULL,
                    knowledge_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_rules_json TEXT NOT NULL,
                    exit_rules_json TEXT NOT NULL,
                    sizing_rules_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    PRIMARY KEY(persona_id, id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    strategy_version_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    universe_target_count INTEGER NOT NULL,
                    universe_evaluated_count INTEGER NOT NULL,
                    universe_excluded_count INTEGER NOT NULL,
                    candidates_count INTEGER NOT NULL,
                    candidates_json TEXT NOT NULL,
                    evaluations_json TEXT NOT NULL,
                    exclusion_reasons_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    execution_ms REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(persona_id, id),
                    FOREIGN KEY(persona_id) REFERENCES personas(id)
                )
            """)
            conn.commit()

    # ==========================================
    # Persona 分身管理
    # ==========================================

    def create_persona(self, persona_id: str, name: str, description: str = "") -> Persona:
        """注册新的博主分身"""
        clean_id = str(persona_id).strip()
        clean_name = str(name).strip()
        now_iso = current_time_iso()

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO personas (id, name, description, status, active_strategy_id, created_at, updated_at)
                VALUES (?, ?, ?, 'ACTIVE', NULL, ?, ?)
                """,
                (clean_id, clean_name, description, now_iso, now_iso)
            )
            conn.commit()

        return Persona(
            id=clean_id,
            name=clean_name,
            description=description,
            status="ACTIVE",
            active_strategy_id=None,
            created_at=now_iso,
            updated_at=now_iso
        )

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        """根据 ID 获取分身"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()
            if row is None:
                return None
            return Persona(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                status=row["status"],
                active_strategy_id=row["active_strategy_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )

    def list_personas(self) -> List[Persona]:
        """列出所有有效分身"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute("SELECT * FROM personas ORDER BY created_at ASC").fetchall()
            return [
                Persona(
                    id=r["id"],
                    name=r["name"],
                    description=r["description"] or "",
                    status=r["status"],
                    active_strategy_id=r["active_strategy_id"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"]
                )
                for r in rows
            ]

    # ==========================================
    # 批次、文件与分块持久化
    # ==========================================

    def save_corpus_batch(self, batch: CorpusBatch):
        """保存或更新资料批次状态"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO corpus_batches (
                    id, persona_id, created_at, status, parser_version,
                    total_files, ready_files, failed_files, total_chars, total_chunks,
                    source_ids_json, summary_report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    ready_files = excluded.ready_files,
                    failed_files = excluded.failed_files,
                    total_chars = excluded.total_chars,
                    total_chunks = excluded.total_chunks,
                    source_ids_json = excluded.source_ids_json,
                    summary_report_json = excluded.summary_report_json
                """,
                (
                    batch.id, batch.persona_id, batch.created_at, batch.status,
                    batch.parser_version, batch.total_files, batch.ready_files,
                    batch.failed_files, batch.total_chars, batch.total_chunks,
                    json.dumps(batch.source_ids, ensure_ascii=False),
                    json.dumps(batch.summary_report, ensure_ascii=False)
                )
            )
            conn.commit()

    def get_corpus_batch(self, batch_id: str, persona_id: str) -> Optional[CorpusBatch]:
        """获取分身下的指定批次"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM corpus_batches WHERE id = ? AND persona_id = ?",
                (batch_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            return CorpusBatch(
                id=row["id"],
                persona_id=row["persona_id"],
                created_at=row["created_at"],
                status=row["status"],
                parser_version=row["parser_version"],
                total_files=row["total_files"],
                ready_files=row["ready_files"],
                failed_files=row["failed_files"],
                total_chars=row["total_chars"],
                total_chunks=row["total_chunks"],
                source_ids=json.loads(row["source_ids_json"]),
                summary_report=json.loads(row["summary_report_json"])
            )

    def list_corpus_batches(self, persona_id: str) -> List[CorpusBatch]:
        """获取分身名下的所有批次列表，按创建时间倒序"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM corpus_batches WHERE persona_id = ? ORDER BY created_at DESC",
                (persona_id,)
            ).fetchall()
            res = []
            for row in rows:
                res.append(CorpusBatch(
                    id=row["id"],
                    persona_id=row["persona_id"],
                    created_at=row["created_at"],
                    status=row["status"],
                    parser_version=row["parser_version"],
                    total_files=row["total_files"],
                    ready_files=row["ready_files"],
                    failed_files=row["failed_files"],
                    total_chars=row["total_chars"],
                    total_chunks=row["total_chunks"],
                    source_ids=json.loads(row["source_ids_json"]),
                    summary_report=json.loads(row["summary_report_json"])
                ))
            return res

    def save_source(self, source: Source):
        """保存单份来源文件元数据"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO sources (
                    id, batch_id, persona_id, filename, file_type, sha256,
                    file_size_bytes, status, raw_file_path, normalized_text_path,
                    extracted_chars, unsupported_parts_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    normalized_text_path = excluded.normalized_text_path,
                    extracted_chars = excluded.extracted_chars,
                    unsupported_parts_json = excluded.unsupported_parts_json,
                    error_message = excluded.error_message
                """,
                (
                    source.id, source.batch_id, source.persona_id, source.filename,
                    source.file_type, source.sha256, source.file_size_bytes,
                    source.status, source.raw_file_path, source.normalized_text_path,
                    source.extracted_chars,
                    json.dumps(source.unsupported_parts, ensure_ascii=False),
                    source.error_message
                )
            )
            conn.commit()

    def get_sources_by_batch(self, batch_id: str, persona_id: str) -> List[Source]:
        """获取指定批次的所有来源文件"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM sources WHERE batch_id = ? AND persona_id = ? ORDER BY id ASC",
                (batch_id, persona_id)
            ).fetchall()
            return [
                Source(
                    id=r["id"],
                    batch_id=r["batch_id"],
                    persona_id=r["persona_id"],
                    filename=r["filename"],
                    file_type=r["file_type"],
                    sha256=r["sha256"],
                    file_size_bytes=r["file_size_bytes"],
                    status=r["status"],
                    raw_file_path=r["raw_file_path"],
                    normalized_text_path=r["normalized_text_path"],
                    extracted_chars=r["extracted_chars"],
                    unsupported_parts=json.loads(r["unsupported_parts_json"]),
                    error_message=r["error_message"]
                )
                for r in rows
            ]

    def get_source(self, source_id: str, persona_id: str) -> Optional[Source]:
        """获取指定分身名下的单份来源文件元数据"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM sources WHERE id = ? AND persona_id = ?",
                (source_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            return Source(
                id=row["id"],
                batch_id=row["batch_id"],
                persona_id=row["persona_id"],
                filename=row["filename"],
                file_type=row["file_type"],
                sha256=row["sha256"],
                file_size_bytes=row["file_size_bytes"],
                status=row["status"],
                raw_file_path=row["raw_file_path"],
                normalized_text_path=row["normalized_text_path"],
                extracted_chars=row["extracted_chars"],
                unsupported_parts=json.loads(row["unsupported_parts_json"]),
                error_message=row["error_message"]
            )

    def save_chunks(self, chunks: List[Chunk]):
        """批量保存分块列表"""
        if not chunks:
            return
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO chunks (
                    id, source_id, persona_id, batch_id, chunk_index,
                    start_char, end_char, char_length, text_sha256, text,
                    status, extraction_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    extraction_key = excluded.extraction_key
                """,
                [
                    (
                        c.id, c.source_id, c.persona_id, c.batch_id, c.chunk_index,
                        c.start_char, c.end_char, c.char_length, c.text_sha256,
                        c.text, c.status, c.extraction_key
                    )
                    for c in chunks
                ]
            )
            conn.commit()

    def get_chunks_by_source(self, source_id: str, persona_id: str) -> List[Chunk]:
        """获取指定文件按顺序排列的所有分块"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM chunks WHERE source_id = ? AND persona_id = ? ORDER BY chunk_index ASC",
                (source_id, persona_id)
            ).fetchall()
            return [
                Chunk(
                    id=r["id"],
                    source_id=r["source_id"],
                    persona_id=r["persona_id"],
                    batch_id=r["batch_id"],
                    chunk_index=r["chunk_index"],
                    start_char=r["start_char"],
                    end_char=r["end_char"],
                    char_length=r["char_length"],
                    text_sha256=r["text_sha256"],
                    text=r["text"],
                    status=r["status"],
                    extraction_key=r["extraction_key"]
                )
                for r in rows
            ]

    def get_chunks_by_batch(self, batch_id: str, persona_id: str) -> List[Chunk]:
        """获取指定批次下按顺序排列的全部有效分块"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM chunks WHERE batch_id = ? AND persona_id = ? ORDER BY source_id ASC, chunk_index ASC",
                (batch_id, persona_id)
            ).fetchall()
            return [
                Chunk(
                    id=r["id"],
                    source_id=r["source_id"],
                    persona_id=r["persona_id"],
                    batch_id=r["batch_id"],
                    chunk_index=r["chunk_index"],
                    start_char=r["start_char"],
                    end_char=r["end_char"],
                    char_length=r["char_length"],
                    text_sha256=r["text_sha256"],
                    text=r["text"],
                    status=r["status"],
                    extraction_key=r["extraction_key"]
                )
                for r in rows
            ]

    def update_chunk_status(
        self,
        chunk_id: str,
        persona_id: str,
        status: str,
        extraction_key: Optional[str] = None
    ):
        """更新单个分块的状态与提炼幂等键"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            if extraction_key is not None:
                cur.execute(
                    "UPDATE chunks SET status = ?, extraction_key = ? WHERE id = ? AND persona_id = ?",
                    (status, extraction_key, chunk_id, persona_id)
                )
            else:
                cur.execute(
                    "UPDATE chunks SET status = ? WHERE id = ? AND persona_id = ?",
                    (status, chunk_id, persona_id)
                )
            conn.commit()

    # ==========================================
    # 阶段 2: 知识版本、知识项与任务持久化
    # ==========================================

    def save_knowledge_version(self, kv: KnowledgeVersion):
        """保存或更新知识版本元数据"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO knowledge_versions (
                    id, persona_id, batch_id, version_number, status, created_at, updated_at,
                    total_chunks, succeeded_chunks, partial_chunks, failed_chunks, total_items,
                    items_by_kind_json, unresolved_count, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    total_chunks = excluded.total_chunks,
                    succeeded_chunks = excluded.succeeded_chunks,
                    partial_chunks = excluded.partial_chunks,
                    failed_chunks = excluded.failed_chunks,
                    total_items = excluded.total_items,
                    items_by_kind_json = excluded.items_by_kind_json,
                    unresolved_count = excluded.unresolved_count,
                    summary = excluded.summary
                """,
                (
                    kv.id, kv.persona_id, kv.batch_id, kv.version_number, kv.status,
                    kv.created_at, kv.updated_at, kv.total_chunks, kv.succeeded_chunks,
                    getattr(kv, "partial_chunks", 0),
                    kv.failed_chunks, kv.total_items,
                    json.dumps(kv.items_by_kind, ensure_ascii=False),
                    kv.unresolved_count, kv.summary
                )
            )
            conn.commit()

    def get_knowledge_version(self, version_id: str, persona_id: str) -> Optional[KnowledgeVersion]:
        """按 ID 与分身隔离查询知识版本"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM knowledge_versions WHERE id = ? AND persona_id = ?",
                (version_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            keys = row.keys()
            partial_c = row["partial_chunks"] if "partial_chunks" in keys else 0
            return KnowledgeVersion(
                id=row["id"],
                persona_id=row["persona_id"],
                batch_id=row["batch_id"],
                version_number=row["version_number"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                total_chunks=row["total_chunks"],
                succeeded_chunks=row["succeeded_chunks"],
                partial_chunks=partial_c,
                failed_chunks=row["failed_chunks"],
                total_items=row["total_items"],
                items_by_kind=json.loads(row["items_by_kind_json"]),
                unresolved_count=row["unresolved_count"],
                summary=row["summary"]
            )

    def list_knowledge_versions(self, persona_id: str) -> List[KnowledgeVersion]:
        """列出分身下的所有知识版本"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM knowledge_versions WHERE persona_id = ? ORDER BY version_number ASC",
                (persona_id,)
            ).fetchall()
            res = []
            for r in rows:
                keys = r.keys()
                partial_c = r["partial_chunks"] if "partial_chunks" in keys else 0
                res.append(
                    KnowledgeVersion(
                        id=r["id"],
                        persona_id=r["persona_id"],
                        batch_id=r["batch_id"],
                        version_number=r["version_number"],
                        status=r["status"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                        total_chunks=r["total_chunks"],
                        succeeded_chunks=r["succeeded_chunks"],
                        partial_chunks=partial_c,
                        failed_chunks=r["failed_chunks"],
                        total_items=r["total_items"],
                        items_by_kind=json.loads(r["items_by_kind_json"]),
                        unresolved_count=r["unresolved_count"],
                        summary=r["summary"]
                    )
                )
            return res

    def save_knowledge_items(self, items: List[KnowledgeItem], knowledge_version_id: str):
        """批量持久化知识条目"""
        if not items:
            return
        now_iso = current_time_iso()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO knowledge_items (
                    id, persona_id, knowledge_version_id, kind, statement,
                    conditions_json, exceptions_json, citations_json, source_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, knowledge_version_id, id) DO UPDATE SET
                    statement = excluded.statement,
                    conditions_json = excluded.conditions_json,
                    exceptions_json = excluded.exceptions_json,
                    citations_json = excluded.citations_json,
                    source_date = excluded.source_date
                """,
                [
                    (
                        it.id,
                        it.persona_id,
                        knowledge_version_id,
                        it.kind,
                        it.statement,
                        json.dumps(it.conditions, ensure_ascii=False),
                        json.dumps(it.exceptions, ensure_ascii=False),
                        json.dumps([c.model_dump() for c in it.citations], ensure_ascii=False),
                        it.source_date,
                        now_iso
                    )
                    for it in items
                ]
            )
            conn.commit()

    def get_knowledge_items(
        self,
        knowledge_version_id: str,
        persona_id: str,
        kind: Optional[str] = None
    ) -> List[KnowledgeItem]:
        """查询指定版本与分身下的结构化知识项，支持按类型过滤"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if kind is not None:
                rows = cur.execute(
                    """
                    SELECT * FROM knowledge_items
                    WHERE knowledge_version_id = ? AND persona_id = ? AND kind = ?
                    ORDER BY id ASC
                    """,
                    (knowledge_version_id, persona_id, kind)
                ).fetchall()
            else:
                rows = cur.execute(
                    """
                    SELECT * FROM knowledge_items
                    WHERE knowledge_version_id = ? AND persona_id = ?
                    ORDER BY id ASC
                    """,
                    (knowledge_version_id, persona_id)
                ).fetchall()

            res: List[KnowledgeItem] = []
            for r in rows:
                c_dicts = json.loads(r["citations_json"])
                citations = [Citation(**cd) for cd in c_dicts]
                item = KnowledgeItem(
                    id=r["id"],
                    persona_id=r["persona_id"],
                    knowledge_version=r["knowledge_version_id"],
                    kind=r["kind"],  # type: ignore
                    statement=r["statement"],
                    conditions=json.loads(r["conditions_json"]),
                    exceptions=json.loads(r["exceptions_json"]),
                    citations=citations,
                    source_date=r["source_date"]
                )
                res.append(item)
            return res

    def save_extraction_job(self, job: ExtractionJob):
        """保存或更新提炼任务状态"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO extraction_jobs (
                    id, persona_id, batch_id, knowledge_version_id, status,
                    created_at, updated_at, total_chunks, processed_chunks,
                    succeeded_chunks, partial_chunks, failed_chunks, error_message, chunk_errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    processed_chunks = excluded.processed_chunks,
                    succeeded_chunks = excluded.succeeded_chunks,
                    partial_chunks = excluded.partial_chunks,
                    failed_chunks = excluded.failed_chunks,
                    error_message = excluded.error_message,
                    chunk_errors_json = excluded.chunk_errors_json
                """,
                (
                    job.id, job.persona_id, job.batch_id, job.knowledge_version_id,
                    job.status, job.created_at, job.updated_at, job.total_chunks,
                    job.processed_chunks, job.succeeded_chunks,
                    getattr(job, "partial_chunks", 0),
                    job.failed_chunks,
                    job.error_message,
                    json.dumps(job.chunk_errors, ensure_ascii=False)
                )
            )
            conn.commit()

    def get_extraction_job(self, job_id: str, persona_id: str) -> Optional[ExtractionJob]:
        """获取提炼任务详情"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM extraction_jobs WHERE id = ? AND persona_id = ?",
                (job_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            keys = row.keys()
            partial_c = row["partial_chunks"] if "partial_chunks" in keys else 0
            return ExtractionJob(
                id=row["id"],
                persona_id=row["persona_id"],
                batch_id=row["batch_id"],
                knowledge_version_id=row["knowledge_version_id"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                total_chunks=row["total_chunks"],
                processed_chunks=row["processed_chunks"],
                succeeded_chunks=row["succeeded_chunks"],
                partial_chunks=partial_c,
                failed_chunks=row["failed_chunks"],
                error_message=row["error_message"],
                chunk_errors=json.loads(row["chunk_errors_json"])
            )

    def list_extraction_jobs(self, persona_id: str) -> List[ExtractionJob]:
        """获取分身名下的所有提炼任务列表，按创建时间倒序"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM extraction_jobs WHERE persona_id = ? ORDER BY created_at DESC",
                (persona_id,)
            ).fetchall()
            res = []
            for row in rows:
                keys = row.keys()
                partial_c = row["partial_chunks"] if "partial_chunks" in keys else 0
                res.append(ExtractionJob(
                    id=row["id"],
                    persona_id=row["persona_id"],
                    batch_id=row["batch_id"],
                    knowledge_version_id=row["knowledge_version_id"],
                    status=row["status"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    total_chunks=row["total_chunks"],
                    processed_chunks=row["processed_chunks"],
                    succeeded_chunks=row["succeeded_chunks"],
                    partial_chunks=partial_c,
                    failed_chunks=row["failed_chunks"],
                    error_message=row["error_message"],
                    chunk_errors=json.loads(row["chunk_errors_json"])
                ))
            return res

    # ==========================================
    # 阶段 3: 策略草案与策略版本持久化
    # ==========================================

    def save_strategy_draft(self, draft: StrategyDraft):
        """保存或更新策略草案（支持修订号与乐观锁校验）"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO strategy_drafts (
                    id, persona_id, knowledge_version, revision, status, draft_hash,
                    entry_rules_json, exit_rules_json, sizing_rules_json,
                    unresolved_fields_json, unsupported_conditions_json, source_refs_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, id) DO UPDATE SET
                    knowledge_version = excluded.knowledge_version,
                    revision = excluded.revision,
                    status = excluded.status,
                    draft_hash = excluded.draft_hash,
                    entry_rules_json = excluded.entry_rules_json,
                    exit_rules_json = excluded.exit_rules_json,
                    sizing_rules_json = excluded.sizing_rules_json,
                    unresolved_fields_json = excluded.unresolved_fields_json,
                    unsupported_conditions_json = excluded.unsupported_conditions_json,
                    source_refs_json = excluded.source_refs_json,
                    updated_at = excluded.updated_at
                """,
                (
                    draft.id, draft.persona_id, draft.knowledge_version, draft.revision,
                    draft.status, draft.draft_hash,
                    json.dumps([r.model_dump() for r in draft.entry_rules], ensure_ascii=False),
                    json.dumps(draft.exit_rules.model_dump(), ensure_ascii=False),
                    json.dumps(draft.sizing_rules.model_dump(), ensure_ascii=False),
                    json.dumps(draft.unresolved_fields, ensure_ascii=False),
                    json.dumps(draft.unsupported_conditions, ensure_ascii=False),
                    json.dumps(draft.source_refs, ensure_ascii=False),
                    draft.created_at, draft.updated_at
                )
            )
            conn.commit()

    def get_strategy_draft(self, draft_id: str, persona_id: str) -> Optional[StrategyDraft]:
        """获取指定分身名下的策略草案"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM strategy_drafts WHERE id = ? AND persona_id = ?",
                (draft_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_strategy_draft(row)

    def list_strategy_drafts(self, persona_id: str) -> List[StrategyDraft]:
        """列出指定分身名下的所有策略草案"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM strategy_drafts WHERE persona_id = ? ORDER BY created_at DESC",
                (persona_id,)
            ).fetchall()
            return [self._row_to_strategy_draft(r) for r in rows]

    def _row_to_strategy_draft(self, row: sqlite3.Row) -> StrategyDraft:
        entry_list = json.loads(row["entry_rules_json"])
        exit_dict = json.loads(row["exit_rules_json"])
        sizing_dict = json.loads(row["sizing_rules_json"])
        return StrategyDraft(
            schema_version=SCHEMA_VERSION,
            id=row["id"],
            persona_id=row["persona_id"],
            knowledge_version=row["knowledge_version"],
            revision=row["revision"],
            status=row["status"],
            draft_hash=row["draft_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            entry_rules=[StrategyCondition(**c) for c in entry_list],
            exit_rules=ExitRules(**exit_dict),
            sizing_rules=SizingRules(**sizing_dict),
            unresolved_fields=json.loads(row["unresolved_fields_json"]),
            unsupported_conditions=json.loads(row["unsupported_conditions_json"]),
            source_refs=json.loads(row["source_refs_json"])
        )

    def save_strategy_version(self, version: StrategyVersion):
        """保存不可变策略版本（一旦确认即不可篡改覆写）"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO strategy_versions (
                    id, persona_id, draft_id, draft_revision, draft_hash,
                    knowledge_version, status, entry_rules_json, exit_rules_json,
                    sizing_rules_json, source_refs_json, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, id) DO NOTHING
                """,
                (
                    version.id, version.persona_id, version.draft_id,
                    version.draft_revision, version.draft_hash,
                    version.knowledge_version, version.status,
                    json.dumps([r.model_dump() for r in version.entry_rules], ensure_ascii=False),
                    json.dumps(version.exit_rules.model_dump(), ensure_ascii=False),
                    json.dumps(version.sizing_rules.model_dump(), ensure_ascii=False),
                    json.dumps(version.source_refs, ensure_ascii=False),
                    version.confirmed_at
                )
            )
            conn.commit()

    def get_strategy_version(self, version_id: str, persona_id: str) -> Optional[StrategyVersion]:
        """获取已确认的策略版本详情"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM strategy_versions WHERE id = ? AND persona_id = ?",
                (version_id, persona_id)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_strategy_version(row)

    def list_strategy_versions(self, persona_id: str) -> List[StrategyVersion]:
        """列出指定分身名下的所有策略版本"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM strategy_versions WHERE persona_id = ? ORDER BY confirmed_at DESC",
                (persona_id,)
            ).fetchall()
            return [self._row_to_strategy_version(r) for r in rows]

    def _row_to_strategy_version(self, row: sqlite3.Row) -> StrategyVersion:
        entry_list = json.loads(row["entry_rules_json"])
        exit_dict = json.loads(row["exit_rules_json"])
        sizing_dict = json.loads(row["sizing_rules_json"])
        return StrategyVersion(
            schema_version=SCHEMA_VERSION,
            id=row["id"],
            persona_id=row["persona_id"],
            draft_id=row["draft_id"],
            draft_revision=row["draft_revision"],
            draft_hash=row["draft_hash"],
            knowledge_version=row["knowledge_version"],
            status=row["status"],
            confirmed_at=row["confirmed_at"],
            entry_rules=[StrategyCondition(**c) for c in entry_list],
            exit_rules=ExitRules(**exit_dict),
            sizing_rules=SizingRules(**sizing_dict),
            source_refs=json.loads(row["source_refs_json"])
        )

    def set_active_strategy(self, persona_id: str, strategy_id: str):
        """设置分身当前激活的策略版本"""
        now_iso = current_time_iso()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE personas
                SET active_strategy_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (strategy_id, now_iso, persona_id)
            )
            conn.commit()

    # ==========================================
    # Opportunity Discovery: ScanRun 扫描记录管理
    # ==========================================

    def save_scan_run(self, scan_run: ScanRun) -> ScanRun:
        """保存扫描执行记录"""
        candidates_json = json.dumps([c.model_dump() for c in scan_run.candidates], ensure_ascii=False)
        evaluations_json = json.dumps([e.model_dump() for e in scan_run.evaluations], ensure_ascii=False)
        exclusion_json = json.dumps(scan_run.exclusion_reasons, ensure_ascii=False)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO scan_runs (
                    id, persona_id, strategy_version_id, mode, status, as_of_date,
                    universe_target_count, universe_evaluated_count, universe_excluded_count,
                    candidates_count, candidates_json, evaluations_json, exclusion_reasons_json,
                    summary, execution_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run.id,
                    scan_run.persona_id,
                    scan_run.strategy_version_id,
                    scan_run.mode,
                    scan_run.status,
                    scan_run.as_of_date,
                    scan_run.universe_target_count,
                    scan_run.universe_evaluated_count,
                    scan_run.universe_excluded_count,
                    scan_run.candidates_count,
                    candidates_json,
                    evaluations_json,
                    exclusion_json,
                    scan_run.summary,
                    scan_run.execution_ms,
                    scan_run.created_at
                )
            )
            conn.commit()
        return scan_run

    def get_scan_run(self, run_id: str, persona_id: str) -> Optional[ScanRun]:
        """获取指定分身名下的单次扫描记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM scan_runs WHERE id = ? AND persona_id = ?",
                (run_id, persona_id)
            ).fetchone()
            if not row:
                return None
            return self._row_to_scan_run(row)

    def list_scan_runs(self, persona_id: str, limit: int = 50) -> List[ScanRun]:
        """列出指定分身名下的扫描记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM scan_runs WHERE persona_id = ? ORDER BY created_at DESC LIMIT ?",
                (persona_id, limit)
            ).fetchall()
            return [self._row_to_scan_run(r) for r in rows]

    def get_latest_scan_run(self, persona_id: str) -> Optional[ScanRun]:
        """获取指定分身最近一次扫描记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM scan_runs WHERE persona_id = ? ORDER BY created_at DESC LIMIT 1",
                (persona_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_scan_run(row)

    def _row_to_scan_run(self, row: sqlite3.Row) -> ScanRun:
        cands_data = json.loads(row["candidates_json"])
        evals_data = json.loads(row["evaluations_json"])
        excl_data = json.loads(row["exclusion_reasons_json"])
        return ScanRun(
            schema_version=SCHEMA_VERSION,
            id=row["id"],
            persona_id=row["persona_id"],
            strategy_version_id=row["strategy_version_id"],
            mode=row["mode"],
            status=row["status"],
            as_of_date=row["as_of_date"],
            universe_target_count=row["universe_target_count"],
            universe_evaluated_count=row["universe_evaluated_count"],
            universe_excluded_count=row["universe_excluded_count"],
            candidates_count=row["candidates_count"],
            candidates=[ScanCandidate(**c) for c in cands_data],
            evaluations=[StockEvaluation(**e) for e in evals_data],
            exclusion_reasons=excl_data,
            summary=row["summary"],
            execution_ms=row["execution_ms"],
            created_at=row["created_at"]
        )


