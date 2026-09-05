# 全项目审查后的实施总报告

日期：2026-09-05。本文接续 [首轮全项目审查报告](PROJECT_AUDIT_2026-09-05.md)，说明用户确认策略后实际完成的代码、数据迁移和验证。首轮报告包含 37 类已修复问题、供应商调查及原始决策背景；当前行为以本文为准。

## 实际完成的决定

| 用户决定 | 当前实现 | 主要代码 |
| --- | --- | --- |
| 全部切换 Schema 保真 | 移除三条请求链路的隐式递归清洗，不再改 strict/required；Gemini 函数参数使用 parametersJsonSchema；旧模型配置已迁移 | `routes/_direct_api_passthrough.py`、`routes/_direct_api_responses.py`、`routes/responses_api.py`、`services/direct_api_service.py` |
| 首业务事件前按规则重试，之后明确中断 | 共享执行器暂存前导事件；文本、思考、工具调用或有效终态开始返回后不重放；缺终态的 EOF 返回错误；取消会关闭上游迭代器 | `services/request_execution.py`、`services/protocol_events.py` |
| 保留长期冷却 | sticky、429/配额及现有模型冷却时长继续生效；修复开启普通重试时跳过 sticky 冷却的问题，避免路由与服务叠加重试 | `services/request_execution.py`、`routes/direct_api_handler.py` |
| 客户端原样回传优先，缓存闲置三天清理 | 原生字段优先；OpenRouter/Gemini 思考签名按调用方、会话、模型、实际端点、Key 指纹隔离；仅完全一致的文本恢复签名 | `core/request_context.py`、`core/conversation_store.py`、`core/request_middleware.py` |
| 轻量多人网关 | 复用已有 Key ID 与认证规则；认证成功后记录调用方；按调用方与币种汇总，不新增注册系统 | `core/auth_observer.py`、`core/usage_analysis.py` |
| 保存失败明确报告 | Key 创建和普通配置更新失败回滚；删除/禁用保持当前进程立即失效并等待后台重试；重载失败保留原配置并报错 | `core/api_key_manager.py`、`routes/apikey_routes.py` |
| 图床三十分钟，其余归档永久保留 | 图床 30 分钟；请求日志、完整原生交换归档与蒸馏数据不设置自动到期删除；新请求日志和归档使用 gzip | `file_bed_server/main.py`、`modules/monitoring.py`、`core/exchange_archive.py` |
| 普通 tokenizer 正常使用，远程代码逐来源开启 | 自定义 HuggingFace/本地 tokenizer 的 trust_remote_code 只对明确来源开启，管理页可维护列表 | `core/tokenizer_trust.py`、`modules/token_counter/_custom.py` |
| 调用时价格固定，现价分析单独提供 | 每次请求保存 pricing_snapshot，移除启动时历史重算；旧重算方法改为只读估算；历史金额与现价估算分别显示 | `core/db_stats.py`、`core/request_metadata.py`、`core/usage_analysis.py` |
| 供应商原生工具与网关管理工具 | 原生工具按协议发送，管理工具提供四项只读操作及 MCP 入口；网关不执行自建通用工具 | `services/provider_capabilities.py`、`routes/gateway_workspace.py`、`routes/gateway_mcp.py` |
| 能力诊断、Playground、阶段耗时、监控优化 | 新管理页支持原生 JSON、工具配置、流式响应、中断、下载和耗时；监控可搜索调用方/会话、按需展开长内容 | `js/admin-gateway.js`、`js/monitor.js` |

## 本阶段发现并处理的问题

1. 递归 Schema 清洗改变请求语义，旧 UI 还会重新保存默认开启值。代码与 UI 统一改为保真，迁移已有端点。
2. 路由和服务各自重试会重复尝试、重复统计；输出部分文字后继续重试会把两个回答拼接。重试集中在共享执行器，业务输出后停止重放。
3. OpenRouter 思考缓存跨用户共用，未命中时删除原始思考字段；Gemini 全局缓存会给裁剪后的文本补签名。缓存改为隔离、持久化、精确匹配，客户端字段保留。
4. 原生搜索事件、引用、加密推理及 Gemini 代码执行结果在 Chat 转换中丢失。原生端点保持原样；Chat 增加 provider_metadata/provider_event 扩展。
5. DeepSeek Responses 的 reasoning_text.delta 未被旧转换器识别，并被错误套用 store 默认值。增加思考事件映射，Chat 转 Responses 时不自动添加 DeepSeek 不支持的 store。
6. Gemini 原生入口单独维护网络、流式、错误和统计逻辑，重试行为与其他入口不一致。约 800 行路由收敛为端点选择和共享转发器调用，保留原认证和轮询规则。
7. Anthropic 流式用量分阶段返回，后续 output_tokens 可能覆盖先前输入量。共享原生统计合并各阶段计数。
8. SQLite 新字段容易只写不读。六个字段已覆盖迁移、请求写入、详情、最近记录和分页查询，测试逐字段验证。
9. 历史成本可能在启动后被当前价格改写。删除自动任务，保留独立只读现价分析。
10. Key 写入失败仍返回成功；重载失败也可能显示成功。失败明确返回 503，并保留安全撤销效果。
11. 监控“折叠”文本实际把全文放入隐藏 DOM。改为点击时才创建全文文本节点；晚到的详情响应不能覆盖更新的详情或已关闭的窗口。
12. Playground 流式每片更新 DOM 容易卡顿。使用 animation frame 合并渲染、限制页面展示长度、提供完整下载；进度缓存仅保留轻量耗时数据。
13. 跨域客户端无法读取新会话头。公开 API 的 CORS 暴露 X-Bridge-Session-ID 和 X-Bridge-Request-ID。
14. 大响应压缩期间取消请求可能与压缩器关闭竞争。取消前等待当前压缩任务结束。

## 官方原生工具兼容

能力目录描述文档与协议支持，不等于所有模型、地区、账户都已开通。Playground 发送真实请求后分别显示成功/中断和实际观察到的工具输出。

| 供应商 | 推荐入口/模型类型 | 工具配置 |
| --- | --- | --- |
| OpenAI | `/v1/responses`，`responses_native` | web_search、file_search、code_interpreter、image_generation、mcp、托管 shell、tool_search |
| DeepSeek 官方 | `/v1/responses`，`responses_native` | web_search；保留 web_search_call 与 reasoning_text.delta；不向上游伪造 previous_response_id/store/background 支持 |
| 阿里百炼 Qwen | `/v1/responses`，`responses_native` | web_search、web_extractor、code_interpreter；具体模型支持以百炼为准 |
| 阿里百炼 Qwen | `/v1/chat/completions`，`direct_api` | enable_search/search_options，保留调用方明确传入的 false |
| Gemini GenerateContent | `/v1beta/models/{alias}:generateContent` 或 `:streamGenerateContent`，`gemini_native` | googleSearch、urlContext、codeExecution、googleMaps、fileSearch |
| Gemini Interactions | `/v1beta/interactions`，`gemini_native` + `upstream_protocol: interactions` | google_search、url_context、code_execution、google_maps、file_search；原生步骤和签名可直接往返 |

核对来源：[OpenAI 工具总览](https://developers.openai.com/api/docs/guides/tools)、[OpenAI 托管 Shell](https://developers.openai.com/api/docs/guides/tools-shell)、[OpenAI 工具搜索](https://developers.openai.com/api/docs/guides/tools-tool-search)、[DeepSeek 官方 Responses](https://api-docs.deepseek.com/zh-cn/guides/responses_api/#tools)、[百炼 Responses](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)、[百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search)、[Gemini 工具](https://ai.google.dev/gemini-api/docs/tools)、[Gemini 工具组合](https://ai.google.dev/gemini-api/docs/tool-combination)。

原生透传保留客户端整个 tools 字段，包括目录尚未列出的供应商扩展。目录只用于可选择的默认工具与配置诊断。需要客户端执行的 function/computer/apply_patch 等调用仍由客户端处理。

### 可直接采用的配置和请求

DeepSeek 模型端点配置示例，模型 ID 按账户实际支持值填写：

```json
{
  "api_type": "responses_native",
  "provider": "deepseek",
  "api_base_url": "https://api.deepseek.com",
  "endpoint_path": "/responses",
  "model_id": "YOUR_MODEL_ID",
  "api_key": "YOUR_UPSTREAM_KEY",
  "sanitize_recursive_schemas": false,
  "native_tools": ["web_search"],
  "native_tool_options": {"web_search": {}}
}
```

客户端也可以直接发送完整工具参数，此时配置默认 tools 不再追加：

```json
{
  "model": "YOUR_GATEWAY_ALIAS",
  "input": "搜索最新发布说明并给出来源",
  "stream": true,
  "tools": [{"type": "web_search"}]
}
```

Gemini GenerateContent：

```json
{
  "contents": [{"role": "user", "parts": [{"text": "搜索最新发布说明"}]}],
  "tools": [{"googleSearch": {}}]
}
```

OpenAI 托管 Shell 使用供应商容器，网关不执行其命令：

```json
{"tools": [{"type": "shell", "environment": {"type": "container_auto"}}]}
```

实际参数映射和默认值代码集中在 `services/provider_capabilities.py`；网络转发、使用量与最终状态在 `services/native_exchange.py`；对应的完整可运行测试位于 `tests/test_provider_capabilities.py`、`tests/test_native_exchange.py` 和 `tests/test_gateway_workspace.py`。

## 会话、保留与价格的具体语义

客户端应沿用响应中的 `X-Bridge-Session-ID`，或自行生成稳定 ID 并在每轮请求头发送；单个 Key 下不同会话使用不同 ID。请求 ID 每次调用独立生成。缓存按实际选中的端点和 Key 指纹再隔离，缓存内容不参与路由选择，已有模型/Key 轮询规则保留。

客户端已经带回 reasoning_details、原生步骤或 provider_metadata 时，优先使用客户端内容。Chat 转换附加的 `provider_metadata.responses_output`、`gemini_parts`、`interactions_steps` 用于完整原生对象往返；忽略扩展字段的旧客户端仍只能使用其协议本身能表达的内容。

会话缓存位于 `data/conversations.db`，连续三天未调用后失效，由后台任务清理。永久原生归档位于 `logs/exchanges/YYYYMMDD/HH/<request-id>.json.gz`，包含完整请求、响应对象以及压缩后的响应字节。认证请求头和上游明文 Key 不写入该归档元数据。请求正文中的用户原始内容按要求完整保存。

请求日志与已有 JSON/gzip 日志可以混合读取；不批量重写旧文件。蒸馏压缩已开启，原有采集筛选和图片脱敏选项保持原配置，完整原生对话另外归档。图床到期只清理临时上传文件，不删除对话归档。

请求体默认上限为 256 MiB，配置项为 `request_limits.max_body_mb`；独立图床默认文件上限 32 MiB，通过 `FILE_BED_MAX_UPLOAD_MB` 调整。

历史记录的原币金额不变，新请求保存调用时价格快照。监控已有跨币种汇总仍是按当前汇率展示的参考换算；按调用方与现价分析页分别列出币种。现价估算按当前第一端点/显示名对应的价格计算，适合比较 Token 成本，不等同于供应商发票。供应商独立收取的搜索、容器、图片等工具费用没有被自动加入 Token 金额。

## 管理工具与后续可用方向

已提供四项工具：`list_gateway_models`、`diagnose_gateway_model`、`gateway_usage_by_caller`、`gateway_current_price_analysis`。HTTP 定义/调用入口为 `/api/admin/tools` 和 `/api/admin/tools/call`；MCP Streamable HTTP 入口为 `/api/admin/mcp`，协商支持 2025-06-18/2025-03-26。均复用现有管理员验证，客户端可发送 `X-Web-Access-Key`。

MCP 按官方 [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) 和 [生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle) 实现，提供 JSON 响应，不创建服务端 SSE 订阅会话。

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {"name": "diagnose_gateway_model", "arguments": {"model": "YOUR_GATEWAY_ALIAS"}}
}
```

后续实用扩展仍可考虑：按供应商账单对账的工具费用分析、按明确预算执行的批量能力探测、蒸馏数据质量筛选/去重、配置版本差异查看、按调用方的预算提醒。这些属于新增产品功能；本次没有在缺少账单或具体预算的情况下伪造收费规则，也没有引入任意文件或 Shell 执行器。

架构上继续采用 FastAPI 与现有原生 JavaScript。已将请求上下文、缓存、元数据、能力目录、重试执行器、原生转发、管理工具和 Playground 分离；整体更换框架没有当前收益证据。后续若前端规模继续增长，适合先为 API DTO 和页面状态增加 TypeScript，再评估 UI 框架迁移。

## 数据迁移与验证

已对真实数据执行 `scripts/migrate_gateway_policy.py --apply`：270 个端点改为保真，增加六个监控字段，开启蒸馏压缩并补齐请求限制/来源信任配置。配置和约 108 MiB 的 SQLite 数据库先备份到 `.migration_backups/20260905-220958-865924`，迁移事务内对历史金额计算摘要并确认前后相同。再次只读执行迁移脚本，结果为零个待迁移端点、零个待加字段、配置无需更改。

验证使用独立临时目录与模拟上游，包含实际 FastAPI 路由、SQLite、MCP 认证、签名隔离、Playwright 真实管理页面资产和流式事件。没有使用真实上游 Key 发起付费能力探测，也没有把文档支持标记成账户实测成功。新后端代码需要在网关进程下次启动时加载；本次没有中断正在运行的生产请求。

- `python -m pytest tests -q --disable-warnings --maxfail=3`：279 项通过。
- 128 个 Python 文件完成 AST 语法检查，17 个非 vendor JavaScript 文件完成 Node 语法检查；无重复模块级定义。
- 独立临时目录完成应用装配检查，新路由注册正常；未加载配置时返回 503，加载有效配置后能力接口返回 200。
- 真实迁移摘要校验历史金额不变，迁移脚本重复执行为无变更。

### 22:51 运行反馈后的回归修复

用户反馈统计接口报 AttributeError。确认替换 `recalculate_costs` 时误删了紧随其后的三个异步包装方法；前述测试缺少真实 StatsDB 与管理路由组合的覆盖，因此未发现该回归。现已恢复 Token 统计、请求统计、概览汇总的线程池入口，并核对 StatsDB 方法清单与修改前一致。新增四项测试覆盖实际 SQLite 管理接口、参数传递、线程池执行及历史金额不变；完整测试结果更新为 283 项通过。

- `python -m pytest tests -q --disable-warnings --maxfail=3`：279 项通过。
- 128 个 Python 文件完成 AST 语法检查，17 个非 vendor JavaScript 文件完成 Node 语法检查；无重复模块级定义。
- 独立临时目录完成应用装配检查，新路由注册正常；未加载配置时返回 503，加载有效配置后能力接口返回 200。
- 真实迁移摘要校验历史金额不变，迁移脚本重复执行为无变更。
