import time
from typing import List, Optional
from pydantic import BaseModel

class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = ["orm_qwen_max.unirouter/qwen/qwen-max"]
        priority: int = 5
        default_tokens_per_1k_chars: int = 1800
        tokens_per_minute: int = 2000
        tokens_per_hour: int = 6000
        global_tokens_per_minute: Optional[int] = 60000
        global_tokens_per_hour: Optional[int] = 600000
        # 可配置的错误信息
        global_limit_error_message: str = "全局 qwen max 令牌限制已超过。系统当前已达到容量。"
        user_limit_error_message: str = "用户 qwen max 令牌限制已超过。请稍后再试。"
        image_token_cost: int = 1024  # 图像的令牌成本估算

    def __init__(self):
        self.type = "filter"
        self.name = "qwenmax Token Limit"
        self.valves = self.Valves()
        self.user_tokens = {}  # 用户token使用记录
        self.global_tokens = []  # 全局token使用记录

    # 其他方法与tokenlimit.py相同...
    async def on_startup(self):
        print(f"on_startup:{__name__}")

    async def on_shutdown(self):
        print(f"on_shutdown:{__name__}")

    # ---- 以下方法由 bvai-tools/token_limit_methods.py 同步生成，勿在单个文件里改 ----
    # 修改流程：改模板 → python bvai-tools/sync_token_limits.py → 跑 bvai-tests

    def prune_tokens(self, user_id: str):
        now = time.time()
        if user_id in self.user_tokens:
            self.user_tokens[user_id] = [
                (tokens, timestamp) for tokens, timestamp in self.user_tokens[user_id]
                if now - timestamp < 3600
            ]

    def prune_global_tokens(self):
        now = time.time()
        self.global_tokens = [
            (tokens, timestamp) for tokens, timestamp in self.global_tokens
            if now - timestamp < 3600
        ]

    def log_tokens(self, user_id: str, tokens: int):
        now = time.time()
        if user_id not in self.user_tokens:
            self.user_tokens[user_id] = []
        self.user_tokens[user_id].append((tokens, now))
        self.global_tokens.append((tokens, now))

    @staticmethod
    def seconds_until_fits(usage, tokens: int, limit: int, window: int) -> float:
        """窗口内已用 + 本次 > 上限时，还要等多少秒才会有足够的旧记录过期。"""
        now = time.time()
        recent = sorted((ts, t) for t, ts in usage if now - ts < window)
        used = sum(t for _, t in recent)
        if used + tokens <= limit:
            return 0
        for ts, t in recent:
            used -= t
            if used + tokens <= limit:
                return ts + window - now
        return window

    def user_limit_wait(self, user_id: str, tokens: int) -> float:
        """0 表示可放行；否则为需要等待的秒数。"""
        self.prune_tokens(user_id)
        usage = self.user_tokens.get(user_id, [])
        return max(
            self.seconds_until_fits(usage, tokens, self.valves.tokens_per_minute, 60),
            self.seconds_until_fits(usage, tokens, self.valves.tokens_per_hour, 3600),
        )

    def global_token_limited(self, tokens: int) -> bool:
        self.prune_global_tokens()
        now = time.time()

        # 检查全局每分钟限制
        if self.valves.global_tokens_per_minute is not None:
            global_tokens_last_minute = sum(token for token, ts in self.global_tokens if now - ts < 60)
            if global_tokens_last_minute + tokens > self.valves.global_tokens_per_minute:
                return True

        # 检查全局每小时限制
        if self.valves.global_tokens_per_hour is not None:
            global_tokens_last_hour = sum(token for token, ts in self.global_tokens if now - ts < 3600)
            if global_tokens_last_hour + tokens > self.valves.global_tokens_per_hour:
                return True

        return False

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(text) // 4 * self.valves.default_tokens_per_1k_chars // 1000

    def message_tokens(self, msg) -> int:
        if not isinstance(msg, dict):
            return 0
        total = 0
        content = msg.get("content")
        if isinstance(content, str):
            total += self.estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    total += self.estimate_tokens(item.get("text") or "")
                elif item.get("type") == "image_url":
                    total += self.valves.image_token_cost
        if msg.get("tool_calls"):
            total += self.estimate_tokens(str(msg["tool_calls"]))
        return total

    def fit_messages(self, messages: list, cap: int):
        """把对话裁到 cap 以内，返回 (保留的消息, 丢弃条数)；放不下返回 (None, 0)。

        开头连续的 system 与「最后一条 user 及其后的工具调用链」始终保留；
        从最早的历史开始丢，切点只落在 user 上，保证不会留下孤立的 tool 消息。
        """
        lead = 0
        while lead < len(messages) and isinstance(messages[lead], dict) and messages[lead].get("role") == "system":
            lead += 1
        rest = messages[lead:]
        last_user = max((i for i, x in enumerate(rest) if isinstance(x, dict) and x.get("role") == "user"), default=0)
        history, tail = rest[:last_user], rest[last_user:]

        fixed = sum(self.message_tokens(x) for x in messages[:lead] + tail)
        if fixed > cap:
            return None, 0

        sizes = [self.message_tokens(x) for x in history]
        remaining = sum(sizes)
        if fixed + remaining <= cap:
            return messages, 0  # 放得下就原样返回，不受「切点须落在 user」约束
        for start in range(len(history) + 1):
            at_user = start == len(history) or (isinstance(history[start], dict) and history[start].get("role") == "user")
            if at_user and fixed + remaining <= cap:
                return messages[:lead] + history[start:] + tail, start
            if start < len(history):
                remaining -= sizes[start]
        return messages[:lead] + tail, len(history)

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        print(f"pipe:{__name__}")

        # 前序限流器已判定超限：跳过计数并原样放行，交由内置 Function 抛出提示
        if not isinstance(body, dict) or body.get("_bvai_limit_msg"):
            return body

        messages = body.get("messages") or []
        is_limited_user = bool(user) and user.get("role", "admin") != "admin"

        # 单次上限：超过就从最早的历史开始裁，而不是永久拒绝
        if is_limited_user and isinstance(messages, list):
            cap = min(self.valves.tokens_per_minute, self.valves.tokens_per_hour)
            kept, dropped = self.fit_messages(messages, cap)
            if kept is None:
                body["_bvai_limit_msg"] = OVERSIZE_ERROR_MESSAGE
                return body
            if dropped:
                body["messages"] = messages = kept
                # 同一模型可能挂多个限流器，依次裁剪，丢弃条数累加
                body["_bvai_trim_notice"] = int(body.get("_bvai_trim_notice") or 0) + dropped

        total_tokens = sum(self.message_tokens(x) for x in messages) if isinstance(messages, list) else 0

        # 检查全局限制
        if self.global_token_limited(total_tokens):
            body["_bvai_limit_msg"] = self.valves.global_limit_error_message
            return body

        if is_limited_user:
            user_id = user.get("id", "default_user")
            wait = self.user_limit_wait(user_id, total_tokens)
            if wait > 0:
                minutes = max(1, -(-int(wait) // 60))
                body["_bvai_limit_msg"] = f"{self.valves.user_limit_error_message}（约 {minutes} 分钟后恢复）"
                return body
            self.log_tokens(user_id, total_tokens)
        else:
            now = time.time()
            self.global_tokens.append((total_tokens, now))

        return body


OVERSIZE_ERROR_MESSAGE = "这条消息太长，超出了该模型的单次上限。请精简内容，或拆成几条分开发送。"
