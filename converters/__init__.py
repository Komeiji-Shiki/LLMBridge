"""converters 包：协议转换模块"""


def is_content_parts(content):
    """区分图文内容块与工具返回的普通 JSON 数组。"""
    return isinstance(content, list) and bool(content) and all(
        isinstance(part, dict) and part.get("type") in (
            "text", "input_text", "output_text", "image", "image_url", "input_image",
        )
        for part in content
    )
