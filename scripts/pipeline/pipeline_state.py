# -*- coding: utf-8 -*-
"""
scripts/pipeline/pipeline_state.py
内阁档册 · Pipeline State Registry

记录整条流水线每个环节的执行状态，是「系统检查官（都察院）」的监察依据。

环节状态机（每环节）：
    PENDING → RUNNING → DONE / FAILED / SKIPPED

整链状态机：
    PENDING → RUNNING → DONE / PARTIAL_FAILED / FAILED

环节命名（中式官称，阶段 2 拆分为独立角色；阶段 1 由四合一引擎粗粒度覆盖前四环节）：
  - tongzhengsi   通政司   （总新闻官：检索新闻/行情/公告）
  - yichuan       驿传     （分发官：按博主画像路由情报）
  - liubu         六部     （6 位博主工作单元：按 Skill 契约执行板块任务）
  - qijuzhu       起居注   （落盘：单点写入 data.json + index.html 内嵌）
  - duchayuan     都察院   （系统检查官：弹劾校验整条链）

档册文件：data/pipeline/pipeline_state.json
结构：{ "schema_version", "latest": {...当前流水线...}, "history": [...最近 N 条摘要...] }
"""

import os
import json
import copy
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIPELINE_DIR = os.path.join(BASE_DIR, 'data', 'pipeline')
STATE_PATH = os.path.join(PIPELINE_DIR, 'pipeline_state.json')

SCHEMA_VERSION = '1.0'
HISTORY_LIMIT = 20          # 档册保留最近 20 条流水线摘要
STAGE_ORDER = ['tongzhengsi', 'yichuan', 'liubu', 'qijuzhu', 'duchayuan']

# 环节中文名与说明（日志/展示用）
STAGE_LABELS = {
    'tongzhengsi': '通政司（总新闻官）',
    'yichuan':     '驿传（分发官）',
    'liubu':       '六部（博主工作单元）',
    'qijuzhu':     '起居注（落盘）',
    'duchayuan':   '都察院（系统检查官）',
}


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def _empty_stage(name):
    """创建空环节记录。"""
    return {
        'name': name,
        'label': STAGE_LABELS.get(name, name),
        'status': 'PENDING',          # PENDING / RUNNING / DONE / FAILED / SKIPPED
        'started_at': None,
        'finished_at': None,
        'duration_ms': None,
        'output': None,               # 产出物路径或摘要
        'detail': None,               # 失败原因 / 结果摘要
        'retried': 0,                 # 复核（重试）次数
    }


def new_pipeline(trigger='automation', pipeline_id=None):
    """新建一条流水线记录（内阁开议）。"""
    if not pipeline_id:
        pipeline_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    return {
        'pipeline_id': pipeline_id,
        'trigger': trigger,
        'status': 'PENDING',
        'created_at': _now_str(),
        'started_at': None,
        'finished_at': None,
        'duration_ms': None,
        'stages': {name: _empty_stage(name) for name in STAGE_ORDER},
    }


def load_state():
    """读取内阁档册；不存在则返回空结构。"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'schema_version': SCHEMA_VERSION, 'latest': None, 'history': []}


def save_state(state):
    """写入内阁档册。"""
    os.makedirs(PIPELINE_DIR, exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def start_pipeline(trigger='automation'):
    """开议：新建流水线并置 RUNNING。返回 (state, pipeline)。"""
    state = load_state()
    pipe = new_pipeline(trigger)
    pipe['status'] = 'RUNNING'
    pipe['started_at'] = _now_str()
    state['latest'] = pipe
    save_state(state)
    return state, pipe


def start_stage(pipe, name):
    """环节开跑：PENDING/RUNNING → RUNNING，记录开始时间。"""
    st = pipe['stages'].setdefault(name, _empty_stage(name))
    st['status'] = 'RUNNING'
    st['started_at'] = _now_str()
    st['finished_at'] = None
    return st


def finish_stage(pipe, name, status='DONE', detail=None, output=None):
    """环节收工：置 DONE/FAILED，记录耗时与结果摘要。"""
    st = pipe['stages'][name]
    st['status'] = status
    st['finished_at'] = _now_str()
    if st.get('started_at'):
        try:
            t0 = datetime.datetime.strptime(st['started_at'], '%Y-%m-%d %H:%M:%S')
            t1 = datetime.datetime.strptime(st['finished_at'], '%Y-%m-%d %H:%M:%S')
            st['duration_ms'] = int((t1 - t0).total_seconds() * 1000)
        except Exception:
            st['duration_ms'] = None
    if detail is not None:
        st['detail'] = detail
    if output is not None:
        st['output'] = output
    return st


def finish_pipeline(pipe, status=None):
    """整链收工：汇总状态，写入档册并归档历史。"""
    if status is None:
        stages = pipe.get('stages', {})
        if any(s['status'] == 'FAILED' for s in stages.values()):
            status = 'PARTIAL_FAILED' if any(s['status'] == 'DONE' for s in stages.values()) else 'FAILED'
        else:
            status = 'DONE'
    pipe['status'] = status
    pipe['finished_at'] = _now_str()
    if pipe.get('started_at'):
        try:
            t0 = datetime.datetime.strptime(pipe['started_at'], '%Y-%m-%d %H:%M:%S')
            t1 = datetime.datetime.strptime(pipe['finished_at'], '%Y-%m-%d %H:%M:%S')
            pipe['duration_ms'] = int((t1 - t0).total_seconds() * 1000)
        except Exception:
            pipe['duration_ms'] = None

    state = load_state()
    state['latest'] = pipe
    # 归档摘要到历史（保留最近 N 条）
    history = state.get('history', [])
    history.insert(0, {
        'pipeline_id': pipe.get('pipeline_id'),
        'trigger': pipe.get('trigger'),
        'status': pipe.get('status'),
        'created_at': pipe.get('created_at'),
        'finished_at': pipe.get('finished_at'),
        'duration_ms': pipe.get('duration_ms'),
        'stages_summary': {k: v.get('status') for k, v in pipe.get('stages', {}).items()},
    })
    state['history'] = history[:HISTORY_LIMIT]
    save_state(state)
    return state


def get_latest_pipeline():
    """取最近一条流水线（供都察院依赖链校验使用）。"""
    state = load_state()
    return state.get('latest')


if __name__ == '__main__':
    # 自检：状态机流转
    state, pipe = start_pipeline('selftest')
    start_stage(pipe, 'tongzhengsi')
    finish_stage(pipe, 'tongzhengsi', 'DONE', detail='自检完成', output='news_brief.json')
    finish_pipeline(pipe)
    print('自检通过，档册路径:', STATE_PATH)
    print(json.dumps(load_state()['latest'], ensure_ascii=False, indent=2))
