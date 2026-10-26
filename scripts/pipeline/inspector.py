# -*- coding: utf-8 -*-
"""
scripts/pipeline/inspector.py
都察院 · System Inspector（系统检查官）

定位：系统层监察 —— 管「整条流水线做没做、做没做对」。
与大理寺（共识裁判长，业务层裁决观点对错）分工明确、正交共存：
  - 都察院：执行监察（时间戳 / 非空 / 哈希 / 依赖链 / 数据质量）
  - 大理寺：观点裁决（共识分 / 多空方向 / 策略矩阵）

5 项基础弹劾校验（阶段 1）：
  1. freshness  新鲜度检 —— picks_updated_at 距今是否超阈值（交易日盘中 60min，其余 240min）
  2. nonempty   非空检   —— blogger_picks=6、hotspots>=3、forward_events>=3、每博主有观点或明确无意见（诚实协议）
  3. hashcheck  哈希检   —— data.json 与 index.html 内嵌 DEFAULT_EMBEDDED_DATA 结构一致
  4. dependency 依赖链检 —— 上游环节 DONE 才允许下游（基于内阁档册）
  5. quality    质量检   —— 共识分∈[-1,1]、pct 合法、无 NaN/空关键字段

产出：data/pipeline/inspection_report.json（弹章）
  overall: PASS / WARN / FAIL
  score:   100 - FAIL*25 - WARN*5 （下限 0）
"""

import os
import re
import json
import math
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_PATH = os.path.join(BASE_DIR, 'workbench', 'data.json')
HTML_PATH = os.path.join(BASE_DIR, 'workbench', 'index.html')
PIPELINE_DIR = os.path.join(BASE_DIR, 'data', 'pipeline')
REPORT_PATH = os.path.join(PIPELINE_DIR, 'inspection_report.json')

SCHEMA_VERSION = '1.0'
START_TAG = 'const DEFAULT_EMBEDDED_DATA ='
END_TAG = 'let workbenchData = DEFAULT_EMBEDDED_DATA;'

# 阶段 1 依赖链：前四环节由四合一引擎粗粒度覆盖
FRONT_STAGES = ['tongzhengsi', 'yichuan', 'liubu', 'qijuzhu']
NO_OPINION_MARKERS = ['无意见', '未选出', '未触发', '空仓', '安全防守', '等待']


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _is_trading_day():
    return datetime.datetime.now().weekday() < 5


def _is_market_hours():
    h, m = datetime.datetime.now().hour, datetime.datetime.now().minute
    t = h * 60 + m
    return 9 * 60 <= t <= 16 * 60


def _freshness_threshold():
    """交易日盘中 60 分钟，其余 240 分钟；非交易日不限。"""
    if _is_trading_day() and _is_market_hours():
        return 60
    if _is_trading_day():
        return 240
    return None  # 非交易日不弹劾新鲜度


def _load_data():
    if not os.path.exists(DATA_PATH):
        return None
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _extract_embedded(html):
    """从 index.html 提取内嵌 DEFAULT_EMBEDDED_DATA JSON。"""
    s = html.find(START_TAG)
    e = html.find(END_TAG, s)
    if s == -1 or e == -1:
        return None
    chunk = html[s + len(START_TAG):e].strip()
    chunk = chunk.rstrip(';').strip()
    try:
        return json.loads(chunk)
    except Exception:
        return None


def _json_eq(a, b):
    try:
        return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(b, ensure_ascii=False, sort_keys=True)
    except Exception:
        return False


def _fmt_age(ts_str, now=None):
    """时间字符串距今分钟数。"""
    if not ts_str:
        return None
    try:
        t = datetime.datetime.strptime(str(ts_str)[:19], '%Y-%m-%d %H:%M:%S')
        if now is None:
            now = datetime.datetime.now()
        return int((now - t).total_seconds() / 60)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 五项校验
# --------------------------------------------------------------------------

def check_freshness(data):
    """1. 新鲜度检：picks_updated_at 距今是否超阈值。"""
    threshold = _freshness_threshold()
    if threshold is None:
        return ('SKIP', '非交易日，不弹劾新鲜度', None)
    if not data:
        return ('FAIL', 'data.json 不存在，无法检新鲜度', '运行流水线生成 data.json')
    ts = data.get('picks_updated_at') or data.get('updated_at')
    age = _fmt_age(ts)
    if age is None:
        return ('FAIL', f'picks_updated_at 缺失或格式异常（{ts}）', '运行流水线重算博主选股')
    if age > threshold:
        return ('FAIL', f'博主选股已 {age} 分钟未更新（阈值 {threshold}min，当前 {ts}）',
                '运行 run_pipeline.py 全链重算')
    if age > threshold * 0.6:
        return ('WARN', f'博主选股距今 {age} 分钟，接近阈值 {threshold}min（{ts}）', '建议尽快全链重算')
    return ('PASS', f'博主选股距今 {age} 分钟，新鲜（{ts}）', None)


def check_nonempty(data):
    """2. 非空检：板块数据齐备 + 博主诚实协议。"""
    if not data:
        return ('FAIL', 'data.json 不存在，无法检非空', '运行流水线生成 data.json')
    problems = []

    picks = data.get('blogger_picks') or data.get('independent_blogger_picks') or []
    if len(picks) != 6:
        problems.append(f'blogger_picks 应 6 条，实为 {len(picks)} 条')

    hotspots = data.get('hotspots') or []
    if len(hotspots) < 3:
        # 阶段四分层治理：hotspots 为 0~2 条时，若有分层统计背书
        # （本周期例行公告潮 → WEAK 全过滤是正确状态），属诚实空缺不弹劾；
        # 无分层背书（弱计数=0 且热点也少）才弹劾
        tiers = data.get('news_tiers') or {}
        weak_n = int(tiers.get('weak_count') or 0)
        if not (weak_n > 0):
            problems.append(f'hotspots 应 >=3 条，实为 {len(hotspots)} 条')

    fwd = data.get('forward_events') or []
    if len(fwd) < 3:
        problems.append(f'forward_events 应 >=3 条，实为 {len(fwd)} 条')

    # 博主诚实协议：每博主有观点或明确"无意见"
    for p in picks:
        bid = p.get('blogger_id', '?')
        reason = (p.get('trigger_reason') or '').strip()
        stock = (p.get('stock_name') or '').strip()
        if not reason and not stock:
            problems.append(f'博主 [{bid}] 既无触发原因也无标的名，违反诚实协议')
        elif not reason and not any(m in stock for m in NO_OPINION_MARKERS):
            problems.append(f'博主 [{bid}] 触发原因为空且未声明"无意见"（{stock}）')

    if problems:
        return ('FAIL', '；'.join(problems), '运行流水线重算各板块数据')
    return ('PASS', '6 博主 + 热点 + 前瞻板块数据齐备，诚实协议通过', None)


def check_hashcheck(data):
    """3. 哈希检：data.json 与 index.html 内嵌数据结构一致。"""
    if not data:
        return ('FAIL', 'data.json 不存在，无法检哈希', '运行流水线生成 data.json')
    if not os.path.exists(HTML_PATH):
        return ('SKIP', 'workbench/index.html 不存在，跳过内嵌一致性检', None)
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html = f.read()
    embedded = _extract_embedded(html)
    if embedded is None:
        return ('FAIL', 'index.html 内嵌 DEFAULT_EMBEDDED_DATA 解析失败', '运行流水线重新嵌入')
    if not _json_eq(data, embedded):
        return ('FAIL', 'data.json 与 index.html 内嵌数据不一致', '运行流水线同步嵌入（embed_data_into_html）')
    return ('PASS', 'data.json 与 index.html 内嵌数据完全一致', None)


def check_dependency(pipe_latest, mode):
    """4. 依赖链检：上游环节 DONE 才允许下游（基于内阁档册）。"""
    if mode == 'inspect_only' or pipe_latest is None:
        # 只检模式无档册或档册为空：不弹劾，标记 SKIP（首次运行无参照）
        return ('SKIP', '仅检模式或无流水线记录，依赖链以档册为准（本次不弹劾）', None)
    stages = pipe_latest.get('stages', {})
    # 都察院是流水线最后一环，检查时整链可能仍 RUNNING，故只核上游四环节
    front = {k: stages.get(k, {}).get('status') for k in FRONT_STAGES}
    failed = [k for k, v in front.items() if v == 'FAILED']
    if failed:
        names = '、'.join(stages[k].get('label', k) for k in failed)
        return ('FAIL', f'上游环节执行失败：{names}', '复核（重跑）失败环节')
    not_done = [k for k, v in front.items() if v != 'DONE']
    if not_done:
        names = '、'.join(stages[k].get('label', k) for k in not_done)
        return ('WARN', f'上游环节未全部收工：{names}', '等待流水线完整收工后复检')
    return ('PASS', f'流水线 {pipe_latest.get("pipeline_id")} 上游四环节全部 DONE，依赖链完整', None)


def check_quality(data):
    """5. 质量检：共识分∈[-1,1]、pct 合法、无 NaN/空关键字段。"""
    if not data:
        return ('FAIL', 'data.json 不存在，无法检质量', '运行流水线生成 data.json')
    problems = []

    consensus = data.get('consensus') or {}
    score = consensus.get('trust_adjusted_score')
    if score is not None:
        try:
            score = float(score)
            if not (-1.0 <= score <= 1.0):
                problems.append(f'共识分 {score} 超出 [-1,1]')
        except (TypeError, ValueError):
            problems.append(f'共识分非数值：{score}')

    picks = data.get('blogger_picks') or []
    for p in picks:
        bid = p.get('blogger_id', '?')
        if p.get('triggered') and p.get('stock_code') and p.get('stock_code') != '无':
            price = p.get('current_price')
            pct = p.get('pct')
            if price is None or (isinstance(price, float) and math.isnan(price)):
                problems.append(f'博主 [{bid}] current_price 为空或 NaN')
            if pct is not None:
                try:
                    pct = float(pct)
                    if abs(pct) > 30:
                        problems.append(f'博主 [{bid}] pct={pct}% 超出合理区间（>30%）')
                except (TypeError, ValueError):
                    problems.append(f'博主 [{bid}] pct 非数值：{pct}')

    if problems:
        return ('FAIL', '；'.join(problems), '检查生成数据的博主契约打分逻辑')
    return ('PASS', '共识分区间、涨跌幅、关键字段质量全部合规', None)


def check_ledger(data):
    """6. 账本完整性检：模拟盘户部账册 SHA-256 与 manifest 比对（防篡改）。"""
    import hashlib
    trades_dir = os.path.join(BASE_DIR, 'data', 'trades')
    manifest_path = os.path.join(trades_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return ('SKIP', '未找到模拟盘账本 manifest（首次运行或账本未初始化），跳过', None)

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception as e:
        return ('FAIL', f'manifest.json 解析失败：{e}', '检查账本 manifest 是否损坏')

    files = manifest.get('files', {})
    problems = []
    for bid, info in files.items():
        path = os.path.join(trades_dir, f'{bid}.json')
        if not os.path.exists(path):
            problems.append(f'博主 [{bid}] 账本文件缺失')
            continue
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            h.update(f.read())
        digest = h.hexdigest()[:16]
        if digest != info.get('sha256'):
            problems.append(f'博主 [{bid}] 账本哈希不符（manifest={info.get("sha256")}，实算={digest}）')

    # 反向检查：存在但未登记的账本文件
    for fname in os.listdir(trades_dir):
        if fname == 'manifest.json' or not fname.endswith('.json'):
            continue
        bid = fname[:-5]
        if bid not in files:
            problems.append(f'账本文件 {fname} 未在 manifest 登记')

    if problems:
        return ('FAIL', '；'.join(problems), '账本可能被篡改，运行 simulated_trading 重建 manifest 或人工核验')
    return ('PASS', f'模拟盘账本完整性通过（{len(files)} 个博主账本哈希一致）', None)


# --------------------------------------------------------------------------
# 弹章（检查报告）
# --------------------------------------------------------------------------

CHECKERS = [
    ('freshness', '新鲜度检', check_freshness),
    ('nonempty', '非空检', check_nonempty),
    ('hashcheck', '哈希检', check_hashcheck),
    ('dependency', '依赖链检', check_dependency),
    ('quality', '质量检', check_quality),
    ('ledger', '账本完整性检', check_ledger),
]


def run_checks(data=None, pipe_latest=None, mode='full'):
    """
    执行全部弹劾校验，返回弹章 dict。

    mode: 'full' 流水线内联执行 / 'inspect_only' 独立只检
    """
    if data is None:
        data = _load_data()

    checks = []
    impeachments = []
    for cid, cname, fn in CHECKERS:
        try:
            if cid == 'dependency':
                status, detail, suggestion = fn(pipe_latest, mode)
            else:
                status, detail, suggestion = fn(data)
        except Exception as e:
            status, detail, suggestion = 'FAIL', f'校验异常：{e}', '修复校验逻辑后重跑'
        checks.append({
            'id': cid,
            'name': cname,
            'status': status,
            'detail': detail,
            'suggestion': suggestion,
        })
        if status in ('FAIL', 'WARN'):
            impeachments.append({
                'check': cid,
                'check_name': cname,
                'severity': status,
                'message': detail,
                'suggestion': suggestion,
            })

    n_fail = sum(1 for c in checks if c['status'] == 'FAIL')
    n_warn = sum(1 for c in checks if c['status'] == 'WARN')
    if n_fail:
        overall = 'FAIL'
    elif n_warn:
        overall = 'WARN'
    else:
        overall = 'PASS'
    score = max(0, 100 - n_fail * 25 - n_warn * 5)

    pipe = pipe_latest or {}
    report = {
        'schema_version': SCHEMA_VERSION,
        'inspected_at': _now_str(),
        'pipeline_id': pipe.get('pipeline_id'),
        'mode': mode,
        'overall': overall,
        'score': score,
        'checks': checks,
        'impeachments': impeachments,
        'summary': (
            f'都察院监察完毕：{len(checks)} 项校验，{n_fail} 项弹劾（FAIL）、{n_warn} 项示警（WARN），'
            f'综合评分 {score}/100。'
        ),
    }
    return report


def save_report(report):
    os.makedirs(PIPELINE_DIR, exist_ok=True)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return REPORT_PATH


def load_report():
    if os.path.exists(REPORT_PATH):
        try:
            with open(REPORT_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


if __name__ == '__main__':
    # 独立自检：只检当前数据，不执行流水线
    rep = run_checks(mode='inspect_only')
    path = save_report(rep)
    print(rep['summary'])
    print('弹章路径:', path)
    for c in rep['checks']:
        print(f"  [{c['status']:5s}] {c['name']}：{c['detail']}")
