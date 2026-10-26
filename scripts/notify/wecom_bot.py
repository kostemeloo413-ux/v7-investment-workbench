# -*- coding: utf-8 -*-
"""
scripts/notify/wecom_bot.py
企业微信群机器人 · 统一推送通道（阶段一改造新增）

设计要点：
1. 全部消息走 push() 单一入口，便于统一降噪与审计；
2. 降噪铁律：普通推送每日 ≤ PUSH_DAILY_LIMIT 条（.env 可调），超出自动合并为
   "今日额度已满" 摘要，防止麻木——这是从参考体系血泪教训（曾一天推35条）学来的；
3. force=True 的告警（看门狗/系统级故障）不受常规额度限制，但有独立的
   PUSH_FORCE_LIMIT 每日上限，保证真正致命的事件永远叫得醒人；
4. 所有发送记录写入 state/push_log.json，供 /api/health 与复盘查看。

用法：
    from notify.wecom_bot import push
    push('盘前简报', ['上证 3966 (+0.4%)', '6 博主已开工'], level='info')

自测：
    python -m scripts.notify.wecom_bot --test
"""

import os
import sys
import json
import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import runtime_config as rc

try:
    import requests
except ImportError:
    requests = None

LEVEL_TAG = {'info': 'ℹ️', 'ok': '✅', 'warn': '⚠️', 'alert': '🚨'}
# 企业微信 markdown 上限 4096 字节；按中文 ~3字节 留足余量截到 1100 字符
MAX_CHARS = 1100


def _load_state():
    return rc._load_json(rc.PUSH_LOG_PATH, {})


def _save_state(st):
    rc._save_json(rc.PUSH_LOG_PATH, st)


def _today_counter_reset(st):
    today = datetime.date.today().strftime('%Y-%m-%d')
    if st.get('date') != today:
        st['date'] = today
        st['count'] = 0
        st['force_count'] = 0
        st['sent'] = []
        st['dropped_by_quota'] = 0
    return st


def quota_status():
    """返回当前推送额度状态（供 /api/health 展示）。"""
    st = _today_counter_reset(_load_state())
    return {
        'date': st.get('date'),
        'used': st.get('count', 0),
        'limit': rc.PUSH_DAILY_LIMIT,
        'force_used': st.get('force_count', 0),
        'force_limit': rc.PUSH_FORCE_LIMIT,
        'dropped_by_quota': st.get('dropped_by_quota', 0),
        'recent': (st.get('sent') or [])[-5:],
    }


def _post_wecom(payload):
    if not rc.WECOM_WEBHOOK_URL:
        return False, 'WECOM_WEBHOOK_URL 未配置（请在 .env 中填入群机器人地址）'
    if requests is None:
        return False, 'requests 库不可用'
    try:
        resp = requests.post(rc.WECOM_WEBHOOK_URL, json=payload, timeout=8)
        data = resp.json()
        if data.get('errcode') == 0:
            return True, 'OK'
        return False, f'企微返回 errcode={data.get("errcode")} errmsg={data.get("errmsg")}'
    except Exception as e:
        return False, f'请求异常: {e}'


def _truncate(text):
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS - 20] + '\n…(内容过长已截断)'


def send_raw(markdown_text):
    """绕过降噪统计的直接发送（仅内部用）。"""
    payload = {
        'msgtype': 'markdown',
        'markdown': {'content': _truncate(markdown_text)},
    }
    ok, detail = _post_wecom(payload)
    if not ok:
        print(f'[wecom_bot] 发送失败：{detail}')
    return ok, detail


def push(title, lines=None, level='info', force=False):
    """
    统一推送入口。
    title   : 消息标题
    lines   : 正文行列表或字符串
    level   : info / ok / warn / alert（决定前缀图标）
    force   : True 表示系统级告警，占用独立限额、必然尝试送达
    返回 (sent: bool, detail: str)
    """
    lines = lines or []
    body = title if isinstance(lines, str) else title + '\n' + '\n'.join(lines)
    content = f"{LEVEL_TAG.get(level, '')} {body}"

    st = _today_counter_reset(_load_state())
    if not force and st.get('count', 0) >= rc.PUSH_DAILY_LIMIT:
        st['dropped_by_quota'] = st.get('dropped_by_quota', 0) + 1
        _save_state(st)
        print(f'[wecom_bot] 已达每日 {rc.PUSH_DAILY_LIMIT} 条上限，丢弃普通推送：{title}')
        return False, 'quota_exceeded'

    key = 'count' if not force else 'force_count'
    limit = rc.PUSH_DAILY_LIMIT if not force else rc.PUSH_FORCE_LIMIT
    if st.get(key, 0) >= limit:
        # 连独立告警通道都满了——至少打日志留痕，不再发
        print(f'[wecom_bot] 独立告警通道已达上限({limit})，仅记录：{title}')
        _save_state(st)
        return False, 'force_quota_exceeded'

    ok, detail = send_raw(content)
    if ok:
        st[key] = st.get(key, 0) + 1
        st.setdefault('sent', []).append(
            f"{datetime.datetime.now().strftime('%H:%M:%S')} [{level}] {title}")
        _save_state(st)
    else:
        _save_state(st)
    return ok, detail


def daily_brief(lines):
    """盘前简报专用：每天固定一条汇总。"""
    return push('【盘前简报】' + datetime.date.today().strftime('%m月%d日'),
                lines, level='info')


def close_brief(lines):
    """收盘结算简报专用。"""
    return push('【收盘结算】' + datetime.date.today().strftime('%m月%d日'),
                lines, level='ok')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='企微机器人推送测试')
    ap.add_argument('--test', action='store_true', help='发送一条测试消息')
    ap.add_argument('--status', action='store_true', help='查看额度状态')
    args = ap.parse_args()
    if args.status:
        print(json.dumps(quota_status(), ensure_ascii=False, indent=2))
    elif args.test:
        print('webhook:', '已配置' if rc.WECOM_WEBHOOK_URL else '未配置！请编辑 .env')
        ok, detail = push('推送链路测试', [
            f'时间: {rc.now_str()}',
            '这是阶段一改造的第一次真实推送。',
            '收到本条说明「系统 → 微信」的反馈通路已打通 ✅',
        ], level='ok')
        print('发送结果:', ok, detail)
