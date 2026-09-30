"""按请求次数限流的 filter（0.ratelimit 等）行为测试。"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

PIPELINES_DIR = Path(__file__).resolve().parent.parent / "bvai-pipelines"
REQUEST_LIMITERS = sorted(
    p.stem for p in PIPELINES_DIR.glob("*.py") if "requests_per_minute" in p.read_text()
)


def load(stem):
    spec = importlib.util.spec_from_file_location(f"req_{stem}", PIPELINES_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.Pipeline()
    p.valves = p.Valves(**{**p.valves.model_dump(), "requests_per_minute": 2, "requests_per_hour": 100,
                           "global_requests_per_minute": None, "global_requests_per_hour": None})
    return p


def send(p, task=False):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    if task:
        body["metadata"] = {"task": "tags_generation", "chat_id": "c1"}
    return asyncio.run(p.inlet(body, {"id": "u1", "role": "user"}))


def test_found_all_request_limiters():
    assert len(REQUEST_LIMITERS) == 4, REQUEST_LIMITERS


@pytest.mark.parametrize("stem", REQUEST_LIMITERS)
def test_third_chat_request_in_a_minute_is_limited(stem):
    p = load(stem)
    assert "_bvai_limit_msg" not in send(p)
    assert "_bvai_limit_msg" not in send(p)
    assert "_bvai_limit_msg" in send(p)


@pytest.mark.parametrize("stem", REQUEST_LIMITERS)
def test_background_tasks_neither_counted_nor_blocked(stem):
    """任务请求不走 bvai_limit_notice，标记会漏到上游；而且一轮聊天会附带多次任务请求，不该占用户的次数。"""
    p = load(stem)
    for _ in range(5):
        out = send(p, task=True)
        assert not any(k.startswith("_bvai") for k in out)
    assert "_bvai_limit_msg" not in send(p)  # 任务没占次数，普通请求仍有 2 次
    assert "_bvai_limit_msg" not in send(p)
    assert "_bvai_limit_msg" in send(p)  # 前提：限流本身仍然生效
    assert not any(k.startswith("_bvai") for k in send(p, task=True))  # 用户已超限，任务仍放行
