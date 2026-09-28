"""伪 testbed AW 包（T12 夹具）：真实 testbed AW 的系统边界替身。

与真包对齐的最小面：
- ``aw.<device>.<op>(**params)`` → 结果对象（``.ok``）；默认恒 ok
- 长时操作（ADR-0010）：export_logs 返回句柄，``wait_completion`` 同步完成，
  ``collect_artifacts`` 产出制品（name/kind/uri/checksum，故事 54）并记入
  real_report.json（制品文件本身不出 testbed，只回收路径与校验和）
- ``play_scenario``（composite，场景文件传递，故事 53）：
  - ``FAKE_AW_DIRECT_LINK=1`` → MBB↔仪表直通：只传引用，不拉文件
  - 否则走 Worker 降级通道：凭 scenario_id+version 从
    ``{WTS_SERVER_URL}/scenarios/{id}/{version}/file`` 拉取后"上传"
  - 拉取失败（不可达/无权限/坏内容）→ 记 env_error(scenario_unreachable)
    且结果 ok=False——环境类失败与断言失败在判决上区分（故事 53）
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

import _wts_report


class _Result:
    def __init__(self, ok=True):
        self.ok = ok


class _Handle:
    """长时操作句柄：testbed 侧异步任务的引用（伪包同步完成）。"""

    def __init__(self, op, params):
        self.op = op
        self.params = params


def wait_completion(_handle):
    return None


def collect_artifacts(handle):
    """产出制品元数据（路径+校验和）并记入 real_report.json（故事 54）。"""
    name = f"{handle.op}-{handle.params.get('log_type', 'all')}"
    artifacts = [
        {
            "name": name,
            "kind": "log_package",
            "uri": f"file:///testbed/artifacts/{name}.zip",
            "checksum": "sha256:" + hashlib.sha256(name.encode()).hexdigest()[:16],
        }
    ]
    _wts_report.update(artifacts=artifacts)
    return artifacts


def _play_scenario(scenario_id, scenario_version):
    """场景文件传递：直通优先，否则凭 ID+版本从后端拉取后上传。"""
    if os.environ.get("FAKE_AW_DIRECT_LINK") == "1":
        return _Result(True)  # MBB↔仪表直通：只传引用，不经过 Worker 拉取
    server = os.environ.get("WTS_SERVER_URL", "").rstrip("/")
    url = f"{server}/scenarios/{scenario_id}/{scenario_version}/file"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = resp.read()
        json.loads(payload)  # 内容非法同样视为环境侧问题
    except (urllib.error.URLError, ValueError, OSError) as exc:
        _wts_report.update(
            env_error={
                "code": "scenario_unreachable",
                "detail": f"场景文件 {scenario_id}@{scenario_version} 拉取失败: {exc}",
            }
        )
        return _Result(False)
    return _Result(True)


class _Namespace:
    """设备命名空间（aw.bbu / aw.ue / aw.instrument / aw.mbb）动态分发。"""

    def __init__(self, device):
        self._device = device

    def __getattr__(self, op):
        if self._device == "instrument" and op == "play_scenario":
            return _play_scenario
        if op == "export_logs":
            return lambda **params: _Handle(op, params)
        return lambda **_params: _Result(True)


def __getattr__(namespace_name):
    if namespace_name.startswith("__"):
        raise AttributeError(namespace_name)
    return _Namespace(namespace_name)
