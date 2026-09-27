"""Worker 轮询执行循环（T8）。

链路：注册 → 心跳线程保活 → 轮询 claim → 拉代码 → 真实 pytest 子进程 →
回传结果 → 下一轮。

保活语义（ADR-0009 / Issue #9 验收）：
- Worker 级心跳由独立线程周期发出（空闲与执行中都保活）；心跳 404 说明
  已被超时摘除，自动重新注册
- 任务级心跳在执行期间随心跳线程发出，防止长任务被误判崩溃而重领
- 断网重试：注册/领取/回传遇网络错误均按间隔重试；后端幂等保证同一
  任务不被重复派发、结果不被重复记录（故事 29/32）
"""
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import httpx

from wts_worker.client import WorkerClient
from wts_worker.runner import TaskOutcome, run_pytest_case


class _Heartbeat:
    """心跳线程：周期发 Worker 心跳；有在执任务时同频发任务心跳。"""

    def __init__(self, client: WorkerClient, interval: float):
        self._client = client
        self._interval = interval
        self._current_task: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="wts-heartbeat", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def set_task(self, task_id: Optional[int]) -> None:
        with self._lock:
            self._current_task = task_id

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            task_id = None
            with self._lock:
                task_id = self._current_task
            try:
                self._client.heartbeat()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    try:  # 已被超时摘除：重新注册
                        self._client.register()
                    except httpx.HTTPError:
                        pass
            except httpx.HTTPError:
                pass  # 断网：下轮重试，后端超时前不会摘除
            if task_id is not None:
                try:
                    self._client.task_heartbeat(task_id)
                except httpx.HTTPError:
                    pass


def _register_until_ok(client: WorkerClient, retry_interval: float) -> None:
    """启动注册：后端暂不可达时按间隔重试（Worker 部署侧常态）。"""
    while True:
        try:
            client.register()
            return
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[wts-worker] 注册失败，{retry_interval}s 后重试: {exc}", file=sys.stderr)
            time.sleep(retry_interval)


def _claim_with_retry(client: WorkerClient, retry_interval: float) -> Optional[dict]:
    """单次领取尝试；网络错误返回 None（下轮再来），未注册则重新注册。"""
    try:
        return client.claim()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _register_until_ok(client, retry_interval)
        else:
            print(f"[wts-worker] claim 被拒绝: {exc}", file=sys.stderr)
    except httpx.HTTPError as exc:
        print(f"[wts-worker] claim 网络错误: {exc}", file=sys.stderr)
    return None


def _submit_with_retry(
    client: WorkerClient, task_id: int, outcome: TaskOutcome, retry_interval: float
) -> None:
    """结果回传：网络错误重试；409 表示已记录（响应丢失后的重试安全）。"""
    while True:
        try:
            client.submit_result(
                task_id,
                verdict=outcome.verdict,
                logs=outcome.logs,
                step_results=outcome.step_results,
                artifacts=outcome.artifacts,
            )
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return  # 已记录：结果未丢失，不重复执行语义由后端保证
            print(f"[wts-worker] 回传被拒绝: {exc}", file=sys.stderr)
            return
        except httpx.HTTPError as exc:
            print(f"[wts-worker] 回传网络错误，{retry_interval}s 后重试: {exc}", file=sys.stderr)
            time.sleep(retry_interval)


def run_worker(
    server: str,
    worker_id: str,
    capabilities: List[str],
    *,
    work_root: Path,
    poll_interval: float = 2.0,
    heartbeat_interval: float = 5.0,
    task_timeout: float = 600.0,
    sim_package_version: Optional[str] = None,
    once: bool = False,
) -> int:
    """Worker 主循环；返回进程退出码。"""
    client = WorkerClient(
        server,
        worker_id,
        list(capabilities),
        sim_package_version=sim_package_version,
    )
    _register_until_ok(client, poll_interval)
    print(f"[wts-worker] 已注册: {worker_id} capabilities={capabilities}")

    heartbeat = _Heartbeat(client, heartbeat_interval)
    heartbeat.start()
    try:
        while True:
            task = _claim_with_retry(client, poll_interval)
            if task is None:
                if once:
                    return 0
                time.sleep(poll_interval)
                continue

            task_id = task["task_id"]
            exec_id = task["executable_case_id"]
            print(f"[wts-worker] 领取任务 {task_id}（exec_case={exec_id}）")
            heartbeat.set_task(task_id)
            try:
                code = client.fetch_code(exec_id)
                outcome = run_pytest_case(code, work_root, timeout_seconds=task_timeout)
            except Exception as exc:  # Worker 自身不随任务崩溃
                outcome = TaskOutcome(verdict="failed", logs=f"worker 内部错误: {exc}")
            finally:
                heartbeat.set_task(None)
            print(f"[wts-worker] 任务 {task_id} 判决: {outcome.verdict}")
            _submit_with_retry(client, task_id, outcome, poll_interval)

            if once:
                return 0
    except KeyboardInterrupt:
        return 130
    finally:
        heartbeat.stop()
        client.close()
