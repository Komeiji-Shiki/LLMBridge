# 普通 Chat 客户端的推理状态恢复

调用链使用：手机 Chat 客户端 → Bridge 的 `/v1/chat/completions` →
Responses 原生上游（`api_type: responses_native`）。如果使用 Codex Go 反代，
Bridge 应请求它的 `/v1/responses`，不要先经过它的 Chat 转换。

手机只需回传普通历史消息。Bridge 保存每个完成轮次的完整原始 Responses
`output`，包括 `reasoning.encrypted_content`，下一轮按助手轮次结束处的可见
历史前缀恢复。消息末尾追加一个或多个用户消息不影响已保存前缀。
推理内容、签名和 `provider_metadata` 不参与可见历史匹配。

缓存保存在已有 `data/conversations.db` 的 `response_prefixes` 表，使用 gzip
压缩，并复用三天闲置过期和现有清理任务。签名只原样保存与回传，不解密、
重建或写入新增诊断日志。对外模型、上游端点、上游凭据、调用者及有效
instructions/tools 都参与隔离。有 `X-Bridge-Session-ID` 时继续按显式会话
隔离；没有时在上述隔离范围内匹配历史前缀。

同一前缀出现不同原始输出时停止自动恢复该前缀，直到闲置过期；不能依据
相同的一句助手回答选择签名。编辑或截断旧历史通常导致匹配不到，服务会
继续使用客户端提供的历史。客户端显式提供的
`provider_metadata.responses_output` 优先于缓存。

重试换 Key 时重新查询对应凭据的缓存，不沿用前一次尝试注入的签名。
流式只在收到完成响应后保存，并在向下游发送终态前完成写入。
自定义参数直接覆盖 `input` 的模型不使用自动恢复，因为实际输入不再对应
手机的 Chat 历史。该机制不负责从请求日志补建升级前的历史。

如果某个反代在相同地址和相同 API Key 下更换其内部账号，Bridge 无法从
现有接口识别这种变化。此时应更换 Bridge 的端点身份或清除对应推理缓存，
不要将旧账号状态用于新账号。

测试：`python -m pytest tests/test_responses_history.py tests/test_responses_bridge.py tests/test_gateway_execution.py -q`。
