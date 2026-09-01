"""
title: 限流提示转发器
author: BVAI
version: 1.0.0
description: 将外部 pipelines 限流器的提示消息透传给用户。OpenWebUI 调用外部 pipeline filter 时先执行 raise_for_status()，aiohttp 随即释放响应体，导致后续读取 detail 失败并退回 "Internal Server Error"，限流器精心配置的中文提示因此永远到不了用户眼前。内置 Function 抛出的异常消息则能原样透传，故由本 filter 代为抛出。
required_open_webui_version: 0.5.0
"""

from pydantic import BaseModel, Field
from typing import Optional

# 与 bvai-pipelines 限流器约定的字段名。限流器判定超限后写入该字段并放行，
# 由本 filter 读取、移除并抛出，使消息走内置 Function 的透传路径。
MARKER = "_bvai_limit_msg"


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-100,
            description="执行优先级，数字越小越先执行。设为负值以先于其它 filter 运行，避免已被限流的请求继续消耗后续处理",
        )
        enabled: bool = Field(default=True, description="是否启用消息透传")

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        # 无论是否启用都必须移除标记，否则该字段会随请求发往上游 API 造成参数错误
        msg = body.pop(MARKER, None) if isinstance(body, dict) else None
        if msg and self.valves.enabled:
            raise Exception(msg)
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
