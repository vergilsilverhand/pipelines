"""今天已超每日贵模型预算的用户：贵模型限流器按 Luna 的额度单独计数。

为什么：open-webui 的 daily_budget Function 在 pipelines 之后执行，超预算时把请求降级到 GPT 6 Luna；
但 pipelines 按原模型挑限流器，降级的请求仍按贵模型的单次上限（3000~8000）裁剪、扣贵模型的每小时额度。
所以限流器自己读 daily_budget 写的 bvai_spend_daily（spend >= budget）判断是否已超预算。

运行：uv run --quiet --with pytest --with pydantic pytest bvai-tests -q
"""
import asyncio
import copy
import json
import re
import sys
import time

import pytest

from test_token_limit import CAP, PIPELINES_DIR, ROOT, TOKEN_LIMITERS, load, m

sys.path.insert(0, str(ROOT))

# 绑定贵模型的限流器（daily_budget 管的、不在便宜/排除清单里的模型）
PREMIUM = {"2.sonnet3.7", "4.gemini2.5prolimit", "5.qwenmax", "6.r1limit", "7.o4minilimit", "11.grok3minilimit", "13.gemini2.5flashlimit"}
LUNA = "15.gpt4.1limit"
OB = dict(over_budget_tokens_per_minute=2000, over_budget_tokens_per_hour=6000)  # 测试用：比 CAP=1000 大


def valves_json(stem):
    return json.loads((PIPELINES_DIR / stem / "valves.json").read_text())


def premium(stem, over=True, **overrides):
    p = load(stem, **{**OB, **overrides})
    calls = []

    async def fake(user_id):
        calls.append(user_id)
        if isinstance(over, Exception):
            raise over
        return over

    p.over_budget_lookup = fake
    return p, calls


def run(p, messages, role="user", uid="u1", **body_extra):
    body = {"messages": copy.deepcopy(messages), **body_extra}
    return asyncio.run(p.inlet(body, {"id": uid, "role": role}))


def chat(tokens_each=300, rounds=2):
    """估算约 (2*rounds+1)*tokens_each 的对话，rounds=2、300 → 1500，超过 CAP=1000、低于 OB 的 2000。"""
    msgs = []
    for i in range(rounds):
        msgs += [m("user", f"u{i}", tokens_each), m("assistant", f"a{i}", tokens_each)]
    return msgs + [m("user", "last", tokens_each)]


def test_premium_limiters_use_luna_quota_when_over_budget():
    """贵模型限流器的超预算额度 = Luna 限流器的额度；其他限流器不配置（不查库）。"""
    luna = valves_json(LUNA)
    for stem in TOKEN_LIMITERS:
        v = valves_json(stem)
        if stem in PREMIUM:
            assert v.get("over_budget_tokens_per_minute") == luna["tokens_per_minute"], stem
            assert v.get("over_budget_tokens_per_hour") == luna["tokens_per_hour"], stem
        else:
            assert v.get("over_budget_tokens_per_hour") is None, stem


def test_over_budget_message_names_current_luna():
    """超预算提示写的是 Luna 的显示名，放在 valves.json 里（换模型时按 valves.json 改文案，不会漏）。"""
    luna_name = re.match(r"用户 (.+?) token限制已超过", valves_json(LUNA)["user_limit_error_message"]).group(1)
    for stem in PREMIUM:
        msg = valves_json(stem).get("over_budget_limit_error_message", "")
        assert msg.startswith(f"用户 {luna_name}（超预算） token限制已超过"), (stem, msg)


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_over_budget_request_uses_luna_cap_not_premium_cap(stem):
    p, calls = premium(stem)
    msgs = chat()
    out = run(p, msgs)
    assert calls == ["u1"]
    assert "_bvai_limit_msg" not in out
    assert "_bvai_trim_notice" not in out  # 按贵模型 CAP=1000 会被裁，按 Luna 额度不裁
    assert out["messages"] == msgs


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_within_budget_request_keeps_premium_limits(stem):
    p, calls = premium(stem, over=False)
    out = run(p, chat())
    assert calls == ["u1"]
    assert out.get("_bvai_trim_notice")  # 未超预算：照旧按贵模型单次上限裁剪


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_over_budget_usage_is_counted_separately(stem):
    """超预算期间用的是 Luna 的额度，不扣贵模型的每小时额度（第二天回到贵模型时额度完整）。"""
    p, _ = premium(stem, over_budget_tokens_per_minute=6000)  # 只测每小时额度
    for _ in range(4):  # 4 × 1500 = 6000，正好用完测试用的超预算每小时额度
        assert "_bvai_limit_msg" not in run(p, chat())
    out = run(p, chat())
    assert out["_bvai_limit_msg"].startswith(p.valves.over_budget_limit_error_message)
    assert "分钟后恢复" in out["_bvai_limit_msg"]
    p.over_budget_lookup = premium(stem, over=False)[0].over_budget_lookup
    out = run(p, [m("user", "x", 500)])
    assert "_bvai_limit_msg" not in out  # 贵模型自己的额度没被动过


def test_over_budget_message_matches_report_pattern():
    """cost_report 按「用户 X token限制已超过」归类限流拦截。"""
    p, _ = premium("7.o4minilimit")
    assert "token限制已超过" in p.valves.over_budget_limit_error_message
    assert p.valves.over_budget_limit_error_message.startswith("用户 ")


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_over_budget_request_skips_premium_global_limit(stem):
    """降级后的请求发给 Luna，不占贵模型的全局额度。"""
    p, _ = premium(stem, global_tokens_per_minute=100, global_tokens_per_hour=100)
    out = run(p, chat())
    assert "_bvai_limit_msg" not in out
    assert p.global_tokens == []


@pytest.mark.parametrize("stem", sorted(set(TOKEN_LIMITERS) - PREMIUM))
def test_limiters_without_over_budget_valves_never_query(stem):
    p = load(stem)
    calls = []

    async def fake(user_id):
        calls.append(user_id)
        return True

    p.over_budget_lookup = fake
    run(p, chat())
    assert calls == []


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_admin_and_tasks_never_query(stem):
    p, calls = premium(stem)
    run(p, chat(), role="admin")
    run(p, chat(), metadata={"task": "title_generation"})
    assert calls == []


@pytest.mark.parametrize("stem", sorted(PREMIUM))
def test_lookup_failure_counts_as_within_budget(stem):
    p, _ = premium(stem, over=RuntimeError("db down"))
    out = run(p, chat())
    assert "_bvai_limit_msg" not in out
    assert out.get("_bvai_trim_notice")  # 按贵模型处理，不挡用户


# ---- utils/bvai_budget.py：查库、缓存、按北京时间分天 ----

from datetime import datetime, timezone  # noqa: E402

from utils import bvai_budget  # noqa: E402


def test_day_key_is_beijing_date_same_as_daily_budget():
    assert bvai_budget.day_key(datetime(2026, 10, 21, 15, 59, tzinfo=timezone.utc)) == "2026-10-21"
    assert bvai_budget.day_key(datetime(2026, 10, 21, 16, 1, tzinfo=timezone.utc)) == "2026-10-22"


def test_no_database_url_means_within_budget(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(bvai_budget, "_query", lambda *a: pytest.fail("不该查库"))
    bvai_budget._cache.clear()
    assert asyncio.run(bvai_budget.is_over_budget("u1")) is False


def test_query_result_is_cached_for_30_seconds(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    calls = []

    def fake_query(user_id, day):
        calls.append((user_id, day))
        return True

    monkeypatch.setattr(bvai_budget, "_query", fake_query)
    bvai_budget._cache.clear()
    bvai_budget._down_until = 0
    now = [1000.0]
    monkeypatch.setattr(bvai_budget.time, "monotonic", lambda: now[0])
    assert asyncio.run(bvai_budget.is_over_budget("u1")) is True
    assert asyncio.run(bvai_budget.is_over_budget("u1")) is True
    assert len(calls) == 1
    now[0] += 31
    asyncio.run(bvai_budget.is_over_budget("u1"))
    assert len(calls) == 2


def test_cache_is_per_day(monkeypatch):
    """北京时间 0 点后不沿用前一天的「已超预算」。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    days = []
    monkeypatch.setattr(bvai_budget, "_query", lambda user_id, day: days.append(day) or True)
    bvai_budget._cache.clear()
    today = ["2026-10-21"]
    monkeypatch.setattr(bvai_budget, "day_key", lambda now=None: today[0])
    asyncio.run(bvai_budget.is_over_budget("u1"))
    today[0] = "2026-10-22"
    asyncio.run(bvai_budget.is_over_budget("u1"))
    assert days == ["2026-10-21", "2026-10-22"]


def test_db_failure_pauses_all_lookups_for_30_seconds(monkeypatch):
    """数据库故障时不让每个请求都去等连接超时（所有限流器共用一把锁，会排队）。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    calls = []

    def boom(user_id, day):
        calls.append(user_id)
        raise RuntimeError("db down")

    monkeypatch.setattr(bvai_budget, "_query", boom)
    bvai_budget._cache.clear()
    bvai_budget._down_until = 0
    now = [1000.0]
    monkeypatch.setattr(bvai_budget.time, "monotonic", lambda: now[0])
    assert asyncio.run(bvai_budget.is_over_budget("u1")) is False
    assert asyncio.run(bvai_budget.is_over_budget("u2")) is False
    assert calls == ["u1"]  # 第二个用户没再去查
    now[0] += 31
    asyncio.run(bvai_budget.is_over_budget("u3"))
    assert calls == ["u1", "u3"]


def test_query_error_means_within_budget(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    def boom(user_id, day):
        raise RuntimeError("db down")

    monkeypatch.setattr(bvai_budget, "_query", boom)
    bvai_budget._cache.clear()
    bvai_budget._down_until = 0
    assert asyncio.run(bvai_budget.is_over_budget("u1")) is False


class _FakePsycopg2:
    """只替换数据库驱动：连接失败前停 0.05 秒（模拟连接超时），记录每次连接的参数。"""

    class OperationalError(Exception):
        pass

    class InterfaceError(Exception):
        pass

    class errors:
        class UndefinedTable(Exception):
            pass

    def __init__(self):
        self.connects = []

    def connect(self, dsn, **kwargs):
        self.connects.append(kwargs)
        time.sleep(0.05)
        raise self.OperationalError("connection timed out")


def test_queued_lookups_do_not_each_wait_for_a_dead_database(monkeypatch):
    """首个连接失败后，已经在锁上排队的请求不再各自去连（否则最后一个要等 N × 连接超时）。"""
    fake = _FakePsycopg2()
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    bvai_budget._cache.clear()
    bvai_budget._down_until = 0
    bvai_budget._conn = None

    async def burst():
        return await asyncio.gather(*[bvai_budget.is_over_budget(f"u{i}") for i in range(8)])

    assert asyncio.run(burst()) == [False] * 8
    assert len(fake.connects) == 1


def test_connection_detects_silently_dropped_network(monkeypatch):
    """静默断网（没有 RST）时，旧连接上的查询不能无限期持锁：连接要带 TCP keepalive 和发送超时。"""
    fake = _FakePsycopg2()
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    bvai_budget._down_until = 0
    bvai_budget._conn = None
    with pytest.raises(fake.OperationalError):
        bvai_budget._query("u1", "2026-10-21")
    kw = fake.connects[0]
    assert kw.get("keepalives") == 1 and kw.get("tcp_user_timeout")
