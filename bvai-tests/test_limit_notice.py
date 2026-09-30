"""bvai_limit_notice（open-webui 内置 Function）行为测试。"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "bvai-pipelines" / "_openwebui-function" / "bvai_limit_notice.py"


def load():
    spec = importlib.util.spec_from_file_location("bvai_limit_notice", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Filter()


def recorder():
    events = []

    async def emit(event):
        events.append(event)

    return events, emit


def test_trim_notice_is_removed_and_shown_as_status():
    f = load()
    events, emit = recorder()
    body = {"messages": [], "_bvai_trim_notice": 12}
    out = asyncio.run(f.inlet(body, {"id": "u1"}, __event_emitter__=emit))
    assert "_bvai_trim_notice" not in out  # 不能随请求发往上游
    assert events == [{"type": "status", "data": {"description": "对话较长，最早的 12 条消息未发送给模型。", "done": True}}]


def test_trim_notice_removed_even_when_disabled():
    f = load()
    f.valves.enabled = False
    events, emit = recorder()
    out = asyncio.run(f.inlet({"_bvai_trim_notice": 3}, None, __event_emitter__=emit))
    assert "_bvai_trim_notice" not in out
    assert events == []


def test_trim_notice_without_emitter_does_not_crash():
    f = load()
    out = asyncio.run(f.inlet({"_bvai_trim_notice": 3}, None))
    assert "_bvai_trim_notice" not in out


def test_limit_message_still_raises_and_clears_both_markers():
    f = load()
    body = {"_bvai_limit_msg": "用户 X token限制已超过。", "_bvai_trim_notice": 3}
    with pytest.raises(Exception, match="用户 X token限制已超过"):
        asyncio.run(f.inlet(body, None))
    assert "_bvai_trim_notice" not in body and "_bvai_limit_msg" not in body
