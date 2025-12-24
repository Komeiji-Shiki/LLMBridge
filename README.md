# LMArena Bridge 

> 🚀 将 LMArena 和各种 AI API 转换为兼容 OpenAI 格式的本地代理服务器

LMArena Bridge 是一个强大的 API 网关，可以将 [LMArena](https://lmarena.ai) 平台以及各种第三方 AI API（如 DeepSeek、Gemini、Claude 等）转换为标准的 OpenAI API 格式，让您可以在任何支持 OpenAI API 的应用中使用这些模型。

## ✨ 主要功能

### 🔄 多模式支持
- **LMArena 模式**：通过油猴脚本桥接 LMArena 网页，支持 Direct Chat 和 Battle 模式
- **Direct API 模式**：直接调用第三方 API（透传模式，零延迟）
- **Gemini Native 模式**：原生支持 Google Gemini API 格式

### 📊 实时监控
- Web 管理面板，实时查看请求状态
- Token 用量统计和成本计算
- 请求日志记录和错误追踪
- 多标签页连接状态监控

### 🔧 高级特性
- 多端点负载均衡（轮询策略）
- 图片自动压缩和格式转换
- 思维链（Thinking）分离输出
- 自定义 Tokenizer 配置
- 自动重试机制

## 📋 系统要求

- Python 3.8+
- 现代浏览器（Chrome/Firefox/Edge）
- Tampermonkey 浏览器扩展（仅 LMArena 模式需要）

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

**核心依赖：**
- `fastapi` - Web 框架
- `uvicorn` - ASGI 服务器
- `aiohttp` / `httpx` - 异步 HTTP 客户端
- `Pillow` - 图片处理

**可选依赖（Token 计数）：**
- `tiktoken` - GPT/Claude 模型
- `anthropic` - Claude 官方分词器
- `google-generativeai` - Gemini 官方分词器
- `transformers` - 通用分词器

### 2. 配置模型端点

复制示例配置文件：
```bash
cp model_endpoint_map.example.json model_endpoint_map.json
```

编辑 `model_endpoint_map.json` 配置您的模型：

```jsonc
{
  // LMArena 模式示例
  "claude-sonnet-4": {
    "session_id": "your-session-id-here",
    "mode": "direct_chat",
    "type": "text"
  },
  
  // Direct API 透传模式示例
  "deepseek-v3": {
    "api_type": "direct_api",
    "api_base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-your-api-key",
    "model_id": "deepseek-chat",
    "display_name": "DeepSeek V3",
    "passthrough": true,
    "pricing": {
      "input": 0.14,
      "output": 0.28,
      "unit": 1000000,
      "currency": "CNY"
    }
  },
  
  // Gemini Native 模式示例
  "gemini-2.0-flash": {
    "api_type": "gemini_native",
    "api_key": "your-gemini-api-key",
    "model_id": "gemini-2.0-flash-exp",
    "display_name": "Gemini 2.0 Flash",
    "enable_thinking": true,
    "thinking_budget": 20000
  }
}
```

### 3. 启动服务器

**Windows：**
```bash
点击启动.CMD
```

**命令行：**
```bash
python -m uvicorn routes:app --host 0.0.0.0 --port 5102 --reload
```

### 4. 安装油猴脚本（仅 LMArena 模式需要）

1. 安装 [Tampermonkey](https://www.tampermonkey.net/) 浏览器扩展
2. 导入 `TampermonkeyScript/LMArenaApiBridge.js` 脚本
3. 打开 [lmarena.ai](https://lmarena.ai) 并确保脚本已激活
4. 页面标题前出现 ✅ 表示连接成功

### 5. 使用 API

现在您可以使用任何支持 OpenAI API 的客户端连接：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:5102/v1",
    api_key="sk-any-key"  # Direct API 模式下可为任意值
)

response = client.chat.completions.create(
    model="deepseek-v3",  # 使用您配置的模型名称
    messages=[{"role": "user", "content": "你好！"}],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content, end="")
```

## 🌐 API 端点

### 核心 API
| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 聊天补全（支持流式/非流式） |
| `/v1/models` | GET | 获取可用模型列表 |

### 管理面板
| 端点 | 说明 |
|------|------|
| `/admin` | 管理面板（配置、统计、模型管理） |
| `/monitor` | 实时监控面板 |

### 监控 API
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/monitor/stats` | GET | 获取统计数据 |
| `/api/monitor/active` | GET | 查看活跃请求 |
| `/api/monitor/logs/requests` | GET | 请求日志 |
| `/api/request/{id}` | GET | 请求详情 |

### WebSocket
| 端点 | 说明 |
|------|------|
| `/ws` | 油猴脚本连接 |
| `/ws/monitor` | 监控面板实时更新 |

## 📁 项目结构

```
LMArenaBridge-ModifiedVersion-12-22/
├── routes/                    # API 路由模块
│   ├── api_routes.py         # 核心 API 路由
│   ├── admin_routes.py       # 管理面板路由
│   ├── monitor_routes.py     # 监控 API 路由
│   └── websocket_routes.py   # WebSocket 路由
├── core/                      # 核心模块
│   ├── config_loader.py      # 配置加载器
│   ├── db_stats.py           # 数据库统计
│   ├── load_balancer.py      # 负载均衡
│   └── tab_manager.py        # 标签页管理
├── modules/                   # 功能模块
│   ├── token_counter.py      # Token 计数器
│   ├── image_processor.py    # 图片处理
│   └── monitoring.py         # 监控服务
├── services/                  # 服务层
│   ├── direct_api_service.py # Direct API 服务
│   ├── stream_processor.py   # 流处理器
│   └── message_converter.py  # 消息格式转换
├── js/                        # 前端 JS 模块
│   ├── admin-core.js         # 管理面板核心
│   ├── admin-charts.js       # 图表功能
│   └── admin-models.js       # 模型管理
├── TampermonkeyScript/        # 油猴脚本
│   └── LMArenaApiBridge.js   # 浏览器桥接脚本
├── admin.html                 # 管理面板页面
├── monitor.html               # 监控面板页面
├── model_endpoint_map.json    # 模型端点配置
├── requirements.txt           # Python 依赖
└── 点击启动.CMD               # Windows 启动脚本
```

## ⚙️ 配置说明

### 模型配置字段

#### LMArena 模式
| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | LMArena 会话 ID（必需） |
| `mode` | string | 操作模式：`direct_chat` 或 `battle` |
| `battle_target` | string | Battle 模式目标：`A` 或 `B` |
| `type` | string | 模型类型：`text` 或 `image` |
| `max_temperature` | number | 温度上限（可选） |

#### Direct API 模式
| 字段 | 类型 | 说明 |
|------|------|------|
| `api_type` | string | 设为 `direct_api` 或 `gemini_native` |
| `api_base_url` | string | API 基础 URL |
| `api_key` | string | API 密钥 |
| `model_id` | string | 目标模型 ID |
| `display_name` | string | 统计显示名称 |
| `passthrough` | boolean | 是否启用透传模式 |
| `enable_prefix` | boolean | 启用 DeepSeek Prefix 模式 |
| `enable_thinking` | boolean | 启用 Gemini 思维链 |
| `thinking_budget` | number | 思维链 Token 预算 |
| `thinking_separator` | string | 思考内容分隔符 |
| `custom_params` | object | 自定义请求参数 |

#### 计费配置
```jsonc
"pricing": {
  "input": 2.5,        // 输入 token 单价
  "output": 10,        // 输出 token 单价
  "unit": 1000000,     // 计价单位（每百万 token）
  "currency": "USD"    // 货币：USD 或 CNY
}
```

#### 图片压缩配置
```jsonc
"image_compression": {
  "enabled": true,
  "target_format": "webp",    // jpg/webp/png
  "quality": 80,              // 1-100
  "target_size_kb": 500,      // 目标大小
  "max_width": 1920,
  "max_height": 1080,
  "convert_png_to_jpg": true
}
```

### 多端点负载均衡

使用数组配置多个端点，系统会自动轮询：

```jsonc
"claude-hybrid": [
  {
    "api_type": "direct_api",
    "api_base_url": "https://api1.example.com/v1",
    "api_key": "key1",
    "model_id": "claude-3-5-sonnet",
    "passthrough": true
  },
  {
    "api_type": "direct_api",
    "api_base_url": "https://api2.example.com/v1",
    "api_key": "key2",
    "model_id": "claude-3-5-sonnet",
    "passthrough": true
  }
]
```

## 🖥️ 管理面板

访问 `http://localhost:5102/admin` 进入管理面板：

### 功能模块

1. **概览** - 系统状态、Token 用量统计、请求趋势图表
2. **模型端点** - 添加/编辑/删除模型配置
3. **Tokenizer 配置** - 为每个模型配置分词器类型
4. **ID 捕获** - 一键捕获 LMArena 会话 ID
5. **配置编辑** - 直接编辑配置文件
6. **监控面板** - 嵌入式实时监控

### ID 捕获使用方法

1. 进入管理面板的"ID 捕获"页面
2. 选择捕获模式（Direct Chat 或 Battle）
3. 点击"开始捕获"按钮
4. 在 LMArena 页面找到已有对话
5. **点击对话的 Retry（刷新）按钮**
6. 自动弹出配置窗口，填写模型名称并保存

## 📈 监控面板

访问 `http://localhost:5102/monitor` 进入监控面板：

- **实时统计** - 活跃请求、总请求数、成功率
- **标签页状态** - 各浏览器标签页连接和负载
- **活跃请求** - 正在处理的请求列表
- **请求日志** - 历史请求记录和详情
- **错误日志** - 失败请求和错误信息

## 🔧 高级用法

### 使用 cURL

```bash
curl http://localhost:5102/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-any-key" \
  -d '{
    "model": "deepseek-v3",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "stream": true
  }'
```

### 多模态请求（图片）

```python
response = client.chat.completions.create(
    model="gemini-2.0-flash",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图片里有什么？"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,..."}
                }
            ]
        }
    ]
)
```

### 思维链模式

配置 `thinking_separator` 后，模型的思考内容会被分离到 `reasoning_content` 字段：

```python
response = client.chat.completions.create(
    model="gemini-thinking",
    messages=[{"role": "user", "content": "解一道复杂的数学题"}]
)

# 访问思考内容
reasoning = response.choices[0].message.reasoning_content
content = response.choices[0].message.content
```

## 🐛 故障排除

### 常见问题

1. **油猴脚本无法连接**
   - 确保服务器已启动（端口 5102）
   - 检查浏览器控制台是否有错误
   - 尝试刷新 LMArena 页面

2. **Direct API 请求失败**
   - 检查 API Key 是否正确
   - 验证 api_base_url 格式
   - 查看服务器日志获取详细错误

3. **Token 计数不准确**
   - 安装对应的 tokenizer 依赖
   - 在 Tokenizer 配置中选择正确的分词器类型

4. **图片处理失败**
   - 确保安装了 Pillow 库
   - 检查图片格式是否支持

### 日志文件

- 请求日志：`logs/requests.db`（SQLite 数据库）
- 可通过监控面板或 API 下载

## 📝 更新日志

### v12-22 版本
- 新增 Gemini Native API 支持
- 优化管理面板 UI（深色主题）
- 增强思维链分离功能
- 改进多标签页负载均衡
- 支持模型级别图片压缩配置
- Token 计数来源可配置（API 返回/本地计算）

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**注意**：本项目仅供学习和研究使用，请遵守各 AI 服务提供商的使用条款。
