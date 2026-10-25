"""converters 包：协议转换模块"""
from converters.audio import extract_audio


def is_content_parts(content):
    """区分多模态内容块与工具返回的普通 JSON 数组。"""
    return isinstance(content, list) and bool(content) and all(
        isinstance(part, dict) and (part.get("type") in (
            "text", "input_text", "output_text", "image", "image_url", "input_image",
            "audio", "input_audio", "audio_url",
        ) or (part.get("type") == "document" and extract_audio(part) is not None))
        for part in content
    )
