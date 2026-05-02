"""
模型列表API路由
提供 /v1/models 和 /v1beta/models 端点
"""
import logging
import time
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _is_archived(config) -> bool:
    """检查模型配置是否标记为已归档"""
    if isinstance(config, dict):
        return config.get("archived", False)
    if isinstance(config, list) and config:
        # 列表配置取第一个元素判断
        first = config[0] if isinstance(config[0], dict) else {}
        return first.get("archived", False)
    return False


async def get_models(MODEL_ENDPOINT_MAP: dict, MODEL_NAME_TO_ID_MAP: dict, allowed_models: list = None):
    """
    提供兼容 OpenAI 的模型列表 - 返回 model_endpoint_map.json 中配置的模型。
    已归档（archived: true）的模型不会出现在列表中，但统计数据仍保留。
    
    Args:
        MODEL_ENDPOINT_MAP: 模型端点映射字典
        MODEL_NAME_TO_ID_MAP: 模型名称到ID的映射字典
        allowed_models: 允许的模型白名单（None 或空列表表示不过滤）
    
    Returns:
        OpenAI 格式的模型列表响应
    """
    def _is_allowed(model_name: str) -> bool:
        """检查模型是否在白名单中（空白名单 = 允许所有）"""
        if not allowed_models:
            return True
        return model_name in allowed_models

    # 优先返回 MODEL_ENDPOINT_MAP 中的模型（已配置会话的模型）
    if MODEL_ENDPOINT_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name, config in MODEL_ENDPOINT_MAP.items()
                if not _is_archived(config) and _is_allowed(model_name)
            ],
        }
    # 如果 MODEL_ENDPOINT_MAP 为空，则返回 models.json 中的模型作为备用
    elif MODEL_NAME_TO_ID_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name in MODEL_NAME_TO_ID_MAP.keys()
                if _is_allowed(model_name)
            ],
        }
    else:
        return JSONResponse(
            status_code=404,
            content={"error": "模型列表为空。请配置 'model_endpoint_map.json' 或 'models.json'。"}
        )


async def get_gemini_models(MODEL_ENDPOINT_MAP: dict):
    """
    提供Gemini v1beta格式的模型列表
    
    Args:
        MODEL_ENDPOINT_MAP: 模型端点映射字典
    
    Returns:
        Gemini v1beta 格式的模型列表响应
    """
    # 只返回配置了gemini_native类型的模型
    gemini_models = []
    
    if MODEL_ENDPOINT_MAP:
        for model_name, config in MODEL_ENDPOINT_MAP.items():
            # 处理单个配置和配置列表
            configs_to_check = [config] if isinstance(config, dict) else config if isinstance(config, list) else []
            
            for cfg in configs_to_check:
                if isinstance(cfg, dict) and cfg.get("api_type") == "gemini_native":
                    # 使用model_id字段作为模型名称
                    model_id = cfg.get("model_id", model_name)
                    display_name = cfg.get("display_name", model_id)
                    
                    gemini_models.append({
                        "name": f"models/{model_id}",
                        "displayName": display_name,
                        "description": f"Gemini model: {display_name}",
                        "supportedGenerationMethods": [
                            "generateContent",
                            "streamGenerateContent"
                        ]
                    })
                    break  # 只添加一次
    
    logger.info(f"[GEMINI_V1BETA] 返回 {len(gemini_models)} 个Gemini原生模型")
    
    return {
        "models": gemini_models
    }