# 普通 Chat 客户端的推理状态恢复

调用链使用：手机 Chat 客户端 → Bridge 的 `/v1/chat/completions` →
Responses 原生上游（`api_type: responses_native`）。如果使用 Codex Go 反代，
Bridge 应请求它的 `/v1/responses`，不要先经过它的 Chat 转换。

手机只需回传普通历史消息。Bridge 保存每个完成轮次的完整原始 Responses
`output`，包括 `reasoning.encrypted_content`，下一轮逐个助手轮次恢复。
消息末尾追加一个或多个用户消息不影响已保存前缀。
推理内容、签名和 `provider_metadata` 不参与可见历史匹配。

每轮存两个键：从开头到本轮的整段可见历史前缀，以及“上一条助手消息之后
的消息 + 本轮助手消息”的局部指纹。上下文完全一致时按整段前缀精确命中；
客户端删掉中间一段、截短某轮回答或按条数上限丢掉最早的轮次时，自身及其
提问未变的轮次按局部指纹恢复，只有真正被改动的那一轮拿不到原始输出。
上游缓存按前缀命中，缺口之前的部分照常命中；缺口之后补回的轮次与原始
字节一致，后续请求可以从缺口处重新建立缓存。Gemini 原生签名
（`services/gemini_history.py`）采用同样的两级匹配。

匹配指纹会统一 CRLF/LF、纯文本/文本块的表示，并忽略助手正文首尾空白，
以兼容手机裁剪回答的行为。此处理不改写回传的原始输出；正文内部空白、
代码缩进及用户消息首尾空白仍准确比较。规范化后有多个不同输出仍视为歧义。
升级前保存的带首尾空白的旧指纹不会自动重建，重新测试时应从升级后生成
的一轮开始建立缓存。

缓存保存在已有 `data/conversations.db` 的 `response_prefixes` 表，使用 gzip
压缩，并复用三天闲置过期和现有清理任务。签名只原样保存与回传，不解密、
重建或写入新增诊断日志。对外模型、上游端点、上游凭据、调用者及有效
instructions/tools 都参与隔离。有 `X-Bridge-Session-ID` 时继续按显式会话
隔离；没有时在上述隔离范围内匹配历史前缀。

同一个键出现不同原始输出时停止按该键自动恢复，直到闲置过期；不能依据
相同的一句助手回答选择签名。不同对话里提问和回答完全相同的轮次会让
局部指纹产生歧义，此时只有整段前缀仍能命中。被编辑的轮次本身匹配不到，
服务继续使用客户端提供的内容。客户端显式提供的
`provider_metadata.responses_output` 优先于缓存。

重试换 Key 时重新查询对应凭据的缓存，不沿用前一次尝试注入的签名。
流式只在收到完成响应后保存，并在向下游发送终态前完成写入。
自定义参数直接覆盖 `input` 的模型不使用自动恢复，因为实际输入不再对应
手机的 Chat 历史。该机制不负责从请求日志补建升级前的历史。

如果某个反代在相同地址和相同 API Key 下更换其内部账号，Bridge 无法从
现有接口识别这种变化。此时应更换 Bridge 的端点身份或清除对应推理缓存，
不要将旧账号状态用于新账号。

测试：`python -m pytest tests/test_responses_history.py tests/test_responses_bridge.py tests/test_gateway_execution.py -q`。

## Anthropic 与 Responses 的思考状态

Anthropic 客户端通过 `/v1/messages` 调用 Responses 上游时，网关把原始
Responses `reasoning` 项编码进对应思考块的 `signature`，并以 `responses.v1.`
标明来源。客户端回传完整思考块后，网关恢复原始推理项。普通文本、工具调用
和推理项按顺序回传；如果本地历史匹配到完整的原始输出，也会保留原始工具
调用项。工具参数只在历史匹配时按 JSON 内容比较，发往上游的原始参数不变。
升级前使用旧指纹保存的工具轮次不能按新指纹自动恢复，需要从升级后的轮次
重新建立原始输出记录。

Responses 客户端通过 `/v1/responses` 调用 Anthropic 上游时，网关把原始
`thinking` 或 `redacted_thinking` 块编码进 Responses 推理项的
`encrypted_content`，并以 `anthropic.v1.` 标明来源。客户端需要将响应的
`reasoning`、`function_call` 和 `function_call_output` 一起放入下一次请求。
这些带协议前缀的值是网关的传输载体，只有原始签名由对应上游验证。

Anthropic 的 `cache_control` 断点不能直接作为 Responses 缓存断点使用；
Responses 的缓存参数也不能直接作为 Anthropic 缓存参数使用。转换请求时，
未应用的字段会在响应的 `bridge_warnings` 和 `X-Bridge-Warnings` 响应头中
列出，字段 `applied` 为 `false`。实际缓存命中量仍由上游返回，Anthropic
流式转换会在最终 `message_delta.usage` 提供该用量。流开始时的
`message.bridge_usage_pending` 为 `true`，最终事件变为 `false`，响应头
`X-Bridge-Usage-Event` 指明应从 `message_delta` 读取最终用量。
