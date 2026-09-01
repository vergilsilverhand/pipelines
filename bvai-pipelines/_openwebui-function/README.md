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
