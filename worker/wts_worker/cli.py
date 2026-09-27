"""Worker 命令行。

T1 仅提供 ``check``：验证 Worker 到后端的连通性（健康检查）。
注册/心跳/领取循环在 T8（沙盒通路）与 T12（真实执行）中加入。
"""
import argparse
import sys
from typing import Optional, Sequence

import httpx


def check_backend(server: str, timeout: float = 5.0) -> int:
    url = server.rstrip("/") + "/health"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"后端不可达或响应异常: {exc}", file=sys.stderr)
        return 1
    if body.get("status") != "ok":
        print(f"后端健康检查返回非 ok: {body}", file=sys.stderr)
        return 1
    print(f"后端健康: {url} -> ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wts-worker", description="WTS 拉取式执行 Worker")
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="检查到后端的连通性")
    check_parser.add_argument("--server", default="http://localhost:8000", help="后端基地址")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return check_backend(args.server)
    return 2


if __name__ == "__main__":
    sys.exit(main())
