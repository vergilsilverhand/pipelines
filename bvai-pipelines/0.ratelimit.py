import time
from typing import List, Optional
from pydantic import BaseModel

class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = ["*"]
        priority: int = 0
        requests_per_minute: Optional[int] = 10  # 用户级RPM
        requests_per_hour: Optional[int] = 1000  # 用户级RPH
        global_requests_per_minute: Optional[int] = 100  # 全局RPM
        global_requests_per_hour: Optional[int] = 10000  # 全局RPH

    def __init__(self):
        self.type = "filter"
        self.name = "Rate Limit"
        self.valves = self.Valves()
        self.user_requests = {}  # 用户请求记录
        self.global_requests = []  # 全局请求记录

    async def on_startup(self):
        print(f"on_startup:{__name__}")

    async def on_shutdown(self):
        print(f"on_shutdown:{__name__}")

    def prune_requests(self, user_id: str):
        """清理过期的用户请求记录"""
        now = time.time()
        if user_id in self.user_requests:
            self.user_requests[user_id] = [
                req for req in self.user_requests[user_id]
                if ((self.valves.requests_per_minute is not None and now - req < 60) or
                    (self.valves.requests_per_hour is not None and now - req < 3600))
            ]

    def prune_global_requests(self):
        """清理过期的全局请求记录"""
        now = time.time()
        self.global_requests = [
            req for req in self.global_requests
            if ((self.valves.global_requests_per_minute is not None and now - req < 60) or
                (self.valves.global_requests_per_hour is not None and now - req < 3600))
        ]

    def log_request(self, user_id: str):
        """记录用户请求"""
        now = time.time()
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []
        self.user_requests[user_id].append(now)
        
        # 同时记录到全局
        self.global_requests.append(now)

    def rate_limited(self, user_id: str) -> bool:
        """检查用户级别的请求限制"""
        self.prune_requests(user_id)
        user_reqs = self.user_requests.get(user_id, [])
        now = time.time()

        if self.valves.requests_per_minute is not None:
            requests_last_minute = sum(1 for req in user_reqs if now - req < 60)
            if requests_last_minute >= self.valves.requests_per_minute:
                return True

        if self.valves.requests_per_hour is not None:
            requests_last_hour = sum(1 for req in user_reqs if now - req < 3600)
            if requests_last_hour >= self.valves.requests_per_hour:
                return True

        return False

    def global_rate_limited(self) -> bool:
        """检查全局级别的请求限制"""
        self.prune_global_requests()
        now = time.time()

        if self.valves.global_requests_per_minute is not None:
            global_requests_last_minute = sum(1 for req in self.global_requests if now - req < 60)
            if global_requests_last_minute >= self.valves.global_requests_per_minute:
                return True

        if self.valves.global_requests_per_hour is not None:
            global_requests_last_hour = sum(1 for req in self.global_requests if now - req < 3600)
            if global_requests_last_hour >= self.valves.global_requests_per_hour:
                return True

        return False

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        print(f"pipe:{__name__}")

        # 先检查全局限制（对所有用户都生效）
        if self.global_rate_limited():
            raise Exception("Global rate limit exceeded. The system is currently at capacity.")

        # 再检查用户级限制（仅对普通用户生效）
        if user and user.get("role", "admin") == "user":
            user_id = user.get("id", "default_user")
            if self.rate_limited(user_id):
                raise Exception("Rate limit exceeded. Please try again later.")
            self.log_request(user_id)
        else:
            # 即使是管理员用户，也记录到全局限制中
            now = time.time()
            self.global_requests.append(now)
            
        return body