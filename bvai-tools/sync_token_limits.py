"""把 token_limit_methods.py 同步到 bvai-pipelines/ 下所有 token 限流器。

每个限流器文件的头部（Valves 默认值、__init__）各自保留，
从 `async def on_startup` 到文件末尾整段替换为模板。

用法：python bvai-tools/sync_token_limits.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "token_limit_methods.py"
SPLIT_MARKER = "    async def on_startup"


def main():
    template = TEMPLATE.read_text()
    for path in sorted((ROOT / "bvai-pipelines").glob("*.py")):
        src = path.read_text()
        if "tokens_per_minute" not in src:
            continue
        head = src[: src.index(SPLIT_MARKER)]
        new = head + template
        if new != src:
            path.write_text(new)
            print(f"updated {path.name}")


if __name__ == "__main__":
    main()
