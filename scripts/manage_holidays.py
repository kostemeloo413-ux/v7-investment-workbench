# -*- coding: utf-8 -*-
"""
scripts/manage_holidays.py
A股节假日表维护工具（阶段二 · 节假日周末表维护）

用法（在项目根目录）：
  .venv\\Scripts\\python.exe -m scripts.manage_holidays list
  .venv\\Scripts\\python.exe -m scripts.manage_holidays add 2026-10-01 2026-10-02 2026-10-07
  .venv\\Scripts\\python.exe -m scripts.manage_holidays range 2026-10-01 2026-10-07
  .venv\\Scripts\\python.exe -m scripts.manage_holidays workday 2026-10-10      # 调休上班日
  .venv\\Scripts\\python.exe -m scripts.manage_holidays remove 2026-10-01
  .venv\\Scripts\\python.exe -m scripts.manage_holidays clear

说明：
- holidays  = 法定放假的【交易日】（如国庆10/1-10/7，周一至周五也算休市）
- workdays  = 调休上班的【周末】（如国庆前那个周六要开市）
- 系统默认周一至周五为交易日；这两张表用于覆盖法定节假日与调休。
- 请按国务院办公厅当年放假安排维护（每年底发布次年安排）。
"""

import os
import sys
import json
import argparse
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def _load():
    return rc._load_json(rc.HOLIDAYS_JSON, {'holidays': [], 'workdays': []})


def _save(data):
    data.setdefault('holidays', [])
    data.setdefault('workdays', [])
    data['holidays'] = sorted(set(data['holidays']))
    data['workdays'] = sorted(set(data['workdays']))
    data['note'] = ('holidays=法定放假(含工作日)；workdays=调休上班(含周末)。'
                    '按国务院放假安排维护，命令见 scripts/manage_holidays.py')
    rc._save_json(rc.HOLIDAYS_JSON, data)


def _parse(ds):
    return datetime.datetime.strptime(ds, '%Y-%m-%d').date()


def cmd_list(_):
    d = _load()
    print(f"holidays(放假 {len(d['holidays'])} 天): {d['holidays']}")
    print(f"workdays(调休上班 {len(d['workdays'])} 天): {d['workdays']}")
    print(f"文件: {rc.HOLIDAYS_JSON}")


def cmd_add(args):
    d = _load()
    added = []
    for ds in args.dates:
        _parse(ds)                          # 格式校验
        if ds not in d['holidays']:
            d['holidays'].append(ds)
            added.append(ds)
    _save(d)
    print(f'已添加放假 {len(added)} 天: {added}')


def cmd_range(args):
    d = _load()
    start, end = _parse(args.start), _parse(args.end)
    added = []
    cur = start
    while cur <= end:
        ds = cur.strftime('%Y-%m-%d')
        if ds not in d['holidays']:
            d['holidays'].append(ds)
            added.append(ds)
        cur += datetime.timedelta(days=1)
    _save(d)
    print(f'已添加区间放假 {len(added)} 天: {args.start} ~ {args.end}')


def cmd_workday(args):
    d = _load()
    added = []
    for ds in args.dates:
        _parse(ds)
        if ds not in d['workdays']:
            d['workdays'].append(ds)
            added.append(ds)
    _save(d)
    print(f'已添加调休上班 {len(added)} 天: {added}')


def cmd_remove(args):
    d = _load()
    for ds in args.dates:
        d['holidays'] = [h for h in d['holidays'] if h != ds]
        d['workdays'] = [w for w in d['workdays'] if w != ds]
    _save(d)
    print(f'已移除: {args.dates}')


def cmd_clear(_):
    _save({'holidays': [], 'workdays': []})
    print('已清空节假日表（恢复默认：周一至周五为交易日）')


def main():
    ap = argparse.ArgumentParser(description='A股节假日表维护')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    p_add = sub.add_parser('add'); p_add.add_argument('dates', nargs='+')
    p_range = sub.add_parser('range'); p_range.add_argument('start'); p_range.add_argument('end')
    p_wd = sub.add_parser('workday'); p_wd.add_argument('dates', nargs='+')
    p_rm = sub.add_parser('remove'); p_rm.add_argument('dates', nargs='+')
    sub.add_parser('clear')
    args = ap.parse_args()

    handlers = {
        'list': cmd_list, 'add': cmd_add, 'range': cmd_range,
        'workday': cmd_workday, 'remove': cmd_remove, 'clear': cmd_clear,
    }
    handlers[args.cmd](args)


if __name__ == '__main__':
    main()
