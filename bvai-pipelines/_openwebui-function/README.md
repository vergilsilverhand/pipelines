# 配套的 Open WebUI 内置 Function

`bvai_limit_notice.py` 需要作为 **Function** 导入 Open WebUI（Workspace → Functions），
并设为 **启用 + 全局**。它不是 pipeline，不会被 pipelines 服务加载。

## 为什么需要它

Open WebUI 调用外部 pipeline filter 时，先执行 `response.raise_for_status()`；
aiohttp 在抛出 `ClientResponseError` 前会 `release()` 响应体，导致紧接着的
`await response.json()` 读不到内容，于是落到兜底分支，把 `e.message`
（即 HTTP reason phrase "Internal Server Error"）当作 detail 返回给用户。

结果是：限流器精心配置的提示消息只出现在 pipelines 日志里，用户永远看到
`Internal Server Error`，既分不清是撞了请求速率还是 token 配额，也不知道要等多久。
该行为在 0.8.12 与 0.11.3 上一致，非升级引入。

内置 Function 抛出的异常则由 `middleware.py` 以 `raise Exception(f'{e}')` 原样透传，
消息能正常到达用户。

## 协作方式

限流器判定超限后不再 `raise`，而是写入 `body["_bvai_limit_msg"]` 并放行；
本 Function 在 pipelines 之后执行（`process_pipeline_inlet_filter` → `process_filter_functions`），
读取该字段、将其移除，并抛出异常。

各限流器 inlet 开头也有一处守卫：若标记已存在则直接放行，不再计数 ——
以此保持原先 `raise` 时的短路语义，避免已被拒绝的请求仍被计入用量。

### 超长对话裁剪提示

对话超过该模型的单次上限（`min(tokens_per_minute, tokens_per_hour)`）时，限流器不再拒绝，
而是从最早的历史开始裁掉，并把丢弃条数写入 `body["_bvai_trim_notice"]`（整数，
同一模型挂多个限流器时累加）。本 Function 移除该字段，并通过 `__event_emitter__`
在对话里显示一行状态「对话较长，最早的 N 条消息未发送给模型。」

**两个字段都必须由本 Function 移除**，否则会随请求发往上游 API 造成参数错误。
注意：标题 / 标签 / 追问等后台任务请求只走 pipelines inlet、不走 Function，
这两个字段会漏到上游 —— 线上这些任务目前全部关闭，开启前需要先处理。

**上线顺序**：先在 Open WebUI 里把本 Function 更新到 ≥1.1.0，再部署 pipelines。
旧版 1.0.0 只移除 `_bvai_limit_msg`，若 pipelines 先上线，裁剪后的请求会带着
`_bvai_trim_notice` 发往上游而报 400。

**已知局限**：同一模型挂多个限流器（如 `12.qwen3limit` 与 `17.chenlimit` 都挂 qwen3.5）
时依次裁剪，前面的限流器按它自己裁完的大小扣额度，会比最终发出的多扣。
根治办法是一个模型只挂一个限流器。
