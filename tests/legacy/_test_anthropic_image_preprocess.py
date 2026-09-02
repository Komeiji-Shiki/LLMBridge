"""
Anthropic 原生格式图片预处理测试
验证 services/image_service.py::preprocess_anthropic_images：
- image 块压缩 + media_type 同步更新（PNG→JPEG）
- tool_result 嵌套 image 块也被处理
- source.type=url 的块不受影响
- 配置未启用时不做任何修改
"""
import asyncio
import base64
import io
import logging

from PIL import Image

from services.image_service import preprocess_anthropic_images

logging.basicConfig(level=logging.WARNING)

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name} {detail}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def make_png_b64(width: int, height: int, color=(255, 0, 0)) -> str:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def image_block(b64data: str, media_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": b64data}
    }


CONFIG_ENABLED = {
    "image_optimization": {
        "enabled": True,
        "max_width": 1920,
        "max_height": 1080,
        "convert_png_to_jpg": True,
        "jpeg_quality": 85,
    },
    "processed_image_cache": {"enabled": False},
}


async def main():
    print("=== Anthropic 原生格式图片预处理测试 ===")

    # 1. 普通 image 块：大 PNG 应被缩放并转 JPEG，media_type 同步更新
    big_png = make_png_b64(2500, 1500)
    req = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        image_block(big_png),
    ]}]}
    count = await preprocess_anthropic_images(req, CONFIG_ENABLED)
    source = req["messages"][0]["content"][1]["source"]
    out_img = Image.open(io.BytesIO(base64.b64decode(source["data"])))
    check("image块被处理", count == 1, f"count={count}")
    check("media_type同步更新", source["media_type"] == "image/jpeg", f"media_type={source['media_type']}")
    check("尺寸已缩放", out_img.width <= 1920 and out_img.height <= 1080, f"size={out_img.size}")
    check("格式确实是JPEG", out_img.format == 'JPEG', f"format={out_img.format}")
    check("体积减小", len(source["data"]) < len(big_png), f"{len(big_png)} -> {len(source['data'])}")

    # 2. tool_result 嵌套 image 块也被处理
    nested_png = make_png_b64(2200, 1200, (0, 255, 0))
    req2 = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
            {"type": "text", "text": "截图结果"},
            image_block(nested_png),
        ]},
    ]}]}
    count2 = await preprocess_anthropic_images(req2, CONFIG_ENABLED)
    nested_source = req2["messages"][0]["content"][0]["content"][1]["source"]
    check("tool_result嵌套图片被处理", count2 == 1 and nested_source["media_type"] == "image/jpeg",
          f"count={count2}, media_type={nested_source['media_type']}")

    # 3. source.type=url 的块不受影响
    url_block = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    req3 = {"messages": [{"role": "user", "content": [dict(url_block)]}]}
    count3 = await preprocess_anthropic_images(req3, CONFIG_ENABLED)
    check("url块不处理", count3 == 0 and req3["messages"][0]["content"][0] == url_block)

    # 4. 配置未启用时不做任何修改
    small_png = make_png_b64(100, 100)
    req4 = {"messages": [{"role": "user", "content": [image_block(small_png)]}]}
    count4 = await preprocess_anthropic_images(req4, {"image_optimization": {"enabled": False}})
    check("未启用时原样保留", count4 == 0 and req4["messages"][0]["content"][0]["source"]["data"] == small_png)

    # 5. 模型级配置单独启用（全局关闭）
    req5 = {"messages": [{"role": "user", "content": [image_block(make_png_b64(2000, 1400))]}]}
    count5 = await preprocess_anthropic_images(
        req5,
        {"image_optimization": {"enabled": False}},
        model_image_config={"enabled": True, "convert_png_to_jpg": True, "quality": 80,
                            "max_width": 1024, "max_height": 1024},
    )
    source5 = req5["messages"][0]["content"][0]["source"]
    out5 = Image.open(io.BytesIO(base64.b64decode(source5["data"])))
    check("模型级配置生效", count5 == 1 and source5["media_type"] == "image/jpeg" and out5.width <= 1024,
          f"count={count5}, size={out5.size}")

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


raise SystemExit(asyncio.run(main()))
