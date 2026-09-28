"""lass 模块：LASS 环境校验客户端（T11，系统边界；spec 模块划分 execution 的职责之一）。

契约（spec Further Notes：联调前冻结，三值 + 差异字段）：

    POST {lass_api_url}/env/check
    请求体: {"required_topology": {...} | null}
    响应体: {"result": "ready"}
          | {"result": "needs_create", "missing": [{"resource": ...}, ...]}
          | {"result": "needs_modify", "diff": "...", "changes": [...]}

三值语义（CONTEXT.md 环境校验结果）：
- ready        已满足，可直接执行
- needs_create 无匹配环境，detail.missing 为缺失资源清单（故事 20）
- needs_modify 已有环境但需调整，detail.diff 为差异说明（故事 21）

边界诚实：LASS 未配置/不可达/超时/响应非法一律抛 :class:`LassUnavailable`——
校验未发生就不产生任何环境结论，绝不默认 ready（不假绿）。阻断分支由环境
中台处理后经 recheck 闭环（故事 22）。
"""
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import settings

# 三值结果集（CONTEXT.md）；其余取值按非法响应处理
ENV_CHECK_RESULTS = ("ready", "needs_create", "needs_modify")


class LassUnavailable(Exception):
    """LASS 未配置/不可达/响应非法：校验未发生；路由转 503。"""


@dataclass(frozen=True)
class EnvCheckOutcome:
    """一次 LASS 校验的规范化结论。detail 承载缺失清单/差异说明原文。"""

    result: str  # ENV_CHECK_RESULTS 之一
    detail: dict[str, Any] = field(default_factory=dict)


def check_topology(required_topology: Optional[dict]) -> EnvCheckOutcome:
    """调用 LASS 校验所需拓扑，返回三值结论。

    响应体只信任 result 字段的枚举值；其余字段原样保留进 detail（缺失清单、
    差异说明的 schema 归环境中台演进，本系统透传呈现）。响应非法（非 JSON、
    result 不在三值内）同样视为 LassUnavailable——坏契约不等于"已满足"。
    """
    if not settings.lass_api_url:
        raise LassUnavailable("LASS 未配置（WTS_LASS_API_URL 为空），无法执行环境校验")

    url = settings.lass_api_url.rstrip("/") + "/env/check"
    try:
        resp = httpx.post(
            url,
            json={"required_topology": required_topology},
            timeout=settings.lass_timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as exc:
        raise LassUnavailable(f"LASS 校验请求失败: {exc}") from exc
    except ValueError as exc:
        raise LassUnavailable(f"LASS 响应不是合法 JSON: {exc}") from exc

    if not isinstance(body, dict) or body.get("result") not in ENV_CHECK_RESULTS:
        raise LassUnavailable(f"LASS 响应非法（result 须为三值之一）: {body!r}")

    result = body["result"]
    detail = {k: v for k, v in body.items() if k != "result"}
    if result == "needs_create" and not isinstance(detail.get("missing"), list):
        raise LassUnavailable(f"needs_create 响应缺 missing 清单: {body!r}")
    if result == "needs_modify" and "diff" not in detail:
        raise LassUnavailable(f"needs_modify 响应缺 diff 差异说明: {body!r}")
    return EnvCheckOutcome(result=result, detail=detail)
