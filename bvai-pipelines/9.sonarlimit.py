import time
from typing import List, Optional
from pydantic import BaseModel

class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = ["perplexityai.https://api.perplexity.ai"]
        priority: int = 9
        default_tokens_per_1k_chars: int = 1800
        tokens_per_minute: int = 15000
        tokens_per_hour: int = 45000
        global_tokens_per_minute: Optional[int] = 450000
        global_tokens_per_hour: Optional[int] = 4000000

    def __init__(self):
        self.type = "filter"
        self.name = "sonar Token Limit"
        self.valves = self.Valves()
        self.user_tokens = {}  # 用户token使用记录
        self.global_tokens = []  # 全局token使用记录

    # 其他方法与tokenlimit.py相同...
    async def on_startup(self):
        print(f"on_startup:{__name__}")

    async def on_shutdown(self):
        print(f"on_shutdown:{__name__}")

    # 实现各种辅助方法
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

    def user_token_limited(self, user_id: str, tokens: int) -> bool:
        self.prune_tokens(user_id)
        user_token_usage = self.user_tokens.get(user_id, [])
        now = time.time()
        
        # 检查用户每分钟限制
        tokens_last_minute = sum(token for token, ts in user_token_usage if now - ts < 60)
        if tokens_last_minute + tokens > self.valves.tokens_per_minute:
            return True

        # 检查用户每小时限制
        tokens_last_hour = sum(token for token, ts in user_token_usage if now - ts < 3600)
        if tokens_last_hour + tokens > self.valves.tokens_per_hour:
            return True

        return False
    
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

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        print(f"pipe:{__name__}")

        # 前序限流器已判定超限：跳过计数并原样放行，交由内置 Function 抛出提示
        if isinstance(body, dict) and body.get("_bvai_limit_msg"):
            return body
        
        # 获取用户输入
        content = ""
        if "messages" in body:
            messages = body.get("messages", [])
            for msg in messages:
                if isinstance(msg, dict) and "content" in msg:
                    content += msg.get("content", "")
        
        # 估算token数量
        tokens = self.estimate_tokens(content)
        
        # 检查全局限制
        if self.global_token_limited(tokens):
            body["_bvai_limit_msg"] = f"全局 sonar 令牌限制已超过。系统当前已达到容量。"
            return body
        # 检查用户级限制
        if user and user.get("role", "admin") == "user":
            user_id = user.get("id", "default_user")
            
            if self.user_token_limited(user_id, tokens):
                body["_bvai_limit_msg"] = f"用户 sonar 令牌限制已超过，请稍后再试。"
                return body
            self.log_tokens(user_id, tokens)
        else:
            now = time.time()
            self.global_tokens.append((tokens, now))
            
        return body