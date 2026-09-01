# BVAI 自定义限流器

19 个 token / 请求限流 filter，服务于 bestvpnai.org。

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
