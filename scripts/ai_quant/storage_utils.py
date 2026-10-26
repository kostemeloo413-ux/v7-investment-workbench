# -*- coding: utf-8 -*-
"""
scripts/ai_quant/storage_utils.py
V7 治理与安全基础设施：原子写与文件持久化核心工具 (Atomic Persistence Utilities)

【最高安全与健壮性契约】：
1. 严禁直接 open(target, "w") 覆盖关键账户/持仓快照，防止断电导致 0 字节截断损坏；
2. 规范写入流程：写入临时文件 (.tmp) -> flush 刷新用户空间缓冲 -> os.fsync 强制操作系统落盘 -> os.replace 原子重命名替换；
3. 支持 JSON 格式化与数据完整性校验；
4. 发生任何写入异常时安全清理 .tmp 临时文件，确保原目标文件毫发无损；
5. 提供安全 JSON 读取辅助，支持备份文件灾备恢复。
"""

import os
import json
import tempfile
import time


def atomic_write_json(file_path, data, indent=2, ensure_ascii=False):
    """
    原子写入 JSON 字典或列表对象至目标路径：
    1. 在同级目录创建临时文件；
    2. json.dump 写入数据；
    3. f.flush() 确保 Python 缓冲区排空；
    4. os.fsync(f.fileno()) 强制操作系统同步到物理存储介质；
    5. 关闭句柄；
    6. os.replace(tmp, file_path) 原子替换目标文件。
    """
    abs_path = os.path.abspath(file_path)
    dir_name = os.path.dirname(abs_path)
    os.makedirs(dir_name, exist_ok=True)

    base_name = os.path.basename(abs_path)
    # 在同一文件系统目录生成临时文件，保证 os.replace 是原子重命名而非跨卷拷贝
    fd, tmp_path = tempfile.mkstemp(prefix=f".{base_name}.tmp_", dir=dir_name)
    try:
        with open(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp_path, abs_path)
                break
            except OSError as exc:
                # Windows scanners/readers can briefly deny a rename. Retry
                # only these two OS errors, retaining the same fsynced temp
                # file and a hard 250 ms wait cap. Persistent errors still fail.
                if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 5:
                    raise
                time.sleep(0.05)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise IOError(f"[atomic_write_json] 写入 {abs_path} 失败: {e}") from e


def load_json_safe(file_path, default=None):
    """安全读取 JSON 文件，若文件损坏、截断或不存在，返回 default"""
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


class CriticalStateStatus:
    """关键状态读取分类状态字 (SAFE-STRICT-STATE-R1)"""
    VALID = "VALID"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    PARSE_ERROR = "PARSE_ERROR"
    TYPE_ERROR = "TYPE_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"


class CriticalStateResult:
    """关键状态加载结果包装体 (严密区分合法空值与异常损坏)"""
    def __init__(self, status, value=None, path=None, error=None, raw_lines=0, malformed_lines=0):
        self.status = status
        self.value = value
        self.path = os.path.abspath(path) if path else None
        self.error = error
        self.raw_lines = raw_lines
        self.malformed_lines = malformed_lines

    @property
    def is_valid(self):
        return self.status == CriticalStateStatus.VALID

    @property
    def is_missing(self):
        return self.status == CriticalStateStatus.MISSING

    @property
    def is_corrupt(self):
        """文件存在但不可用或损坏 (0字节、语法错误、类型非法、字段残缺)"""
        return self.status in (
            CriticalStateStatus.EMPTY,
            CriticalStateStatus.PARSE_ERROR,
            CriticalStateStatus.TYPE_ERROR,
            CriticalStateStatus.SCHEMA_ERROR
        )

    def __repr__(self):
        return f"<CriticalStateResult status={self.status} error={self.error}>"


# =====================================================================
# SAFE-STRICT-STATE-R2: 关键治理、执行与账本文件的标准最小约束 Schema
# =====================================================================

HALT_REQUIRED_FIELDS = ['new_entry_disabled', 'risk_exit_enabled', 'execution_state_safe', 'halt_reasons']
HALT_FIELD_TYPES = {
    'new_entry_disabled': bool,
    'risk_exit_enabled': bool,
    'execution_state_safe': bool,
    'halt_reasons': list
}

REGISTRY_REQUIRED_FIELDS = ['records']
REGISTRY_FIELD_TYPES = {
    'records': dict
}

ACCOUNT_REQUIRED_FIELDS = ['account_id', 'cash', 'total_equity']
ACCOUNT_FIELD_TYPES = {
    'account_id': str,
    'cash': (int, float),
    'total_equity': (int, float),
    'initial_capital': (int, float),
    'market_value': (int, float),
    'net_pnl': (int, float),
    'net_pnl_pct': (int, float),
    'peak_equity': (int, float),
    'max_drawdown_pct': (int, float),
}


def validate_positions_schema(pos_list):
    """
    positions.json 业务 Schema 校验器 (R2 强化):
    - 根类型必须为 list
    - 内部每项必须为 dict
    - 每项必须具备合法 symbol (str 非空)
    - 若含 shares，必须为数值类型且不能为 bool
    - 若含 cost_price，必须为数值类型且不能为 bool
    """
    if not isinstance(pos_list, list):
        return False, f"根数据类型必须为 list，实际为 {type(pos_list).__name__}"
    for idx, p in enumerate(pos_list):
        if not isinstance(p, dict):
            return False, f"持仓项 #{idx} 类型必须为 dict，实际为 {type(p).__name__}"
        sym = p.get('symbol')
        if not sym or not isinstance(sym, str) or not sym.strip():
            return False, f"持仓项 #{idx} 缺少合法 symbol 字段"
        if 'shares' in p:
            val = p['shares']
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False, f"持仓项 #{idx} shares 必须为数值型，实际为 {type(val).__name__}"
        if 'cost_price' in p:
            val = p['cost_price']
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False, f"持仓项 #{idx} cost_price 必须为数值型，实际为 {type(val).__name__}"
    return True, None


def strict_load_json(file_path, expected_type=None, required_fields=None, field_types=None, validator=None):
    """
    关键状态严格 JSON 读取器 (SAFE-STRICT-STATE-R2 强化):
    - 文件不存在: status=MISSING, value=None
    - 0 字节或纯空白: status=EMPTY, value=None
    - JSON 解析异常: status=PARSE_ERROR, value=None
    - 根类型不匹配: status=TYPE_ERROR, value=None
    - 缺少必填字段: status=SCHEMA_ERROR, value=None
    - 字段类型不匹配: status=SCHEMA_ERROR, value=None
    - 业务 Validator 未通过: status=SCHEMA_ERROR, value=None
    - 正常解析并合规: status=VALID, value=解析出的对象
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return CriticalStateResult(CriticalStateStatus.MISSING, value=None, path=abs_path)

    try:
        size = os.path.getsize(abs_path)
        if size == 0:
            return CriticalStateResult(CriticalStateStatus.EMPTY, value=None, path=abs_path, error="文件存在但大小为 0 字节")
        with open(abs_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if not content.strip():
            return CriticalStateResult(CriticalStateStatus.EMPTY, value=None, path=abs_path, error="文件内容全为空白字符")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return CriticalStateResult(CriticalStateStatus.PARSE_ERROR, value=None, path=abs_path, error=f"JSON解析语法错误: {e}")
    except Exception as e:
        return CriticalStateResult(CriticalStateStatus.PARSE_ERROR, value=None, path=abs_path, error=f"读取异常: {e}")

    if expected_type is not None and not isinstance(data, expected_type):
        exp_name = expected_type.__name__ if hasattr(expected_type, '__name__') else str(expected_type)
        got_name = type(data).__name__
        return CriticalStateResult(
            CriticalStateStatus.TYPE_ERROR, value=data, path=abs_path,
            error=f"根数据类型不匹配: 期望 {exp_name}，实际为 {got_name}"
        )

    if required_fields and isinstance(data, dict):
        missing = [f for f in required_fields if f not in data]
        if missing:
            return CriticalStateResult(
                CriticalStateStatus.SCHEMA_ERROR, value=data, path=abs_path,
                error=f"缺少关键必填字段: {missing}"
            )

    if field_types and isinstance(data, dict):
        for field, exp_t in field_types.items():
            if field in data:
                val = data[field]
                if exp_t in (int, float, (int, float), (float, int)):
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        return CriticalStateResult(
                            CriticalStateStatus.SCHEMA_ERROR, value=data, path=abs_path,
                            error=f"字段 [{field}] 类型错误: 期望数值型(int/float)，实际为 {type(val).__name__}"
                        )
                elif exp_t is bool:
                    if not isinstance(val, bool):
                        return CriticalStateResult(
                            CriticalStateStatus.SCHEMA_ERROR, value=data, path=abs_path,
                            error=f"字段 [{field}] 类型错误: 期望布尔型(bool)，实际为 {type(val).__name__}"
                        )
                elif not isinstance(val, exp_t):
                    exp_name = exp_t.__name__ if hasattr(exp_t, '__name__') else str(exp_t)
                    return CriticalStateResult(
                        CriticalStateStatus.SCHEMA_ERROR, value=data, path=abs_path,
                        error=f"字段 [{field}] 类型错误: 期望 {exp_name}，实际为 {type(val).__name__}"
                    )

    if validator and callable(validator):
        ok, v_err = validator(data)
        if not ok:
            return CriticalStateResult(
                CriticalStateStatus.SCHEMA_ERROR, value=data, path=abs_path,
                error=f"Schema业务约束校验未通过: {v_err}"
            )

    return CriticalStateResult(CriticalStateStatus.VALID, value=data, path=abs_path)


def strict_read_jsonl(file_path):
    """
    关键账本严格 JSONL 读取器 (SAFE-STRICT-STATE-R1):
    - 文件不存在: status=MISSING, value=[]
    - 文件存在但 0 字节或纯空白: status=VALID, value=[] (合法空账本)
    - 文件中含有坏行/截断行: status=PARSE_ERROR, value=成功解析的行, malformed_lines > 0
    - 全行解析合法: status=VALID, value=全部记录
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return CriticalStateResult(CriticalStateStatus.MISSING, value=[], path=abs_path)

    records = []
    malformed = 0
    raw = 0
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                raw += 1
                try:
                    records.append(json.loads(s))
                except Exception:
                    malformed += 1
    except Exception as e:
        return CriticalStateResult(CriticalStateStatus.PARSE_ERROR, value=[], path=abs_path, error=f"读取异常: {e}")

    if malformed > 0:
        return CriticalStateResult(
            CriticalStateStatus.PARSE_ERROR, value=records, path=abs_path,
            error=f"检测到 {malformed} 行格式损坏行", raw_lines=raw, malformed_lines=malformed
        )

    return CriticalStateResult(CriticalStateStatus.VALID, value=records, path=abs_path, raw_lines=raw, malformed_lines=0)


def atomic_rewrite_jsonl(file_path, records, ensure_ascii=False):
    """
    FIX-1: 原子重写 JSONL 文件工具
    用于 Recovery 阶段对 fills.jsonl / trade_results.jsonl 等账本的整文件过滤重写：
    1. 在目标文件同级目录创建临时文件；
    2. 逐行 json.dumps 写入记录并追加换行；
    3. f.flush() 排空缓冲区；
    4. os.fsync(f.fileno()) 强制物理介质刷盘；
    5. 关闭句柄；
    6. os.replace(tmp, file_path) 原子替换目标文件；
    7. 发生任何异常时确保原 target 保持不变，并安全清理 .tmp 临时文件。
    """
    abs_path = os.path.abspath(file_path)
    dir_name = os.path.dirname(abs_path)
    os.makedirs(dir_name, exist_ok=True)

    base_name = os.path.basename(abs_path)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{base_name}.tmp_", dir=dir_name)
    try:
        with open(fd, 'w', encoding='utf-8') as f:
            for rec in records:
                line = json.dumps(rec, ensure_ascii=ensure_ascii) + '\n'
                f.write(line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise IOError(f"[atomic_rewrite_jsonl] 重写 {abs_path} 失败: {e}") from e
