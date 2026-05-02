# Gemma Tokenizer 本地安装指南

本目录用于存放Gemma tokenizer文件，用于精确计算Gemini模型的token数量。

## 📁 目录结构

将tokenizer文件放在以下任一目录（优先级从高到低）：

```
tokenizers/
├── gemma3-27b-it/        # 🌟 最优：Gemma 3 27B Instruction-Tuned（如果有）
│   ├── tokenizer.json
│   ├── tokenizer_config.json
│   └── special_tokens_map.json
│
├── gemma-2b-it/          # 推荐：Gemma 2B Instruction-Tuned
│   ├── tokenizer.json
│   ├── tokenizer_config.json
│   └── special_tokens_map.json
│
├── gemma-7b-it/          # 备选：Gemma 7B Instruction-Tuned
│   ├── tokenizer.json
│   ├── tokenizer_config.json
│   └── special_tokens_map.json
│
├── gemma-2b/             # 备选：Gemma 2B Base
│   └── ...
│
└── gemma/                # 通用：任意Gemma tokenizer
    └── ...
```

## 📥 下载文件

### 方法1：从Hugging Face下载

访问以下任一链接：
- **Gemma 3 27B IT**（最新最优）: https://huggingface.co/google/gemma-3-27b-it/tree/main
- **Gemma 2B IT**（推荐）: https://huggingface.co/google/gemma-2b-it/tree/main
- **Gemma 7B IT**: https://huggingface.co/google/gemma-7b-it/tree/main

下载这3个必需文件：
- `tokenizer.json` （约17-30MB，取决于模型大小）
- `tokenizer_config.json` （约1KB）
- `special_tokens_map.json` （约1KB）

**注意**：
- ✅ **只需要下载这3个JSON文件**
- ❌ **不要下载** `.safetensors` 或 `.bin` 文件（那些是模型权重，几GB大小）

### 方法2：使用Git LFS（高级用户）

```bash
# 安装Git LFS
git lfs install

# 克隆仅tokenizer文件
GIT_LFS_SKIP_SMUDGE=1 git clone https://huggingface.co/google/gemma-2b-it tokenizers/gemma-2b-it

# 下载tokenizer文件
cd tokenizers/gemma-2b-it
git lfs pull --include="tokenizer*.json,special_tokens_map.json"
```

## 📂 文件放置

### Windows系统

1. 在项目根目录创建文件夹：
```cmd
# 使用Gemma 3（如果有）
mkdir tokenizers\gemma3-27b-it

# 或使用Gemma 2
mkdir tokenizers\gemma-2b-it
```

2. 将下载的文件复制到该文件夹：
```
LMArenaBridge-ModifiedVersion-10-28-lite\
└── tokenizers\
    └── gemma3-27b-it\        # 或 gemma-2b-it\
        ├── tokenizer.json
        ├── tokenizer_config.json
        └── special_tokens_map.json
```

**复制命令示例**：
```cmd
# 假设tokenizer文件在 D:\downloads\gemma3-27b-it\
copy D:\downloads\gemma3-27b-it\*.json tokenizers\gemma3-27b-it\
```

### Linux/Mac系统

```bash
# 创建文件夹
mkdir -p tokenizers/gemma3-27b-it

# 复制文件
cp /path/to/downloaded/gemma3-27b-it/*.json tokenizers/gemma3-27b-it/
```

## ✅ 验证安装

重启程序后，查看日志：

**成功**（示例）：
```
[TOKEN_COUNTER] 已从本地加载Gemma tokenizer: gemma3-27b-it
```
或
```
[TOKEN_COUNTER] 已从本地加载Gemma tokenizer: gemma-2b-it
```

**未安装**（使用tiktoken替代）：
```
[TOKEN_COUNTER] Gemma tokenizer不可用，将使用tiktoken（这是正常的，不影响使用）
[TOKEN_COUNTER] 提示：可将tokenizer文件放到 .../tokenizers/gemma 目录
```

## 🔍 文件说明

| 文件名 | 大小 | 说明 |
|--------|------|------|
| `tokenizer.json` | 17-30MB | 主要的tokenizer数据（词表），大小取决于模型 |
| `tokenizer_config.json` | ~1KB | Tokenizer配置 |
| `special_tokens_map.json` | ~1KB | 特殊token映射 |
| `tokenizer.model` | 可选 | 某些版本可能包含此文件 |

## 💡 常见问题

### Q: 是否必须安装Gemma tokenizer？

A: **不是必须的**。如果不安装，系统会自动使用tiktoken（准确度已经很高，误差<5%）。

### Q: 需要安装PyTorch吗？

A: **不需要**。Tokenizer功能不需要深度学习框架。看到"None of PyTorch"的警告是正常的，可以忽略。

### Q: 文件下载不了怎么办？

A: 可以使用代理，或者使用国内镜像站点（如hf-mirror.com）。

### Q: 可以删除tokenizers目录吗？

A: 可以。删除后系统会自动回退到tiktoken，不影响正常使用。

### Q: Gemma 3和Gemma 2有什么区别？

A: Gemma 3是更新版本，通常有更好的性能和更准确的token计数。如果你有Gemma 3的tokenizer，建议优先使用。

### Q: 可以同时放多个版本吗？

A: 可以！系统会按优先级自动选择：gemma3-27b-it > gemma-2b-it > gemma-7b-it > gemma-2b > gemma。

## 📊 Token计数准确度对比

| 方案 | 准确度 | 说明 |
|------|--------|------|
| Google官方API | 100% | 需要GOOGLE_API_KEY |
| Gemma tokenizer | 99% | 本地文件，无需API密钥 |
| Tiktoken + 校准 | 95% | 默认方案，快速稳定 |
| 字符估算 | 70-80% | 最后备选 |

## 🎯 推荐配置

对于大多数用户，**使用默认的tiktoken已经足够**。

如果你需要更高精度（如用于计费或精确分析），可以安装Gemma tokenizer。