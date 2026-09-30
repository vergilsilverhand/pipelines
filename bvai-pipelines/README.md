# BVAI 自定义限流器

17 个 token / 请求限流 filter，服务于 bestvpnai.org（2026-09-30 删除了停用的 8.grok3limit 与重复挂在 qwen3.5 上的 12.qwen3limit）。

## 为什么放在这个目录而不是 pipelines/

本仓库是 `open-webui/pipelines` 的 fork。上游在 `.gitignore` 中排除了 `pipelines/*`，
把它当作运行时目录。若为了入库而修改上游的 `.gitignore`，这个 fork 就不再纯净，
将来同步上游更新时会产生冲突。

因此改用独立目录 `bvai-pipelines/`，并通过环境变量指定：

```
PIPELINES_DIR=/app/bvai-pipelines
```

这样**不改动上游任何文件**，fork 与上游之间可以随时无冲突合并。

## 背景

这些 filter 此前仅存在于 Railway 容器的临时文件系统中——服务没有挂载 volume，
Pipelines API 也没有下载端点。容器一经重建即永久丢失。入库后，重建会自动带上。

## 结构

- `<id>.py` — filter 源码
- `<id>/valves.json` — 该 filter 的生效配额

`valves.json` **必须一并版本化**：缺失时会回退到源码中的默认值
（例如 `3.r11776limit` 从 40000 掉到 3000 tokens/min），限流强度将与预期不符。

## 维护约定

在 Open WebUI 后台调整配额后，需把新的 valves 同步回本仓库，否则下次容器重建会被旧值覆盖：

```bash
curl -s "https://<webui>/api/v1/pipelines/<id>/valves?urlIdx=0" \
  -H "Authorization: Bearer <admin-token>" > bvai-pipelines/<id>/valves.json
```

> 切勿向 `valves/update` 端点发送空对象 `{}`，那会把配额重置为源码默认值。

## 修改 token 限流逻辑

13 个 token 限流器（Valves 含 `tokens_per_minute` 的文件）共用同一段方法，
源头是 `bvai-tools/token_limit_methods.py`。**不要直接改单个文件**：

```bash
# 改模板后同步到全部 token 限流器（各文件 Valves / __init__ 头部保留）
python bvai-tools/sync_token_limits.py
# 跑测试（含「与模板一致」「每个模型只挂一个限流器」「pipelines 必须是真实模型 id」的检查）
uv run --quiet --with pytest --with pydantic pytest bvai-tests -q
```

行为要点：单次请求超过 `min(tokens_per_minute, tokens_per_hour)` 时从最早的历史裁剪
（切点只落在 user，开头 system 与最后一轮完整保留），按裁剪后大小扣额度；
最后一轮本身放不下才拒绝。配套的提示机制见 `_openwebui-function/README.md`。

**上线前**：若线上 valves 在后台改过，先把它们同步回各 `valves.json`，
否则重新部署会用仓库里的旧值覆盖线上配置。
