# Tokenizer 本地配置指南

本目录用于存放本地 tokenizer 文件，支持多种模型的高精度 Token 计数。

## 📁 目录结构

```
tokenizers/
├── gemma3-27b-it/          # Gemma 3 27B（Gemini 系列）
│   ├── tokenizer.json
│   ├── tokenizer_config.json
│   └── special_tokens_map.json
│
├── dsv3.2/                 # DeepSeek V3.2
│   ├── tokenizer.json
│   └── tokenizer_config.json
│
├── glm4.7/                 # GLM-4 系列
│   ├── tokenizer.json
│   └── tokenizer_config.json
│
├── kimi/                   # Kimi K2
│   ├── tiktoken.model
│   └── tokenizer_config.json
│
├── minimax/                # MiniMax
│   ├── tokenizer.json
│   └── tokenizer_config.json
│
├── qwen/                   # Qwen 系列
│   ├── tokenizer.json
│   └── tokenizer_config.json
│
├── .gitignore
└── README.md
```

## 📥 获取 tokenizer 文件

### 方法 1：HuggingFace 下载

| Tokenizer | HuggingFace 仓库 | 所需文件 |
|-----------|-----------------|---------|
| Gemma 3 27B | `google/gemma-3-27b-it` | `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json` |
| DeepSeek V3 | `deepseek-ai/DeepSeek-V3` | `tokenizer.json`, `tokenizer_config.json` |
| GLM-4 | `THUDM/glm-4-9b-chat` | `tokenizer.json`, `tokenizer_config.json` |
| Qwen | `Qwen/Qwen2.5-7B` | `tokenizer.json`, `tokenizer_config.json` |
| MiniMax | `MiniMaxAI/MiniMax-Text-01` | `tokenizer.json`, `tokenizer_config.json` |

> **注意**：只下载 JSON 文件，不要下载 `.safetensors` 或 `.bin` 模型权重文件。

### 方法 2：国内镜像加速

```bash
# 使用 hf-mirror.com 镜像
export HF_ENDPOINT=https://hf-mirror.com
huggingface-cli download google/gemma-3-27b-it tokenizer.json tokenizer_config.json special_tokens_map.json --local-dir tokenizers/gemma3-27b-it/
```

### 方法 3：管理面板上传

访问 `http://localhost:5102/admin` → **Tokenizer** Tab，可以直接上传 tokenizer 文件或配置 HuggingFace 自动下载。

## 🧩 自定义 Tokenizer 配置

除了本地目录放置外，还可以通过 `custom_tokenizers.json` 注册 tokenizer：

```json
{
  "kimi": {
    "name": "kimi",
    "display_name": "Kimi K2",
    "source_type": "tiktoken_file",
    "source": "./tokenizers/kimi/tiktoken.model",
    "supported_models": ["kimi"]
  }
}
```

支持的来源类型：
| 类型 | 说明 |
|------|------|
| `huggingface` | 从 HuggingFace Hub 自动加载 |
| `tiktoken_model` | 使用 tiktoken 内置模型名（如 `gpt-4o`） |
| `tiktoken_file` | 本地 `.tiktoken` 文件 |
| `local` | 本地 HuggingFace tokenizer 目录 |

## ✅ 验证安装

重启服务后查看日志：

```
[TOKEN_COUNTER] 已从本地加载 tokenizer: gemma3-27b-it
[TOKEN_COUNTER] 已从本地加载 tokenizer: dsv3.2
[TOKEN_COUNTER] 已加载自定义 tokenizer: kimi (tiktoken_file)
```

也可以在管理面板 → **Tokenizer** Tab 中查看所有已加载 tokenizer 的状态。

## 📊 Token 计数方案优先级

| 优先级 | 方案 | 准确度 | 说明 |
|--------|------|--------|------|
| 1 | 本地 tokenizer 文件 | 99%+ | 从本目录加载，零网络依赖 |
| 2 | HuggingFace 自动下载 | 99%+ | 首次自动下载并缓存 |
| 3 | API 原生计数 | 100% | 部分上游 API 返回 usage 信息 |
| 4 | Tiktoken 近似 | ~95% | 通用回退方案 |
| 5 | 字符估算 | 70-80% | 最后备选 |

## 💡 常见问题

### Q: 必须安装 tokenizer 吗？
A: **不是必须的**。系统默认使用 tiktoken 估算，准确度约 95%，对大多数场景足够。

### Q: 需要 PyTorch 吗？
A: **不需要**。tokenizer 功能不依赖深度学习框架。

### Q: 可以同时放多个版本吗？
A: 可以。系统会按模型名自动匹配对应的 tokenizer。

### Q: 如何为模型指定 tokenizer？
A: 在管理面板的「模型编辑」中设置 `tokenizer_model` 字段，或在 `model_endpoint_map.json` 中手动配置。
