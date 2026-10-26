# -*- coding: utf-8 -*-
"""
scripts/param_version_manager.py
系统关键参数配置与版本回溯审计引擎 (System Parameter Versioning & Rollback Engine)

核心能力：
1. 统一管理 6 博主 Skill 契约、模拟盘风控、主板大池过滤及分时渲染 4 大维度核心参数；
2. 每次参数变更自动创建完整快照（Snapshot）并记录审计日志（变更时间、操作人、修改原因、新旧值 Diff）；
3. 支持秒级一键回溯至任意历史版本（Rollback），自动创建回溯存证与安全快照；
4. 提供 CLI 命令行工具与 Python API 接口，并自动同步至 workbench/data.json 供前端可视化渲染。
"""

import os
import sys
import json
import copy
import datetime

# Windows 终端编码保护
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'system_params.json')
HISTORY_DIR = os.path.join(BASE_DIR, 'data', 'param_history')
SNAPSHOTS_DIR = os.path.join(HISTORY_DIR, 'snapshots')
LOG_PATH = os.path.join(HISTORY_DIR, 'log.json')
WORKBENCH_DATA_PATH = os.path.join(BASE_DIR, 'workbench', 'data.json')


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_dirs():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


def load_current_config():
    """读取当前生效的参数配置文件"""
    ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_current_config(config_data):
    """保存当前生效的参数配置文件"""
    ensure_dirs()
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)


def load_log():
    """读取参数变更审计日志"""
    ensure_dirs()
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_log(log_entries):
    """保存参数变更审计日志"""
    ensure_dirs()
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log_entries, f, ensure_ascii=False, indent=2)


def save_snapshot(version, config_data):
    """保存指定版本的完整快照文件"""
    ensure_dirs()
    snapshot_file = os.path.join(SNAPSHOTS_DIR, f"{version}.json")
    with open(snapshot_file, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)
    return snapshot_file


def increment_version(current_version, is_minor=False):
    """自动生成递增版本号 (语义化版本规范)"""
    v_clean = current_version.lstrip('v')
    parts = v_clean.split('.')
    try:
        major = int(parts[0]) if len(parts) > 0 else 2
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        if is_minor:
            minor += 1
            patch = 0
        else:
            patch += 1
        return f"v{major}.{minor}.{patch}"
    except Exception:
        return current_version + ".1"


def init_system_baseline():
    """初始化参数版本系统基准与历史记录"""
    ensure_dirs()
    current = load_current_config()
    logs = load_log()

    # 1. 如果尚无基准日志，初始化基准版本 v2.0.0 和 v2.1.0
    if not logs:
        v2_0_0 = copy.deepcopy(current)
        v2_0_0["version"] = "v2.0.0"
        v2_0_0["updated_at"] = "2026-08-17 18:00:00"
        v2_0_0["last_operator"] = "用户"
        v2_0_0["last_change_summary"] = "A股多博主量化共识交易系统 v2.0 初始基线发布（确立 6 博主 100 万模拟盘契约与 3103 纯正主板大池）"

        # 调整 v2.0.0 部分历史参数作为对照基准
        v2_0_0["categories"]["blogger_strategies"]["params"]["hanshu"]["pe_range"] = [10.0, 50.0]
        v2_0_0["categories"]["chart_and_sparkline"]["params"]["sparkline_polyline_threshold"] = 30

        save_snapshot("v2.0.0", v2_0_0)

        # v2.1.0 升级
        save_snapshot("v2.1.0", current)

        init_logs = [
            {
                "version": "v2.1.0",
                "previous_version": "v2.0.0",
                "timestamp": "2026-08-21 14:15:00",
                "operator": "用户 & AI协作助手",
                "action": "UPDATE",
                "summary": "升级韩叔 PE 估值上限至 75 倍、分时走势图优化 240 分钟 X 轴比例与午间中轴线、全量纳入参数版本控制",
                "changes": [
                    {
                        "category": "blogger_strategies",
                        "field_path": "blogger_strategies.hanshu.pe_range",
                        "field_name": "韩叔 PE 估值筛选区间",
                        "old_value": [10.0, 50.0],
                        "new_value": [10.0, 75.0],
                        "reason": "适配长鑫存储及半导体高成长设备股在周期拐点期的估值特性"
                    },
                    {
                        "category": "chart_and_sparkline",
                        "field_path": "chart_and_sparkline.sparkline_polyline_threshold",
                        "field_name": "分时折线 Polyline 启用门槛点数",
                        "old_value": 30,
                        "new_value": 20,
                        "reason": "让开盘前 20~30 分钟也能拥有锐角分明的高精折线"
                    },
                    {
                        "category": "chart_and_sparkline",
                        "field_path": "chart_and_sparkline.sparkline_midline_enabled",
                        "field_name": "A股分时图 11:30 中午中轴虚线",
                        "old_value": False,
                        "new_value": True,
                        "reason": "对齐东方财富/同花顺标准分时图，盘中 11:30/13:00 准确定位"
                    }
                ],
                "snapshot_file": "v2.1.0.json"
            },
            {
                "version": "v2.0.0",
                "previous_version": "None",
                "timestamp": "2026-08-17 18:00:00",
                "operator": "用户",
                "action": "INITIALIZE",
                "summary": "创建系统核心参数架构基线：6博主Skill契约、100万模拟盘风控、3103主板大池与分时渲染参数",
                "changes": [
                    {
                        "category": "system",
                        "field_path": "all",
                        "field_name": "系统全量参数基线",
                        "old_value": "None",
                        "new_value": "v2.0.0 规范化基线",
                        "reason": "系统 v2.0 架构升级正式基准确立"
                    }
                ],
                "snapshot_file": "v2.0.0.json"
            }
        ]
        save_log(init_logs)
        print(f"[{now_str()}] ✅ 已初始化参数版本基线: v2.0.0 & v2.1.0")

    # 同步更新前端 workbench/data.json
    sync_to_workbench_data()


def update_param(field_path, new_value, reason="参数优化调整", operator="用户", is_minor=False):
    """
    修改指定参数并自动生成新版本、记录 diff 与快照
    
    参数:
    - field_path: 形如 "blogger_strategies.hanshu.stop_loss_pct" 或 "simulation_and_risk.capital_per_blogger"
    - new_value: 新的参数值
    - reason: 修改原因（必填，审计备查）
    - operator: 操作者名称
    - is_minor: 是否为主版本/次版本升级
    """
    ensure_dirs()
    current = load_current_config()
    old_version = current.get("version", "v2.1.0")
    new_version = increment_version(old_version, is_minor=is_minor)

    # 路径拆解与旧值定位
    parts = field_path.split('.')
    target = current.get("categories", {})
    old_value = None

    # 递归查找或定位
    ptr = target
    for idx, p in enumerate(parts):
        if idx == len(parts) - 1:
            if isinstance(ptr, dict) and p in ptr:
                old_value = copy.deepcopy(ptr[p])
                ptr[p] = new_value
            elif isinstance(ptr, dict) and "params" in ptr and isinstance(ptr["params"], dict) and p in ptr["params"]:
                old_value = copy.deepcopy(ptr["params"][p])
                ptr["params"][p] = new_value
            else:
                # 尝试穿透
                found = False
                for cat_key, cat_val in target.items():
                    if isinstance(cat_val, dict) and "params" in cat_val and p in cat_val["params"]:
                        old_value = copy.deepcopy(cat_val["params"][p])
                        cat_val["params"][p] = new_value
                        found = True
                        break
                if not found:
                    raise KeyError(f"未找到参数路径: {field_path}")
        else:
            if isinstance(ptr, dict) and p in ptr:
                ptr = ptr[p]
            elif isinstance(ptr, dict) and "params" in ptr and p in ptr["params"]:
                ptr = ptr["params"][p]
            else:
                # 尝试在 categories 下查找
                found = False
                for cat_key, cat_val in target.items():
                    if cat_key == p:
                        ptr = cat_val.get("params", cat_val)
                        found = True
                        break
                    elif isinstance(cat_val, dict) and "params" in cat_val and p in cat_val["params"]:
                        ptr = cat_val["params"][p]
                        found = True
                        break
                if not found:
                    raise KeyError(f"未找到参数子节点: {p} in {field_path}")

    # 更新元信息
    current["version"] = new_version
    current["updated_at"] = now_str()
    current["last_operator"] = operator
    current["last_change_summary"] = f"修改 {field_path}: {old_value} -> {new_value} ({reason})"

    # 保存新配置与快照
    save_current_config(current)
    save_snapshot(new_version, current)

    # 记录审计日志
    logs = load_log()
    change_entry = {
        "category": parts[0] if len(parts) > 0 else "general",
        "field_path": field_path,
        "field_name": parts[-1],
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason
    }
    log_entry = {
        "version": new_version,
        "previous_version": old_version,
        "timestamp": current["updated_at"],
        "operator": operator,
        "action": "UPDATE",
        "summary": current["last_change_summary"],
        "changes": [change_entry],
        "snapshot_file": f"{new_version}.json"
    }
    logs.insert(0, log_entry)  # 最新的在最前
    save_log(logs)

    # 同步至前端
    sync_to_workbench_data()

    print(f"[{now_str()}] 🚀 参数已成功更新并发布新版本: {new_version}")
    print(f"     - 变更项: {field_path}")
    print(f"     - 旧值: {old_value} ➔ 新值: {new_value}")
    print(f"     - 原因: {reason}")
    print(f"     - 快照存证: {SNAPSHOTS_DIR}/{new_version}.json")
    return new_version


def rollback_to_version(target_version, reason="人工纠偏回溯", operator="用户"):
    """
    一键回溯至指定的历史版本，并生成新的安全回溯审计版本
    """
    ensure_dirs()
    snapshot_file = os.path.join(SNAPSHOTS_DIR, f"{target_version}.json")
    if not os.path.exists(snapshot_file):
        raise FileNotFoundError(f"未找到版本快照: {snapshot_file}")

    with open(snapshot_file, 'r', encoding='utf-8') as f:
        target_config = json.load(f)

    current = load_current_config()
    current_version = current.get("version", "v2.1.0")
    new_version = increment_version(current_version)

    # 恢复目标配置并重置版本号与元数据
    restored_config = copy.deepcopy(target_config)
    restored_config["version"] = new_version
    restored_config["updated_at"] = now_str()
    restored_config["last_operator"] = operator
    restored_config["last_change_summary"] = f"⏪ 安全回溯至历史版本 {target_version} ({reason})"

    # 保存配置与新快照
    save_current_config(restored_config)
    save_snapshot(new_version, restored_config)

    # 记录审计日志
    logs = load_log()
    log_entry = {
        "version": new_version,
        "previous_version": current_version,
        "target_rollback_version": target_version,
        "timestamp": restored_config["updated_at"],
        "operator": operator,
        "action": "ROLLBACK",
        "summary": restored_config["last_change_summary"],
        "changes": [
            {
                "category": "system_rollback",
                "field_path": "all",
                "field_name": "全量系统配置",
                "old_value": f"版本 {current_version}",
                "new_value": f"版本 {target_version} (重放生效为 {new_version})",
                "reason": reason
            }
        ],
        "snapshot_file": f"{new_version}.json"
    }
    logs.insert(0, log_entry)
    save_log(logs)

    # 同步至前端
    sync_to_workbench_data()

    print(f"[{now_str()}] ⏪ 系统已成功回溯至版本 {target_version} (新版本号: {new_version})")
    print(f"     - 回溯原因: {reason}")
    print(f"     - 操作人: {operator}")
    print(f"     - 还原前版本: {current_version}")
    return new_version


def sync_to_workbench_data():
    """将当前参数状态与版本历史同步写入 workbench/data.json"""
    if not os.path.exists(WORKBENCH_DATA_PATH):
        return

    try:
        with open(WORKBENCH_DATA_PATH, 'r', encoding='utf-8') as f:
            wb_data = json.load(f)
    except Exception:
        wb_data = {}

    current = load_current_config()
    logs = load_log()

    # 获取所有快照列表概览
    snapshots_summary = []
    if os.path.exists(SNAPSHOTS_DIR):
        for fname in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
            if fname.endswith('.json'):
                v_name = fname.replace('.json', '')
                try:
                    with open(os.path.join(SNAPSHOTS_DIR, fname), 'r', encoding='utf-8') as sf:
                        s_data = json.load(sf)
                    snapshots_summary.append({
                        "version": v_name,
                        "updated_at": s_data.get("updated_at", ""),
                        "operator": s_data.get("last_operator", ""),
                        "summary": s_data.get("last_change_summary", "")
                    })
                except Exception:
                    pass

    wb_data["param_system"] = {
        "current_version": current.get("version", "v2.1.0"),
        "updated_at": current.get("updated_at", now_str()),
        "last_operator": current.get("last_operator", "用户"),
        "last_change_summary": current.get("last_change_summary", ""),
        "categories": current.get("categories", {}),
        "recent_logs": logs[:20],  # 最近 20 条日志
        "snapshots": snapshots_summary
    }

    with open(WORKBENCH_DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(wb_data, f, ensure_ascii=False, indent=2)

    # 尝试同步注入 HTML 内嵌底座
    try:
        from auto_update_hotspots import embed_data_into_html
        embed_data_into_html(wb_data)
    except Exception:
        pass


def list_versions():
    """查看所有可用版本与日志"""
    logs = load_log()
    print("\n================ 📜 系统关键参数版本变更历史与审计流水 ================")
    for idx, l in enumerate(logs):
        print(f"[{idx+1}] 版本: {l.get('version')} (前序: {l.get('previous_version')}) | 动作: {l.get('action')} | 时间: {l.get('timestamp')}")
        print(f"    操作人: {l.get('operator')}")
        print(f"    变更概述: {l.get('summary')}")
        if l.get('changes'):
            for ch in l['changes']:
                print(f"    - 详细变更: {ch.get('field_name')} ({ch.get('field_path')})")
                print(f"      旧值: {ch.get('old_value')} ➔ 新值: {ch.get('new_value')}")
                print(f"      原因: {ch.get('reason')}")
        print("-" * 72)


def compare_versions(v1, v2):
    """对比任意两个版本的参数快照差异"""
    f1 = os.path.join(SNAPSHOTS_DIR, f"{v1}.json")
    f2 = os.path.join(SNAPSHOTS_DIR, f"{v2}.json")
    if not os.path.exists(f1) or not os.path.exists(f2):
        print(f"❌ 快照文件不存在: {f1} 或 {f2}")
        return

    with open(f1, 'r', encoding='utf-8') as sf1:
        d1 = json.load(sf1)
    with open(f2, 'r', encoding='utf-8') as sf2:
        d2 = json.load(sf2)

    print(f"\n🔍 版本对比: {v1} ({d1.get('updated_at')}) vs {v2} ({d2.get('updated_at')})")
    # 简要比对
    diffs = []
    def compare_dict(p1, p2, path=""):
        if isinstance(p1, dict) and isinstance(p2, dict):
            keys = set(p1.keys()).union(set(p2.keys()))
            for k in keys:
                if k in ['updated_at', 'last_operator', 'last_change_summary', 'version']:
                    continue
                v1_sub = p1.get(k)
                v2_sub = p2.get(k)
                cur_path = f"{path}.{k}" if path else k
                if v1_sub != v2_sub:
                    if isinstance(v1_sub, dict) and isinstance(v2_sub, dict):
                        compare_dict(v1_sub, v2_sub, cur_path)
                    else:
                        diffs.append({"path": cur_path, "v1": v1_sub, "v2": v2_sub})

    compare_dict(d1.get("categories", {}), d2.get("categories", {}))
    if not diffs:
        print("  ✅ 两个版本核心业务参数完全一致")
    else:
        for d in diffs:
            print(f"  • {d['path']}:")
            print(f"      {v1}: {d['v1']}")
            print(f"      {v2}: {d['v2']}")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'init':
            init_system_baseline()
        elif cmd == 'list':
            list_versions()
        elif cmd == 'rollback' and len(sys.argv) > 2:
            target_v = sys.argv[2]
            reason = sys.argv[3] if len(sys.argv) > 3 else "CLI 手动回溯"
            rollback_to_version(target_v, reason=reason)
        elif cmd == 'compare' and len(sys.argv) > 3:
            compare_versions(sys.argv[2], sys.argv[3])
        elif cmd == 'update' and len(sys.argv) > 4:
            f_path = sys.argv[2]
            # 尝试转数字/json
            raw_val = sys.argv[3]
            try:
                val = json.loads(raw_val)
            except Exception:
                val = raw_val
            reason = sys.argv[4] if len(sys.argv) > 4 else "CLI 修改"
            update_param(f_path, val, reason=reason)
        else:
            print("用法:")
            print("  python scripts/param_version_manager.py init")
            print("  python scripts/param_version_manager.py list")
            print("  python scripts/param_version_manager.py compare <v1> <v2>")
            print("  python scripts/param_version_manager.py rollback <target_version> [reason]")
            print("  python scripts/param_version_manager.py update <field_path> <new_value_json> [reason]")
    else:
        init_system_baseline()
