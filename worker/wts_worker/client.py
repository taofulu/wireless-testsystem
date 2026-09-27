"""Worker HTTP 客户端：后端 REST 接口的薄封装（T8）。

设计锚点（ADR-0003）：
- 拉取式轮询：Worker 主动 claim，后端不推送
- 幂等领取：断网重试时同一 Worker 拿回同一任务，不重复执行（故事 29）
- 心跳超时被摘除：Worker 应重新注册
"""
from typing import Any, List, Optional

import httpx


class WorkerClient:
    """拉取式执行 Worker 的后端客户端。

    每个 WorkerClient 实例绑定一个 worker_id + capabilities；注册/心跳/
    领取/回传均携带此身份。
    """

    def __init__(
        self,
        base_url: str,
        worker_id: str,
        capabilities: List[str],
        sim_package_version: Optional[str] = None,
        topology_tags: Optional[List[str]] = None,
    ):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0)
        self.worker_id = worker_id
        self.capabilities = capabilities
        self.sim_package_version = sim_package_version
        self.topology_tags = topology_tags

    # -----------------------------------------------------------------------
    # Worker 注册与保活
    # -----------------------------------------------------------------------

    def register(self) -> dict:
        """Worker 启动注册；心跳超时被摘除后需重新调用。"""
        resp = self._client.post(
            "/worker/register",
            json={
                "worker_id": self.worker_id,
                "capabilities": self.capabilities,
                **self._opt("sim_package_version"),
                **self._opt("topology_tags"),
            },
        )
        resp.raise_for_status()
        return resp.json()

    def heartbeat(self) -> dict:
        """Worker 保活心跳；返回 404 时说明已被摘除，需重新 register。"""
        resp = self._client.post(
            "/worker/heartbeat", json={"worker_id": self.worker_id}
        )
        resp.raise_for_status()
        return resp.json()

    # -----------------------------------------------------------------------
    # 任务领取与心跳
    # -----------------------------------------------------------------------

    def claim(self) -> Optional[dict]:
        """领取任务；204 表示无可领任务。"""
        resp = self._client.post(
            "/worker/tasks/claim",
            json={
                "worker_id": self.worker_id,
                "capabilities": self.capabilities,
            },
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def task_heartbeat(self, task_id: int) -> None:
        """任务执行中心跳：防止超时被其他 Worker 重领。"""
        resp = self._client.post(
            f"/worker/tasks/{task_id}/heartbeat",
            json={"worker_id": self.worker_id},
        )
        resp.raise_for_status()

    # -----------------------------------------------------------------------
    # 代码拉取与结果回传
    # -----------------------------------------------------------------------

    def fetch_code(self, executable_case_id: int) -> str:
        """拉取可执行用例全文（由 Worker 本地临时落盘，故事 30）。"""
        resp = self._client.get(f"/executable-cases/{executable_case_id}/code")
        resp.raise_for_status()
        return resp.json()["code"]

    def fetch_sandbox_context(self, executable_case_id: int, task_id: int) -> dict:
        """拉取沙盒内核执行上下文（T9）：逐步骤仿真供给 + 该任务的调试预设。

        携带 task_id 使后端按任务行取 preset；预设只属于本次调试会话。
        """
        resp = self._client.get(
            f"/executable-cases/{executable_case_id}/sandbox-context",
            params={"task_id": task_id},
        )
        resp.raise_for_status()
        return resp.json()

    def submit_result(
        self,
        task_id: int,
        verdict: str,
        logs: str,
        step_results: List[Any],
        artifacts: List[Any],
    ) -> dict:
        """执行完成后回传结果；409 表示该结果已记录（重试安全）。"""
        resp = self._client.post(
            f"/worker/tasks/{task_id}/result",
            json={
                "worker_id": self.worker_id,
                "verdict": verdict,
                "logs": logs,
                "step_results": step_results,
                "artifacts": artifacts,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def _opt(self, name: str) -> dict:
        value = getattr(self, name)
        return {name: value} if value is not None else {}
