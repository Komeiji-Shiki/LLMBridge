"""
Direct API - Anthropic 原生格式兼容处理
让 /v1/chat/completions 接口也能调用 anthropic_native 类型的模型。

流程：
  OpenAI 请求 → Anthropic 请求 → 上游 /messages 端点 → Anthropic 响应 → OpenAI 响应

支持：
  - text / image / tool_use / tool_result / thinking 的双向转换
  - 流式 SSE 和非流式 JSON
  - thinking 参数（enabled / adaptive / disabled + display）
  - 系统提示词注入（Anthropic 原生格式，system 顶层字段）
"""
import asyncio
import codecs
import copy
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

from core.constants import TimeoutDefaults
from utils.monitor_params import build_monitor_request_params
from utils.task_registry import spawn
from converters.anthropic_openai import extract_anthropic_usage_tokens
from utils.json_unescape import StreamingUnicodeUnescaper
from ._direct_api_utils import (
    detect_first_chunk_error,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    is_error_json,
    map_upstream_error_to_status_code,
    normalize_to_openai_error,
)

logger = logging.getLogger(__name__)


def _truncate_body_for_log(body: dict) -> dict:
    """截断请求体中的长文本块用于调试日志，避免日志过大。

    🔧 性能修复：旧版对整个请求体 copy.deepcopy，含 base64 图片时
    每次打日志都要完整深拷贝几 MB 数据。现在只对需要截断的路径
    做选择性浅拷贝（返回值仅供 json.dumps 打印，不会被修改），
    并顺带截断图片 source.data，避免序列化大 base64 白耗 CPU。
    """
    def _trunc(val):
        if isinstance(val, str) and len(val) > 80:
            return val[:80] + "...[truncated]"
        return val

    def _trunc_block(block):
        if not isinstance(block, dict):
            return block
        nb = dict(block)
        for key in ("text", "thinking", "data", "partial_json"):
            if isinstance(nb.get(key), str):
                nb[key] = _trunc(nb[key])
        if nb.get("type") == "tool_result" and isinstance(nb.get("content"), str):
            nb["content"] = _trunc(nb["content"])
        source = nb.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            ns = dict(source)
            ns["data"] = _trunc(ns["data"])
            nb["source"] = ns
        return nb

    debug = dict(body)
    if isinstance(debug.get("messages"), list):
        new_msgs = []
        for msg in debug["messages"]:
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                new_msg = dict(msg)
                new_msg["content"] = [_trunc_block(b) for b in msg["content"]]
                new_msgs.append(new_msg)
            else:
                new_msgs.append(msg)
        debug["messages"] = new_msgs
    sys_val = debug.get("system")
    if isinstance(sys_val, list):
        debug["system"] = [_trunc_block(b) for b in sys_val]
    elif isinstance(sys_val, str):
        debug["system"] = _trunc(sys_val)
    return debug


# ============================================================
# OpenAI 请求 → Anthropic 请求
# ============================================================

def _convert_openai_system_to_anthropic(system_msg: Any) -> Any:
    """将 OpenAI system 消息内容转换为 Anthropic system 字段格式。

    OpenAI: system 消息的 content（string 或 array of {type:text, text:...}）
    Anthropic: 字符串 或 [{type:text, text:...}, ...]
    """
    if isinstance(system_msg, str):
        return system_msg
    if isinstance(system_msg, list):
        blocks = []
        for part in system_msg:
            if isinstance(part, dict):
                ptype = part.get("type", "text")
                text = part.get("text", "")
                if ptype in ("text", "input_text") and text:
                    blocks.append({"type": "text", "text": text})
        return blocks if blocks else ""
    return str(system_msg) if system_msg else ""


def _parse_data_url(url: str) -> Optional[Tuple[str, str]]:
    """解析 data URL，返回 (media_type, base64_data) 或 None。

    支持的格式：
      data:image/png;base64,<data>  → ("image/png", "<data>")
      data:image/jpeg,<data>        → ("image/jpeg", "<data>")
      data:image/webp;base64,<data> → ("image/webp", "<data>")
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, data = url.split(",", 1)
    except (ValueError, IndexError):
        return None

    # 从 header 中提取纯 MIME 类型（去掉 "data:" 前缀，去掉 ;base64 等参数）
    media_type = "image/jpeg"
    if ":" in header:
        # "data:image/png;base64" → "image/png;base64" → ["image/png", "base64"] → "image/png"
        # "data:image/png"           → "image/png"           → ["image/png"]            → "image/png"
        mime_part = header.split(":", 1)[1]
        extracted = mime_part.split(";")[0].strip()
        if extracted and "/" in extracted:
            media_type = extracted

    return media_type, data


def _convert_openai_content_to_anthropic_blocks(content: Any, role: str) -> List[Dict[str, Any]]:
    """将 OpenAI 消息 content 转换为 Anthropic content blocks 数组。

    处理 text / image_url 类型的 content part。
    """
    blocks: List[Dict[str, Any]] = []

    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return blocks

    if not isinstance(content, list):
        text = str(content) if content is not None else ""
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "text")

        if ptype in ("text", "input_text"):
            text = part.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})

        elif ptype == "image_url":
            url_content = part.get("image_url", {})
            if isinstance(url_content, dict):
                url = url_content.get("url", "")
                parsed = _parse_data_url(url)
                if parsed:
                    media_type, data = parsed
                    # 安全兜底：杜绝 image/png;base64 这样的非法 media_type
                    # （正常路径 _parse_data_url 已保证正确，此处对异常做最终清洁）
                    clean_media_type = media_type.split(";")[0].strip()
                    if clean_media_type != media_type:
                        logger.warning(
                            f"[OAI_TO_ANTHROPIC] 修正异常的 media_type: "
                            f"{media_type!r} → {clean_media_type!r}")
                        media_type = clean_media_type
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data}
                    })
                elif url:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url}
                    })

        elif ptype == "input_image":
            # 部分客户端使用 input_image 类型
            url = part.get("image_url", "") or part.get("url", "")
            parsed = _parse_data_url(url)
            if parsed:
                media_type, data = parsed
                # 安全兜底：同上
                clean_media_type = media_type.split(";")[0].strip()
                if clean_media_type != media_type:
                    logger.warning(
                        f"[OAI_TO_ANTHROPIC] 修正异常的 media_type (input_image): "
                        f"{media_type!r} → {clean_media_type!r}")
                    media_type = clean_media_type
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data}
                })

    return blocks


def _convert_openai_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI tools → Anthropic tools。

    OpenAI:   [{type:function, function:{name, description, parameters}}]
    Anthropic: [{name, description, input_schema}]
    """
    anthropic_tools: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function", tool)
        if not isinstance(func, dict):
            continue
        name = func.get("name", "")
        if not name:
            continue
        anthropic_tools.append({
            "name": name,
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {}) if isinstance(func.get("parameters"), dict) else {}
        })
    return anthropic_tools


def _convert_openai_tool_choice_to_anthropic(tool_choice: Any) -> Any:
    """OpenAI tool_choice → Anthropic tool_choice。

    OpenAI:    "auto" | "required" | "none" | {type:function, function:{name}}
    Anthropic: {type:auto} | {type:any} | {type:tool, name:...} | {type:none}
    """
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        mapping = {
            "auto": {"type": "auto"},
            "required": {"type": "any"},
            "none": {"type": "none"},
        }
        return mapping.get(tool_choice)
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "auto")
        if tc_type == "function":
            func = tool_choice.get("function", {})
            name = func.get("name", "")
            if name:
                return {"type": "tool", "name": name}
            return {"type": "auto"}
        if tc_type == "auto":
            return {"type": "auto"}
        if tc_type == "any":
            return {"type": "any"}
        if tc_type == "tool":
            return {"type": "tool", "name": tool_choice.get("name", "")}
        if tc_type == "none":
            return {"type": "none"}
    return None


def convert_openai_to_anthropic_request(openai_req: Dict[str, Any]) -> Dict[str, Any]:
    """将 OpenAI chat.completions 请求转换为 Anthropic Messages 请求。

    支持：system / text / image / tool_calls / tool 结果 / tools / tool_choice / thinking
    """
    messages_in = openai_req.get("messages", [])
    if not isinstance(messages_in, list):
        messages_in = []

    # ── 提取 system 消息 ──
    system_blocks: List[Dict[str, Any]] = []
    conversation_messages: List[Dict[str, Any]] = []

    for msg in messages_in:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "system":
            converted = _convert_openai_system_to_anthropic(content)
            if isinstance(converted, str) and converted:
                system_blocks.append({"type": "text", "text": converted})
            elif isinstance(converted, list):
                system_blocks.extend(converted)
            continue

        # ── tool 结果消息（OpenAI role=tool）→ Anthropic user 消息中的 tool_result 块 ──
        if role == "tool":
            tool_use_id = msg.get("tool_call_id", "")
            tool_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            conversation_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_content
                }]
            })
            continue

        # ── assistant 消息：处理 tool_calls（reasoning_content 不转换，缺少 signature 会导致 Anthropic 400）──
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []

            # reasoning_content 不转为 thinking 块，因为缺少 Anthropic 要求的 signature 字段。
            # Anthropic 官方文档允许省略历史消息中的 thinking 块。

            # text 内容
            text_blocks = _convert_openai_content_to_anthropic_blocks(content, role)
            blocks.extend(text_blocks)

            # tool_calls → tool_use 块
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function", {})
                    if not isinstance(func, dict):
                        continue
                    tc_name = func.get("name", "")
                    tc_args_str = func.get("arguments", "{}")
                    try:
                        tc_args = json.loads(tc_args_str) if isinstance(tc_args_str, str) else tc_args_str
                    except json.JSONDecodeError:
                        tc_args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or f"toolu_{uuid.uuid4().hex}",
                        "name": tc_name,
                        "input": tc_args if isinstance(tc_args, dict) else {"raw": tc_args}
                    })

            if not blocks:
                blocks.append({"type": "text", "text": " "})
            conversation_messages.append({"role": "assistant", "content": blocks})
            continue

        # ── user 消息 ──
        blocks = _convert_openai_content_to_anthropic_blocks(content, role)
        if not blocks:
            blocks.append({"type": "text", "text": " "})
        conversation_messages.append({"role": role, "content": blocks})

    # ── Anthropic 要求消息以 user 或 assistant 开头 ──
    if conversation_messages and conversation_messages[0].get("role") == "assistant":
        conversation_messages.insert(0, {"role": "user", "content": [{"type": "text", "text": " "}]})

    # ── Anthropic 要求 user/assistant 交替出现，合并连续同角色消息 ──
    merged_messages: List[Dict[str, Any]] = []
    for msg in conversation_messages:
        if merged_messages and merged_messages[-1].get("role") == msg.get("role"):
            prev_content = merged_messages[-1].get("content", [])
            new_content = msg.get("content", [])
            if isinstance(prev_content, list) and isinstance(new_content, list):
                prev_content.extend(new_content)
        else:
            merged_messages.append(dict(msg))

    anthropic_req: Dict[str, Any] = {
        "model": openai_req.get("model", ""),
        "messages": merged_messages,
        "max_tokens": openai_req.get("max_tokens") or openai_req.get("max_completion_tokens", 4096),
        "stream": bool(openai_req.get("stream", False)),
    }

    # system 字段
    if system_blocks:
        anthropic_req["system"] = system_blocks

    # 常见参数透传（🔧 白名单：OpenAI 的 user/metadata 不是 Anthropic 合法顶层字段，
    # 原样透传会被 Anthropic 严格校验拒绝 400。user 映射到 metadata.user_id，
    # metadata 只挑 user_id，其余键同样非法）
    for key in ("temperature", "top_p", "top_k"):
        if key in openai_req:
            anthropic_req[key] = openai_req[key]

    if "user" in openai_req and openai_req["user"] is not None:
        # Anthropic 要求 metadata.user_id 为不含 PII 的字符串，长度上限 256
        anthropic_req.setdefault("metadata", {})["user_id"] = str(openai_req["user"])[:256]

    openai_meta = openai_req.get("metadata")
    if isinstance(openai_meta, dict) and openai_meta.get("user_id") is not None:
        anthropic_req.setdefault("metadata", {})["user_id"] = str(openai_meta["user_id"])[:256]

    # stop → stop_sequences
    stop = openai_req.get("stop")
    if isinstance(stop, list) and stop:
        anthropic_req["stop_sequences"] = stop
    elif isinstance(stop, str) and stop:
        anthropic_req["stop_sequences"] = [stop]

    # tools 转换
    tools = openai_req.get("tools")
    if isinstance(tools, list) and tools:
        anthropic_tools = _convert_openai_tools_to_anthropic(tools)
        if anthropic_tools:
            anthropic_req["tools"] = anthropic_tools

    # tool_choice 转换
    tool_choice = openai_req.get("tool_choice")
    if tool_choice is not None:
        anthropic_tc = _convert_openai_tool_choice_to_anthropic(tool_choice)
        if anthropic_tc is not None:
            anthropic_req["tool_choice"] = anthropic_tc

    return anthropic_req


def apply_thinking_config(passthrough_body: Dict[str, Any], endpoint_config: Dict[str, Any]) -> Dict[str, Any]:
    """应用 thinking 参数配置（与 /v1/messages 接口的 anthropic_native 分支保持一致）。

    enable_thinking 支持：
      None/""  透传客户端 thinking 参数
      true     强制 enabled + budget_tokens
      "adaptive" 将 enabled 转为 adaptive（Claude Opus 4.7+）
      false    强制 disabled
    """
    et_mode = endpoint_config.get("enable_thinking")
    if et_mode is True:
        et_mode = "enabled"
    elif et_mode is False:
        et_mode = "disabled"

    if et_mode == "enabled":
        # 思考控制方式二选一：
        #   - 配置了 reasoning_effort（前端“强度等级”模式）：Anthropic 新模型
        #     已废弃 thinking.budget_tokens，思考强度由 output_config.effort 控制，
        #     只注入 effort，绝不注入额度字段（二者并存会被上游拒绝）。
        #   - 仅配置了 thinking_budget（前端“Token 预算”模式，旧模型兼容）：
        #     注入 thinking.type=enabled + budget_tokens，并协调 max_tokens。
        #   - 均未配置：不强改，透传客户端原始 thinking 参数。
        configured_effort = endpoint_config.get("reasoning_effort")
        thinking_budget = endpoint_config.get("thinking_budget")
        if configured_effort:
            passthrough_body["output_config"] = {"effort": configured_effort}
            # 客户端已带 thinking 时：enabled → adaptive 并清掉废弃的 budget_tokens
            thinking_obj = passthrough_body.get("thinking")
            if isinstance(thinking_obj, dict) and thinking_obj.get("type") in ("enabled", "adaptive"):
                thinking_obj["type"] = "adaptive"
                thinking_obj.pop("budget_tokens", None)
            else:
                passthrough_body["thinking"] = {"type": "adaptive"}
            logger.info(f"[OAI_TO_ANTHROPIC] thinking 启用(强度等级): output_config.effort={configured_effort}, thinking=adaptive（无 budget_tokens）")
        elif thinking_budget:
            budget = int(thinking_budget or 0)
            # 🔧 校验修复：Anthropic 强制要求 max_tokens > thinking.budget_tokens，
            # 而 convert_openai_to_anthropic_request 在客户端不传 max_tokens 时兜底
            # 为 4096，enabled 模式下必然 400。这里把 max_tokens 与 budget 协调：
            # 不超过端点硬上限时给正文预留空间，否则优先保证 max_tokens 合法。
            hard_cap = endpoint_config.get("max_tokens")
            reserve = 4096  # 给正文输出预留的最小 token 数
            if hard_cap:
                hard_cap = int(hard_cap)
                if budget + reserve > hard_cap:
                    budget = max(1024, hard_cap - reserve)
            passthrough_body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            cur_max = int(passthrough_body.get("max_tokens") or 0)
            if cur_max <= budget:
                new_max = budget + reserve
                passthrough_body["max_tokens"] = min(new_max, hard_cap) if hard_cap else new_max
            passthrough_body.pop("output_config", None)
            logger.info(f"[OAI_TO_ANTHROPIC] thinking 强制启用(Token预算): budget_tokens={budget}, max_tokens={passthrough_body.get('max_tokens')}")
        else:
            # 未配置思考控制方式：尊重客户端原始参数
            logger.info(f"[OAI_TO_ANTHROPIC] thinking 配置未指定控制方式，透传客户端参数")
    elif et_mode == "adaptive":
        thinking = passthrough_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            thinking["type"] = "adaptive"
            thinking.pop("budget_tokens", None)
            logger.info(f"[OAI_TO_ANTHROPIC] thinking.type=enabled → adaptive")
        elif not isinstance(thinking, dict):
            passthrough_body["thinking"] = {"type": "adaptive"}
            logger.info(f"[OAI_TO_ANTHROPIC] thinking 注入 adaptive")
        configured_effort = endpoint_config.get("thinking_effort")
        if configured_effort:
            passthrough_body["output_config"] = {"effort": configured_effort}
            logger.info(f"[OAI_TO_ANTHROPIC] output_config.effort={configured_effort}")
        else:
            passthrough_body.pop("output_config", None)
    elif et_mode == "disabled":
        passthrough_body["thinking"] = {"type": "disabled"}
        passthrough_body.pop("output_config", None)
        logger.info(f"[OAI_TO_ANTHROPIC] thinking 显式禁用")

    # thinking display 注入
    # 🔧 白名单修复：disabled 等不支持 display 的 type 不能注入该字段，
    # 否则 enable_thinking=false 的模型每次请求都会被 Anthropic 400。
    thinking_display = endpoint_config.get("thinking_display", "summarized")
    if thinking_display:
        thinking_obj = passthrough_body.get("thinking")
        if (isinstance(thinking_obj, dict)
                and thinking_obj.get("type") in ("enabled", "adaptive")
                and "display" not in thinking_obj):
            thinking_obj["display"] = thinking_display
            logger.info(f"[OAI_TO_ANTHROPIC] thinking.display={thinking_display}")

    return passthrough_body


def apply_system_injection(passthrough_body: Dict[str, Any], endpoint_config: Dict[str, Any]) -> Dict[str, Any]:
    """应用系统提示词注入（Anthropic 原生格式，system 顶层字段）。

    与 /v1/messages 接口保持一致。
    """
    system_injection_config = endpoint_config.get("system_prompt_injection")
    if not isinstance(system_injection_config, dict) or not system_injection_config.get("enabled", False):
        return passthrough_body

    inject_content = (system_injection_config.get("content") or "").strip()
    if not inject_content:
        return passthrough_body

    position = system_injection_config.get("position", "before_system")
    inject_block = {"type": "text", "text": inject_content}
    existing_system = passthrough_body.get("system")

    if isinstance(existing_system, str):
        existing_blocks = [{"type": "text", "text": existing_system}] if existing_system else []
    elif isinstance(existing_system, list):
        existing_blocks = existing_system
    else:
        existing_blocks = []

    if position == "replace_system":
        passthrough_body["system"] = [inject_block]
    elif position == "after_system":
        passthrough_body["system"] = existing_blocks + [inject_block]
    else:  # before_system（默认）
        passthrough_body["system"] = [inject_block] + existing_blocks

    logger.info(
        f"[OAI_TO_ANTHROPIC] 系统提示词注入已启用 "
        f"(位置: {position}, 内容长度: {len(inject_content)})")

    return passthrough_body


# ============================================================
# 自动提示词缓存（Anthropic Prompt Caching）
# ============================================================

# 允许携带 cache_control 的 content block 类型（thinking/redacted_thinking 不允许打点）
_CACHEABLE_BLOCK_TYPES = ("text", "image", "tool_use", "tool_result", "document", "search_result")


def _find_last_cacheable_block(blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """返回 blocks 中最后一个可打 cache_control 断点的 block，找不到返回 None。"""
    for block in reversed(blocks):
        if isinstance(block, dict) and block.get("type") in _CACHEABLE_BLOCK_TYPES:
            return block
    return None


def _body_has_cache_control(body: Dict[str, Any]) -> bool:
    """检测请求体（tools/system/messages）中是否已存在客户端设置的 cache_control 断点。"""
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("cache_control"):
                return True

    system = body.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("cache_control"):
                return True

    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("cache_control"):
                        return True
    return False


def apply_auto_cache_control(
    passthrough_body: Dict[str, Any],
    endpoint_config: Dict[str, Any],
    log_tag: str = "OAI_TO_ANTHROPIC"
) -> Dict[str, Any]:
    """自动注入 Anthropic prompt caching 断点（cache_control: {"type": "ephemeral"}）。

    endpoint_config.auto_cache 为 true 时启用。注入策略（Anthropic 上限 4 个断点）：
      1. tools 数组最后一个工具（缓存工具定义）
      2. system 最后一个 block（缓存系统提示词，字符串形式先转 blocks）
      3. 倒数第二个 user 消息的最后一个可缓存 block（命中上一轮写入的缓存）
      4. 最后一个 user 消息的最后一个可缓存 block（写入本轮缓存供下一轮命中）

    客户端已自带 cache_control 时跳过注入，尊重客户端的打点策略。
    低于模型最小可缓存长度（如 1024 tokens）时上游会自动忽略断点，不会报错。
    """
    if not endpoint_config.get("auto_cache", False):
        return passthrough_body

    if _body_has_cache_control(passthrough_body):
        logger.info(f"[{log_tag}] 自动缓存：客户端已自带 cache_control，跳过注入")
        return passthrough_body

    injected: List[str] = []

    # 1. tools 最后一个工具
    tools = passthrough_body.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        injected.append("tools")

    # 2. system 最后一个 block（字符串形式先转 blocks 数组）
    system = passthrough_body.get("system")
    if isinstance(system, str) and system:
        system = [{"type": "text", "text": system}]
        passthrough_body["system"] = system
    if isinstance(system, list):
        target = _find_last_cacheable_block(system)
        if target is not None:
            target["cache_control"] = {"type": "ephemeral"}
            injected.append("system")

    # 3/4. 最后两个 user 消息（滚动缓存：上一断点命中缓存，最新断点写入缓存）
    messages = passthrough_body.get("messages")
    if isinstance(messages, list):
        user_indexes = [i for i, m in enumerate(messages)
                        if isinstance(m, dict) and m.get("role") == "user"]
        for msg_idx in user_indexes[-2:]:
            msg = messages[msg_idx]
            content = msg.get("content")
            if isinstance(content, str):
                if not content:
                    continue
                content = [{"type": "text", "text": content}]
                msg["content"] = content
            if isinstance(content, list):
                target = _find_last_cacheable_block(content)
                if target is not None:
                    target["cache_control"] = {"type": "ephemeral"}
                    injected.append(f"messages[{msg_idx}]")

    if injected:
        logger.info(
            f"[{log_tag}] 自动缓存：已注入 {len(injected)} 个 cache_control 断点 → {', '.join(injected)}")
    else:
        logger.info(f"[{log_tag}] 自动缓存：请求体中没有可打断点的位置，未注入")

    return passthrough_body


# ============================================================
# Anthropic 响应 → OpenAI 响应
# ============================================================

def _map_anthropic_stop_reason_to_openai(stop_reason: Optional[str]) -> str:
    """Anthropic stop_reason → OpenAI finish_reason。"""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "pause_turn": "stop",
    }
    return mapping.get(stop_reason or "", "stop")


def extract_anthropic_response_content(response_json: dict) -> Tuple[str, str, List[Dict[str, Any]], int, int, int]:
    """从 Anthropic 响应 JSON 中提取内容。

    返回 (content_text, reasoning_text, tool_calls_list, input_tokens, output_tokens, cached_tokens)
    """
    content_text = ""
    reasoning_text = ""
    tool_calls: List[Dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0

    if not isinstance(response_json, dict):
        return content_text, reasoning_text, tool_calls, input_tokens, output_tokens, cached_tokens

    content_blocks = response_json.get("content")
    if isinstance(content_blocks, list):
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "")
                if t:
                    text_parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking", "")
                if t:
                    thinking_parts.append(t)
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                    }
                })
        content_text = "\n".join(text_parts)
        reasoning_text = "\n".join(thinking_parts)

    input_tokens, output_tokens, cached_tokens = extract_anthropic_usage_tokens(
        response_json.get("usage"))

    return content_text, reasoning_text, tool_calls, input_tokens, output_tokens, cached_tokens


def convert_anthropic_response_to_openai(
    response_json: dict,
    model_name: str,
    request_id: str
) -> dict:
    """将 Anthropic 非流式响应转换为 OpenAI chat.completions 格式。"""
    content_text, reasoning_text, tool_calls, input_tokens, output_tokens, cached_tokens = \
        extract_anthropic_response_content(response_json)

    message: Dict[str, Any] = {"role": "assistant"}

    # reasoning_content（如果有思考内容）
    if reasoning_text:
        message["reasoning_content"] = reasoning_text

    # content
    message["content"] = content_text if content_text else ""

    # tool_calls
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = response_json.get("stop_reason")
    finish_reason = _map_anthropic_stop_reason_to_openai(stop_reason)

    openai_response = {
        "id": f"chatcmpl-{request_id[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            **({"prompt_tokens_details": {"cached_tokens": cached_tokens}} if cached_tokens else {})
        }
    }

    return openai_response


# ============================================================
# Anthropic SSE → OpenAI SSE 流式转换
# ============================================================

# SSE 事件分隔符：兼容 LF 空行与 CRLF 空行
_SSE_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")


def _parse_anthropic_sse_events(chunk_str: str, pending: str) -> Tuple[List[Dict[str, Any]], str]:
    """从 SSE 数据块中解析出 Anthropic 事件。

    返回 (events列表, 剩余未完成的片段)
    每个 event 是 dict，带 _event_type 字段。
    """
    events: List[Dict[str, Any]] = []
    buffer = pending + chunk_str

    while True:
        m = _SSE_EVENT_SPLIT.search(buffer)
        if not m:
            break
        event_block, buffer = buffer[:m.start()], buffer[m.end():]
        event_type = None
        data_str = None
        for line in event_block.splitlines():
            line = line.strip()
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()

        if data_str:
            try:
                data = json.loads(data_str)
                if isinstance(data, dict):
                    if event_type:
                        data["_event_type"] = event_type
                    events.append(data)
            except json.JSONDecodeError:
                pass

    return events, buffer


def build_openai_stream_from_anthropic(
    api_iterator,
    model_name: str,
    request_id: str,
    monitoring_service,
    endpoint_config: dict,
    pricing_config: dict,
    direct_api_service,
    estimate_message_tokens_func,
    openai_req: dict,
    full_messages: list,
    estimate_tokens_func=None,
    thinking_separator: Optional[str] = None
):
    """将 Anthropic SSE 流转换为 OpenAI SSE 流。

    Anthropic 事件序列：
      message_start → content_block_start → content_block_delta(可多次)
      → content_block_stop → ... → message_delta(stop_reason, usage) → message_stop

    OpenAI chunk：
      {choices:[{delta:{content/reasoning_content/tool_calls}, finish_reason:null}]}
      最后 usage chunk + [DONE]
    """
    completion_id = f"chatcmpl-{request_id[:24]}"
    created = int(time.time())

    # 监控统计
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls_acc: List[Dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    upstream_usage: Dict[str, Any] = {}  # 上游原生 usage（message_start/message_delta 增量合并）
    # 只有收到上游 message_delta.stop_reason 才是真实值（1047 行处更新）；
    # 初始为 None：网络波动、上游截断、客户端断开等异常路径不会伪造成 end_turn
    stop_reason: Optional[str] = None
    token_stats_local = (endpoint_config or {}).get("token_stats_mode") == "local"

    async def stream_generator():
        nonlocal input_tokens, output_tokens, cached_tokens, stop_reason

        pending = ""
        success = True
        error_msg = None
        # 增量 UTF-8 解码器：防止中文多字节字符被 chunk 边界切断后变成 U+FFFD
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # tool_use 状态
        next_oai_idx = 0
        active_tool: Optional[Dict[str, Any]] = None  # {anthropic_idx, oai_idx, id, name}

        # thinking_separator 流式切分状态
        accumulated_for_split = ""
        split_done = False
        output_position = 0

        def _make_delta_chunk(delta: Dict[str, Any], finish_reason=None) -> bytes:
            choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [choice]
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        def _make_usage_chunk() -> bytes:
            total = input_tokens + output_tokens
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": total,
                    **({"prompt_tokens_details": {"cached_tokens": cached_tokens}} if cached_tokens else {})
                }
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        try:
            async for chunk_bytes in api_iterator:
                if isinstance(chunk_bytes, bytes):
                    chunk_str = utf8_decoder.decode(chunk_bytes, final=False)
                else:
                    chunk_str = str(chunk_bytes)

                events, pending = _parse_anthropic_sse_events(chunk_str, pending)

                for event in events:
                    etype = event.get("_event_type") or event.get("type", "")

                    if etype == "message_start":
                        msg = event.get("message", {})
                        raw_usage = msg.get("usage")
                        if isinstance(raw_usage, dict) and raw_usage:
                            upstream_usage.update(raw_usage)
                        inp, outp, cached = extract_anthropic_usage_tokens(raw_usage)
                        if inp:
                            input_tokens = inp
                        if cached:
                            cached_tokens = cached

                    elif etype == "content_block_start":
                        cb = event.get("content_block", {})
                        idx = event.get("index", 0)
                        cb_type = cb.get("type", "")

                        if cb_type == "tool_use":
                            oai_idx = next_oai_idx
                            next_oai_idx += 1
                            tool_id = cb.get("id", f"call_{uuid.uuid4().hex[:24]}")
                            tool_name = cb.get("name", "")
                            active_tool = {
                                "anthropic_idx": idx,
                                "oai_idx": oai_idx,
                                "id": tool_id,
                                "name": tool_name,
                                "arguments_parts": [],
                                # 把 arguments 增量中的 \uXXXX 转义提前解码为明文
                                "args_unescaper": StreamingUnicodeUnescaper()
                            }
                            # 发送 tool_call 起始 delta
                            tc_delta = {
                                "tool_calls": [{
                                    "index": oai_idx,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {"name": tool_name, "arguments": ""}
                                }]
                            }
                            yield _make_delta_chunk(tc_delta)
                        elif cb_type == "text":
                            # 如果初始就有 text，直接发
                            initial_text = cb.get("text", "")
                            if initial_text:
                                content_parts.append(initial_text)
                                yield _make_delta_chunk({"content": initial_text})

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")

                        if dtype == "thinking_delta":
                            t = delta.get("thinking", "")
                            if t:
                                reasoning_parts.append(t)
                                yield _make_delta_chunk({"reasoning_content": t})

                        elif dtype == "text_delta":
                            t = delta.get("text", "")
                            if t:
                                if thinking_separator and not split_done:
                                    accumulated_for_split += t
                                    if thinking_separator in accumulated_for_split:
                                        split_done = True
                                        parts = accumulated_for_split.split(thinking_separator, 1)
                                        full_reasoning = parts[0]
                                        content_part = parts[1] if len(parts) > 1 else ""
                                        remaining_reasoning = full_reasoning[output_position:]
                                        if remaining_reasoning:
                                            reasoning_parts.append(remaining_reasoning)
                                            yield _make_delta_chunk({"reasoning_content": remaining_reasoning})
                                        if content_part:
                                            content_parts.append(content_part)
                                            yield _make_delta_chunk({"content": content_part})
                                        logger.info(f"[THINKING_SPLIT_STREAM] 检测到分隔符'{thinking_separator}'")
                                        logger.info(f"  - 思考总长: {len(full_reasoning)} 字符")
                                    else:
                                        sep_len = len(thinking_separator)
                                        safe_position = max(output_position, len(accumulated_for_split) - sep_len)
                                        safe_content = accumulated_for_split[output_position:safe_position]
                                        if safe_content:
                                            reasoning_parts.append(safe_content)
                                            yield _make_delta_chunk({"reasoning_content": safe_content})
                                            output_position = safe_position
                                else:
                                    content_parts.append(t)
                                    yield _make_delta_chunk({"content": t})

                        elif dtype == "input_json_delta":
                            pj = delta.get("partial_json", "")
                            if pj and active_tool:
                                # 提前把 \uXXXX 转义解码为明文再发给下游（跨 chunk 安全）
                                decoded = active_tool["args_unescaper"].feed(pj)
                                if decoded:
                                    active_tool["arguments_parts"].append(decoded)
                                    yield _make_delta_chunk({
                                        "tool_calls": [{
                                            "index": active_tool["oai_idx"],
                                            "function": {"arguments": decoded}
                                        }]
                                    })

                    elif etype == "content_block_stop":
                        idx = event.get("index", 0)
                        if active_tool and active_tool.get("anthropic_idx") == idx:
                            # 冲刷解码器残留（正常完整 JSON 无残留；截断流原样补发）
                            tail = active_tool["args_unescaper"].flush()
                            if tail:
                                active_tool["arguments_parts"].append(tail)
                                yield _make_delta_chunk({
                                    "tool_calls": [{
                                        "index": active_tool["oai_idx"],
                                        "function": {"arguments": tail}
                                    }]
                                })
                            # 记录完整的 tool_call（arguments 从流式增量中累积，供监控日志使用）
                            tool_calls_acc.append({
                                "id": active_tool["id"],
                                "type": "function",
                                "function": {
                                    "name": active_tool["name"],
                                    "arguments": "".join(active_tool.get("arguments_parts", []))
                                }
                            })
                            active_tool = None

                    elif etype == "message_delta":
                        delta = event.get("delta", {})
                        usage = event.get("usage", {})
                        sr = delta.get("stop_reason")
                        if sr:
                            stop_reason = sr
                        if isinstance(usage, dict):
                            if usage:
                                upstream_usage.update(usage)
                            inp, outp, cached = extract_anthropic_usage_tokens(usage)
                            if inp:
                                input_tokens = inp
                            if outp:
                                output_tokens = outp
                            if cached:
                                cached_tokens = cached

                    elif etype == "message_stop":
                        pass

                    elif etype == "error" or is_error_json(event):
                        # 两种形态：
                        # - Anthropic 原生错误事件（event: error + {"type":"error","error":{...}}）
                        # - 无 type 字段的错误对象（如 OpenRouter /messages 或
                        #   call_api_passthrough 包装的 {"error": {...}}），
                        #   此前会因不匹配任何事件分支被静默丢弃，导致监控记 success、
                        #   客户端收到一条空消息而看不到任何报错
                        success = False
                        event_payload = {k: v for k, v in event.items() if k != "_event_type"}
                        # normalize 会通过 _enrich_error_message 合并 metadata.raw 中的详细原因
                        normalized = normalize_to_openai_error(event_payload)
                        error_obj = normalized.get("error", normalized)
                        error_msg = json.dumps(error_obj, ensure_ascii=False)
                        logger.warning(f"[OAI_FROM_ANTHROPIC] 流式上游返回错误: {error_msg[:300]}")
                        error_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [],
                            "error": error_obj
                        }
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

            # 发送 finish chunk
            finish_reason = _map_anthropic_stop_reason_to_openai(stop_reason)
            yield _make_delta_chunk({}, finish_reason=finish_reason)

            # local 统计模式：忽略上游 usage，用本地 tokenizer 重算
            if token_stats_local:
                try:
                    input_tokens = await estimate_message_tokens_non_blocking(
                        estimate_message_tokens_func,
                        openai_req.get("messages", []), model_name)
                    local_output_text = "".join(reasoning_parts) + "".join(content_parts)
                    if local_output_text and estimate_tokens_func:
                        output_tokens = await estimate_text_tokens_non_blocking(
                            estimate_tokens_func, local_output_text, model_name)
                    logger.info(f"[TOKEN_STATS_LOCAL] 本地tokenizer统计: 输入={input_tokens}, 输出={output_tokens}")
                except Exception as te:
                    logger.warning(f"[TOKEN_STATS_LOCAL] 本地token估算失败，保留上游值: {te}")

            # 发送 usage chunk
            if input_tokens > 0 or output_tokens > 0:
                yield _make_usage_chunk()
            yield b"data: [DONE]\n\n"

        except asyncio.CancelledError:
            success = False
            error_msg = "客户端断开连接"
            raise
        except Exception as e:
            success = False
            error_msg = str(e)
            logger.error(f"[OAI_FROM_ANTHROPIC] 流式处理异常: {e}", exc_info=True)
            raise
        finally:
            resp_content = "".join(content_parts) if success else (error_msg or "")
            resp_reasoning = "".join(reasoning_parts) if success else None

            # 兜底：流式过程中未找到分隔符时，对最终累积内容应用分隔符
            if thinking_separator and resp_content and not resp_reasoning and direct_api_service:
                reasoning_part, main_part = direct_api_service.split_thinking_content(
                    resp_content, thinking_separator)
                if reasoning_part:
                    resp_reasoning = reasoning_part
                    resp_content = main_part
                    logger.info(f"[THINKING_SPLIT] 流式兜底：已应用思考分隔，reasoning={len(reasoning_part)}字符, content={len(main_part)}字符")

            # 如果上游没有返回 input_tokens，用 tokenizer 估算
            if input_tokens == 0:
                try:
                    input_tokens = await estimate_message_tokens_non_blocking(
                        estimate_message_tokens_func,
                        openai_req.get("messages", []),
                        model_name
                    )
                except Exception as te:
                    logger.warning(f"[OAI_FROM_ANTHROPIC] input token估算失败: {te}")

            # 🔧 修复：旧版无条件套用 "output_tokens < len(text)//6 就改成 len//4"
            # 的字符数启发式，有两个问题：
            #   1. token_stats_mode=local 上面刚用本地 tokenizer 精确算完，
            #      这里立刻被 len//4 粗估覆盖，local 模式形同虚设；
            #   2. len//4 对中文严重低估（中文约 1.5 字符/token），对代码等
            #      高压缩内容又会误触发，等于用更差的数据替换上游精确值。
            # 现与 PassthroughStreamSession.finalize 的策略对齐：只有上游确实
            # 没给出有效 output_tokens 时才回退到本地估算。
            total_output = (resp_reasoning or "") + (resp_content if success else "")
            if total_output and output_tokens <= 1:
                if estimate_tokens_func:
                    try:
                        output_tokens = await estimate_text_tokens_non_blocking(
                            estimate_tokens_func, total_output, model_name)
                    except Exception as te:
                        logger.warning(f"[OAI_FROM_ANTHROPIC] output token估算失败: {te}")
                        output_tokens = len(total_output) // 4
                else:
                    output_tokens = len(total_output) // 4

            cost_info = {}
            if pricing_config and direct_api_service:
                try:
                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        pricing=pricing_config
                    )
                except Exception:
                    pass
            # ── 把 stop_reason 写入日志，排查截断问题 ──
            if isinstance(cost_info, dict):
                cost_info["stop_reason"] = stop_reason

            monitoring_service.request_end(
                request_id=request_id,
                success=success,
                error=error_msg,
                response_content=resp_content or None,
                reasoning_content=resp_reasoning,
                response_tool_calls=tool_calls_acc if tool_calls_acc else None,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                cached_tokens=cached_tokens or 0,
                cost_info=cost_info,
                full_messages=full_messages,
                upstream_usage=upstream_usage or None
            )
            try:
                spawn(
                    monitoring_service.broadcast_to_monitors({
                        "type": "request_end",
                        "request_id": request_id,
                        "success": success
                    }),
                    name="anthropic-native-broadcast"
                )
            except Exception:
                pass

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        }
    )


# ============================================================
# 主处理函数
# ============================================================

async def handle_anthropic_native_from_openai(
    openai_req: dict,
    model_name: str,
    endpoint_config: dict,
    api_key: str,
    CONFIG: dict,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages: Optional[list] = None,
    thinking_separator: Optional[str] = None
):
    """处理 OpenAI 格式请求 → Anthropic 原生透传 → OpenAI 格式响应。

    在 /v1/chat/completions 接口中调用 anthropic_native 类型的模型时使用。

    🔧 api_key 由调用方（direct_api_handler）统一轮询后传入，本函数不再二次
    轮询——旧版 handler 与这里各调一次 get_round_robin_api_key，每次请求把
    计数器推进两次，偶数个 key 时有一半永远不会被选中。
    """
    api_base_url = endpoint_config.get("api_base_url")
    if not api_base_url:
        raise HTTPException(
            status_code=500,
            detail=f"模型 '{model_name}' 的 anthropic_native 配置缺少 api_base_url。")

    target_model_id = endpoint_config.get("model_id", model_name)
    display_name = endpoint_config.get("display_name", model_name)
    pricing_config = endpoint_config.get("pricing", {})

    # 确定端点路径
    endpoint_path = endpoint_config.get("endpoint_path") or "/messages"
    endpoint_path = endpoint_path.strip()
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    # 温度限制
    max_temperature = endpoint_config.get("max_temperature")
    if max_temperature is not None and "temperature" in openai_req:
        original_temp = openai_req["temperature"]
        if original_temp > max_temperature:
            openai_req["temperature"] = max_temperature
            logger.info(f"[TEMP_LIMIT] 模型 '{model_name}' 温度限制: {original_temp} -> {max_temperature}")

    # max_tokens 限制（新版 OpenAI SDK 可能只发 max_completion_tokens，两者都检查）
    max_tokens_limit = endpoint_config.get("max_tokens")
    if max_tokens_limit is not None:
        if "max_tokens" in openai_req:
            original_max = openai_req["max_tokens"]
            if original_max > max_tokens_limit:
                openai_req["max_tokens"] = max_tokens_limit
                logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' max_tokens: {original_max} -> {max_tokens_limit}")
        if "max_completion_tokens" in openai_req:
            original_max = openai_req["max_completion_tokens"]
            if original_max > max_tokens_limit:
                openai_req["max_completion_tokens"] = max_tokens_limit
                logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' max_completion_tokens: {original_max} -> {max_tokens_limit}")

    # ── OpenAI 请求 → Anthropic 请求 ──
    anthropic_body = convert_openai_to_anthropic_request(openai_req)
    anthropic_body["model"] = target_model_id

    # ── 应用 thinking 配置 ──
    anthropic_body = apply_thinking_config(anthropic_body, endpoint_config)

    # ── 应用系统提示词注入 ──
    anthropic_body = apply_system_injection(anthropic_body, endpoint_config)

    # ── 自定义参数合并（deepcopy：后续 auto_cache 会在请求体上原地打
    # cache_control 断点，直接引用配置对象会把断点永久写进内存配置，
    # 造成跨请求污染）──
    custom_params = endpoint_config.get("custom_params", {})
    if isinstance(custom_params, dict) and custom_params:
        anthropic_body.update(copy.deepcopy(custom_params))
        logger.info(f"[OAI_TO_ANTHROPIC] 已合并自定义参数: {list(custom_params.keys())}")

    # ── 附加主体参数合并（在自定义参数之后，同名键优先）──
    extra_body_params = endpoint_config.get("extra_body_params", {})
    if isinstance(extra_body_params, dict) and extra_body_params:
        anthropic_body.update(copy.deepcopy(extra_body_params))
        logger.info(f"[OAI_TO_ANTHROPIC] 已合并附加主体参数: {list(extra_body_params.keys())}")

    # ── 自动提示词缓存（在 custom_params 之后注入，避免断点被顶层字段覆盖丢失）──
    anthropic_body = apply_auto_cache_control(anthropic_body, endpoint_config)

    # ── 诊断：打印最终请求体中图片的 media_type ──
    for msg_idx, msg in enumerate(anthropic_body.get("messages", [])):
        content = msg.get("content")
        if isinstance(content, list):
            for blk_idx, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "image":
                    src = block.get("source", {})
                    logger.info(
                        f"[OAI_TO_ANTHROPIC] 消息[{msg_idx}] 图片块[{blk_idx}]: "
                        f"source.type={src.get('type')!r}, "
                        f"media_type={src.get('media_type')!r}, "
                        f"data_len={len(src.get('data', ''))}")

    is_stream = bool(openai_req.get("stream", False))

    request_id = str(uuid.uuid4())
    full_messages = full_messages or openai_req.get("messages", [])

    monitor_extra = {
        "upstream_model": target_model_id,
        "endpoint_path": endpoint_path,
        "mode": "oai_to_anthropic_passthrough"
    }
    # 自定义参数/附加主体参数以独立字段记录到监控日志，避免平铺覆盖监控字段
    custom_params_for_monitor = endpoint_config.get("custom_params")
    if isinstance(custom_params_for_monitor, dict) and custom_params_for_monitor:
        monitor_extra["custom_params"] = custom_params_for_monitor
    extra_body_params_for_monitor = endpoint_config.get("extra_body_params")
    if isinstance(extra_body_params_for_monitor, dict) and extra_body_params_for_monitor:
        monitor_extra["extra_body_params"] = extra_body_params_for_monitor
    # 记录思考/缓存相关配置到监控日志
    for key in ("enable_thinking", "reasoning_effort", "thinking_budget", "thinking_effort",
                "thinking_display", "force_stream", "auto_cache"):
        val = endpoint_config.get(key)
        if val is not None and val != "":
            monitor_extra[key] = val

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(full_messages),
        session_id=None,
        mode="oai_to_anthropic_passthrough",
        messages=full_messages,
        params=build_monitor_request_params(anthropic_body, extra=monitor_extra)
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })

    logger.info(
        f"[OAI_TO_ANTHROPIC] 直通模式: model={model_name} → target={target_model_id}, "
        f"endpoint={endpoint_path}, stream={is_stream}")

    # 调试：打印发送给上游的请求体（去掉长文本，只显示前500字符）
    debug_body = _truncate_body_for_log(anthropic_body)
    logger.info(f"[OAI_TO_ANTHROPIC] 发送请求体 keys={list(anthropic_body.keys())}")
    logger.info(f"[OAI_TO_ANTHROPIC] 请求体前500字符: {json.dumps(debug_body, ensure_ascii=False)[:500]}")

    # ── 首块超时配置 ──
    first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
    if CONFIG:
        first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

    if is_stream:
        # ── 流式：Anthropic SSE → OpenAI SSE ──
        api_iter = direct_api_service.call_api_passthrough(
            base_url=api_base_url,
            api_key=api_key,
            request_body=anthropic_body,
            endpoint_path=endpoint_path
        )

        # 预读第一个块，检测错误和超时（下限 1 秒，避免配置为 0/负数时 busy-loop）
        api_task = asyncio.create_task(anext(api_iter))
        heartbeat_interval = max(1, min(endpoint_config.get("client_disconnect_probe_interval", 30) or 30, 30))
        wait_start = time.time()

        while not api_task.done():
            if time.time() - wait_start > first_chunk_timeout:
                api_task.cancel()
                try:
                    await api_task
                except asyncio.CancelledError:
                    pass
                # 🔧 资源回收：504 超时路径此前同样丢弃 api_iter，上游连接挂到超时
                try:
                    await api_iter.aclose()
                except Exception:
                    pass
                error_msg = f"上游API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                logger.error(f"[OAI_TO_ANTHROPIC] {error_msg}")
                monitoring_service.request_end(
                    request_id=request_id, success=False, error=error_msg,
                    input_tokens=0, output_tokens=0,
                    full_messages=full_messages)
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end", "request_id": request_id, "success": False})
                raise HTTPException(status_code=504, detail=error_msg)
            try:
                await asyncio.wait_for(asyncio.shield(api_task), timeout=heartbeat_interval)
                break  # 任务正常完成，交给循环外 await api_task 取结果
            except asyncio.TimeoutError:
                continue
            except BaseException:
                # 🔧 死代码修复：StopAsyncIteration（上游空响应）会从 wait_for 直接
                # 抛出并绕过 while 逃逸——原本会逃出整个函数使请求永久挂起。
                # 跳出循环，统一交给下面的 await api_task 处理（此处会重新抛出）。
                break

        try:
            first_chunk = await api_task
        except StopAsyncIteration:
            error_msg = "上游API返回空响应"
            # 🔧 资源回收：空响应路径此前直接把 api_iter 丢弃，上游连接挂到超时
            try:
                await api_iter.aclose()
            except Exception:
                pass
            monitoring_service.request_end(
                request_id=request_id, success=False, error=error_msg,
                full_messages=full_messages)
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})
            raise HTTPException(status_code=502, detail=error_msg)

        # 检测首个块是否为上游错误：裸 JSON 错误体，或
        # call_api_passthrough 对非 2xx 响应输出的 "data: {错误}" SSE 包装块
        # （此前只解析裸 JSON，SSE 包装的错误会被当作正常流漏检）
        is_err, normalized = detect_first_chunk_error(first_chunk)
        if is_err:
            status_code = map_upstream_error_to_status_code(normalized, default_status_code=500)
            error_msg = str(normalized.get("error", {}))
            logger.error(f"[OAI_TO_ANTHROPIC] 流式上游返回错误: {error_msg[:300]}")
            monitoring_service.request_end(
                request_id=request_id, success=False, error=error_msg,
                full_messages=full_messages)
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})
            return JSONResponse(status_code=status_code, content=normalized)

        # 构建一个能先发送 first_chunk 再继续的迭代器
        async def _stream_with_first():
            yield first_chunk
            async for chunk in api_iter:
                yield chunk

        return build_openai_stream_from_anthropic(
            api_iterator=_stream_with_first(),
            model_name=display_name,
            request_id=request_id,
            monitoring_service=monitoring_service,
            endpoint_config=endpoint_config,
            pricing_config=pricing_config,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            openai_req=openai_req,
            full_messages=full_messages,
            thinking_separator=thinking_separator
        )

    else:
        # ── 非流式：Anthropic JSON → OpenAI JSON ──
        response_buf = bytearray()
        response_bytes = b""
        success = True
        error_msg = None
        resp_content = None
        resp_reasoning = None
        resp_tool_calls = None
        resp_input_tokens = None
        resp_output_tokens = None
        resp_cached_tokens = 0
        resp_upstream_usage = None
        resp_stop_reason = None

        try:
            async for chunk in direct_api_service.call_api_passthrough(
                base_url=api_base_url,
                api_key=api_key,
                request_body=anthropic_body,
                endpoint_path=endpoint_path
            ):
                response_buf.extend(chunk)

            response_text = bytes(response_buf).decode("utf-8")
            response_json = json.loads(response_text)

            if is_error_json(response_json):
                success = False
                error_msg = json.dumps(response_json, ensure_ascii=False)
                logger.warning(f"[OAI_TO_ANTHROPIC] 非流式上游返回错误: {error_msg[:300]}")
                normalized = normalize_to_openai_error(response_json)
                status_code = map_upstream_error_to_status_code(normalized, default_status_code=500)
                monitoring_service.request_end(
                    request_id=request_id, success=False, error=error_msg,
                    full_messages=full_messages)
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end", "request_id": request_id, "success": False})
                return JSONResponse(status_code=status_code, content=normalized)

            # 提取内容用于监控
            resp_content, resp_reasoning, resp_tool_calls, resp_input_tokens, resp_output_tokens, resp_cached_tokens = \
                extract_anthropic_response_content(response_json)
            raw_usage = response_json.get("usage")
            resp_upstream_usage = raw_usage if isinstance(raw_usage, dict) else None
            resp_stop_reason = response_json.get("stop_reason")

            # local 统计模式：忽略上游 usage，用本地 tokenizer 重算
            if endpoint_config.get("token_stats_mode") == "local":
                try:
                    resp_input_tokens = await estimate_message_tokens_non_blocking(
                        estimate_message_tokens_func,
                        openai_req.get("messages", []), display_name)
                    local_output_text = (resp_reasoning or "") + (resp_content or "")
                    if local_output_text:
                        resp_output_tokens = await estimate_text_tokens_non_blocking(
                            estimate_tokens_func, local_output_text, display_name)
                    logger.info(f"[TOKEN_STATS_LOCAL] 本地tokenizer统计: 输入={resp_input_tokens}, 输出={resp_output_tokens}")
                except Exception as te:
                    logger.warning(f"[TOKEN_STATS_LOCAL] 本地token估算失败，保留上游值: {te}")

            # 转换为 OpenAI 格式
            openai_response = convert_anthropic_response_to_openai(
                response_json, display_name, request_id)

            # 应用思考内容分隔符（仅在无原生 reasoning 且用户配置了分隔符时生效）
            if thinking_separator and resp_content and not resp_reasoning:
                reasoning_part, main_part = direct_api_service.split_thinking_content(
                    resp_content, thinking_separator)
                if reasoning_part:
                    resp_reasoning = reasoning_part
                    resp_content = main_part
                    msg = openai_response["choices"][0]["message"]
                    msg["reasoning_content"] = reasoning_part
                    msg["content"] = main_part
                    logger.info(f"[THINKING_SPLIT] 非流式响应已应用思考分隔，reasoning={len(reasoning_part)}字符, content={len(main_part)}字符")

            response_bytes = json.dumps(openai_response, ensure_ascii=False).encode("utf-8")

        except Exception as e:
            success = False
            error_msg = str(e)
            logger.error(f"[OAI_TO_ANTHROPIC] 非流式处理失败: {e}", exc_info=True)
            monitoring_service.request_end(
                request_id=request_id, success=False, error=error_msg,
                full_messages=full_messages)
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})
            raise HTTPException(status_code=500, detail=f"Anthropic 原生透传失败: {str(e)}")

        finally:
            if success:
                cost_info = {}
                if pricing_config and direct_api_service:
                    try:
                        cost_info = direct_api_service.calculate_cost(
                            input_tokens=resp_input_tokens or 0,
                            output_tokens=resp_output_tokens or 0,
                            cached_tokens=resp_cached_tokens or 0,
                            pricing=pricing_config)
                    except Exception:
                        pass
                if isinstance(cost_info, dict) and resp_stop_reason:
                    cost_info["stop_reason"] = resp_stop_reason

                monitoring_service.request_end(
                    request_id=request_id, success=True,
                    input_tokens=resp_input_tokens or 0,
                    output_tokens=resp_output_tokens or 0,
                    cached_tokens=resp_cached_tokens or 0,
                    response_content=resp_content,
                    reasoning_content=resp_reasoning,
                    response_tool_calls=resp_tool_calls,
                    cost_info=cost_info,
                    full_messages=full_messages,
                    upstream_usage=resp_upstream_usage)
                try:
                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id, "success": True})
                except Exception:
                    pass

        return Response(content=response_bytes, media_type="application/json")
