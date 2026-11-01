# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/router.py
V7 博主实验室可复用 APIRouter (阶段 1 核心)
包含：
1. 统一路由前缀: /api/blogger-lab
2. 局部本机回环门禁 (verify_blogger_lab_local_access)
3. 24 个全套业务端点（分身、批次、提炼监控、引用查看、策略确认、扫描、回放撮合与账户）
4. 统一响应格式与运行时状态隔离
"""

from __future__ import annotations
import os
import sys
import uuid
import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, List

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from fastapi import APIRouter, Request, UploadFile, File, Form, Query, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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
    LabAccount,
    LabPosition,
    LabFill,
    PaperCycle
)
from blogger_lab.service import BloggerLabService
from blogger_lab.llm import BloggerLabLLMAdapter
from blogger_lab.replay import (
    list_replay_scenarios,
    get_replay_scenario,
    execute_replay_step,
    REPLAY_SCENARIOS
)
from blogger_lab.store import current_time_iso
from blogger_lab.runtime import BloggerLabRuntime


# ==========================================
# 统一响应包装
# ==========================================

def ok_response(data: Any = None, request_id: Optional[str] = None) -> JSONResponse:
    """统一成功响应包装"""
    req_id = request_id or f"req_{uuid.uuid4().hex[:8]}"
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "data": data,
            "errors": [],
            "request_id": req_id
        }
    )


def error_response(code: str, message: str, status_code: int = 400, request_id: Optional[str] = None) -> JSONResponse:
    """统一可读错误响应包装"""
    req_id = request_id or f"req_{uuid.uuid4().hex[:8]}"
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "data": None,
            "errors": [{"code": code, "message": message}],
            "request_id": req_id
        }
    )


# ==========================================
# 本机访问门禁 (Local Loopback Gate)
# ==========================================

async def verify_blogger_lab_local_access(request: Request) -> None:
    """
    实验 API 局部本机门禁依赖项 (阶段 1 R2 严控)：
    1. 实验路径局部防代理穿透：显式拒绝代理转发标头 (Forwarded, X-Forwarded-For, X-Real-IP)；
    2. 物理套接字来源校验：真实客户端 IP 必须属于回环地址 ('127.0.0.1', '::1', 'localhost', 'testclient')；
    3. Host 请求头校验：必须存在且主机名属于合法本地主机名 ('127.0.0.1', 'localhost', '[::1]', '::1', 'testserver')；
    4. Origin 严格同源校验：
       - 若存在且为 'null'，刚性拒绝 (403)；
       - scheme、hostname 与有效 port 必须与当前服务端实际 URL 完全一致；
       - 取消 testserver 跨端口跨主机豁免；不同协议、主机或端口一律拒绝 (403)；
    5. Referer 校验（当 Origin 缺失时）：
       - 若存在，按相同 scheme + hostname + port 规则严格同源校验；
    6. 无 Origin/Referer 时：
       - 仅在客户端为合法本机且未检测到代理转发标头时放行（允许本地 CLI、脚本及单元测试工具访问）。
    """
    # 1. 实验路径局部防代理穿透：拒绝显式代理转发标记
    for hdr in ("x-forwarded-for", "forwarded", "x-real-ip"):
        if request.headers.get(hdr):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"检测到代理转发标记 '{hdr}'，博主实验室严禁经由代理转发访问"
            )

    # 2. 物理客户端 IP 严格校验（底层连接 IP）
    client_host = (request.client.host if request.client else "") or ""
    allowed_clients = ("127.0.0.1", "::1", "localhost", "testclient")
    if client_host not in allowed_clients:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="博主实验室仅允许本机回环 (127.0.0.1) 访问，拒绝远端连接"
        )

    # 3. Host 请求头校验
    host_hdr = (request.headers.get("host") or "").strip()
    if not host_hdr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少 Host 请求头，访问被拒绝"
        )
    parsed_host = urllib.parse.urlsplit(f"//{host_hdr}")
    host_name = (parsed_host.hostname or "").lower()
    allowed_hosts = ("127.0.0.1", "localhost", "[::1]", "::1", "testserver")
    if host_name not in allowed_hosts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"非法 Host 主机名 '{host_name}'，仅允许本地访问"
        )

    # 计算当前服务端的有效协议、主机名与端口
    server_scheme = (request.url.scheme or "http").lower()
    server_host = (request.url.hostname or host_name).lower()
    server_port = request.url.port or parsed_host.port or (443 if server_scheme == "https" else 80)

    def _validate_origin_like(target_url: str, header_name: str) -> None:
        target_clean = target_url.strip()
        if not target_clean or target_clean.lower() == "null":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"拒绝 {header_name}: null 跨源请求"
            )
        try:
            parsed = urllib.parse.urlsplit(target_clean)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"非法 {header_name} 格式"
            )

        scheme = (parsed.scheme or "").lower()
        if scheme != server_scheme:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{header_name} 协议 ({scheme}) 与服务协议 ({server_scheme}) 不匹配"
            )

        host = (parsed.hostname or "").lower()
        if host != server_host:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{header_name} 主机 ({host}) 与服务主机 ({server_host}) 不匹配"
            )

        port = parsed.port or (443 if scheme == "https" else 80)
        if port != server_port:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{header_name} 端口 ({port}) 与服务端口 ({server_port}) 不匹配"
            )

    # 4. Origin 校验
    origin = request.headers.get("origin")
    if origin is not None:
        _validate_origin_like(origin, "Origin")
    # 5. Referer 校验（当 Origin 缺失但 Referer 存在时）
    elif request.headers.get("referer"):
        referer = request.headers.get("referer")
        _validate_origin_like(referer, "Referer")

# ==========================================
# Pydantic 请求模型定义
# ==========================================

class CreatePersonaRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    persona_id: Optional[str] = None


class ExtractJobRequest(BaseModel):
    persona_id: str
    model: Optional[str] = None
    use_mock: Optional[bool] = False


class RetryJobRequest(BaseModel):
    persona_id: str
    use_mock: Optional[bool] = False


class CreateStrategyDraftRequest(BaseModel):
    knowledge_version_id: str


class UpdateStrategyDraftRequest(BaseModel):
    persona_id: str
    expected_revision: int
    entry_rules: Optional[List[Dict[str, Any]]] = None
    exit_rules: Optional[Dict[str, Any]] = None
    sizing_rules: Optional[Dict[str, Any]] = None


class ConfirmStrategyDraftRequest(BaseModel):
    persona_id: str
    expected_revision: int
    expected_hash: str


class RunScanRequest(BaseModel):
    strategy_version_id: Optional[str] = None
    mode: str = "REPLAY"
    as_of_date: Optional[str] = "20261030"
    universe_symbols: Optional[List[str]] = None
    universe_limit: int = 50


class ReplayStepRequest(BaseModel):
    scenario_id: str = "standard_t1_cycle"
    step_index: int
    candidate_id: Optional[str] = None
    request_key: str


class PaperCycleRequest(BaseModel):
    request_key: str
    mode: str = "REPLAY"
    candidate_id: Optional[str] = None
    as_of_date: Optional[str] = None
    scenario_id: Optional[str] = "standard_t1_cycle"
    step_index: Optional[int] = None


# ==========================================
# APIRouter 构造工厂
# ==========================================

def create_blogger_lab_router(runtime: BloggerLabRuntime) -> APIRouter:
    """
    构造可复用的博主实验室 APIRouter。
    依赖显式注入的 BloggerLabRuntime，并自动挂载本机回环访问门禁。
    """
    router = APIRouter(
        prefix="/api/blogger-lab",
        tags=["Blogger Lab"],
        dependencies=[Depends(verify_blogger_lab_local_access)]
    )

    def _ensure_service() -> BloggerLabService:
        """确保运行时就绪并返回 service 实例"""
        if runtime.status == "UNINITIALIZED":
            runtime.start()
        if runtime.status != "READY" or not runtime.service:
            err = runtime.error_message or f"运行时状态为 {runtime.status}"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"博主实验室服务不可用: {err}"
            )
        return runtime.service

    # ==========================================
    # 1. 分身管理与资料批次导入
    # ==========================================

    @router.get("/personas")
    def list_personas():
        svc = _ensure_service()
        personas = svc.list_personas()
        return ok_response([p.model_dump() for p in personas])

    @router.post("/personas")
    def create_persona(payload: CreatePersonaRequest):
        svc = _ensure_service()
        try:
            p_id = payload.persona_id or f"p_{uuid.uuid4().hex[:6]}"
            persona = svc.create_persona(
                persona_id=p_id,
                name=payload.name,
                description=payload.description or ""
            )
            return ok_response(persona.model_dump())
        except ValueError as ve:
            return error_response("PERSONA_ALREADY_EXISTS", str(ve), status_code=409)
        except Exception as e:
            return error_response("CREATE_PERSONA_FAILED", str(e), status_code=400)

    @router.get("/personas/{persona_id}")
    def get_persona(persona_id: str):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)
        return ok_response(persona.model_dump())

    @router.post("/personas/{persona_id}/batches")
    async def upload_corpus_batch(
        persona_id: str,
        files: List[UploadFile] = File(...),
        source_date: Optional[str] = Form(None)
    ):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        if not files:
            return error_response("NO_FILES_UPLOADED", "请上传至少一个有效的语料文件", status_code=400)

        import tempfile
        import shutil
        temp_dir = tempfile.mkdtemp(prefix="blogger_upload_")
        temp_files: List[Path] = []
        try:
            for file in files:
                out_path = Path(temp_dir) / (file.filename or "upload.txt")
                with open(out_path, "wb") as f_out:
                    f_out.write(await file.read())
                temp_files.append(out_path)

            batch = svc.import_corpus_files(
                persona_id=persona_id,
                files=temp_files
            )
            return ok_response({
                "batch": batch.model_dump(),
                "sources_count": len(batch.source_ids),
                "chunks_count": batch.total_chunks
            })
        except ValueError as ve:
            return error_response("INGESTION_FAILED", str(ve), status_code=400)
        except Exception as e:
            return error_response("SERVER_ERROR", f"上传语料失败: {e}", status_code=500)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @router.get("/personas/{persona_id}/batches")
    def list_batches(persona_id: str):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)
        batches = svc.store.list_corpus_batches(persona_id)
        return ok_response([b.model_dump() for b in batches])

    @router.get("/personas/{persona_id}/batches/{batch_id}")
    def get_batch(persona_id: str, batch_id: str):
        svc = _ensure_service()
        batch = svc.store.get_corpus_batch(batch_id, persona_id)
        if not batch:
            return error_response("BATCH_NOT_FOUND", f"批次 '{batch_id}' 不存在或不属于分身 '{persona_id}'", status_code=404)
        return ok_response(batch.model_dump())

    @router.get("/personas/{persona_id}/jobs")
    def list_jobs(persona_id: str):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)
        jobs = svc.store.list_extraction_jobs(persona_id)
        return ok_response([j.model_dump() for j in jobs])

    @router.get("/personas/{persona_id}/overview")
    def get_persona_overview(persona_id: str):
        svc = _ensure_service()
        try:
            overview = svc.get_persona_overview(persona_id)
            return ok_response(overview)
        except ValueError as ve:
            return error_response("PERSONA_NOT_FOUND", str(ve), status_code=404)
        except Exception as e:
            return error_response("GET_OVERVIEW_FAILED", str(e), status_code=500)

    # ==========================================
    # 2. 全文提炼任务监控与引文片段查看
    # ==========================================

    def _run_extraction_worker(
        persona_id: str,
        batch_id: str,
        job_id: str,
        version_id: str,
        use_mock: bool = False
    ):
        """后台单工作器执行提取函数"""
        svc = runtime.service
        if not svc:
            return
        try:
            if use_mock:
                def _mock_provider(chunk_text, src_id, chk_id):
                    excerpt = chunk_text[:25].strip()
                    payload = {
                        "schema_version": "V7_BLOGGER_LAB_V1.0",
                        "items": [
                            {
                                "id": f"ITEM_{uuid.uuid4().hex[:6]}",
                                "kind": "ENTRY_RULE",
                                "statement": "突破20日均线买入",
                                "conditions": ["突破20日均线"],
                                "exceptions": [],
                                "citations": [{"quote": excerpt}],
                                "source_date": None
                            },
                            {
                                "id": f"ITEM_{uuid.uuid4().hex[:6]}",
                                "kind": "EXIT_RULE",
                                "statement": "跌破均线止损或达到目标止盈",
                                "conditions": ["跌破成本价3.5%止损", "浮盈达到8.5%止盈"],
                                "exceptions": [],
                                "citations": [{"quote": excerpt}],
                                "source_date": None
                            },
                            {
                                "id": f"ITEM_{uuid.uuid4().hex[:6]}",
                                "kind": "POSITION_RISK",
                                "statement": "单标的仓位上限50%",
                                "conditions": ["单笔仓位50%"],
                                "exceptions": [],
                                "citations": [{"quote": excerpt}],
                                "source_date": None
                            }
                        ],
                        "unresolved_notes": []
                    }
                    return json.dumps(payload, ensure_ascii=False), {"provider": "mock"}

                llm_adapter = BloggerLabLLMAdapter(mock_provider=_mock_provider)
            else:
                llm_adapter = BloggerLabLLMAdapter()

            svc.knowledge_engine.start_batch_extraction(
                persona_id=persona_id,
                batch_id=batch_id,
                llm_adapter=llm_adapter,
                version_id=version_id,
                job_id=job_id
            )
        except Exception as e:
            job = svc.store.get_extraction_job(job_id, persona_id)
            if job:
                job.status = "FAILED"
                job.error_message = str(e)
                job.updated_at = current_time_iso()
                svc.store.save_extraction_job(job)

    @router.post("/batches/{batch_id}/extract")
    def start_extraction(batch_id: str, payload: ExtractJobRequest):
        svc = _ensure_service()
        persona_id = payload.persona_id
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        batch = svc.store.get_corpus_batch(batch_id, persona_id)
        if not batch:
            return error_response("BATCH_NOT_FOUND", f"批次 '{batch_id}' 不存在或不属于分身 '{persona_id}'", status_code=404)

        if batch.status != "READY":
            return error_response("BATCH_NOT_READY", f"批次状态为 '{batch.status}'，不可启动知识提炼", status_code=400)

        # 检查是否已有同批任务（RUNNING 任务或已有完成任务，同批重复启动返回已有任务）
        with sqlite3.connect(svc.store.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            existing_active = cur.execute(
                "SELECT * FROM extraction_jobs WHERE batch_id = ? AND persona_id = ? AND status = 'RUNNING'",
                (batch_id, persona_id)
            ).fetchone()
            if not existing_active:
                existing_active = cur.execute(
                    "SELECT * FROM extraction_jobs WHERE batch_id = ? AND persona_id = ? AND status IN ('SUCCEEDED', 'COMPLETED') ORDER BY created_at DESC LIMIT 1",
                    (batch_id, persona_id)
                ).fetchone()

            if existing_active:
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "status": "ok",
                        "data": {
                            "job_id": existing_active["id"],
                            "status": existing_active["status"],
                            "knowledge_version_id": existing_active["knowledge_version_id"],
                            "message": "当前批次已有提炼任务，返回现有任务"
                        },
                        "errors": [],
                        "request_id": f"req_{uuid.uuid4().hex[:8]}"
                    }
                )

        chunks = svc.store.get_chunks_by_batch(batch_id, persona_id)
        if not chunks:
            return error_response("NO_CHUNKS", f"批次 '{batch_id}' 下未发现任何有效分块", status_code=400)

        now_iso = current_time_iso()
        existing_versions = svc.store.list_knowledge_versions(persona_id)
        v_num = len(existing_versions) + 1
        knowledge_version_id = f"kv_{persona_id}_{v_num:03d}"

        raw_job_id = f"job_{uuid.uuid4().hex[:8]}"
        job = ExtractionJob(
            id=raw_job_id,
            persona_id=persona_id,
            batch_id=batch_id,
            knowledge_version_id=knowledge_version_id,
            total_chunks=len(chunks),
            processed_chunks=0,
            succeeded_chunks=0,
            partial_chunks=0,
            failed_chunks=0,
            status="RUNNING",
            error_message="",
            chunk_errors={},
            created_at=now_iso,
            updated_at=now_iso
        )
        svc.store.save_extraction_job(job)

        # 异步提交到单工作器线程池
        if not runtime.executor:
            runtime.executor = LabThreadPoolExecutor(runtime, max_workers=1, thread_name_prefix="lab_worker")

        runtime.executor.submit(
            _run_extraction_worker,
            persona_id=persona_id,
            batch_id=batch_id,
            job_id=raw_job_id,
            version_id=knowledge_version_id,
            use_mock=bool(payload.use_mock)
        )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "ok",
                "data": {
                    "job_id": raw_job_id,
                    "batch_id": batch_id,
                    "knowledge_version_id": knowledge_version_id,
                    "status": "RUNNING",
                    "total_chunks": len(chunks)
                },
                "errors": [],
                "request_id": f"req_{uuid.uuid4().hex[:8]}"
            }
        )

    @router.get("/jobs/{job_id}")
    def get_job_status(job_id: str, persona_id: Optional[str] = Query(None)):
        svc = _ensure_service()
        job = svc.get_extraction_job(job_id, persona_id)
        if not job:
            return error_response("JOB_NOT_FOUND", f"任务 '{job_id}' 不存在", status_code=404)
        return ok_response({"job": job.model_dump()})

    @router.post("/jobs/{job_id}/retry")
    def retry_job(job_id: str, payload: RetryJobRequest):
        svc = _ensure_service()
        persona_id = payload.persona_id
        job = svc.get_extraction_job(job_id, persona_id)
        if not job:
            return error_response("JOB_NOT_FOUND", f"任务 '{job_id}' 不存在", status_code=404)

        if job.status not in ("FAILED", "PARTIAL", "INTERRUPTED"):
            return error_response("JOB_CANNOT_RETRY", f"任务状态为 '{job.status}'，无需或不支持重试", status_code=400)

        def _run_retry_worker():
            try:
                if payload.use_mock:
                    def _mock_provider(chunk_text, src_id, chk_id):
                        excerpt = chunk_text[:25].strip()
                        payload_data = {
                            "schema_version": "V7_BLOGGER_LAB_V1.0",
                            "items": [
                                {
                                    "id": f"ITEM_RETRY_{uuid.uuid4().hex[:6]}",
                                    "kind": "ENTRY_RULE",
                                    "statement": "重试突破均线买入",
                                    "conditions": ["突破20日均线"],
                                    "exceptions": [],
                                    "citations": [{"quote": excerpt}],
                                    "source_date": None
                                }
                            ],
                            "unresolved_notes": []
                        }
                        return json.dumps(payload_data, ensure_ascii=False), {"provider": "mock_retry"}
                    llm = BloggerLabLLMAdapter(mock_provider=_mock_provider)
                else:
                    llm = BloggerLabLLMAdapter()

                retry_kv, retry_job = svc.retry_knowledge_extraction(
                    persona_id=persona_id,
                    batch_id=job.batch_id,
                    knowledge_version_id=job.knowledge_version_id,
                    llm_adapter=llm
                )
                cur_job = svc.get_extraction_job(job_id, persona_id)
                if cur_job:
                    cur_job.status = retry_job.status
                    cur_job.processed_chunks = retry_job.processed_chunks
                    cur_job.succeeded_chunks = retry_job.succeeded_chunks
                    cur_job.partial_chunks = retry_job.partial_chunks
                    cur_job.failed_chunks = retry_job.failed_chunks
                    cur_job.error_message = retry_job.error_message
                    cur_job.chunk_errors = retry_job.chunk_errors
                    cur_job.updated_at = current_time_iso()
                    svc.store.save_extraction_job(cur_job)
            except Exception as e:
                cur_job = svc.get_extraction_job(job_id, persona_id)
                if cur_job:
                    cur_job.status = "FAILED"
                    cur_job.error_message = f"重试异常: {e}"
                    cur_job.updated_at = current_time_iso()
                    svc.store.save_extraction_job(cur_job)

        job.status = "RUNNING"
        job.updated_at = current_time_iso()
        svc.store.save_extraction_job(job)

        if not runtime.executor:
            runtime.executor = LabThreadPoolExecutor(runtime, max_workers=1, thread_name_prefix="lab_worker")
        runtime.executor.submit(_run_retry_worker)

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "ok",
                "data": {"job_id": job.id, "status": "RUNNING", "message": "重试任务已提交执行"},
                "errors": [],
                "request_id": f"req_{uuid.uuid4().hex[:8]}"
            }
        )

    @router.get("/sources/{source_id}/excerpt")
    def get_source_excerpt(
        source_id: str,
        start_char: int = Query(..., ge=0),
        end_char: int = Query(..., ge=0),
        persona_id: Optional[str] = Query(None)
    ):
        svc = _ensure_service()
        try:
            p_id = persona_id or ""
            res = svc.get_source_text_excerpt(
                source_id=source_id,
                persona_id=p_id,
                start_char=start_char,
                end_char=end_char
            )
            return ok_response(res)
        except ValueError as ve:
            return error_response("INVALID_RANGE", str(ve), status_code=400)
        except Exception as e:
            return error_response("SLICE_ERROR", str(e), status_code=500)

    @router.get("/personas/{persona_id}/knowledge")
    def list_knowledge(persona_id: str, version_id: Optional[str] = Query(None)):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        if not version_id:
            versions = svc.store.list_knowledge_versions(persona_id)
            if not versions:
                return ok_response({"versions": [], "items": []})
            latest = versions[-1]
            items = svc.store.get_knowledge_items(latest.id, persona_id)
            return ok_response({
                "versions": [v.model_dump() for v in versions],
                "active_version": latest.model_dump(),
                "items": [it.model_dump() for it in items]
            })

        kv = svc.store.get_knowledge_version(version_id, persona_id)
        if not kv:
            return error_response("VERSION_NOT_FOUND", f"知识版本 '{version_id}' 不存在", status_code=404)
        items = svc.store.get_knowledge_items(version_id, persona_id)
        return ok_response({
            "version": kv.model_dump(),
            "items": [it.model_dump() for it in items]
        })

    # ==========================================
    # 3. 策略草案与确认
    # ==========================================

    @router.post("/personas/{persona_id}/strategy-drafts")
    def create_strategy_draft(persona_id: str, payload: CreateStrategyDraftRequest):
        svc = _ensure_service()
        try:
            draft = svc.generate_strategy_draft(
                persona_id=persona_id,
                knowledge_version_id=payload.knowledge_version_id
            )
            return ok_response(draft.model_dump())
        except ValueError as ve:
            return error_response("CREATE_DRAFT_FAILED", str(ve), status_code=400)
        except Exception as e:
            return error_response("SERVER_ERROR", f"生成策略草案失败: {e}", status_code=500)

    @router.get("/strategy-drafts/{draft_id}")
    def get_strategy_draft(draft_id: str, persona_id: Optional[str] = Query(None)):
        svc = _ensure_service()
        draft = svc.store.get_strategy_draft(draft_id, persona_id)
        if not draft:
            return error_response("DRAFT_NOT_FOUND", f"策略草案 '{draft_id}' 不存在", status_code=404)
        return ok_response(draft.model_dump())

    @router.put("/strategy-drafts/{draft_id}")
    def update_strategy_draft(draft_id: str, payload: UpdateStrategyDraftRequest):
        svc = _ensure_service()
        try:
            parsed_entry = [StrategyCondition(**r) if isinstance(r, dict) else r for r in payload.entry_rules] if payload.entry_rules is not None else None
            parsed_exit = ExitRules(**payload.exit_rules) if (payload.exit_rules and isinstance(payload.exit_rules, dict)) else payload.exit_rules
            parsed_sizing = SizingRules(**payload.sizing_rules) if (payload.sizing_rules and isinstance(payload.sizing_rules, dict)) else payload.sizing_rules

            updated = svc.update_strategy_draft(
                draft_id=draft_id,
                persona_id=payload.persona_id,
                expected_revision=payload.expected_revision,
                entry_rules=parsed_entry,
                exit_rules=parsed_exit,
                sizing_rules=parsed_sizing
            )
            return ok_response(updated.model_dump())
        except ValueError as ve:
            msg = str(ve)
            if "冲突" in msg or "Conflict" in msg or "版本不一致" in msg:
                return error_response("REVISION_CONFLICT", msg, status_code=409)
            return error_response("UPDATE_DRAFT_FAILED", msg, status_code=400)
        except Exception as e:
            return error_response("SERVER_ERROR", f"修改草案失败: {e}", status_code=500)

    @router.post("/strategy-drafts/{draft_id}/confirm")
    def confirm_strategy_draft(draft_id: str, payload: ConfirmStrategyDraftRequest):
        svc = _ensure_service()
        try:
            version = svc.confirm_strategy_draft(
                draft_id=draft_id,
                persona_id=payload.persona_id,
                expected_revision=payload.expected_revision,
                expected_hash=payload.expected_hash
            )
            return ok_response(version.model_dump())
        except ValueError as ve:
            msg = str(ve)
            if "冲突" in msg or "不一致" in msg or "Conflict" in msg or "哈希" in msg:
                return error_response("CONFIRM_CONFLICT", msg, status_code=409)
            if "UNRESOLVED" in msg or "未决" in msg or "未确认" in msg:
                return error_response("UNRESOLVED_PARAMETERS", msg, status_code=422)
            return error_response("CONFIRM_FAILED", msg, status_code=400)
        except Exception as e:
            return error_response("SERVER_ERROR", f"确认策略失败: {e}", status_code=500)

    @router.get("/personas/{persona_id}/strategies")
    def list_strategies(persona_id: str):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)
        drafts = svc.store.list_strategy_drafts(persona_id)
        versions = svc.store.list_strategy_versions(persona_id)
        return ok_response({
            "drafts": [d.model_dump() for d in drafts],
            "versions": [v.model_dump() for v in versions]
        })

    # ==========================================
    # 4. 今日机会扫描与证据解释
    # ==========================================

    @router.post("/personas/{persona_id}/scans")
    def run_strategy_scan(persona_id: str, payload: RunScanRequest):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        try:
            scan_run = svc.run_opportunity_scan(
                persona_id=persona_id,
                strategy_version_id=payload.strategy_version_id,
                mode=payload.mode,
                as_of_date=payload.as_of_date,
                universe_symbols=payload.universe_symbols,
                universe_limit=payload.universe_limit
            )
            return ok_response(scan_run.model_dump())
        except ValueError as ve:
            return error_response("SCAN_FAILED", str(ve), status_code=400)
        except Exception as e:
            return error_response("SCAN_ERROR", f"执行机会扫描异常: {e}", status_code=500)

    @router.get("/runs/{run_id}")
    def get_scan_run(run_id: str, persona_id: Optional[str] = Query(None)):
        svc = _ensure_service()
        scan_run = svc.store.get_scan_run(run_id, persona_id)
        if not scan_run:
            return error_response("RUN_NOT_FOUND", f"扫描任务 '{run_id}' 不存在", status_code=404)
        return ok_response(scan_run.model_dump())

    # ==========================================
    # 5. 受控回放与模拟账户
    # ==========================================

    @router.get("/replay-scenarios")
    def get_scenarios():
        scenarios = list_replay_scenarios()
        return ok_response(scenarios)

    @router.post("/personas/{persona_id}/replay-step")
    def run_replay_step_api(persona_id: str, payload: ReplayStepRequest):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        try:
            cycle = execute_replay_step(
                service=svc,
                persona_id=persona_id,
                scenario_id=payload.scenario_id,
                step_index=payload.step_index,
                candidate_id=payload.candidate_id,
                request_key=payload.request_key
            )
            acc = svc.get_paper_account(persona_id, mode="REPLAY")
            return ok_response({
                "cycle": cycle.model_dump(),
                "account": acc.model_dump()
            })
        except ValueError as ve:
            msg = str(ve)
            if "幂等冲突" in msg:
                return error_response("IDEMPOTENCY_CONFLICT", msg, status_code=409)
            return error_response("REPLAY_STEP_FAILED", msg, status_code=400)
        except Exception as e:
            return error_response("REPLAY_STEP_ERROR", f"执行回放步骤失败: {e}", status_code=500)

    @router.post("/personas/{persona_id}/paper-cycles")
    def run_paper_cycle_general(persona_id: str, payload: PaperCycleRequest):
        svc = _ensure_service()
        if payload.mode.upper() == "LIVE":
            return error_response("LIVE_MODE_BLOCKED", "首版受控环境已安全禁用 LIVE 实时撮合，仅支持受控 REPLAY 回放", status_code=400)

        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        if payload.step_index is not None:
            try:
                cycle = execute_replay_step(
                    service=svc,
                    persona_id=persona_id,
                    scenario_id=payload.scenario_id or "standard_t1_cycle",
                    step_index=payload.step_index,
                    candidate_id=payload.candidate_id,
                    request_key=payload.request_key
                )
                acc = svc.get_paper_account(persona_id, mode="REPLAY")
                return ok_response({
                    "cycle": cycle.model_dump(),
                    "account": acc.model_dump()
                })
            except ValueError as ve:
                msg = str(ve)
                if "幂等冲突" in msg:
                    return error_response("IDEMPOTENCY_CONFLICT", msg, status_code=409)
                return error_response("PAPER_CYCLE_FAILED", msg, status_code=400)

        try:
            cycle = svc.run_paper_cycle(
                persona_id=persona_id,
                request_key=payload.request_key,
                mode="REPLAY",
                candidate_id=payload.candidate_id,
                as_of_date=payload.as_of_date
            )
            acc = svc.get_paper_account(persona_id, mode="REPLAY")
            return ok_response({
                "cycle": cycle.model_dump(),
                "account": acc.model_dump()
            })
        except ValueError as ve:
            msg = str(ve)
            if "幂等冲突" in msg:
                return error_response("IDEMPOTENCY_CONFLICT", msg, status_code=409)
            return error_response("PAPER_CYCLE_FAILED", msg, status_code=400)

    @router.get("/personas/{persona_id}/paper")
    def get_paper_status(
        persona_id: str,
        mode: str = Query("REPLAY")
    ):
        svc = _ensure_service()
        persona = svc.get_persona(persona_id)
        if not persona:
            return error_response("PERSONA_NOT_FOUND", f"分身 '{persona_id}' 不存在", status_code=404)

        mode_clean = mode.upper()
        account = svc.get_paper_account(persona_id, mode=mode_clean)
        cycles = svc.list_paper_cycles(persona_id, mode=mode_clean, limit=50)
        fills = svc.list_paper_fills(persona_id, mode=mode_clean, limit=50)
        return ok_response({
            "account": account.model_dump(),
            "positions": [p.model_dump() for p in account.positions.values()],
            "cycles": [c.model_dump() for c in cycles],
            "fills": [f.model_dump() for f in fills]
        })

    return router
