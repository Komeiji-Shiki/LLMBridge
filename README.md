# LMArenaBridge (LLMBridge)

一个强大的 LLM API 聚合网关/反向代理，支持多种 AI 模型的统一接入、负载均衡、成本监控和流式响应处理。

## 🌟 功能特性

### 核心代理
- **多模型支持**：支持 OpenAI、Claude、Gemini、DeepSeek 等主流模型及任何 OpenAI 兼容 API
- **多格式桥接**：
  - OpenAI 兼容格式 ↔ Gemini Native API 双向转换
  - **Anthropic ↔ OpenAI 双向转换**：Claude 客户端发 `/v1/messages` → 自动转为 OpenAI 格式请求 → 转发后端 → 将 OpenAI 响应（含流式 SSE）转回 Anthropic 格式返回；也可直通 `/messages` 端点
  - 透传模式：不修改请求/响应，原样转发到任何 OpenAI 兼容后端
  - 消息角色自动转换（system/user/assistant 重映射）
- **LMArena 集成（已弃用）**：兼容 LMArena 协议的会话管理和 Battle 模式（新版本不再依赖 LMArena）
- **多 API Key 轮询**：支持配置多个 API Key 并轮询调用，提升并发上限

### 请求增强
- **自动重试**：支持 429/503 错误自动重试，可配置重试次数和延迟
- **系统提示词注入**：在请求中自动注入预设的系统提示词
- **图片优化**：自动压缩和处理多模态请求中的图片（支持格式转换、尺寸限制、质量调整）
- **思考内容分离**：通过 thinking_separator 自动分离思维链和正文内容
- **温度/最大 Token 限制**：按模型限制 temperature 和 max_tokens，防止配置错误

### Token 与成本
- **精确 Token 计数**：支持多种 tokenizer（Anthropic、Gemma、DeepSeek、tiktoken、自定义）
- **缓存命中统计**：从上游 API 提取缓存命中 Token（`cached_tokens`），单独记录和定价
- **成本计算**：支持三级定价——未缓存输入 Token、缓存命中 Token、输出 Token
- **可配置汇率**：USD/CNY 双货币支持，汇率在 `config.jsonc` 中配置（默认 7.2）
- **成本趋势图**：管理面板中的每日成本柱状图，支持 USD/CNY 切换
- **导出报告**：一键导出 CSV 格式的 Token 使用/成本报告

### 监控与管理
- **Web 管理面板**：深色科技风格，实时监控、模型配置、Token 统计、成本分析
- **WebSocket 实时推送**：请求状态实时更新到监控面板
- **SQLite 统计数据库**：高性能查询，按模型/日期聚合 Token 和成本
- **分层 JSON 日志**：按日期/小时组织的请求日志（可选 gzip 压缩）
- **请求详情查看**：支持查看历史请求的完整 messages 和 response
- **模型健康检查**：一键测试所有 API Key 的连通性
- **自动抓取模型（已弃用）**：从 Chrome 标签页自动抓取 LMArena 模型列表（新版本不再依赖 LMArena）

### 运维友好
- **配置热更新**：修改 `config.jsonc` 或 `model_endpoint_map.json` 自动重载
- **内存管理**：Tokenizer 空闲自动清理、图片缓存 LRU 淘汰
- **自定义 Tokenizer**：支持上传 HuggingFace / tiktoken / 本地 tokenizer
- **Web 端配置编辑**：管理面板中直接编辑模型端点、定价等全部配置

## 📁 项目结构

```
LLMBridge/
├── api_server_new.py          # FastAPI 主服务器入口
├── config.jsonc               # 主配置文件（支持注释、热更新）
├── model_endpoint_map.json    # 模型端点映射配置
├── models.json                # 模型列表
│
├── core/                      # 核心模块
│   ├── config_loader.py      # 配置加载器（JSONC 解析、热更新）
│   ├── constants.py           # 集中常量管理
│   ├── db_stats.py            # SQLite 统计查询（Token/成本/请求聚合）
│   ├── api_key_manager.py     # 访客 Key 权限管理与 RPM 限流
│   ├── app_state.py           # 应用全局状态管理
│   ├── context.py             # 请求上下文管理
│   ├── lifespan.py            # FastAPI 生命周期管理
│   ├── load_balancer.py       # 负载均衡器
│   └── ...
│
├── routes/                    # API 路由
│   ├── api_routes.py          # 主 API 路由（/v1/chat/completions 等）
│   ├── direct_api_handler.py  # Direct API 请求分发入口
│   ├── _direct_api_passthrough.py  # OpenAI 兼容透传处理
│   ├── _direct_api_gemini.py  # Gemini Native API 处理
│   ├── admin_routes.py        # 管理面板 API（统计/配置/导出）
│   ├── monitor_routes.py      # 监控面板 API + WebSocket
│   └── ...
│
├── converters/                 # 数据转换模块
│   └── __init__.py
│
├── services/                  # 业务服务
│   ├── direct_api_service.py  # Direct API 调用、成本计算
│   ├── stream_processor.py    # 流处理主逻辑
│   ├── message_converter.py   # 消息格式转换
│   ├── image_handler.py       # 图片处理（压缩/格式转换）
│   ├── image_service.py       # 图片服务（上传/缓存）
│   ├── token_service.py       # Token 计数服务
│   └── ...
│
├── modules/                   # 功能模块
│   ├── monitoring.py          # 请求监控与统计服务
│   ├── monitoring_sqlite.py   # SQLite 日志写入/查询
│   ├── file_uploader.py       # 文件上传处理
│   ├── image_processor.py     # 图片处理器
│   ├── update_script.py       # 模型列表更新脚本
│   └── token_counter/         # Token 计数（多 tokenizer 支持）
│
├── background_tasks/           # 后台任务
│   ├── monitors.py            # 内存/配置监控、超时请求清理
│   └── request_processor.py   # 暂存请求处理器
│
├── js/                        # 前端管理界面
│   ├── admin-core.js          # 核心功能（消息提示、导航、设置）
│   ├── admin-overview.js      # 系统概览、Token 统计加载
│   ├── admin-charts.js        # 图表渲染（Chart.js）
│   ├── admin-models-edit.js   # 模型编辑（API Key、表单、保存）
│   ├── admin-models-list.js   # 模型列表渲染、排序、删除
│   ├── admin-models-capture.js # 自动抓取模型、键测试
│   ├── admin-config.js        # 配置管理
│   ├── admin-apikeys.js       # 访客 Key 管理
│   └── admin-tokenizer.js     # Tokenizer 测试
│
├── file_bed_server/           # 文件床/图床服务（独立运行）
│   ├── main.py                # 图床服务器入口
│   └── requirements.txt       # 图床依赖
│
├── admin.html                 # 管理面板 HTML
├── monitor.html               # 监控面板 HTML
└── token_calculator.html      # Token 计算器页面
```

## 🏗 架构概览


```
客户端 (OpenAI/Claude SDK)
    │
    ▼
LMArenaBridge  (FastAPI :5102)
    │
    ├── Direct API 模式 ────────────► 上游 API (OpenAI/Gemini/DeepSeek 等)
    │
    └── LMArena 代理模式（已弃用）──WebSocket──► 浏览器 (油猴脚本)
                                            │
                                            ▼
                                       LMArena 网站
```

- **Direct API 模式**：直接转发到上游 API，支持流式 SSE 透传
- **LMArena 代理模式（已弃用）**：通过 WebSocket 连接浏览器中的油猴脚本，把请求注入 LMArena 网页，再将响应转回 OpenAI 格式（新版本使用 Direct API 模式即可）
- **监控 / 管理面板**：独立的管理界面，实时统计、Token 成本、请求日志

## 🚀 快速开始

### 安装依赖
```bash
pip install -r requirements.txt
```

关键依赖：`fastapi`, `uvicorn`, `aiohttp`, `psutil`, `cachetools`

### 配置文件

```bash
# 1. 复制示例配置
cp config.jsonc.example config.jsonc
cp model_endpoint_map.json.example model_endpoint_map.json

# 2. 编辑 config.jsonc（`session_id` 等 LMArena 相关字段已弃用，无需填写）
# 3. 编辑 model_endpoint_map.json，配置你要代理的模型

# 4. (可选) 清理示例配置中的敏感占位符
python 清理敏感数据.py
```

### 启动服务

```bash
# Windows
点击启动.CMD

# Linux / Mac
python api_server_new.py

# 改进版启动（带进程守护）
start_server_improved.cmd
```

### 访问管理界面
```
http://localhost:5102/admin
```


## 🐵 油猴脚本使用（已弃用）

> ⚠️ LMArena 代理模式已弃用。以下内容仅供参考，新版本直接使用 Direct API 模式即可，无需油猴脚本。

LMArena 代理模式（已弃用）需要浏览器运行油猴脚本 `TampermonkeyScript/LMArenaApiBridge.js`。

1. 在 Chrome 中安装 [Tampermonkey](https://www.tampermonkey.net/) 扩展
2. 导入 `LMArenaApiBridge.js` 到油猴仪表盘
3. 打开 `https://lmarena.ai` 并登录
4. 脚本自动连接本机 WebSocket (`ws://localhost:5102/ws`)
5. 管理面板 `标签页` Tab 中看到绿色连接即就绪（已弃用）

**多标签页并发（已弃用）**：打开多个 LMArena 标签页 = 成倍提升并发上限（每标签页 6 并发）

### 可选 Tokenizer
```bash
pip install anthropic    # Claude tokenizer
pip install transformers # Gemma tokenizer
pip install tiktoken     # GPT tokenizer
```

## ⚙️ 配置说明

### `config.jsonc` 主要配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `server_port` | int | 服务端口（默认 5102） |
| `web_access_key` | string | Web 访问密码 |
| `exchange_rate.USD_TO_CNY` | float | 美元→人民币汇率（默认 7.2） |
| `session_id` | string | LMArena 会话 ID（已弃用） |
| `id_updater_last_mode` | string | ID 更新模式（已弃用） |
| `enable_auto_retry` | bool | 全局自动重试开关 |
| `image_optimization` | dict | 图片优化全局配置 |

### `model_endpoint_map.json` 模型配置

```json
{
  "gpt-4o": {
    "api_type": "direct_api",
    "api_base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxx",
    "model_id": "gpt-4o",
    "display_name": "GPT-4o",
    "passthrough": true,
    "pricing": {
      "input": 2.5,
      "output": 10.0,
      "cached_input": 1.25,
      "unit": 1000000,
      "currency": "USD"
    },
    "max_temperature": 1.0,
    "max_tokens": 128000
  }
}
```

**定价字段说明**：
- `input`: 输入 Token 单价（未缓存部分）
- `output`: 输出 Token 单价
- `cached_input` (可选): 缓存命中 Token 单价，不配置则等于 `input`
- `unit`: 计价单位 Token 数（通常 1000000）
- `currency`: 货币（USD 或 CNY）

**成本公式**：
```
total_cost = (uncached_input × input_price + cached_input × cached_input_price + output × output_price) ÷ unit
```

## 🔧 主要 API 端点

| 端点 | 方法 | 描述 |
|-----|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容聊天接口 |
| `/v1/messages` | POST | Anthropic Claude 兼容接口 |
| `/v1beta/models/{model}:generateContent` | POST | Gemini Native API 接口 |
| `/v1/models` | GET | 模型列表 |
| `/admin` | GET | 管理面板 |
| `/api/admin/token_stats` | GET | Token 统计数据 |
| `/api/admin/export_report` | GET | 导出 CSV 报告 |
| `/api/admin/models` | GET/POST/DELETE | 模型配置 CRUD |
| `/api/admin/config` | GET/POST | 主配置管理 |
| `/api/admin/tokenizer/calculate` | POST | Token 计算 |
| `/api/monitor/stats` | GET | 实时监控统计 |
| `/api/monitor/ws` | WebSocket | 实时推送 |

## 📊 监控与统计

### Token 统计面板
- 输入/输出 Token 饼状图 & 条形图（按模型）
- 每日 Token 趋势折线图（输入/输出/缓存命中/比率）
- 每日成本趋势柱状图（USD/CNY 切换）
- 模型统计表格（Token/成本/缓存命中/RPM/TPM）
- 模型合并/删除操作

### 成本监控
- 按模型实时计算 API 调用成本
- 缓存命中 Token 独立定价和统计
- USD/CNY 双货币汇总（按配置汇率换算）
- 失败请求也计算输入成本

### 请求日志
- SQLite 数据库：高性能聚合查询
- 分层 JSON 日志：按日期/小时组织
- 请求详情查看：完整 messages + response

## 🔑 访客 Key 系统

通过 web端的API Key 管理 配置访客 API Key，实现**细粒度的模型权限控制**和 **RPM 请求速率限制**。

### 配置文件格式
```json
{
  "a76056bfc0936d95": {
    "name": "访客名称",
    "secret": "sk-your-visitor-key",
    "allowed_models": ["gpt-4o", "deepseek-chat"],
    "rpm_limit": 35,
    "enabled": true,
    "description": "",
    "created_at": 1772218824.7140331,
    "last_used_at": 1774750312.9903347,
    "total_requests": 41312
  }
}
```

### 字段说明
| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 访客名称（用于标识） |
| `secret` | string | 访客的 API Key（客户端携带此 Key 请求） |
| `allowed_models` | string[] | 该访客可以访问的模型列表 |
| `rpm_limit` | int | 每分钟最大请求数限制 |
| `enabled` | bool | 是否启用该访客 |
| `description` | string | 备注描述 |
| `total_requests` | int | 累计请求次数（自动统计） |

### 使用方式
访客使用自己的 `secret` 作为 API Key 调用接口，系统自动：
1. 校验 Key 是否有效且启用
2. 检查请求的模型是否在 `allowed_models` 白名单内
3. 检查 RPM 是否超限
4. 通过后转发到上游并记录用量

> 管理面板 → API Keys Tab 可直接增删改查访客 Key，无需手动编辑 JSON。

## 🧮 Token 计算器

访问 `http://localhost:5102/token_calculator.html` 打开独立的 Token 计数测试页面。

- 输入文本实时计算 Token 数量
- 支持切换不同 tokenizer（Anthropic、tiktoken、DeepSeek、Gemma 等）
- 适用于调试提示词长度、预估 API 成本

## 🔧 自定义 Tokenizer 配置

项目支持上传自定义 tokenizer，配置文件为 `custom_tokenizers.json`（已 gitignore，使用 `custom_tokenizers.json.example` 作为模板）。

> 📖 本地 tokenizer 文件放置与各模型下载指南详见 [`tokenizers/README.md`](tokenizers/README.md)

### 支持的来源类型
| 来源类型 | 说明 | 示例 |
|---------|------|------|
| `huggingface` | 从 HuggingFace Hub 加载 | `"source": "google/gemma-3-27b-it"` |
| `tiktoken_model` | 使用 tiktoken 模型名 | `"source": "gpt-4o"` |
| `tiktoken_file` | 从本地 .tiktoken 文件加载 | `"source": "./tokenizers/kimi/tiktoken.model"` |
| `local` | 本地 HuggingFace tokenizer 目录 | `"source": "./tokenizers/glm4.7"` |

### 配置示例
```json
{
  "kimi": {
    "name": "kimi",
    "display_name": "Kimi K2",
    "source_type": "tiktoken_file",
    "source": "./tokenizers/kimi/tiktoken.model",
    "description": "Kimi K2 tokenizer",
    "supported_models": ["kimi"]
  }
}
```

> 管理面板 → Tokenizer Tab 可上传、测试、管理自定义 tokenizer，无需手动编辑 JSON。

## 🆔 ID 捕获工具（已弃用）

> ⚠️ 此工具用于 LMArena 代理模式，已随 LMArena 集成一同弃用。新版本无需此工具。

`id_updater.py` 是一个独立运行的 HTTP 服务器（端口 5103），用于从浏览器中自动捕获 LMArena 会话 ID 并写入配置文件。

### 运行方式
```bash
python id_updater.py
```

### 支持的模式（已弃用）
- **Direct Chat**：捕获单模型对话的 session_id
- **Battle**：捕获匿名对战模型的 session_id（需指定 A/B 侧）
- **三种自动保存策略**：`model`（保存到指定模型）、`global`（保存到全局配置）、`ask`（每次询问）

> 此工具配合油猴脚本 `LMArenaApiBridge.js` 使用，详见「油猴脚本使用」章节。

## 🔧 辅助脚本

| 脚本 | 用途 |
|------|------|
| `点击启动.CMD` | 一键启动服务 |
| `start_server_improved.cmd` | 带进程守护的启动 |
| `check_processes.cmd` | 检查端口占用和进程状态 |
| `kill_server.cmd` | 停止所有相关进程 |
| `刷新模型.cmd` | 强制刷新模型列表 |
| `清理敏感数据.py` | 清除配置中的占位 key 和敏感信息 |
| `id_updater.py` | LMArena ID 捕获工具（已弃用） |
| `start_tunnel.cmd` | Cloudflare Tunnel 启动脚本 |

## ❓ 常见问题

### 流式输出卡顿？
1. 检查是否有大量请求在 admin 面板同时加载（统计查询、日志详情等）
2. 检查 SQLite 是否启用（默认启用，走 WAL 模式）
3. 查看 `logs/requests.db` 大小，过大可考虑清理

### API Key 轮询不生效？
- 在 `model_endpoint_map.json` 中将 `api_key` 字段改为 `api_keys` 数组

```json
"api_keys": ["sk-key1", "sk-key2", "sk-key3"]
```

### 模型配置热更新不生效？
修改 `model_endpoint_map.json` 后，服务会自动检测并重载（默认 5 秒延迟）。如仍不生效，运行 `刷新模型.cmd`。

### 启动提示 "地址已被占用"？
端口 5102 被占用。运行 `check_processes.cmd` 查看，或运行 `kill_server.cmd` 停止旧进程。

### 公网访问安全？
- **不建议直接将服务暴露在公网**
- 推荐使用 Cloudflare Tunnel（`start_tunnel.cmd`）
- 在 `config.jsonc` 中设置 `web_access_key` 保护管理面板
- 访客 Key 系统：配合 `api_key_manager` 实现细粒度模型权限和 RPM 限制


MIT License

---

Made with Shiki and Gray
