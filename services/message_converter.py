"""
消息转换服务
负责OpenAI格式与LMArena格式之间的消息转换
"""

import json
import logging
import mimetypes
import re
import time
import uuid
from typing import Optional

# 导入配置
from core.config_loader import CONFIG

logger = logging.getLogger(__name__)


async def _process_openai_message(message: dict) -> dict:
    """
    处理OpenAI消息，分离文本和附件。
    - 将多模态内容列表分解为纯文本和附件列表。
    - 文件床逻辑已移至 chat_completions 预处理，此处仅处理常规附件构建。
    - 确保 user 角色的空内容被替换为空格，以避免 LMArena 出错。
    - 特殊处理assistant角色的图片：检测Markdown图片并转换为experimental_attachments
    """
    content = message.get("content")
    role = message.get("role")
    attachments = []
    experimental_attachments = []
    text_content = ""

    # 添加诊断日志
    logger.debug(f"[MSG_PROCESS] 处理消息 - 角色: {role}, 内容类型: {type(content).__name__}")
    
    # 特殊处理assistant角色的字符串内容中的Markdown图片
    if role == "assistant" and isinstance(content, str):
        # 匹配 ![...](url) 格式的Markdown图片
        markdown_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        matches = re.findall(markdown_pattern, content)
        
        if matches:
            logger.info(f"[MSG_PROCESS] 在assistant消息中检测到 {len(matches)} 个Markdown图片")
            
            # 移除Markdown图片，只保留文本
            text_content = re.sub(markdown_pattern, '', content).strip()
            
            # 将图片转换为experimental_attachments格式
            for alt_text, url in matches:
                # 确定内容类型
                if url.startswith("data:"):
                    # base64格式
                    content_type = url.split(';')[0].split(':')[1] if ':' in url else 'image/png'
                elif url.startswith("http"):
                    # HTTP URL
                    content_type = mimetypes.guess_type(url)[0] or 'image/jpeg'
                else:
                    content_type = 'image/jpeg'
                
                # 生成文件名
                if '/' in url and not url.startswith("data:"):
                    # 从URL提取文件名
                    filename = url.split('/')[-1].split('?')[0]
                    if '.' not in filename:
                        filename = f"image_{uuid.uuid4()}.{content_type.split('/')[-1]}"
                else:
                    filename = f"image_{uuid.uuid4()}.{content_type.split('/')[-1]}"
                
                experimental_attachment = {
                    "name": filename,
                    "contentType": content_type,
                    "url": url
                }
                experimental_attachments.append(experimental_attachment)
                logger.debug(f"[MSG_PROCESS] 添加experimental_attachment: {filename}")
        else:
            text_content = content
    elif isinstance(content, list):
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                # 此处的 URL 可能是 base64 或 http URL (已被预处理器替换)
                image_url_data = part.get("image_url", {})
                url = image_url_data.get("url")
                original_filename = image_url_data.get("detail")

                try:
                    # 对于 base64，我们需要提取 content_type
                    if url.startswith("data:"):
                        content_type = url.split(';')[0].split(':')[1]
                    else:
                        # 对于 http URL，我们尝试猜测 content_type
                        content_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'

                    if original_filename:
                        file_name = original_filename
                    else:
                        ext = mimetypes.guess_extension(content_type)
                        if ext:
                            ext = ext.lstrip('.')
                        else:
                            # Fallback for unregistered MIME types like webp on Windows
                            ext = content_type.split('/')[-1] if '/' in content_type else 'png'
                        
                        # Final sanity check on extension
                        if not ext or len(ext) > 5 or ext == 'plain':
                            ext = 'png'
                            
                        file_name = f"image_{uuid.uuid4()}.{ext}"
                    
                    attachment = {
                        "name": file_name,
                        "contentType": content_type,
                        "url": url
                    }
                    
                    # Assistant角色使用experimental_attachments
                    if role == "assistant":
                        experimental_attachments.append(attachment)
                        logger.debug(f"[MSG_PROCESS] Assistant图片添加到experimental_attachments")
                    else:
                        attachments.append(attachment)
                        logger.debug(f"[MSG_PROCESS] {role}图片添加到attachments")

                except (AttributeError, IndexError, ValueError) as e:
                    logger.warning(f"处理附件URL时出错: {url[:100]}... 错误: {e}")

        text_content = "\n\n".join(text_parts)
    elif isinstance(content, str):
        text_content = content

    if role == "user" and not text_content.strip():
        text_content = " "

    # 构建返回结果
    result = {
        "role": role,
        "content": text_content,
        "attachments": attachments
    }
    
    # Assistant角色添加experimental_attachments
    if role == "assistant" and experimental_attachments:
        result["experimental_attachments"] = experimental_attachments
        logger.info(f"[MSG_PROCESS] Assistant消息包含 {len(experimental_attachments)} 个experimental_attachments")
    
    return result


async def convert_openai_to_lmarena_payload(
    openai_data: dict,
    session_id: str,
    mode_override: str = None,
    battle_target_override: str = None
) -> dict:
    """
    将 OpenAI 请求体转换为油猴脚本所需的简化载荷，并应用酒馆模式、绕过模式以及对战模式。
    新增了模式覆盖参数，以支持模型特定的会话模式。
    """
    # 导入依赖（避免循环导入）
    from core.config_loader import MODEL_NAME_TO_ID_MAP, MODEL_ENDPOINT_MAP
    
    # 0. 预处理：从历史消息中剥离思维链（如果配置启用）
    messages = openai_data.get("messages", [])
    if CONFIG.get("strip_reasoning_from_history", True) and CONFIG.get("enable_lmarena_reasoning", False):
        reasoning_mode = CONFIG.get("reasoning_output_mode", "openai")
        
        # 仅对think_tag模式有效（OpenAI模式的reasoning_content不在content中）
        if reasoning_mode == "think_tag":
            think_pattern = re.compile(r'<think>.*?</think>\s*', re.DOTALL)
            
            for msg in messages:
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    original_content = msg["content"]
                    # 移除<think>标签及其内容
                    cleaned_content = think_pattern.sub('', original_content).strip()
                    if cleaned_content != original_content:
                        msg["content"] = cleaned_content
                        logger.debug(f"[REASONING_STRIP] 从历史消息中剥离了思维链内容")
    
    # 1. 规范化角色并处理消息
    #    - 将非标准的 'developer' 角色转换为 'system' 以提高兼容性。
    #    - 分离文本和附件。
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
            logger.info("消息角色规范化：将 'developer' 转换为 'system'。")
    
    processed_messages = []
    for msg in messages:
        processed_msg = await _process_openai_message(msg.copy())
        processed_messages.append(processed_msg)

    # 1.5 应用消息角色转换模式
    conversion_mode = CONFIG.get("message_role_conversion_mode", "none")
    preserve_role_labels = CONFIG.get("merge_preserve_role_labels", False)
    
    if conversion_mode != "none":
        logger.info(f"应用消息角色转换模式: {conversion_mode}")
        if preserve_role_labels:
            logger.info(f"  - 合并时保留角色标签")
        
        if conversion_mode == "system_to_user":
            # 模式1: 将所有system角色转换为user角色
            for msg in processed_messages:
                if msg.get("role") == "system":
                    if preserve_role_labels:
                        # 保留原始角色标签（JSON格式）
                        content = msg.get("content", "")
                        content_escaped = content.replace('\\', '\\\\').replace('"', '\\"')
                        msg["content"] = f'"system": "{content_escaped}"'
                        logger.debug(f"已为system消息添加JSON格式角色标签")
                    msg["role"] = "user"
                    logger.debug(f"已将system消息转换为user: {msg.get('content', '')[:50]}...")
            logger.info(f"system_to_user模式：已将所有system角色转换为user角色")
            if preserve_role_labels:
                logger.info(f'  - 已为转换的消息添加 JSON 格式标签')
        
        elif conversion_mode == "system_merge":
            # 模式2: 合并第一条user/assistant前的所有system为一条，之后的system转为user
            # 找到第一条非system消息的位置
            first_non_system_idx = None
            for idx, msg in enumerate(processed_messages):
                if msg.get("role") in ["user", "assistant"]:
                    first_non_system_idx = idx
                    break
            
            if first_non_system_idx is not None:
                # 收集第一条非system消息之前的所有system消息
                system_messages_before = []
                other_messages = []
                
                for idx, msg in enumerate(processed_messages):
                    if idx < first_non_system_idx and msg.get("role") == "system":
                        system_messages_before.append(msg)
                    elif idx < first_non_system_idx:
                        # 不应该到这里，因为我们找到的是第一条非system消息
                        other_messages.append(msg)
                    else:
                        other_messages.append(msg)
                
                # 合并所有前置system消息为一条
                if system_messages_before:
                    if preserve_role_labels:
                        # 保留角色标签的合并方式：JSON格式
                        merged_parts = []
                        for msg in system_messages_before:
                            role = msg.get("role", "system")
                            content = msg.get("content", "")
                            content_escaped = content.replace('\\', '\\\\').replace('"', '\\"')
                            merged_parts.append(f'"{role}": "{content_escaped}"')
                        merged_content = ",".join(merged_parts)
                        logger.info(f"system_merge模式：合并时已添加JSON格式角色标签")
                    else:
                        # 普通合并方式
                        merged_content = "\n\n".join([msg.get("content", "") for msg in system_messages_before])
                    
                    merged_system = {
                        "role": "system",
                        "content": merged_content,
                        "attachments": [],
                        "_already_labeled": preserve_role_labels  # 标记已处理过角色标签
                    }
                    # 构建新的消息列表：合并后的system + 其他消息
                    processed_messages = [merged_system] + other_messages
                    logger.info(f"system_merge模式：已合并{len(system_messages_before)}条前置system消息为一条")
                else:
                    processed_messages = other_messages
                
                # 将剩余的system消息转换为user
                converted_count = 0
                for msg in processed_messages[1:]:  # 跳过第一条（合并后的system）
                    if msg.get("role") == "system":
                        # 检查是否已经处理过标签（避免重复添加）
                        if preserve_role_labels and not msg.get("_already_labeled"):
                            # 在转换前添加JSON格式角色标签
                            content = msg.get("content", "")
                            content_escaped = content.replace('\\', '\\\\').replace('"', '\\"')
                            msg["content"] = f'"system": "{content_escaped}"'
                            logger.debug(f"已为后续system消息添加JSON格式角色标签")
                        # 移除内部标记字段
                        if "_already_labeled" in msg:
                            del msg["_already_labeled"]
                        msg["role"] = "user"
                        converted_count += 1
                        logger.debug(f"已将后续system消息转换为user: {msg.get('content', '')[:50]}...")
                
                if converted_count > 0:
                    logger.info(f"system_merge模式：已将{converted_count}条后续system消息转换为user")
                    if preserve_role_labels:
                        logger.info(f'  - 已为转换的消息添加 JSON 格式标签')
            else:
                # 没有找到非system消息，所有消息都是system
                # 合并所有system为一条
                if processed_messages:
                    merged_content = "\n\n".join([msg.get("content", "") for msg in processed_messages if msg.get("role") == "system"])
                    if merged_content:
                        processed_messages = [{
                            "role": "system",
                            "content": merged_content,
                            "attachments": []
                        }]
                        logger.info(f"system_merge模式：所有消息均为system，已合并为一条")
        
        elif conversion_mode == "system_smart_merge":
            # 模式3: 找到第一条user，往回找两条system，合并第二条system及之前的所有system，然后将所有system转为user
            # 步骤1: 找到第一条user消息
            first_user_idx = None
            for idx, msg in enumerate(processed_messages):
                if msg.get("role") == "user":
                    first_user_idx = idx
                    break
            
            if first_user_idx is not None:
                logger.info(f"system_smart_merge模式：找到第一条user消息在位置 {first_user_idx}")
                
                # 步骤2: 从第一条user往回找第一条system（system1）
                system1_idx = None
                for idx in range(first_user_idx - 1, -1, -1):
                    if processed_messages[idx].get("role") == "system":
                        system1_idx = idx
                        break
                
                if system1_idx is not None:
                    logger.info(f"system_smart_merge模式：找到第一条system在位置 {system1_idx}")
                    
                    # 步骤3: 从system1往回找第二条system（system2）
                    system2_idx = None
                    for idx in range(system1_idx - 1, -1, -1):
                        if processed_messages[idx].get("role") == "system":
                            system2_idx = idx
                            break
                    
                    if system2_idx is not None:
                        logger.info(f"system_smart_merge模式：找到第二条system在位置 {system2_idx}")
                        
                        # 步骤4: 合并system2及之前的所有system
                        systems_to_merge = []
                        other_messages = []
                        
                        for idx, msg in enumerate(processed_messages):
                            if idx <= system2_idx and msg.get("role") == "system":
                                systems_to_merge.append(msg)
                            elif idx <= system2_idx:
                                # system2之前的非system消息也保留
                                other_messages.append((idx, msg))
                            else:
                                # system2之后的消息都保留
                                other_messages.append((idx, msg))
                        
                        if systems_to_merge:
                            # 合并这些system消息
                            if preserve_role_labels:
                                # 保留角色标签的合并方式：JSON格式
                                merged_parts = []
                                for msg in systems_to_merge:
                                    role = msg.get("role", "system")
                                    content = msg.get("content", "")
                                    content_escaped = content.replace('\\', '\\\\').replace('"', '\\"')
                                    merged_parts.append(f'"{role}": "{content_escaped}"')
                                merged_content = ",".join(merged_parts)
                                logger.info(f"system_smart_merge模式：合并时已添加JSON格式角色标签")
                            else:
                                # 普通合并方式
                                merged_content = "\n\n".join([msg.get("content", "") for msg in systems_to_merge])
                            
                            merged_system = {
                                "role": "system",
                                "content": merged_content,
                                "attachments": [],
                                "_already_labeled": preserve_role_labels  # 标记已处理过角色标签
                            }
                            
                            # 重建消息列表：将合并后的system插入到原system2的位置
                            new_messages = []
                            merged_inserted = False
                            for orig_idx, msg in other_messages:
                                if orig_idx == system2_idx and not merged_inserted:
                                    new_messages.append(merged_system)
                                    merged_inserted = True
                                if orig_idx > system2_idx or (orig_idx < system2_idx and msg.get("role") != "system"):
                                    new_messages.append(msg)
                            
                            # 如果没有插入（所有消息都是system），则在开头插入
                            if not merged_inserted:
                                new_messages.insert(0, merged_system)
                            
                            processed_messages = new_messages
                            logger.info(f"system_smart_merge模式：已合并{len(systems_to_merge)}条system消息")
                    else:
                        logger.info(f"system_smart_merge模式：未找到第二条system，跳过合并")
                else:
                    logger.info(f"system_smart_merge模式：未找到第一条system，跳过合并")
            else:
                logger.info(f"system_smart_merge模式：未找到user消息，跳过合并")
            
            # 步骤5: 将所有system转为user
            converted_count = 0
            for msg in processed_messages:
                if msg.get("role") == "system":
                    # 检查是否已经处理过标签（避免重复添加）
                    if preserve_role_labels and not msg.get("_already_labeled"):
                        # 在转换前添加JSON格式角色标签
                        content = msg.get("content", "")
                        content_escaped = content.replace('\\', '\\\\').replace('"', '\\"')
                        msg["content"] = f'"system": "{content_escaped}"'
                        logger.debug(f"system_smart_merge模式：已为system消息添加JSON格式角色标签")
                    # 移除内部标记字段
                    if "_already_labeled" in msg:
                        del msg["_already_labeled"]
                    msg["role"] = "user"
                    converted_count += 1
            
            if converted_count > 0:
                logger.info(f"system_smart_merge模式：已将{converted_count}条system消息转换为user")
                if preserve_role_labels:
                    logger.info(f'  - 已为转换的消息添加 JSON 格式标签')

    # 2. 应用酒馆模式 (Tavern Mode)
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in processed_messages if msg['role'] == 'system']
        other_messages = [msg for msg in processed_messages if msg['role'] != 'system']
        
        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []
        
        if merged_system_prompt:
            # 系统消息不应有附件
            final_messages.append({"role": "system", "content": merged_system_prompt, "attachments": []})
        
        final_messages.extend(other_messages)
        processed_messages = final_messages

    # 3. 确定目标模型 ID 和类型
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    
    # 优先从 MODEL_ENDPOINT_MAP 获取模型类型（如果定义了）
    model_type = "text"  # 默认类型
    endpoint_info = MODEL_ENDPOINT_MAP.get(model_name, {})
    
    # 诊断日志：记录模型类型判断过程
    logger.info(f"[BYPASS_DEBUG] 开始判断模型 '{model_name}' 的类型...")
    logger.info(f"[BYPASS_DEBUG] endpoint_info 类型: {type(endpoint_info).__name__}, 内容: {endpoint_info}")
    
    if isinstance(endpoint_info, dict) and "type" in endpoint_info:
        model_type = endpoint_info.get("type", "text")
        logger.info(f"[BYPASS_DEBUG] 从 model_endpoint_map.json (dict) 获取模型类型: {model_type}")
    elif isinstance(endpoint_info, list) and endpoint_info:
        # 如果是列表格式，取第一个元素的类型
        first_endpoint = endpoint_info[0] if isinstance(endpoint_info[0], dict) else {}
        if "type" in first_endpoint:
            model_type = first_endpoint.get("type", "text")
            logger.info(f"[BYPASS_DEBUG] 从 model_endpoint_map.json (list) 获取模型类型: {model_type}")
    
    # 回退到 models.json 中的定义（仅在model_endpoint_map.json未提供type时）
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # 关键修复：确保 model_info 总是一个字典
    if not endpoint_info.get("type") and model_info:
        old_type = model_type
        model_type = model_info.get("type", "text")
        logger.info(f"[BYPASS_DEBUG] 从 models.json 获取模型类型: {old_type} -> {model_type}")
    
    logger.info(f"[BYPASS_DEBUG] 最终确定的模型类型: {model_type}")
    
    # 尝试从models.json获取模型ID（仅作为备用，不是必需的）
    target_model_id = None
    if model_info:
        target_model_id = model_info.get("id")
        if target_model_id:
            logger.debug(f"从 models.json 获取到模型ID（备用）")

    # 4. 构建消息模板
    message_templates = []
    for msg in processed_messages:
        msg_template = {
            "role": msg["role"],
            "content": msg.get("content", ""),
            "attachments": msg.get("attachments", [])
        }
        
        # 对于user角色，附件需要放在experimental_attachments中
        if msg["role"] == "user" and msg.get("attachments"):
            msg_template["experimental_attachments"] = msg.get("attachments", [])
            logger.info(f"[LMARENA_CONVERT] 将user的 {len(msg['attachments'])} 个附件添加到experimental_attachments")
        
        # 保留assistant的experimental_attachments字段（图片生成模型需要）
        if msg["role"] == "assistant" and "experimental_attachments" in msg:
            msg_template["experimental_attachments"] = msg["experimental_attachments"]
            logger.info(f"[LMARENA_CONVERT] 保留assistant的 {len(msg['experimental_attachments'])} 个experimental_attachments")
        
        message_templates.append(msg_template)

    # 4.5 应用图片附件审查绕过 - 根据模型类型决定是否启用
    attachment_bypass_settings = CONFIG.get("attachment_bypass_settings", {})
    attachment_bypass_enabled = attachment_bypass_settings.get(model_type, False)
    
    logger.info(f"[ATTACHMENT_BYPASS] 模型类型 '{model_type}' 的附件绕过设置: {attachment_bypass_enabled}")
    
    if attachment_bypass_enabled:
        # 查找最后一条用户消息
        last_user_msg_idx = None
        for i in range(len(message_templates) - 1, -1, -1):
            if message_templates[i]["role"] == "user":
                last_user_msg_idx = i
                break
        
        if last_user_msg_idx is not None:
            last_user_msg = message_templates[last_user_msg_idx]
            
            # 检查是否包含图片附件
            has_image_attachment = False
            if last_user_msg.get("attachments"):
                for attachment in last_user_msg["attachments"]:
                    if attachment.get("contentType", "").startswith("image/"):
                        has_image_attachment = True
                        break
            
            # 如果包含图片附件且有文本内容，执行分离
            if has_image_attachment and last_user_msg.get("content", "").strip():
                original_content = last_user_msg["content"]
                original_attachments = last_user_msg["attachments"]
                
                # 创建两条消息：
                # 第一条：只包含图片附件（成为历史记录）
                image_only_msg = {
                    "role": "user",
                    "content": " ",  # 空内容或空格
                    "experimental_attachments": original_attachments,
                    "attachments": original_attachments
                }
                
                # 第二条：只包含文本内容（作为最新请求）
                text_only_msg = {
                    "role": "user",
                    "content": original_content,
                    "attachments": []
                }
                
                # 替换原消息为两条分离的消息
                message_templates[last_user_msg_idx] = image_only_msg
                message_templates.insert(last_user_msg_idx + 1, text_only_msg)
                
                logger.info(f"图片模型审查绕过已启用：将包含 {len(original_attachments)} 个附件的请求分离为两条消息")

    # 5. 应用绕过模式 (Bypass Mode) - 根据模型类型和配置决定是否启用
    bypass_settings = CONFIG.get("bypass_settings", {})
    global_bypass_enabled = CONFIG.get("bypass_enabled", False)
    
    # 诊断日志：详细记录绕过决策过程
    logger.info(f"[BYPASS_DEBUG] ===== 绕过决策开始 =====")
    logger.info(f"[BYPASS_DEBUG] 全局 bypass_enabled: {global_bypass_enabled}")
    logger.info(f"[BYPASS_DEBUG] bypass_settings: {bypass_settings}")
    logger.info(f"[BYPASS_DEBUG] 当前模型类型: {model_type}")
    
    # 根据模型类型确定是否启用绕过
    bypass_enabled_for_type = False
    
    # 修复：全局bypass_enabled为False时，无论bypass_settings如何设置都应该禁用
    if not global_bypass_enabled:
        bypass_enabled_for_type = False
        logger.info(f"[BYPASS_DEBUG] ⛔ 全局 bypass_enabled=False，强制禁用所有绕过功能")
    elif bypass_settings:
        # 如果有细粒度配置，检查是否明确定义了该类型
        if model_type in bypass_settings:
            # 如果明确定义了，使用定义的值（但仍受全局开关控制）
            bypass_enabled_for_type = bypass_settings.get(model_type, False)
            logger.info(f"[BYPASS_DEBUG] 使用 bypass_settings 中明确定义的值: bypass_settings['{model_type}'] = {bypass_enabled_for_type}")
        else:
            # 如果未明确定义，默认为False（更安全的默认值）
            bypass_enabled_for_type = False
            logger.info(f"[BYPASS_DEBUG] model_type '{model_type}' 未在 bypass_settings 中定义，默认禁用")
    else:
        # 如果没有细粒度配置，使用全局设置（保持向后兼容）
        # 但对于 image 和 search 类型，默认为 False（保持原有行为）
        if model_type in ["image", "search"]:
            bypass_enabled_for_type = False
            logger.info(f"[BYPASS_DEBUG] 无 bypass_settings，模型类型 '{model_type}' 属于 ['image', 'search']，强制设为 False")
        else:
            bypass_enabled_for_type = global_bypass_enabled
            logger.info(f"[BYPASS_DEBUG] 无 bypass_settings，使用全局 bypass_enabled: {bypass_enabled_for_type}")
    
    logger.info(f"[BYPASS_DEBUG] 最终决策：bypass_enabled_for_type = {bypass_enabled_for_type}")
    
    if bypass_enabled_for_type:
        # 从配置中读取绕过注入内容
        bypass_injection = CONFIG.get("bypass_injection", {})
        
        # 支持预设模式
        bypass_presets = bypass_injection.get("presets", {})
        active_preset_name = bypass_injection.get("active_preset", "default")
        
        # 尝试获取激活的预设
        injection_config = bypass_presets.get(active_preset_name)
        
        # 如果预设不存在，回退到自定义配置或默认值
        if not injection_config:
            logger.warning(f"[BYPASS_DEBUG] 预设 '{active_preset_name}' 不存在，使用自定义配置")
            injection_config = bypass_injection.get("custom", {
                "role": "user",
                "content": " ",
                "participantPosition": "a"
            })
        
        logger.info(f"[BYPASS_DEBUG] ⚠️ 模型类型 '{model_type}' 的绕过模式已启用")
        logger.info(f"[BYPASS_DEBUG]   - 使用预设: {active_preset_name}")
        
        # 🔧 核心改进：支持多轮消息注入
        # 检查injection_config是列表还是单个对象
        if isinstance(injection_config, list):
            # 多消息格式：注入多条消息
            logger.info(f"[BYPASS_DEBUG]   - 注入模式: 多轮消息 (共{len(injection_config)}条)")
            
            for idx, msg_config in enumerate(injection_config):
                inject_role = msg_config.get("role", "user")
                inject_content = msg_config.get("content", " ")
                
                logger.info(f"[BYPASS_DEBUG]   - 消息#{idx+1}: 角色={inject_role}")
                logger.info(f"[BYPASS_DEBUG]     内容: {inject_content[:50]}{'...' if len(inject_content) > 50 else ''}")
                
                # 构建注入消息（不设置participantPosition，让后续逻辑自动设置）
                inject_msg = {
                    "role": inject_role,
                    "content": inject_content,
                    "attachments": []
                }
                
                # 如果配置中明确指定了participantPosition，则使用指定的值
                if "participantPosition" in msg_config:
                    inject_msg["participantPosition"] = msg_config["participantPosition"]
                    logger.info(f"[BYPASS_DEBUG]     手动指定位置: {msg_config['participantPosition']}")
                
                message_templates.append(inject_msg)
        else:
            # 单消息格式（向后兼容）：注入单条消息
            inject_role = injection_config.get("role", "user")
            inject_content = injection_config.get("content", " ")
            
            logger.info(f"[BYPASS_DEBUG]   - 注入模式: 单条消息")
            logger.info(f"[BYPASS_DEBUG]   - 注入角色: {inject_role}")
            logger.info(f"[BYPASS_DEBUG]   - 注入内容: {inject_content[:50]}{'...' if len(inject_content) > 50 else ''}")
            
            # 构建注入消息（不设置participantPosition，让后续逻辑自动设置）
            inject_msg = {
                "role": inject_role,
                "content": inject_content,
                "attachments": []
            }
            
            # 如果配置中明确指定了participantPosition，则使用指定的值
            if "participantPosition" in injection_config:
                inject_msg["participantPosition"] = injection_config["participantPosition"]
                logger.info(f"[BYPASS_DEBUG]   - 手动指定位置: {injection_config['participantPosition']}")
            
            message_templates.append(inject_msg)
    else:
        if global_bypass_enabled or any(bypass_settings.values()) if bypass_settings else False:
            # 如果有任何绕过设置启用，但当前类型未启用，记录日志
            logger.info(f"[BYPASS_DEBUG] ✅ 模型类型 '{model_type}' 的绕过模式已禁用。")
    
    logger.info(f"[BYPASS_DEBUG] ===== 绕过决策结束 =====")

    # 6. 应用参与者位置 (Participant Position)
    # 优先使用覆盖的模式，否则回退到全局配置
    mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    target_participant = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
    target_participant = target_participant.lower()  # 确保是小写

    logger.info(f"正在根据模式 '{mode}' (目标: {target_participant if mode == 'battle' else 'N/A'}) 设置 Participant Positions...")
    logger.info(f"[POSITION_DEBUG] 待处理消息数量: {len(message_templates)}")
    
    position_start_time = time.time()

    for idx, msg in enumerate(message_templates):
        if idx % 10 == 0:  # 每10条消息输出一次进度
            elapsed = time.time() - position_start_time
            logger.info(f"[POSITION_DEBUG] 处理进度: {idx}/{len(message_templates)} (耗时: {elapsed:.2f}秒)")
        
        # 超时检测
        if time.time() - position_start_time > 30:
            logger.error(f"[POSITION_DEBUG] ❌ 消息处理超时（30秒）！")
            logger.error(f"  - 当前进度: {idx}/{len(message_templates)}")
            logger.error(f"  - 当前消息: {str(msg)[:200]}...")
            raise TimeoutError("Participant Position设置超时")
        
        if msg['role'] == 'system':
            if mode == 'battle':
                # Battle 模式: system 与用户选择的助手在同一边 (A则a, B则b)
                msg['participantPosition'] = target_participant
            else:
                # DirectChat 模式: system 固定为 'b'
                msg['participantPosition'] = 'b'
        elif mode == 'battle':
            # Battle 模式下，非 system 消息使用用户选择的目标 participant
            msg['participantPosition'] = target_participant
        else:  # DirectChat 模式
            # DirectChat 模式下，非 system 消息使用默认的 'a'
            msg['participantPosition'] = 'a'

    logger.info(f"[POSITION_DEBUG] ✅ Participant Positions设置完成，共处理 {len(message_templates)} 条消息")
    
    # 确定最终的 battle_target 值
    # 获取实际使用的模式
    final_mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    
    if final_mode == "direct_chat":
        # DirectChat 模式：总是使用 'a'
        final_battle_target = 'a'
        logger.debug("DirectChat 模式：battle_target 自动设置为 'a'")
    else:
        # Battle 模式：使用配置或覆盖值
        final_battle_target = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
        final_battle_target = final_battle_target.lower()
        logger.debug(f"Battle 模式：battle_target 设置为 '{final_battle_target}'")
    
    # 新的 LMArena API 只需要 session_id
    return {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        # message_id 已移除
        "battle_target": final_battle_target
    }