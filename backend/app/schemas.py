"""API 输入输出 schema（Pydantic）。字段命名与 CONTEXT.md / spec 数据模型对齐。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TextCaseCreate(BaseModel):
    """新建文本用例：三栏分栏录入，落库为 draft 态。

    三栏允许留空——草稿常是半成品，稍后从列表回来补全（用户故事 2）。
    """

    title: str = Field(min_length=1, max_length=200)
    precondition: str = ""
    steps_text: str = ""
    expected_text: str = ""


class TextCasePatch(BaseModel):
    """草稿继续编辑：仅允许修改三栏内容与标题，不回退状态。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    precondition: Optional[str] = None
    steps_text: Optional[str] = None
    expected_text: Optional[str] = None


class TextCaseOut(BaseModel):
    """文本用例对外视图（录入阶段字段子集，追溯/拓扑字段后续票补充）。"""

    id: int
    title: str
    precondition: str
    steps_text: str
    expected_text: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
