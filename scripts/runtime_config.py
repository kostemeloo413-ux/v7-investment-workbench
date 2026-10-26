# -*- coding: utf-8 -*-
"""
scripts/runtime_config.py
运行时统一配置中枢（阶段一改造新增）：
- 加载项目根 .env（企微 webhook、LLM API Key 等敏感信息不入库不入git）
- 统一路径常量
- A股交易日历与交易时段判断（严格版本化交易所日历）
- 通用 JSON 读写小工具

被 daemon / watchdog / notify / analysis 各新模块共用；
旧的朝廷流水线脚本不依赖本文件，保持向后兼容。
"""

import os
import sys
import json
import datetime

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_PREVIEW = os.environ.get('V7_PUBLIC_PREVIEW') == '1'

# ---- .env 加载（python-dotenv，缺失时静默降级为空配置）----
try:
    from dotenv import load_dotenv
    if not PUBLIC_PREVIEW:
        load_dotenv(os.path.join(BASE_DIR, '.env'))
except ImportError:
    pass

ENV = os.environ

# ---- 敏感配置（全部来自 .env）----
WECOM_WEBHOOK_URL = '' if PUBLIC_PREVIEW else ENV.get('WECOM_WEBHOOK_URL', '').strip()
LLM_API_KEY = '' if PUBLIC_PREVIEW else (ENV.get('DEEPSEEK_API_KEY') or ENV.get('LLM_API_KEY') or '').strip()
LLM_BASE_URL = '' if PUBLIC_PREVIEW else ENV.get('LLM_BASE_URL', 'https://api.deepseek.com').strip()
LLM_MODEL = '' if PUBLIC_PREVIEW else ENV.get('LLM_MODEL', 'deepseek-chat').strip()

# ---- 行为参数（.env 可覆盖）----
PUSH_DAILY_LIMIT = int(ENV.get('PUSH_DAILY_LIMIT', '10'))       # 全渠道每日推送上限（降噪铁律）
PUSH_FORCE_LIMIT = int(ENV.get('PUSH_FORCE_LIMIT', '4'))        # force 告警独立限额（看门狗用）
DAEMON_PORT = int(ENV.get('DAEMON_PORT', '8018'))
HEARTBEAT_STALE_MINUTES = float(ENV.get('HEARTBEAT_STALE_MINUTES', '10'))
NEWS_MAX_PER_CYCLE = int(ENV.get('NEWS_MAX_PER_CYCLE', '5'))    # 每周期最多 LLM 解构的新闻条数

# ---- 路径常量 ----
STATE_DIR = os.path.join(BASE_DIR, 'state')
DATA_DIR = os.path.join(BASE_DIR, 'data')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
DB_PATH = os.path.join(DATA_DIR, 'market', 'market.db')
LLM_PROFILES_PATH = os.path.join(CONFIG_DIR, 'llm_profiles.json')
HEARTBEAT_PATH = os.path.join(STATE_DIR, 'heartbeat.json')
ALIVE_PATH = os.path.join(STATE_DIR, 'daemon_alive.json')
WATCHDOG_ALIVE_PATH = os.path.join(STATE_DIR, 'watchdog_alive.json')
PUSH_LOG_PATH = os.path.join(STATE_DIR, 'push_log.json')
SELL_ALERTS_PATH = os.path.join(STATE_DIR, 'sell_alerts.json')
LLM_CACHE_PATH = os.path.join(STATE_DIR, 'llm_cache.json')
SCHEDULE_YAML = os.path.join(CONFIG_DIR, 'schedule.yaml')
HOLIDAYS_JSON = os.path.join(CONFIG_DIR, 'holidays.json')
A_SHARE_TRADING_CALENDAR_JSON = os.path.join(CONFIG_DIR, 'a_share_trading_calendar.json')


class TradingCalendarStatus:
    """安全关键交易所日历的确定性读取状态。"""
    VALID = 'VALID'
    MISSING = 'MISSING'
    PARSE_ERROR = 'PARSE_ERROR'
    TYPE_ERROR = 'TYPE_ERROR'
    SCHEMA_ERROR = 'SCHEMA_ERROR'
    YEAR_UNAVAILABLE = 'YEAR_UNAVAILABLE'


def load_a_share_trading_calendar(path=None, required_year=None):
    """严格读取 A 股交易所日历；任何不确定性均返回非 VALID。"""
    path = path or A_SHARE_TRADING_CALENDAR_JSON
    if not os.path.isfile(path):
        return {'status': TradingCalendarStatus.MISSING, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_UNAVAILABLE'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            value = json.load(f)
    except json.JSONDecodeError:
        return {'status': TradingCalendarStatus.PARSE_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_INVALID'}
    except Exception as exc:
        return {'status': TradingCalendarStatus.PARSE_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': f'TRADING_CALENDAR_READ:{type(exc).__name__}'}
    if not isinstance(value, dict):
        return {'status': TradingCalendarStatus.TYPE_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_TYPE_ERROR'}
    required = ('schema_version', 'market', 'timezone', 'year', 'version',
                'source_authority', 'source_publication_date', 'last_verified_at',
                'closed_dates', 'exception_open_dates')
    if any(field not in value for field in required):
        return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_SCHEMA_ERROR'}
    if (value.get('schema_version') != 'v1' or value.get('market') != 'A_SHARE' or
            value.get('timezone') != 'Asia/Shanghai' or
            not isinstance(value.get('year'), int) or isinstance(value.get('year'), bool) or
            not isinstance(value.get('version'), str) or
            not isinstance(value.get('source_authority'), list) or
            not value.get('source_authority') or
            not isinstance(value.get('source_publication_date'), str) or
            not isinstance(value.get('last_verified_at'), str) or
            not isinstance(value.get('closed_dates'), list) or
            not isinstance(value.get('exception_open_dates'), list)):
        return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_SCHEMA_ERROR'}
    year = value['year']
    if required_year is not None and year != int(required_year):
        return {'status': TradingCalendarStatus.YEAR_UNAVAILABLE, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_YEAR_UNAVAILABLE'}
    parsed = {}
    for field in ('closed_dates', 'exception_open_dates'):
        dates = value[field]
        if len(set(dates)) != len(dates) or any(not isinstance(item, str) for item in dates):
            return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                    'calendar': None, 'reason': 'TRADING_CALENDAR_DATE_LIST_INVALID'}
        parsed_dates = set()
        for item in dates:
            try:
                parsed_date = datetime.datetime.strptime(item, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                        'calendar': None, 'reason': 'TRADING_CALENDAR_DATE_INVALID'}
            if parsed_date.year != year:
                return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                        'calendar': None, 'reason': 'TRADING_CALENDAR_DATE_YEAR_MISMATCH'}
            parsed_dates.add(parsed_date)
        parsed[field] = parsed_dates
    if parsed['closed_dates'] & parsed['exception_open_dates']:
        return {'status': TradingCalendarStatus.SCHEMA_ERROR, 'path': os.path.abspath(path),
                'calendar': None, 'reason': 'TRADING_CALENDAR_DATE_CONFLICT'}
    value = dict(value)
    value['_closed_dates'] = parsed['closed_dates']
    value['_exception_open_dates'] = parsed['exception_open_dates']
    return {'status': TradingCalendarStatus.VALID, 'path': os.path.abspath(path),
            'calendar': value, 'reason': None}


def trading_calendar_status(d=None, path=None):
    """Return calendar validation and date decision without failing open."""
    d = d or datetime.date.today()
    if isinstance(d, datetime.datetime):
        d = d.date()
    if not isinstance(d, datetime.date):
        return {'status': TradingCalendarStatus.TYPE_ERROR, 'trading_day': False,
                'reason': 'TRADING_CALENDAR_DATE_TYPE_ERROR'}
    loaded = load_a_share_trading_calendar(path=path, required_year=d.year)
    if loaded['status'] != TradingCalendarStatus.VALID:
        return {'status': loaded['status'], 'trading_day': False,
                'reason': loaded['reason'], 'path': loaded['path']}
    calendar = loaded['calendar']
    if d in calendar['_exception_open_dates']:
        result = True
    elif d.weekday() >= 5:
        result = False
    else:
        result = d not in calendar['_closed_dates']
    return {'status': TradingCalendarStatus.VALID, 'trading_day': result,
            'reason': None, 'path': loaded['path'], 'version': calendar['version'],
            'year': calendar['year']}


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def today_str():
    return datetime.datetime.now().strftime('%Y-%m-%d')


def _load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_holidays():
    """config/holidays.json: {"holidays": ["2026-10-01", ...], "workdays": ["2026-10-10", ...]}
    holidays = 放假的交易日；workdays = 调休上班的周末。维护说明见 README。"""
    cfg = _load_json(HOLIDAYS_JSON, {})
    return set(cfg.get('holidays', [])), set(cfg.get('workdays', []))


def is_trading_day(d=None):
    return bool(trading_calendar_status(d)['trading_day'])


def market_phase(dt=None, calendar_path=None):
    """返回当前所处交易时段：
    closed  非交易日 / 已收盘 / 未开盘
    pre     开盘前（09:15 之前）
    open    盘中连续竞价（含 13:00 后午盘）
    break   午间休市（11:31 ~ 12:59）
    """
    dt = dt or datetime.datetime.now()
    if not isinstance(dt, datetime.datetime):
        return 'closed'
    if not trading_calendar_status(dt.date(), path=calendar_path)['trading_day']:
        return 'closed'
    hm = dt.hour * 100 + dt.minute
    if hm < 915:
        return 'pre'
    if 915 <= hm <= 1130:
        return 'open'
    if 1131 <= hm <= 1259:
        return 'break'
    if 1300 <= hm <= 1500:
        return 'open'
    return 'closed'


# ---------------------------------------------------------------- LLM 多模型配置档
# 支持多套模型配置（名称/BaseURL/Key/模型名），切换即热生效。
# 配置档存 config/llm_profiles.json（不入库），未建档时回退 .env 的 LLM_* 单套配置。

def load_llm_profiles():
    """返回 {'active': id|None, 'profiles': [ {...} ]}；文件缺失返回空结构。
    主文件丢失/清空时自动从 state/llm_profiles.bak.json 恢复（防误删丢Key）。"""
    d = _load_json(LLM_PROFILES_PATH, {})
    if not isinstance(d, dict) or not d.get('profiles'):
        bak = _load_json(os.path.join(STATE_DIR, 'llm_profiles.bak.json'), {})
        if isinstance(bak, dict) and bak.get('profiles'):
            _save_json(LLM_PROFILES_PATH, bak)          # 自动恢复
            return bak
        return {'active': None, 'profiles': []}
    return {'active': d.get('active'), 'profiles': d.get('profiles', [])}


def save_llm_profiles(active, profiles):
    _save_json(LLM_PROFILES_PATH, {'active': active, 'profiles': profiles})
    # 双写备份（state/ 目录已 gitignore，Key 不入库）
    try:
        _save_json(os.path.join(STATE_DIR, 'llm_profiles.bak.json'),
                   {'active': active, 'profiles': profiles})
    except Exception:
        pass


def apply_profile_to_env(profile):
    """把某配置档热应用到模块级 LLM_* 变量（供研判层即时生效）。"""
    global LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    LLM_API_KEY = (profile.get('api_key') or '').strip()
    LLM_BASE_URL = (profile.get('base_url') or LLM_BASE_URL).strip()
    LLM_MODEL = (profile.get('model') or LLM_MODEL).strip()


def get_active_profile():
    """返回当前生效配置档 dict；未建档时用 .env 值构造一个只读档。"""
    data = load_llm_profiles()
    if data['active']:
        for p in data['profiles']:
            if p.get('id') == data['active']:
                return dict(p)
    return {'id': 'env-fallback', 'name': '(.env 单套配置)', 'base_url': LLM_BASE_URL,
            'api_key': LLM_API_KEY, 'model': LLM_MODEL, 'builtin_env': True}


def activate_llm_profile(profile_id):
    """切换激活配置档并热生效；返回 (ok, profile|err)。"""
    data = load_llm_profiles()
    for p in data['profiles']:
        if p.get('id') == profile_id:
            save_llm_profiles(profile_id, data['profiles'])
            apply_profile_to_env(p)
            return True, p
    return False, f'配置档不存在: {profile_id}'


def upsert_llm_profile(profile):
    """新增/更新配置档（id 为空则生成）；不自动切换。返回 (profile, is_new)。"""
    data = load_llm_profiles()
    profiles = data.get('profiles', [])
    if profile.get('id'):
        for i, p in enumerate(profiles):
            if p.get('id') == profile['id']:
                profiles[i] = profile
                save_llm_profiles(data['active'], profiles)
                return profile, False
    profile['id'] = profile.get('id') or ('p' + datetime.datetime.now().strftime('%H%M%S%f'))
    profiles.append(profile)
    save_llm_profiles(data['active'], profiles)
    return profile, True


def delete_llm_profile(profile_id):
    data = load_llm_profiles()
    kept = [p for p in data['profiles'] if p.get('id') != profile_id]
    active = data['active']
    if active == profile_id:
        active = kept[0]['id'] if kept else None
    save_llm_profiles(active, kept)
    if active:
        for p in kept:
            if p.get('id') == active:
                apply_profile_to_env(p)
                break
    else:
        # 无激活档：回退 .env 单套配置，避免残留已删除档的Key
        global LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        LLM_API_KEY = (ENV.get('DEEPSEEK_API_KEY') or ENV.get('LLM_API_KEY') or '').strip()
        LLM_BASE_URL = ENV.get('LLM_BASE_URL', 'https://api.deepseek.com').strip()
        LLM_MODEL = ENV.get('LLM_MODEL', 'deepseek-chat').strip()
    return active


# 进程启动时：自动应用已激活的 LLM 配置档（无激活档则保留 .env 单套配置）
# ——否则重启后配置档丢失、页面状态条回退为"未配置"
_pdata = {'active': None, 'profiles': []} if PUBLIC_PREVIEW else load_llm_profiles()
if _pdata.get('active'):
    for _p in _pdata['profiles']:
        if _p.get('id') == _pdata['active']:
            apply_profile_to_env(_p)
            break


if __name__ == '__main__':
    print('BASE_DIR      =', BASE_DIR)
    print('is_trading_day=', is_trading_day())
    print('market_phase  =', market_phase())
    print('webhook 配置  =', '已配置' if WECOM_WEBHOOK_URL else '未配置（.env 中 WECOM_WEBHOOK_URL）')
    print('llm 配置      =', ('已配置 ' + LLM_MODEL) if LLM_API_KEY else '未配置（.env 中 DEEPSEEK_API_KEY）')
