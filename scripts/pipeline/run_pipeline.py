# -*- coding: utf-8 -*-
"""
scripts/pipeline/run_pipeline.py
流水线一键入口（automation / 手动共用）

用法：
  python scripts/pipeline/run_pipeline.py                 # 全链执行（内阁 + 都察院）
  python scripts/pipeline/run_pipeline.py --inspect-only  # 只弹劾不执行（检查现有数据）
  python scripts/pipeline/run_pipeline.py --trigger manual # 指定触发源

退出码（供 automation 判断）：
  0 = PASS   1 = WARN   2 = FAILED
"""

import os
import sys
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts', 'pipeline'))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
sys.path.insert(0, BASE_DIR)

EXIT = {'PASS': 0, 'WARN': 1, 'FAILED': 2, 'FAIL': 2}


def main():
    ap = argparse.ArgumentParser(description='朝廷流水线一键执行（内阁 + 都察院）')
    ap.add_argument('--inspect-only', action='store_true',
                    help='只弹劾不执行：校验现有 data.json，不重跑流水线')
    ap.add_argument('--trigger', default='automation',
                    help='触发源标识（automation / manual / selftest）')
    args = ap.parse_args()

    if args.inspect_only:
        import inspector
        rep = inspector.run_checks(mode='inspect_only')
        inspector.save_report(rep)
        print(rep['summary'])
        for c in rep['checks']:
            print(f"  [{c['status']:5s}] {c['name']}：{c['detail']}")
        return EXIT.get(rep['overall'], 1)

    import orchestrator
    try:
        overall, _rep = orchestrator.run_full_pipeline(trigger=args.trigger)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print('[run_pipeline] 流水线执行异常：', e)
        try:
            from persist import write_failure_snapshot
            write_failure_snapshot(trigger=args.trigger, error=str(e) + '\n' + tb)
        except Exception as pe:
            print('[run_pipeline] 失败快照回写失败：', pe)
        return EXIT.get('FAILED', 2)
    return EXIT.get(overall, 1)


if __name__ == '__main__':
    sys.exit(main())
