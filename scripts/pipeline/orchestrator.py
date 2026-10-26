# -*- coding: utf-8 -*-
"""
scripts/pipeline/orchestrator.py
内阁 · Orchestrator（总控编排器）

职责：
1. 开议：新建流水线记录（内阁档册 pipeline_state.json）
2. 按依赖顺序执行各环节，每环节记录时间戳 / 耗时 / 状态 / 产物：
     通政司（检索情报）→ 驿传（画像路由）→ 六部（博主执行）→ 起居注（统一落盘）→ 都察院（弹劾校验）
3. 环节失败自动复核（重试 1 次）
4. 都察院弹章写盘；起居注将告警与档册摘要并入 data.json（前端体检数据源）

阶段 2 细粒度编排：每环节独立落盘产物
  - 通政司 → data/pipeline/news_brief.json（通政奏报）
  - 驿传   → data/pipeline/dispatch_log.json（驿传行单）
  - 六部   → data/pipeline/liubu_report.json（六部奏本）
  - 起居注 → workbench/data.json + index.html（卷宗库）
  - 都察院 → data/pipeline/inspection_report.json（弹章）
"""

import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIPE_DIR = os.path.join(BASE_DIR, 'scripts', 'pipeline')
SCRIPTS_DIR = os.path.join(BASE_DIR, 'scripts')
sys.path.insert(0, PIPE_DIR)
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, BASE_DIR)

import pipeline_state as ps
import inspector


def _run_stage_with_retry(pipe, stage_name, fn, *args, **kwargs):
    """
    通用环节执行器：开跑→执行→收工，失败复核 1 次。
    返回 (ok, detail, output)
    """
    ps.start_stage(pipe, stage_name)
    for attempt in (0, 1):
        try:
            detail, output = fn(*args, **kwargs)
            ps.finish_stage(pipe, stage_name, 'DONE', detail=detail, output=output)
            return True, detail, output
        except Exception as e:
            err = f'{e}\n{traceback.format_exc()[:300]}'
            pipe['stages'][stage_name]['retried'] = attempt + 1
            if attempt == 1:
                ps.finish_stage(pipe, stage_name, 'FAILED', detail=err)
                return False, err, None
            print(f'[内阁] {stage_name} 首次失败（{e}），复核中...')
    return False, 'unknown', None


def _stage_tongzhengsi():
    """通政司：检索情报并落盘通政奏报。"""
    from news_officer import gather_intelligence
    brief, path = gather_intelligence()
    return f'通政奏报落盘（新闻 {brief.get("news_count", 0)} 条）', path


def _stage_yichuan():
    """驿传：画像路由并落盘驿传行单。"""
    from dispatcher import dispatch
    dlog, path = dispatch()
    return f'驿传行单落盘（6 博主订单已生成）', path


def _stage_liubu():
    """六部：全池扫描 + 题材关联，落盘六部奏本。"""
    from blogger_workers import execute_liubu
    report, path = execute_liubu()
    n_pick = sum(1 for p in report['picks'] if p.get('triggered'))
    return f'六部奏本落盘（6 博主选股，{n_pick} 只触发）', path


def _stage_qijuzhu(pipe, brief, dlog, liubu, rep):
    """起居注：统一落盘 data.json + 嵌入 index.html。返回 (ok, detail, output)。"""
    from persist import record, _load_data
    data = _load_data()
    _data, embed_ok = record(data, brief=brief, dispatch_log=dlog,
                             liubu_report=liubu, report=rep, pipe=pipe)
    return True, f'卷宗库更新完成（内嵌 {"成功" if embed_ok else "失败"}）', 'workbench/data.json'


def run_full_pipeline(trigger='automation'):
    """
    全链执行：内阁开议 → 通政司 → 驿传 → 六部 → 起居注 → 都察院 → 收工归档。

    返回 (overall, report)
    """
    state, pipe = ps.start_pipeline(trigger)
    pipe_id = pipe['pipeline_id']
    brief = dlog = liubu = rep = None

    # 1. 通政司
    ok, _, brief_path = _run_stage_with_retry(pipe, 'tongzhengsi', _stage_tongzhengsi)
    if ok:
        from news_officer import load_brief
        brief = load_brief()
    else:
        ps.finish_pipeline(pipe, 'FAILED')
        print(f'[内阁] 通政司失败，流水线 {pipe_id} 终止')
        return 'FAILED', None

    # 2. 驿传
    ok, _, _ = _run_stage_with_retry(pipe, 'yichuan', _stage_yichuan)
    if ok:
        from dispatcher import load_dispatch
        dlog = load_dispatch()
    else:
        ps.finish_pipeline(pipe, 'FAILED')
        print(f'[内阁] 驿传失败，流水线 {pipe_id} 终止')
        return 'FAILED', None

    # 3. 六部
    ok, _, _ = _run_stage_with_retry(pipe, 'liubu', _stage_liubu)
    if ok:
        from blogger_workers import load_liubu
        liubu = load_liubu()
    else:
        ps.finish_pipeline(pipe, 'FAILED')
        print(f'[内阁] 六部失败，流水线 {pipe_id} 终止')
        return 'FAILED', None

    # 4. 起居注（依赖前三个产物，不独立重试——上游失败则无从落盘）
    ps.start_stage(pipe, 'qijuzhu')
    try:
        ok, detail, output = _stage_qijuzhu(pipe, brief, dlog, liubu, None)
        ps.finish_stage(pipe, 'qijuzhu', 'DONE' if ok else 'FAILED', detail=detail, output=output)
    except Exception as e:
        ps.finish_stage(pipe, 'qijuzhu', 'FAILED', detail=str(e)[:200])

    # 5. 都察院（基于已落盘的 data.json 弹劾校验）
    ps.start_stage(pipe, 'duchayuan')
    try:
        from inspector import run_checks, save_report, _load_data
        data = _load_data()
        rep = run_checks(data=data, pipe_latest=pipe, mode='full')
        report_path = save_report(rep)
        ps.finish_stage(pipe, 'duchayuan', 'DONE', detail=rep['summary'], output=report_path)
    except Exception as e:
        print(f'[内阁] 都察院异常：{e}')
        rep = None
        ps.finish_stage(pipe, 'duchayuan', 'FAILED', detail=str(e)[:200])

    # 6. 起居注补录：并入弹章与档册摘要（幂等）
    if rep is not None:
        try:
            from persist import record, _load_data as _ld
            data = _ld()
            record(data, brief=brief, dispatch_log=dlog, liubu_report=liubu,
                   report=rep, pipe=pipe)
        except Exception as e:
            print(f'[内阁] 起居注补录提示：{e}')

    # 7. 收工归档
    ps.finish_pipeline(pipe)

    print(f'[内阁] 流水线 {pipe_id} 收工：{pipe["status"]}')
    if rep:
        print(f'[都察院] {rep["summary"]}')
        for imp in rep['impeachments']:
            print(f"  [{imp['severity']}] {imp['check_name']}：{imp['message']}")
    return (rep or {}).get('overall', 'FAILED'), rep


if __name__ == '__main__':
    overall, rep = run_full_pipeline(trigger='manual')
    print(f'整体判定：{overall}')
