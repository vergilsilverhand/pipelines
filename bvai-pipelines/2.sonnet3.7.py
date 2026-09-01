import time
from typing import List, Optional
from pydantic import BaseModel

class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = ["orm.unirouter/anthropic/claude-3.7-sonnet"]
        priority: int = 2
        default_tokens_per_1k_chars: int = 1800
        tokens_per_minute: int = 2000
        tokens_per_hour: int = 4000
        global_tokens_per_minute: Optional[int] = 40000
        global_tokens_per_hour: Optional[int] = 120000
        # Error messages
        global_limit_error_message: str = "Global Sonnet 3.7 token limit exceeded. System currently at capacity."
        user_limit_error_message: str = "User Sonnet 3.7 token limit exceeded. Please try again later."
        image_token_cost: int = 1024  # Estimated token cost for images

    def __init__(self):
        self.type = "filter"
        self.name = "Sonnet 3.7 Token Limit"
        # Make sure to set a valid ID without spaces or special characters
        self.id = "sonnet37_token_limit"
        self.valves = self.Valves()
        self.user_tokens = {}  # User token usage records
        self.global_tokens = []  # Global token usage records

    async def on_startup(self):
        print(f"on_startup:{__name__}")

    async def on_shutdown(self):
        print(f"on_shutdown:{__name__}")

    # Helper method implementations
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
        
        # Check user per-minute limit
        tokens_last_minute = sum(token for token, ts in user_token_usage if now - ts < 60)
        if tokens_last_minute + tokens > self.valves.tokens_per_minute:
            return True

        # Check user per-hour limit
        tokens_last_hour = sum(token for token, ts in user_token_usage if now - ts < 3600)
        if tokens_last_hour + tokens > self.valves.tokens_per_hour:
            return True

        return False
    
    def global_token_limited(self, tokens: int) -> bool:
        self.prune_global_tokens()
        now = time.time()
        
        # Check global per-minute limit
        if self.valves.global_tokens_per_minute is not None:
            global_tokens_last_minute = sum(token for token, ts in self.global_tokens if now - ts < 60)
            if global_tokens_last_minute + tokens > self.valves.global_tokens_per_minute:
                return True

        # Check global per-hour limit
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
        print(f"Body: {body}")
        print(f"User: {user}")
        
        # Get user input and estimate tokens
        total_tokens = 0
        if "messages" in body:
            messages = body.get("messages", [])
            for msg in messages:
                if isinstance(msg, dict) and "content" in msg:
                    content = msg.get("content", "")
                    # Handle string content
                    if isinstance(content, str):
                        total_tokens += self.estimate_tokens(content)
                    # Handle image or other content types (usually lists)
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "image_url":
                                # Add fixed token cost for images
                                total_tokens += self.valves.image_token_cost
                            elif isinstance(item, dict) and item.get("type") == "text":
                                # Process text content
                                total_tokens += self.estimate_tokens(item.get("text", ""))
        
        print(f"Estimated tokens: {total_tokens}")
        
        # Check global limits
        if self.global_token_limited(total_tokens):
            raise Exception(self.valves.global_limit_error_message)
        
        # Check user-level limits
        if user and user.get("role", "admin") != "admin":
            user_id = user.get("id", "default_user")
            
            if self.user_token_limited(user_id, total_tokens):
                raise Exception(self.valves.user_limit_error_message)
            
            self.log_tokens(user_id, total_tokens)
        else:
            # Just log global tokens for admin users
            now = time.time()
            self.global_tokens.append((total_tokens, now))
            
        return body