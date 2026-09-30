"""BVAI token 限流器行为测试。

运行：uv run --quiet --with pytest --with pydantic pytest bvai-tests -q

所有断言都针对 bvai-pipelines/ 下真正会被部署的文件，逐个参数化执行。
"""
import asyncio
import copy
import importlib.util
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PIPELINES_DIR = ROOT / "bvai-pipelines"
TEMPLATE = ROOT / "bvai-tools" / "token_limit_methods.py"
SPLIT_MARKER = "    async def on_startup"

TOKEN_LIMITERS = sorted(
    p.stem for p in PIPELINES_DIR.glob("*.py") if "tokens_per_minute" in p.read_text()
)

# 测试用配额：每 4 个字符算 1 token，单次上限 = min(1000, 3000) = 1000
TEST_VALVES = dict(
    default_tokens_per_1k_chars=1000,
    tokens_per_minute=1000,
    tokens_per_hour=3000,
    global_tokens_per_minute=None,
    global_tokens_per_hour=None,
    image_token_cost=100,
)
CAP = 1000


def load(stem, **overrides):
    spec = importlib.util.spec_from_file_location(f"lim_{stem}", PIPELINES_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.Pipeline()
    p.valves = p.Valves(**{**p.valves.model_dump(), **TEST_VALVES, **overrides})
    return p


def text(tag, tokens):
    """恰好 `tokens` 个 token 的文本（按 len//4 估算），以 tag 开头便于辨认。"""
    s = f"[{tag}]"
    return s + "字" * (tokens * 4 - len(s))


def m(role, tag, tokens, **extra):
    return {"role": role, "content": text(tag, tokens), **extra}


def est(messages):
    """与被测估算口径一致：纯文本消息按 len//4。"""
    return sum(len(x["content"]) // 4 for x in messages if isinstance(x.get("content"), str))


def run(p, messages, role="user", uid="u1"):
    body = {"messages": copy.deepcopy(messages)}
    return asyncio.run(p.inlet(body, {"id": uid, "role": role}))


def long_chat(rounds=10):
    msgs = [m("system", "sys", 50)]
    for i in range(rounds):
        msgs += [m("user", f"u{i}", 100), m("assistant", f"a{i}", 100)]
    msgs.append(m("user", "last", 100))
    return msgs


def test_found_all_token_limiters():
    assert len(TOKEN_LIMITERS) == 15, TOKEN_LIMITERS


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_short_chat_passes_unchanged(stem):
    p = load(stem)
    msgs = [m("system", "sys", 50), m("user", "u0", 100), m("assistant", "a0", 100), m("user", "last", 100)]
    assert est(msgs) <= CAP  # 前提：没超上限
    out = run(p, msgs)
    assert out["messages"] == msgs
    assert "_bvai_limit_msg" not in out
    assert "_bvai_trim_notice" not in out


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_long_chat_is_trimmed_instead_of_rejected(stem):
    p = load(stem)
    msgs = long_chat()
    assert est(msgs) > CAP  # 前提：确实超过单次上限（旧逻辑会永久拒绝）
    out = run(p, msgs)
    kept = out["messages"]
    assert "_bvai_limit_msg" not in out
    assert kept[0] == msgs[0]  # system 保留
    assert kept[1]["role"] == "user"  # 切点落在 user
    assert kept[-1] == msgs[-1]  # 最新提问保留
    assert est(kept) <= CAP
    # 尽量多留：50 + 4 轮×200 + 100 = 950 ≤ 1000，再多一轮就超
    assert len(kept) == 1 + 4 * 2 + 1
    assert kept[1:] == msgs[-9:]
    assert out["_bvai_trim_notice"] == 12  # 22 条里丢了 12 条


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_trim_never_leaves_orphan_tool_messages(stem):
    p = load(stem)
    msgs = [m("system", "sys", 50)]
    for i in range(5):
        msgs += [
            m("user", f"u{i}", 100),
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                                                                  "function": {"name": "search", "arguments": "{}"}}]},
            m("tool", f"t{i}", 150, tool_call_id=f"c{i}"),
            m("assistant", f"a{i}", 100),
        ]
    msgs.append(m("user", "last", 100))
    assert est(msgs) > CAP  # 前提
    out = run(p, msgs)
    assert "_bvai_limit_msg" not in out and "_bvai_trim_notice" in out  # 确实走了裁剪分支
    kept = out["messages"]
    assert len(kept) < len(msgs)
    assert kept[1]["role"] == "user"
    call_ids = {c["id"] for x in kept for c in x.get("tool_calls") or []}
    tool_ids = {x["tool_call_id"] for x in kept if x["role"] == "tool"}
    assert tool_ids and tool_ids == call_ids


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_current_turn_tool_chain_is_kept_whole(stem):
    p = load(stem)
    tail = [
        m("user", "last", 100),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "cz", "type": "function",
                                                              "function": {"name": "search", "arguments": "{}"}}]},
        m("tool", "tz", 300, tool_call_id="cz"),
    ]
    msgs = long_chat()[:-1] + tail
    assert est(msgs) > CAP  # 前提
    out = run(p, msgs)
    assert "_bvai_limit_msg" not in out and "_bvai_trim_notice" in out  # 确实走了裁剪分支
    kept = out["messages"]
    assert len(kept) < len(msgs)
    assert kept[-3:] == tail


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_oversized_single_message_rejected_with_accurate_reason(stem):
    p = load(stem)
    msgs = [m("system", "sys", 50), m("user", "huge", 1200)]
    assert est(msgs[1:]) > CAP  # 前提：光最新一条就放不下
    out = run(p, msgs)
    msg = out["_bvai_limit_msg"]
    assert "稍后再试" not in msg
    assert "精简" in msg
    assert not p.user_tokens.get("u1")  # 被拒的请求不扣额度


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_charge_equals_trimmed_size(stem):
    p = load(stem)
    out = run(p, long_chat())
    charged = sum(t for t, _ in p.user_tokens["u1"])
    assert charged == est(out["messages"])
    assert charged <= CAP


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_exhausted_quota_says_when_it_recovers(stem):
    p = load(stem)
    assert "_bvai_limit_msg" not in run(p, long_chat())  # 第一轮：裁剪后放行，扣 950
    out = run(p, long_chat())  # 同一分钟内再发：950 + 950 > 1000
    msg = out["_bvai_limit_msg"]
    assert msg.startswith(p.valves.user_limit_error_message)
    assert "约 1 分钟" in msg


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_wait_time_follows_hour_window(stem):
    p = load(stem, tokens_per_minute=1000, tokens_per_hour=1500)
    p.user_tokens["u1"] = [(900, time.time() - 120)]  # 一分钟前用的，仍占小时额度
    msgs = [m("user", "u0", 300), m("assistant", "a0", 300), m("user", "last", 100)]
    assert 900 + est(msgs) > 1500 and est(msgs) <= 1000  # 前提：只卡小时窗口
    msg = run(p, msgs)["_bvai_limit_msg"]
    assert "约 58 分钟" in msg  # 900 那笔在 3600-120=3480 秒后过期


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_text_inside_list_content_is_counted(stem):
    p = load(stem)
    msgs = [m("system", "sys", 50)]
    for i in range(3):
        msgs += [
            {"role": "user", "content": [{"type": "text", "text": text(f"u{i}", 400)},
                                         {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
            m("assistant", f"a{i}", 100),
        ]
    msgs.append(m("user", "last", 100))
    out = run(p, msgs)
    assert "_bvai_trim_notice" in out  # 旧逻辑每条列表只算 image_token_cost，永远不会触发
    kept = out["messages"]
    assert kept[1]["role"] == "user" and kept[-1] == msgs[-1]


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_admin_is_not_trimmed(stem):
    p = load(stem)
    msgs = long_chat()
    out = run(p, msgs, role="admin")
    assert out["messages"] == msgs
    assert "_bvai_trim_notice" not in out and "_bvai_limit_msg" not in out


def test_all_limiters_share_template():
    template = TEMPLATE.read_text()
    for stem in TOKEN_LIMITERS:
        src = (PIPELINES_DIR / f"{stem}.py").read_text()
        assert SPLIT_MARKER in src, stem
        assert src[src.index(SPLIT_MARKER):] == template, f"{stem} 与模板不一致，运行 bvai-tools/sync_token_limits.py"


@pytest.mark.parametrize("stem", TOKEN_LIMITERS)
def test_fitting_chat_starting_with_assistant_is_untouched(stem):
    """预设开场白等：system 之后第一条是 assistant，没超上限就不能被裁。"""
    p = load(stem)
    msgs = [m("system", "sys", 50), m("assistant", "greet", 50), m("user", "last", 100)]
    assert est(msgs) <= CAP  # 前提
    out = run(p, msgs)
    assert out["messages"] == msgs
    assert "_bvai_trim_notice" not in out


def test_chained_limiters_report_total_dropped():
    """同一模型挂两个限流器（线上 qwen3.5 就是 12 号 + 17 号）：提示的丢弃条数要累加。"""
    first = load(TOKEN_LIMITERS[0])
    second = load(TOKEN_LIMITERS[1], tokens_per_minute=600)
    msgs = long_chat()
    body = {"messages": copy.deepcopy(msgs)}
    body = asyncio.run(first.inlet(body, {"id": "u1", "role": "user"}))
    body = asyncio.run(second.inlet(body, {"id": "u1", "role": "user"}))
    assert "_bvai_limit_msg" not in body
    dropped = len(msgs) - len(body["messages"])
    assert dropped > 12  # 前提：第二个限流器确实又裁了
    assert body["_bvai_trim_notice"] == dropped
