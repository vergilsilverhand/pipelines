"""BVAI：查用户今天是否已超每日贵模型预算。

数据由 open-webui 的 daily_budget Function 写进 open-webui 库（BVAI 仓库 backups/functions/filter__daily_budget.py）：
每次生成完成后在 bvai_spend_daily 累加当天花费并记下预算；每次请求把 valve enforce 同步进 bvai_spend_settings。
全局 enforce 为真且 spend >= budget 才算超预算。按北京时间分天，算法与 daily_budget 的 day_key 一致。
需要环境变量 DATABASE_URL（Railway 上引用 Postgres 的内网地址）；没有它、查询出错或表还没建，一律按未超预算处理，不挡用户。
TZ_HOURS 要与 daily_budget 的 valve tz_hours 一致（都是 8），否则两边分天错开。
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

TZ_HOURS = 8
CACHE_SECONDS = 30
DOWN_SECONDS = 30  # 查库失败后这么久内不再查（所有限流器共用一把锁，故障时逐个等超时会让请求排队）

log = logging.getLogger(__name__)
_cache = {}  # (user_id, day) -> (到期时间, 是否已超)
_down_until = 0.0
_conn = None
_lock = threading.Lock()


def day_key(now=None, tz_hours=TZ_HOURS):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=tz_hours))).date().isoformat()


def _query(user_id, day):
    """同步查询，在线程里跑。所有限流器共用一个连接；连接断了重连一次。
    拿到锁后先看熔断：首个失败之前已在锁上排队的请求，不再各自去等一次连接超时。"""
    global _conn, _down_until
    import psycopg2

    with _lock:
        if time.monotonic() < _down_until:
            return False
        try:
            return _query_locked(psycopg2, user_id, day)
        except Exception:
            _down_until = time.monotonic() + DOWN_SECONDS
            raise


def _query_locked(psycopg2, user_id, day):
    global _conn
    for attempt in (1, 2):
        stale = _conn is not None and not _conn.closed
        try:
            if not stale:
                # keepalive + tcp_user_timeout：静默断网（没有 RST）时，旧连接上的查询几秒内报错，不会无限期持锁
                _conn = psycopg2.connect(
                    os.environ["DATABASE_URL"], connect_timeout=2, options="-c statement_timeout=2000",
                    keepalives=1, keepalives_idle=30, keepalives_interval=5, keepalives_count=3, tcp_user_timeout=5000,
                )
                _conn.autocommit = True
            with _conn.cursor() as cur:
                cur.execute(
                    "SELECT d.spend >= d.budget FROM bvai_spend_daily d JOIN bvai_spend_settings s ON s.id = 1 AND s.enforce "
                    "WHERE d.user_id = %s AND d.day = %s AND d.budget IS NOT NULL",
                    (user_id, day),
                )
                row = cur.fetchone()
            return bool(row and row[0])
        except psycopg2.errors.UndefinedTable:
            return False  # daily_budget 还没建表（1.2.0 还没上线或还没有任何请求）
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _conn = None
            if attempt == 2 or not stale:  # 只有旧连接断了才重连一次；新连接连不上直接放弃
                raise


async def is_over_budget(user_id):
    global _down_until
    if not os.environ.get("DATABASE_URL"):
        return False
    now = time.monotonic()
    if now < _down_until:
        return False
    key = (user_id, day_key())
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        over = await asyncio.to_thread(_query, user_id, key[1])
    except Exception as e:  # _query 已在锁内设好熔断；线程层面的失败也在这里补上（都用失败时刻）
        _down_until = max(_down_until, time.monotonic() + DOWN_SECONDS)
        log.warning(f"[bvai_budget] 查询失败，{DOWN_SECONDS} 秒内按未超预算处理：{e}")
        return False
    if len(_cache) > 10000:
        _cache.clear()
    _cache[key] = (now + CACHE_SECONDS, over)
    return over
