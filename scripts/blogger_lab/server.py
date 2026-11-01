# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/server.py
V7 博主实验室独立轻量服务器启动入口 (阶段 6 核心)
严格遵循：
1. 本地单用户受控运行，严格绑定 127.0.0.1；
2. 先检测端口占用，自动寻找独立空闲端口（默认 8089，自增至 8099）；
3. 严禁占用或影响生产 8018 守护进程；
4. 明确展示持久化目录与本地访问入口。
"""

from __future__ import annotations
import os
import sys
import socket
import argparse
from pathlib import Path

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """检测指定端口是否已被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def find_available_port(start_port: int = 8089, max_attempts: int = 20, host: str = "127.0.0.1") -> int:
    """寻找可用端口，严格跳过 8018 生产端口"""
    for offset in range(max_attempts):
        candidate = start_port + offset
        if candidate == 8018:
            continue  # 绝对不可触碰生产端口
        if not is_port_in_use(candidate, host=host):
            return candidate
    raise RuntimeError(f"在 {start_port} ~ {start_port + max_attempts - 1} 范围内未找到可用端口")


def main():
    parser = argparse.ArgumentParser(description="V7 博主实验室独立轻量 API 服务")
    parser.add_argument("--port", type=int, default=None, help="指定监听端口（默认自动寻找 8089 起的空闲端口）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="绑定地址（严格限制 127.0.0.1）")
    parser.add_argument("--lab-root", type=str, default=None, help="实验持久化根目录路径")
    args = parser.parse_args()

    # 1. 严格主机绑定限制
    if args.host not in ("127.0.0.1", "localhost"):
        print("[安全阻断] 博主实验室仅允许绑定本地回环地址 127.0.0.1，禁止暴露外部网络！")
        sys.exit(1)

    # 2. 端口选择与生产隔离检查
    target_port = args.port
    if target_port == 8018:
        print("[安全阻断] 端口 8018 为生产守护进程专用端口，严禁占用！请使用默认 8089 或其他独立端口。")
        sys.exit(1)

    if target_port is None:
        target_port = find_available_port(start_port=8089, host=args.host)
    else:
        if is_port_in_use(target_port, host=args.host):
            print(f"[错误] 指定端口 {target_port} 已被占用，请更换端口或直接启动使用默认空闲端口。")
            sys.exit(1)

    # 3. 确定数据持久化目录
    lab_root_dir = Path(args.lab_root) if args.lab_root else Path(_BASE) / "data" / "blogger_lab"
    lab_root_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("  [*] V7 博主量化实验室 · 独立轻量工作台服务")
    print(f"  • 绑定地址: {args.host}:{target_port}")
    print(f"  • Web 操作入口: http://{args.host}:{target_port}/")
    print(f"  • API 根路径:  http://{args.host}:{target_port}/api/blogger-lab")
    print(f"  • 持久化根目录: {lab_root_dir.resolve()}")
    print("  • 生产隔离状态: 生产 8018 端口已独立隔离，不受本次运行影响")
    print("  • 安全运行规范: 本地单用户受控运行，禁止公开预览或穿透 Tunnel")
    print("=" * 64)

    import uvicorn
    # 无论通过环境变量还是显式参数均将 lab_root 传入
    resolved_root = str(lab_root_dir.resolve())
    os.environ["BLOGGER_LAB_ROOT"] = resolved_root

    from blogger_lab.api import create_app
    app_instance = create_app(lab_root=resolved_root)

    uvicorn.run(
        app_instance,
        host=args.host,
        port=target_port,
        reload=False,
        log_level="info"
    )


if __name__ == "__main__":
    main()
