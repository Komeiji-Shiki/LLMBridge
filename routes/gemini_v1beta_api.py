"""
Gemini v1beta 原生API路由
处理 /v1beta/models/{model_name}:generateContent 和 streamGenerateContent 端点
"""
import asyncio
import json
import logging
import time
import uuid
from urllib.parse import unquote, quote
from datetime import datetime
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from core.config_loader import CONFIG, MODEL_ROUND_ROBIN_INDEX, MODEL_ROUND_ROBIN_LOCK
from core.model_archive import is_model_archived
from utils.monitor_params import build_monitor_request_params
from ._direct_api_utils import get_api_key

logger = logging.getLogger(__name__)


async def gemini_native_api(
    model_name: str,
    request: Request,
    MODEL_ENDPOINT_MAP: dict,
    monitoring_service,
    direct_api_service,
    last_activity_time_setter,
    aiohttp_session=None
):
    """
    处理Gemini原生API格式的请求
    支持 generateContent 和 streamGenerateContent
    
    Args:
        model_name: 模型名称
        request: FastAPI请求对象
        MODEL_ENDPOINT_MAP: 模型端点映射
        monitoring_service: 监控服务
        direct_api_service: 直连API服务
        last_activity_time_setter: 活动时间设置函数
        aiohttp_session: 共享的aiohttp会话
    """
    last_activity_time_setter(datetime.now())
    
    # 解码URL编码的模型名称
    model_name = unquote(model_name)
    
    logger.info(f"[GEMINI_V1BETA] 收到请求: 模型={model_name}")
    
    # 检查是否为流式请求
    is_stream = request.url.path.endswith(":streamGenerateContent")
    query_params = dict(request.query_params)
    if query_params.get("alt") == "sse":
        is_stream = True
    # 🔧 修复：流式一律向上游请求 alt=sse（见下方 target_url 构造），所以
    # 转发给客户端的必然是 SSE 字节。旧版却用 query_params["alt"]=="sse"
    # 决定响应 Content-Type 与旁路统计的解析分支：客户端调用
    # :streamGenerateContent 但没带 alt=sse 时，实际收到 SSE 却被标成
    # application/json，且统计走 JSON 分支解析全部失败 —— 这类请求的
    # 响应内容和 token 统计整个丢失。统一以 is_stream 为准。
    forward_as_sse = is_stream

    try:
        # 解析Gemini原生格式的请求体
        gemini_req = await request.json()
        
        # 查找模型配置
        endpoint_config = MODEL_ENDPOINT_MAP.get(model_name)
        
        if not endpoint_config:
            raise HTTPException(
                status_code=404,
                detail=f"模型 '{model_name}' 未在配置中找到"
            )
        
        # 归档模型拦截：与 /v1 链路一致按“模型不存在”处理。
        # 检查原始配置（而非轮询后的单端点），list 多端点任一端点都拦得住。
        if is_model_archived(endpoint_config):
            raise HTTPException(
                status_code=404,
                detail=f"模型 '{model_name}' 未在配置中找到"
            )

        # 处理多端点情况（🔧 轮询而非固定取第一个，与 /v1 链路行为一致；
        # 计数器与 /v1 共享同一 MODEL_ROUND_ROBIN_INDEX，轮询状态统一）
        if isinstance(endpoint_config, list) and endpoint_config:
            endpoints = endpoint_config
            async with MODEL_ROUND_ROBIN_LOCK:
                current_index = MODEL_ROUND_ROBIN_INDEX.get(model_name, 0) % len(endpoints)
                MODEL_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(endpoints)
            endpoint_config = endpoints[current_index]
        
        # 验证是否为gemini_native类型
        api_type = endpoint_config.get("api_type")
        if api_type != "gemini_native":
            raise HTTPException(
                status_code=400,
                detail=f"模型 '{model_name}' 不是Gemini原生API类型"
            )
        
        # 获取配置（🔧 支持 api_keys 列表轮询，与 /v1 链路对齐）
        api_base_url = endpoint_config.get("api_base_url")
        raw_api_key = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
        api_key_strategy = endpoint_config.get("api_key_strategy", "round_robin")
        api_key_cooldown = int(endpoint_config.get("api_key_cooldown_seconds", 0) or 0)
        if api_key_cooldown <= 0:
            api_key_cooldown = 172800
        api_key = await get_api_key(model_name, raw_api_key, strategy=api_key_strategy, cooldown_seconds=api_key_cooldown)
        target_model_id = endpoint_config.get("model_id", model_name)
        display_name = endpoint_config.get("display_name", model_name)
        pricing_config = endpoint_config.get("pricing", {})
        
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=f"模型 '{model_name}' 缺少API密钥配置"
            )
        
        logger.info(f"[GEMINI_V1BETA] 转发到目标模型: {target_model_id}")
        logger.info(f"[GEMINI_V1BETA] 流式模式: {is_stream}")
        
        # 生成请求ID
        request_id = str(uuid.uuid4())
        
        # 记录请求开始
        monitoring_service.request_start(
            request_id=request_id,
            model=display_name,
            messages_count=len(gemini_req.get("contents", [])),
            session_id=None,
            mode="gemini_v1beta",
            messages=[],
            params=build_monitor_request_params(
                gemini_req, extra={"streaming": is_stream, "upstream_model": target_model_id})
        )
        
        # 广播请求开始
        await monitoring_service.broadcast_to_monitors({
            "type": "request_start",
            "request_id": request_id,
            "model": display_name,
            "timestamp": time.time()
        })
        
        # 直接转发到目标API
        try:
            # 构建目标URL
            if api_base_url:
                base_url = api_base_url.rstrip('/')
            else:
                base_url = "https://generativelanguage.googleapis.com/v1beta"
            
            method = "streamGenerateContent" if is_stream else "generateContent"
            target_url = f"{base_url}/models/{target_model_id}:{method}"
            
            # 添加API key到查询参数（🔧 URL 编码，防特殊字符破坏 URL）
            key_param = quote(api_key or "", safe="")
            if "?" in target_url:
                target_url += f"&key={key_param}"
            else:
                target_url += f"?key={key_param}"
            
            # 🔧 修复：流式请求必须添加 alt=sse 参数，让 Gemini 返回标准 SSE 格式
            # 否则 Gemini 返回多行 JSON 数组格式，逐行/逐块读取会导致 JSON 解析失败
            if is_stream:
                target_url += "&alt=sse"
            
            logger.info(f"[GEMINI_V1BETA] 目标URL: {target_url.replace(key_param, '***') if key_param else target_url}")
            
            # 转发请求 - 使用传入的aiohttp_session或创建临时session
            import aiohttp
            
            # 选择要使用的session
            session = aiohttp_session
            temp_session = None
            if not session:
                logger.warning("[GEMINI_V1BETA] 未提供aiohttp_session，创建临时session（性能较差）")
                temp_session = aiohttp.ClientSession()
                session = temp_session
            
            try:
                if is_stream:
                    # 🔧 修复：流式模式不使用 async with 上下文管理器
                    # 因为 StreamingResponse 的生成器会在 async with 退出后才被消费
                    # 改为手动管理 response 的生命周期，在 stream_generator 的 finally 中释放
                    gemini_req_json = await asyncio.to_thread(
                        json.dumps,
                        gemini_req,
                        ensure_ascii=False,
                        separators=(',', ':')
                    )
                    resp = await session.post(
                        target_url,
                        data=gemini_req_json.encode('utf-8'),
                        headers={
                            "Content-Type": "application/json"
                        },
                        timeout=aiohttp.ClientTimeout(
                            total=CONFIG.get("api_call_timeout_seconds", 3000),
                            sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                            sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                        )
                    )
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        resp.release()
                        logger.error(f"[GEMINI_V1BETA] 上游API错误: {resp.status} - {error_text}")
                        
                        # 计算输入tokens（即使请求失败也计算，只累加 text 内容，避免 base64 图片污染）
                        partial_input_tokens = 0
                        try:
                            contents = gemini_req.get("contents", [])
                            text_chars = 0
                            for c in (contents if isinstance(contents, list) else []):
                                parts = c.get("parts", []) if isinstance(c, dict) else []
                                for p in (parts if isinstance(parts, list) else []):
                                    if isinstance(p, dict) and "text" in p:
                                        text_chars += len(str(p["text"]))
                            partial_input_tokens = text_chars // 4
                        except Exception as token_err:
                            logger.warning(f"[GEMINI_V1BETA] 输入token计算失败: {token_err}")
                        
                        monitoring_service.request_end(
                            request_id=request_id,
                            success=False,
                            error=error_text,
                            input_tokens=partial_input_tokens,
                            output_tokens=0
                        )
                        
                        if temp_session:
                            await temp_session.close()
                        
                        # 尝试解析上游错误 JSON，避免双层 JSON 包裹
                        try:
                            error_obj = json.loads(error_text)
                        except (json.JSONDecodeError, TypeError):
                            error_obj = {"message": error_text}
                        return JSONResponse(
                            status_code=resp.status,
                            content=error_obj if isinstance(error_obj, dict) else {"error": error_obj}
                        )
                    
                    # 流式响应 - 真正的流式转发
                    async def stream_generator():
                        # 🔧 list+join 累积，避免 str += 的 O(n²) 全量复制
                        accumulated_parts = []
                        input_tokens = 0
                        output_tokens = 0
                        request_success = False
                        error_msg = None
                        
                        try:
                            # 逐 HTTP chunk 转发，尽量保留上游的流式刷新粒度
                            async for chunk, _ in resp.content.iter_chunks():
                                if not chunk:
                                    continue
                                
                                # 立即转发给客户端
                                yield chunk
                                
                                # 异步解析统计信息，不阻塞转发
                                try:
                                    chunk_str = chunk.decode('utf-8', errors='ignore')
                                    
                                    # 处理SSE格式
                                    if forward_as_sse:
                                        for line in chunk_str.split('\n'):
                                            line = line.strip()
                                            if line.startswith('data: '):
                                                data_str = line[6:]
                                                if data_str and data_str != '[DONE]':
                                                    try:
                                                        chunk_data = json.loads(data_str)
                                                        if 'candidates' in chunk_data:
                                                            for candidate in chunk_data['candidates']:
                                                                if 'content' in candidate and 'parts' in candidate['content']:
                                                                    for part in candidate['content']['parts']:
                                                                        if 'text' in part:
                                                                            accumulated_parts.append(part['text'])
                                                        
                                                        if 'usageMetadata' in chunk_data:
                                                            usage_meta = chunk_data['usageMetadata']
                                                            input_tokens = usage_meta.get('promptTokenCount', input_tokens)
                                                            output_tokens = usage_meta.get('candidatesTokenCount', output_tokens) + usage_meta.get('thoughtsTokenCount', 0)
                                                    except json.JSONDecodeError:
                                                        pass
                                    else:
                                        # JSON流格式 (可能包含多个对象)
                                        try:
                                            chunk_data = json.loads(chunk_str.strip('[], \n'))
                                            if 'candidates' in chunk_data:
                                                for candidate in chunk_data['candidates']:
                                                    if 'content' in candidate and 'parts' in candidate['content']:
                                                        for part in candidate['content']['parts']:
                                                            if 'text' in part:
                                                                accumulated_parts.append(part['text'])
                                            
                                            if 'usageMetadata' in chunk_data:
                                                usage_meta = chunk_data['usageMetadata']
                                                input_tokens = usage_meta.get('promptTokenCount', input_tokens)
                                                output_tokens = usage_meta.get('candidatesTokenCount', output_tokens) + usage_meta.get('thoughtsTokenCount', 0)
                                        except Exception:
                                            pass
                                except Exception as e:
                                    logger.debug(f"[GEMINI_V1BETA] 解析块统计失败: {e}")
                            
                            request_success = True
                            
                        except Exception as e:
                            logger.error(f"[GEMINI_V1BETA] 流式处理错误: {e}")
                            error_msg = str(e)
                        finally:
                            accumulated_text = ''.join(accumulated_parts)
                            # 🔧 关键：在生成器结束时释放 response 和临时 session
                            try:
                                resp.release()
                            except Exception:
                                pass
                            if temp_session:
                                try:
                                    await temp_session.close()
                                except Exception:
                                    pass
                            
                            # 如果API没有返回usage，使用tokenizer计算
                            if output_tokens == 0 and accumulated_text:
                                try:
                                    from modules.token_counter import estimate_tokens
                                    output_tokens = await asyncio.to_thread(
                                        estimate_tokens, accumulated_text, model=display_name)
                                    logger.info(f"[GEMINI_V1BETA] 使用tokenizer计算输出: {output_tokens} tokens")
                                except Exception as token_err:
                                    logger.warning(f"[GEMINI_V1BETA] Token计算失败: {token_err}")
                            
                            # 注入最终的 token 统计块
                            if request_success and output_tokens > 0 and input_tokens >= 0:
                                # 构建 usage 块
                                usage_block = {
                                    "usageMetadata": {
                                        "promptTokenCount": input_tokens,
                                        "candidatesTokenCount": output_tokens,
                                        "totalTokenCount": input_tokens + output_tokens
                                    },
                                    "usage": {
                                        "prompt_tokens": input_tokens,
                                        "completion_tokens": output_tokens,
                                        "total_tokens": input_tokens + output_tokens
                                    }
                                }
                                
                                try:
                                    if forward_as_sse:
                                        yield f"data: {json.dumps(usage_block, ensure_ascii=False)}\n\n".encode('utf-8')
                                        
                                        oai_final_chunk = {
                                            "id": f"chatcmpl-{request_id}",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": display_name,
                                            "choices": [],
                                            "usage": usage_block["usage"]
                                        }
                                        yield f"data: {json.dumps(oai_final_chunk, ensure_ascii=False)}\n\n".encode('utf-8')
                                    else:
                                        yield f",{json.dumps(usage_block, ensure_ascii=False)}".encode('utf-8')
                                except Exception as e:
                                    logger.debug(f"[GEMINI_V1BETA] 注入 usage 统计失败: {e}")

                            # 记录请求完成（包含响应内容）
                            monitoring_service.request_end(
                                request_id=request_id,
                                success=request_success,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                error=error_msg,
                                response_content=accumulated_text
                            )
                            await monitoring_service.broadcast_to_monitors(
                                {"type": "request_end", "request_id": request_id, "success": request_success})
                            
                            logger.info(f"[GEMINI_V1BETA] 流式请求完成: {request_id[:8]}")
                            logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")

                    return StreamingResponse(
                        stream_generator(),
                        media_type="text/event-stream" if forward_as_sse else "application/json",
                        headers={
                            'Cache-Control': 'no-cache',
                            'Connection': 'keep-alive',
                            'X-Accel-Buffering': 'no'
                        }
                    )
                else:
                    # 非流式响应 - 使用 async with 是安全的（在上下文内就消费完了）
                    gemini_req_json = await asyncio.to_thread(
                        json.dumps,
                        gemini_req,
                        ensure_ascii=False,
                        separators=(',', ':')
                    )
                    async with session.post(
                        target_url,
                        data=gemini_req_json.encode('utf-8'),
                        headers={
                            "Content-Type": "application/json"
                        },
                        timeout=aiohttp.ClientTimeout(
                            total=CONFIG.get("api_call_timeout_seconds", 3000),
                            sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                            sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                        )
                    ) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            logger.error(f"[GEMINI_V1BETA] 上游API错误: {resp.status} - {error_text}")
                            
                            partial_input_tokens = 0
                            try:
                                contents = gemini_req.get("contents", [])
                                text_chars = 0
                                for c in (contents if isinstance(contents, list) else []):
                                    parts = c.get("parts", []) if isinstance(c, dict) else []
                                    for p in (parts if isinstance(parts, list) else []):
                                        if isinstance(p, dict) and "text" in p:
                                            text_chars += len(str(p["text"]))
                                partial_input_tokens = text_chars // 4
                            except Exception as token_err:
                                logger.warning(f"[GEMINI_V1BETA] 输入token计算失败: {token_err}")
                            
                            monitoring_service.request_end(
                                request_id=request_id,
                                success=False,
                                error=error_text,
                                input_tokens=partial_input_tokens,
                                output_tokens=0
                            )
                            await monitoring_service.broadcast_to_monitors(
                                {"type": "request_end", "request_id": request_id, "success": False})
                            
                            # 尝试解析上游错误 JSON，避免双层 JSON 包裹
                            try:
                                error_obj = json.loads(error_text)
                            except (json.JSONDecodeError, TypeError):
                                error_obj = {"message": error_text}
                            return JSONResponse(
                                status_code=resp.status,
                                content=error_obj if isinstance(error_obj, dict) else {"error": error_obj}
                            )
                        
                        # 非流式响应
                        response_data = await resp.json()
                        
                        # 提取token统计
                        usage_metadata = response_data.get('usageMetadata', {})
                        input_tokens = usage_metadata.get('promptTokenCount', 0)
                        output_tokens = usage_metadata.get('candidatesTokenCount', 0) + usage_metadata.get('thoughtsTokenCount', 0)
                        
                        # 提取响应文本
                        response_text = ""
                        if 'candidates' in response_data:
                            for candidate in response_data['candidates']:
                                if 'content' in candidate and 'parts' in candidate['content']:
                                    for part in candidate['content']['parts']:
                                        if 'text' in part:
                                            response_text += part['text']
                        
                        # 如果 API 没给 usage 且有内容，则计算
                        if output_tokens == 0 and response_text:
                            try:
                                from modules.token_counter import estimate_tokens
                                output_tokens = await asyncio.to_thread(
                                    estimate_tokens, response_text, model=display_name)
                            except Exception:
                                pass

                        # 注入 OpenAI 兼容的 usage 字段
                        if output_tokens > 0:
                            response_data["usage"] = {
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
                                "total_tokens": input_tokens + output_tokens
                            }
                            if not usage_metadata:
                                response_data["usageMetadata"] = {
                                    "promptTokenCount": input_tokens,
                                    "candidatesTokenCount": output_tokens,
                                    "totalTokenCount": input_tokens + output_tokens
                                }

                        # 记录请求完成（包含响应内容）
                        monitoring_service.request_end(
                            request_id=request_id,
                            success=True,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            response_content=response_text
                        )
                        await monitoring_service.broadcast_to_monitors(
                            {"type": "request_end", "request_id": request_id, "success": True})
                        
                        return JSONResponse(content=response_data)
            finally:
                # 🔧 非流式模式下清理临时 session（流式模式在 stream_generator 的 finally 中清理）
                if temp_session and not is_stream:
                    await temp_session.close()
                    
        except Exception as e:
            # 🔧 脱敏：api_key 拼在 URL 查询串中，aiohttp 异常的 str() 可能携带
            # 完整 URL，不脱敏会把 key 泄漏进日志与客户端错误响应
            safe_err = str(e)
            if api_key:
                safe_err = safe_err.replace(api_key, '***')
                quoted_key = quote(api_key, safe="")
                if quoted_key != api_key:
                    safe_err = safe_err.replace(quoted_key, '***')
            logger.error(f"[GEMINI_V1BETA] 请求处理失败: {safe_err}", exc_info=True)
            monitoring_service.request_end(request_id=request_id, success=False, error=safe_err)
            await monitoring_service.broadcast_to_monitors(
                {"type": "request_end", "request_id": request_id, "success": False})
            raise HTTPException(status_code=500, detail=safe_err)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[GEMINI_V1BETA] 请求解析失败: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))