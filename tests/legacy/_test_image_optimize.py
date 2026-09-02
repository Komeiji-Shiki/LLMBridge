"""
图片优化功能测试
验证 modules/image_processor.py 重构后的正确性：
- P模式调色板图片颜色保持（旧版调色板丢失bug）
- EXIF方向矫正
- 透明图转JPEG白底合成
- 目标大小压缩
- 动图跳过保护
- 尺寸缩放
- 负优化回退原图
- WEBP剥离EXIF
"""
import io
import logging

from PIL import Image

from modules.image_processor import optimize_image

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


def encode(img: Image.Image, fmt: str, **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def avg_color(data: bytes):
    img = Image.open(io.BytesIO(data)).convert('RGB')
    return img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))


print("=== 图片优化功能测试 ===")

# 1. P模式（调色板）图片压缩后颜色不错乱
#    旧版 strip_metadata 用 getdata/putdata 重建像素但丢失调色板，颜色会完全错乱
red_p = Image.new('RGB', (200, 200), (255, 0, 0)).convert('P', palette=Image.Palette.ADAPTIVE)
data = encode(red_p, 'PNG')
out, fmt, err = optimize_image(data, {'strip_metadata': True, 'target_format': 'jpg', 'jpeg_quality': 90})
r, g, b = avg_color(out) if out else (0, 0, 0)
check("P模式颜色保持", err is None and r > 200 and g < 60 and b < 60, f"avg=({r},{g},{b}) fmt={fmt}")

# 2. EXIF方向矫正：Orientation=6（顺时针旋转90度）应交换宽高
img = Image.new('RGB', (300, 100), (0, 128, 255))
exif = Image.Exif()
exif[0x0112] = 6
data = encode(img, 'JPEG', exif=exif, quality=90)
out, fmt, err = optimize_image(data, {'strip_metadata': True, 'jpeg_quality': 90})
size = Image.open(io.BytesIO(out)).size if out else (0, 0)
check("EXIF方向矫正", err is None and size == (100, 300), f"size={size}")

# 3. 全透明RGBA PNG 转 JPEG 应合成白底
rgba = Image.new('RGBA', (100, 100), (255, 0, 0, 0))
data = encode(rgba, 'PNG')
out, fmt, err = optimize_image(data, {'target_format': 'jpg', 'jpeg_quality': 90})
r, g, b = avg_color(out) if out else (0, 0, 0)
check("透明合成白底", err is None and min(r, g, b) > 245, f"avg=({r},{g},{b})")

# 4. 目标大小压缩：噪声图压到 50KB 以下
noise = Image.effect_noise((600, 400), 100).convert('RGB')
data = encode(noise, 'PNG')
out, fmt, err = optimize_image(data, {'target_format': 'jpg', 'target_size_kb': 50, 'jpeg_quality': 95})
check("目标大小压缩", err is None and out is not None and len(out) <= 50 * 1024,
      f"{len(data)/1024:.1f}KB -> {len(out)/1024:.1f}KB" if out else "无输出")

# 5. 动图GIF跳过优化（避免丢帧）
frames = [Image.new('RGB', (50, 50), c) for c in ((255, 0, 0), (0, 255, 0))]
buf = io.BytesIO()
frames[0].save(buf, format='GIF', save_all=True, append_images=frames[1:], duration=100)
data = buf.getvalue()
out, fmt, err = optimize_image(data, {'convert_to_webp': True, 'webp_quality': 80})
check("动图跳过优化", err is None and out == data, f"fmt={fmt}")

# 6. 尺寸缩放：3000x2000 限制到 1920x1080 内
big = Image.effect_noise((3000, 2000), 60).convert('RGB')
data = encode(big, 'JPEG', quality=90)
out, fmt, err = optimize_image(data, {'max_width': 1920, 'max_height': 1080, 'jpeg_quality': 85})
size = Image.open(io.BytesIO(out)).size if out else (0, 0)
check("尺寸缩放", err is None and size[0] <= 1920 and size[1] <= 1080, f"size={size}")

# 7. 负优化回退：低质量JPEG被更高质量重压会变大，应保留原图
small = Image.effect_noise((400, 300), 80).convert('RGB')
low_q = encode(small, 'JPEG', quality=30)
out, fmt, err = optimize_image(low_q, {'jpeg_quality': 95})
check("负优化回退原图", err is None and out == low_q,
      f"原图{len(low_q)/1024:.1f}KB, 输出{len(out)/1024:.1f}KB" if out else "无输出")

# 8. 大尺寸P模式图片缩放+转WEBP（旧版会退化为NEAREST且可能颜色错乱）
grad = Image.new('RGB', (2500, 1500))
grad.paste(Image.linear_gradient('L').resize((2500, 1500)).convert('RGB'))
p_big = grad.convert('P', palette=Image.Palette.ADAPTIVE)
data = encode(p_big, 'PNG')
out, fmt, err = optimize_image(data, {'max_width': 1920, 'max_height': 1080, 'convert_to_webp': True, 'webp_quality': 85})
size = Image.open(io.BytesIO(out)).size if out else (0, 0)
check("P模式缩放转WEBP", err is None and fmt == 'WEBP' and size[0] <= 1920, f"size={size}")

# 9. WEBP输出剥离EXIF（旧版会从原图info继承EXIF）
img = Image.new('RGB', (200, 200), (0, 255, 0))
exif = Image.Exif()
exif[0x010F] = "TestCamera"  # Make 标签
data = encode(img, 'JPEG', exif=exif, quality=90)
out, fmt, err = optimize_image(data, {'strip_metadata': True, 'convert_to_webp': True, 'webp_quality': 80})
out_exif = dict(Image.open(io.BytesIO(out)).getexif()) if out else {'error': True}
check("WEBP剥离EXIF", err is None and not out_exif, f"exif={out_exif}")

# 10. CMYK图片转WEBP不报错（旧版可能因模式不支持而降级）
cmyk = Image.new('CMYK', (100, 100), (0, 255, 255, 0))  # 红色
data = encode(cmyk, 'JPEG', quality=90)
out, fmt, err = optimize_image(data, {'convert_to_webp': True, 'webp_quality': 85})
check("CMYK转WEBP", err is None and fmt == 'WEBP', f"err={err}")

# 11. 16位灰度图（I;16）转JPEG不报错
i16 = Image.new('I;16', (100, 100), 30000)
data = encode(i16, 'PNG')
out, fmt, err = optimize_image(data, {'target_format': 'jpg', 'jpeg_quality': 85})
check("16位灰度转JPEG", err is None and fmt == 'JPEG', f"err={err}")

print(f"\n结果: {passed} 通过, {failed} 失败")
raise SystemExit(1 if failed else 0)
