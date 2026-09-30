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
**后台任务请求（标题 / 标签 / 追问等）**：只走 pipelines inlet、不走本 Function，写进 body 的标记
没人移除、会漏到上游；在限流器里抛异常也不行 —— 回答完成后触发的这些任务在 open-webui 里没有 try，
异常会把已经成功的回答标成错误。所以所有限流器识别 `body["metadata"]["task"]`：
- token 限流器：照常计入用户额度，但永不拦截、不裁剪、不写标记；
- 请求次数限流器：不计次数、不拦截。

普通聊天的 metadata 由 open-webui 服务端生成并整体覆盖，用户无法伪造 task。
**任务模型不要选挂了限流器的昂贵模型**：任务接口任何登录用户都能直接调用，
任务请求又不被拦截，那样就能绕过额度（目前任务模型是 gemini-2.0-flash，未挂限流器）。

**上线顺序**：先在 Open WebUI 里把本 Function 更新到 ≥1.1.0，再部署 pipelines。
旧版 1.0.0 只移除 `_bvai_limit_msg`，若 pipelines 先上线，裁剪后的请求会带着
`_bvai_trim_notice` 发往上游而报 400。

**一个模型只挂一个 token 限流器**：多个限流器会依次裁剪，前面的按它自己裁完的大小扣额度，
比最终发出的多扣。`bvai-tests` 里有检查，valves.json 出现重复挂载会直接失败。
（注意：线上在后台改 valves 不经过测试，改完要同步回仓库再跑一次。）
