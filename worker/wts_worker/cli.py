"""Worker 命令行。

T1 仅提供 ``check``：验证 Worker 到后端的连通性（健康检查）。
T8 加入 ``run``：启动 Worker 注册/心跳/轮询领取/执行/回传主循环（沙盒与
真实双能力）。
"""
import argparse
import sys
from pathlib import Path
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

    run_parser = sub.add_parser("run", help="启动 Worker 主循环（注册/心跳/轮询/执行/回传）")
    run_parser.add_argument("--server", default="http://localhost:8000", help="后端基地址")
    run_parser.add_argument(
        "--worker-id", default="wts-worker-local", help="Worker 唯一标识"
    )
    run_parser.add_argument(
        "--capabilities",
        default="sandbox",
        help="声明能力，逗号分隔（sandbox / real）",
    )
    run_parser.add_argument(
        "--work-root",
        default="/tmp/wts-worker",
        help="任务执行临时目录根（自动创建）",
    )
    run_parser.add_argument(
        "--poll-interval", type=float, default=2.0, help="空队列轮询间隔（秒）"
    )
    run_parser.add_argument(
        "--heartbeat-interval", type=float, default=5.0, help="保活心跳间隔（秒）"
    )
    run_parser.add_argument(
        "--task-timeout", type=float, default=600.0, help="单条 pytest 执行超时（秒）"
    )
    run_parser.add_argument(
        "--once", action="store_true", help="执行一轮即退出（测试与手动调试用）"
    )
    run_parser.add_argument(
        "--sim-package-version", default=None, help="仿真包版本号（沙盒 Worker 声明）"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return check_backend(args.server)
    if args.command == "run":
        from wts_worker.run import run_worker

        capabilities = [c.strip() for c in args.capabilities.split(",")]
        return run_worker(
            server=args.server,
            worker_id=args.worker_id,
            capabilities=capabilities,
            work_root=Path(args.work_root),
            poll_interval=args.poll_interval,
            heartbeat_interval=args.heartbeat_interval,
            task_timeout=args.task_timeout,
            sim_package_version=args.sim_package_version,
            once=args.once,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
