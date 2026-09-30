"""
title: 限流提示转发器
author: BVAI
version: 1.1.0
description: 将外部 pipelines 限流器的提示消息透传给用户。OpenWebUI 调用外部 pipeline filter 时先执行 raise_for_status()，aiohttp 随即释放响应体，导致后续读取 detail 失败并退回 "Internal Server Error"，限流器配置的提示因此到不了用户眼前。内置 Function 抛出的异常消息则能原样透传，故由本 filter 代为抛出。另外，限流器裁剪超长对话后会写入丢弃条数，由本 filter 以状态行告知用户。
required_open_webui_version: 0.5.0
"""

from pydantic import BaseModel, Field
from typing import Awaitable, Callable, Optional

# 与 bvai-pipelines 限流器约定的字段名。限流器判定超限后写入该字段并放行，
# 由本 filter 读取、移除并抛出，使消息走内置 Function 的透传路径。
MARKER = "_bvai_limit_msg"
# 限流器把超长对话裁短后在该字段写入丢弃的消息条数，由本 filter 以状态行告知用户。
TRIM_MARKER = "_bvai_trim_notice"


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-100,
            description="执行优先级，数字越小越先执行。设为负值以先于其它 filter 运行，避免已被限流的请求继续消耗后续处理",
        )
        enabled: bool = Field(default=True, description="是否启用消息透传")

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> dict:
        # 无论是否启用都必须移除标记，否则该字段会随请求发往上游 API 造成参数错误
        if not isinstance(body, dict):
            return body
        msg = body.pop(MARKER, None)
        dropped = body.pop(TRIM_MARKER, None)
        if not self.valves.enabled:
            return body
        if msg:
            raise Exception(msg)
        if dropped and __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"对话较长，最早的 {dropped} 条消息未发送给模型。",
                        "done": True,
                    },
                }
            )
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
