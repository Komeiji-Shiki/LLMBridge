# 模型首发价格与缓存写入计费

`model_endpoint_map.json` 是本地敏感配置，不提交到 Git。公开、可复核的价格目录在 [model_launch_prices.json](model_launch_prices.json)，各模型条目包含来源和特殊条件。本次核对日期为 2026-09-09。

价格以对应模型 ID 首次公开提供 API 时为准，发布即生效的限时优惠可以采用，后续降价不追溯。独立日期快照和稳定版按各自发布时点处理。转发站、订阅套餐的实际账单可能不同，这里记录的是配置指定的 API 等价金额。

关键取舍：

- DeepSeek V4 使用 2026-04-24 首次预览发布价。Pro 输入/输出/命中为 12/24/1 元，Flash 为 1/2/0.2 元。未采用后来缓存降价或正式版价格。
- 旧的 `deepseek-chat`、`deepseek-reasoner` 等别名按用户确认的 V3.2，使用 2/3/0.2 元。`lithiumflow`、`orionmist` 按用户确认的 Gemini 3 Pro 检查点处理。
- Gemini 2.5 Pro 保留早期 75% 缓存折扣。Gemini 3.6 Flash 保留 1.5/7.5 美元原价；3.7、3.8 Flash 使用各自首发的 0.75/3.75 美元优惠。
- Claude Opus/Sonnet 4.6 保留首发超过 200K 输入时的长上下文溢价，不使用 2026-03-13 取消溢价后的价格。Fable 5.1 的首发缓存命中价为 0.25 美元，区别于 Fable 5 的 1 美元。
- MiniMax M2、MiMo V2 Flash 发布即限免，因此首发价记录为零。M3 首发七天五折只适用于不超过 512K 的请求，长上下文按首发原价。
- Qwen3.8 Flash 使用独立价目表：输入 0.8、输出 2.7、命中 0.1、显式写入 1.25 元，不能直接套其他千问模型的缓存比例。
- 用户明确选择的本地或社区模型按零元记录。硬件、电费及可能存在的托管费用不在其中。未知测试模型和无法核实的价格保留原值。

## 价格字段

单位由 `unit` 决定，目录统一采用每百万 Token，币种由 `currency` 指定。

| 字段 | 含义 |
| --- | --- |
| `input` / `output` | 普通输入 / 输出单价 |
| `cached_input` | 缓存命中输入单价；缺省沿用普通输入价 |
| `cached_input_explicit` | 可选的显式缓存命中价，按实际上游请求中的缓存标记选择 |
| `cache_write` | 缓存写入的**完整单价**，Anthropic 对应默认 5 分钟缓存 |
| `cache_write_1h` | Anthropic 一小时缓存写入的完整单价，缺省沿用 `cache_write` |
| `input_tiers` | 按单次请求总输入量选择的价格档位，`min_input_tokens` 为包含边界的下限 |

写入完整单价缺省时，写入 Token 仍包含在普通输入中，不收附加费用。因此，“无额外写入费”应留空，不能填零；零表示写入 Token 本身免费。管理页可编辑默认及一小时写入价。额外的显式命中价和长度档位在 JSON 中配置，普通表单保存会保留它们。

总输入 Token 包含普通输入、缓存读取与缓存写入。计费先将三者分开，再按各自完整单价相加，输出另计。混合 5 分钟和 1 小时写入时，一小时数量是总写入量的子集，不重复累加。

`cache_write_cost` 是写入完整费用；`cache_write_extra_cost` 是它相对普通输入费用的差额，仅用于展示，不能再次加进 `total_cost`。例如 100 万总输入中，有 40 万缓存读取、20 万五分钟写入、10 万一小时写入、30 万普通输入，Sonnet 4.5 写入完整费用为 1.35 美元，其中额外费用为 0.45 美元。

## 用量、记录与验证边界

支持 OpenAI `input_tokens_details.cache_write_tokens`、兼容 Chat 的 `prompt_tokens_details` 写入字段、Anthropic `cache_creation_input_tokens` 及两个 TTL 明细。只有上游实际报告写入量才计算附加费用，不把所有未命中输入都猜成写入。兼容接口转换会保留写入量及一小时明细。

新增列通过现有 SQLite 迁移协议添加，详情、列表、模型/每日汇总、多来源合计和 CSV 均可读取。没有价格时仍记录写入量。旧账单金额保持不变；只读价格对比可以利用旧记录保存的原始 usage 恢复写入量，并逐请求选择档位。

本次不实现 Gemini 按小时的缓存存储费用、峰谷时间自动切换、搜索/图片等独立工具费用、区域溢价、Fast/Batch 服务档位或中转商加价。V4 Flash Vision Exp 记录其首发高峰价，闲时价格列在目录说明中。GPT-5.4/5.5 的会话级长上下文规则仍由已有 Codex 用量估算处理，网关价格目录不推测跨请求会话溢价。

## 重新应用目录

```powershell
python scripts/update_launch_prices.py --aliases logs/confirmed-pricing-aliases.json
python scripts/update_launch_prices.py --aliases logs/confirmed-pricing-aliases.json --apply
```

别名文件为本地可选输入，内容是“配置名或上游 ID → 目录模型名”。预览报告只含模型名称、价格和来源，不含端点密钥。写入前会备份原配置到被 Git 忽略的 `logs` 目录。默认报告为 `logs/launch-price-update-report.json`；没有确定对应关系的项目列在 `unresolved` 中，不自动套价。
