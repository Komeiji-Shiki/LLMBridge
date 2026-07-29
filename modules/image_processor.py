"""
图片处理模块 - 独立的图片优化和转换功能
支持根据配置进行图片优化、格式转换和base64编码
"""
import base64
import io
import logging
from typing import Tuple, Optional
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# EXIF Orientation 标签编号
_EXIF_ORIENTATION_TAG = 0x0112


def _normalize_mode_for_format(img: Image.Image, output_format: str) -> Image.Image:
    """
    根据输出格式规范化图片模式，避免编码器报错或输出异常

    - JPEG: 不支持透明度，RGBA/LA 合成白色背景；I/F 等特殊模式转 RGB
    - WEBP: 仅支持 RGB/RGBA，其他模式按是否带透明通道转换
    """
    if output_format == 'JPEG':
        if img.mode in ('RGBA', 'LA'):
            logger.debug("[IMG_OPT] 转换透明背景为白色（JPEG不支持透明）")
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.getchannel('A'))
            img = background
        elif img.mode not in ('RGB', 'L', 'CMYK'):
            img = img.convert('RGB')
    elif output_format == 'WEBP':
        if img.mode not in ('RGB', 'RGBA'):
            has_alpha = 'A' in img.getbands() or 'transparency' in img.info
            img = img.convert('RGBA' if has_alpha else 'RGB')
    return img


def _build_save_kwargs(
    output_format: str,
    quality: int,
    config: dict,
    source_info: Optional[dict] = None
) -> dict:
    """
    构造 PIL save 参数。常规压缩与目标大小压缩共用，保证两条路径行为一致。

    元数据处理说明：
    - JPEG: Pillow 保存时默认不写 EXIF，仅在保留元数据时显式带回
    - WEBP: Pillow 保存时会从原图 info 自动继承 EXIF，剥离时必须显式清空
    """
    save_kwargs = {}
    strip_metadata = config.get('strip_metadata', True)

    if output_format == 'JPEG':
        save_kwargs['quality'] = quality
        if config.get('optimize_encoding', True):
            save_kwargs['optimize'] = True
        if config.get('progressive_encoding', False):
            save_kwargs['progressive'] = True
        if not strip_metadata and source_info and source_info.get('exif'):
            save_kwargs['exif'] = source_info['exif']
    elif output_format == 'WEBP':
        save_kwargs['quality'] = quality
        if config.get('progressive_encoding', False):
            save_kwargs['method'] = 6  # 最慢但压缩率最高
        if strip_metadata:
            save_kwargs['exif'] = b''
    else:
        # PNG 等无损格式
        if config.get('optimize_encoding', True):
            save_kwargs['optimize'] = True

    return save_kwargs


def optimize_image(
    image_data: bytes,
    config: dict,
    original_format: Optional[str] = None
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    优化图片：压缩、调整尺寸、转换格式等
    
    Args:
        image_data: 原始图片的二进制数据
        config: image_optimization 配置字典，支持以下字段：
            - enabled: 是否启用优化
            - strip_metadata: 是否清除EXIF元数据
            - max_width/max_height: 最大尺寸
            - convert_to_webp: 是否转为WEBP
            - convert_png_to_jpg: 是否将PNG转为JPG
            - target_format: 目标格式 (png/jpg/jpeg/webp)
            - jpeg_quality: JPEG质量 (1-100)
            - webp_quality: WEBP质量 (1-100)
            - target_size_kb: 目标文件大小（KB），会自动调整质量
            - optimize_encoding: 是否优化编码
            - progressive_encoding: 渐进式编码（JPEG progressive / WEBP method=6）
        original_format: 原始图片格式（如'PNG', 'JPEG'等）
        
    Returns:
        (优化后的图片数据, 输出格式, 错误信息)
    """
    try:
        # 打开图片
        img = Image.open(io.BytesIO(image_data))
        original_size = len(image_data)
        
        if original_format is None:
            original_format = img.format or 'PNG'
        
        logger.info(f"[IMG_OPT] 开始优化图片: {img.width}x{img.height}, 格式: {original_format}, 大小: {original_size/1024:.2f}KB")
        
        # 动图保护：重新编码只会保留第一帧，直接返回原图避免丢帧
        if getattr(img, 'is_animated', False) and getattr(img, 'n_frames', 1) > 1:
            logger.info(f"[IMG_OPT] 检测到动图（{getattr(img, 'n_frames', '?')}帧），跳过优化以避免丢帧")
            return image_data, original_format, None
        
        # 记录原图是否带EXIF、是否需要方向矫正（用于后续判断）
        original_has_exif = bool(img.info.get('exif'))
        try:
            orientation = img.getexif().get(_EXIF_ORIENTATION_TAG, 1)
        except Exception:
            orientation = 1
        needs_orientation_fix = orientation not in (None, 1)
        
        # 步骤1: EXIF方向矫正（物理旋转像素）
        # 必须在剥离元数据前执行，否则带Orientation标签的照片会因标签丢失而方向错误
        if needs_orientation_fix:
            img = ImageOps.exif_transpose(img)
            logger.debug(f"[IMG_OPT] EXIF方向矫正完成 (orientation={orientation})")
        
        # 步骤2: 调色板/二值模式提前转换
        # Pillow 对 P/1 模式缩放会强制退化为 NEAREST 重采样，必须先转换才能获得高质量缩放
        if img.mode == 'P':
            has_alpha = 'transparency' in img.info or 'A' in img.getbands()
            img = img.convert('RGBA' if has_alpha else 'RGB')
        elif img.mode == '1':
            img = img.convert('L')
        
        # 步骤3: 调整尺寸
        max_w = config.get('max_width', 1920)
        max_h = config.get('max_height', 1080)
        resized = False
        if img.width > max_w or img.height > max_h:
            old_size = (img.width, img.height)
            img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            resized = True
            logger.info(f"[IMG_OPT] 调整尺寸: {old_size[0]}x{old_size[1]} -> {img.width}x{img.height}")
        
        # 步骤4: 确定输出格式
        output_format = original_format.upper()
        if output_format == 'JPG':
            output_format = 'JPEG'
        
        # 检查是否要转换PNG到JPG
        if config.get('convert_png_to_jpg', False) and original_format.upper() == 'PNG':
            output_format = 'JPEG'
            logger.info(f"[IMG_OPT] PNG转JPG: {original_format} -> JPEG")
        
        # 检查是否有指定目标格式
        target_format = config.get('target_format', '').upper()
        if target_format in ('PNG', 'JPG', 'JPEG', 'WEBP'):
            if target_format == 'JPG':
                target_format = 'JPEG'
            output_format = target_format
            logger.info(f"[IMG_OPT] 使用指定格式: {output_format}")
        
        # 检查是否转换为WEBP（优先级最高）
        if config.get('convert_to_webp', False):
            output_format = 'WEBP'
            logger.debug(f"[IMG_OPT] 转换格式: {original_format} -> WEBP")
        
        # 步骤5: 按输出格式规范化图片模式（透明度合成、特殊模式转换）
        img = _normalize_mode_for_format(img, output_format)
        
        # 步骤6: 获取初始质量参数
        if output_format == 'JPEG':
            quality = config.get('jpeg_quality', 85)
        elif output_format == 'WEBP':
            quality = config.get('webp_quality', 85)
        else:
            quality = 95  # PNG等无损格式不使用该值
        
        # 步骤7: 目标大小压缩
        target_size_kb = config.get('target_size_kb')
        if target_size_kb and target_size_kb > 0 and output_format in ('JPEG', 'WEBP'):
            optimized_data, final_quality = _compress_to_target_size(
                img, output_format, target_size_kb, quality, config
            )
            optimized_size = len(optimized_data)
            if optimized_size > target_size_kb * 1024:
                logger.warning(f"[IMG_OPT] 即使最低质量也无法达到目标大小 {target_size_kb}KB (当前: {optimized_size/1024:.2f}KB)")
            reduction = (1 - optimized_size / original_size) * 100 if original_size > 0 else 0
            logger.info(f"[IMG_OPT] 目标大小压缩完成: {original_size/1024:.2f}KB -> {optimized_size/1024:.2f}KB ({reduction:.1f}% 压缩, 质量={final_quality})")
            return optimized_data, output_format, None
        
        # 步骤8: 常规压缩
        output = io.BytesIO()
        save_kwargs = _build_save_kwargs(output_format, quality, config, img.info)
        img.save(output, format=output_format, **save_kwargs)
        optimized_data = output.getvalue()
        optimized_size = len(optimized_data)
        
        # 负优化保护：没有发生任何有意义的变换（未缩放、未旋转、未转格式、
        # 无需剥离元数据）且重编码后体积不降反升时，保留原图
        normalized_original = original_format.upper()
        if normalized_original == 'JPG':
            normalized_original = 'JPEG'
        must_strip = config.get('strip_metadata', True) and original_has_exif
        if (optimized_size >= original_size
                and normalized_original == output_format
                and not resized
                and not needs_orientation_fix
                and not must_strip):
            logger.info(f"[IMG_OPT] 重编码后体积未减小（{original_size/1024:.2f}KB -> {optimized_size/1024:.2f}KB），保留原图")
            return image_data, original_format, None
        
        # 计算压缩率
        reduction = (1 - optimized_size / original_size) * 100 if original_size > 0 else 0
        logger.info(f"[IMG_OPT] 优化完成: {original_size/1024:.2f}KB -> {optimized_size/1024:.2f}KB ({reduction:.1f}% 压缩)")
        
        return optimized_data, output_format, None
        
    except Exception as e:
        error_msg = f"图片优化失败: {type(e).__name__}: {e}"
        logger.error(f"[IMG_OPT] {error_msg}", exc_info=True)
        return None, None, error_msg


def _compress_to_target_size(
    img: Image.Image,
    output_format: str,
    target_size_kb: int,
    initial_quality: int,
    config: dict,
    min_quality: int = 10,
    max_iterations: int = 10
) -> Tuple[bytes, int]:
    """
    使用二分法压缩图片到目标大小
    
    Args:
        img: PIL Image对象
        output_format: 输出格式
        target_size_kb: 目标大小（KB）
        initial_quality: 初始质量（同时作为质量上限）
        config: 配置字典
        min_quality: 最低质量限制
        max_iterations: 最大迭代次数
        
    Returns:
        (压缩后的数据, 最终质量)。若最低质量仍超出目标，返回最低质量的结果。
    """
    target_size_bytes = target_size_kb * 1024

    def encode(quality: int) -> bytes:
        buffer = io.BytesIO()
        save_kwargs = _build_save_kwargs(output_format, quality, config, img.info)
        img.save(buffer, format=output_format, **save_kwargs)
        return buffer.getvalue()

    logger.info(f"[IMG_OPT] 开始目标大小压缩: 目标={target_size_kb}KB, 初始质量={initial_quality}")

    # 先尝试初始质量（质量上限），若已满足目标则无需二分
    initial_data = encode(initial_quality)
    if len(initial_data) <= target_size_bytes:
        logger.debug(f"[IMG_OPT] 初始质量 {initial_quality} 已满足目标大小 ({len(initial_data)/1024:.2f}KB)")
        return initial_data, initial_quality

    low_quality = min_quality
    high_quality = initial_quality - 1
    best_data: Optional[bytes] = None
    best_quality = min_quality

    for iteration in range(max_iterations):
        if low_quality > high_quality:
            break
        mid_quality = (low_quality + high_quality) // 2
        current_data = encode(mid_quality)
        logger.debug(f"[IMG_OPT] 迭代 {iteration+1}: 质量={mid_quality}, 大小={len(current_data)/1024:.2f}KB")

        if len(current_data) <= target_size_bytes:
            # 当前大小符合目标，尝试更高质量
            best_data = current_data
            best_quality = mid_quality
            low_quality = mid_quality + 1
        else:
            # 当前大小超出目标，降低质量
            high_quality = mid_quality - 1

    if best_data is None:
        # 二分范围内无法满足目标，返回最低质量的结果（可能仍超出目标，由调用方记录警告）
        best_data = encode(min_quality)
        best_quality = min_quality

    return best_data, best_quality


def image_to_base64(image_data: bytes, mime_type: str = 'image/png') -> str:
    """
    将图片数据转换为base64 Data URI
    
    Args:
        image_data: 图片的二进制数据
        mime_type: MIME类型（如'image/png', 'image/jpeg'等）
        
    Returns:
        完整的base64 Data URI字符串
    """
    b64_encoded = base64.b64encode(image_data).decode('utf-8')
    data_uri = f"data:{mime_type};base64,{b64_encoded}"
    logger.debug(f"[IMG_BASE64] 转换为base64: {len(image_data)/1024:.2f}KB -> {len(data_uri)} 字符")
    return data_uri


def get_mime_type_from_format(image_format: str) -> str:
    """
    根据图片格式获取MIME类型
    
    Args:
        image_format: 图片格式（如'PNG', 'JPEG', 'WEBP'等）
        
    Returns:
        MIME类型字符串
    """
    format_map = {
        'PNG': 'image/png',
        'JPEG': 'image/jpeg',
        'JPG': 'image/jpeg',
        'WEBP': 'image/webp',
        'GIF': 'image/gif',
        'BMP': 'image/bmp',
        'TIFF': 'image/tiff'
    }
    return format_map.get(image_format.upper(), 'image/png')


def decode_base64_image(base64_data: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    解码base64图片数据
    
    Args:
        base64_data: base64字符串（可以是纯base64或Data URI格式）
        
    Returns:
        (图片二进制数据, 图片格式, 错误信息)
    """
    try:
        # 处理Data URI格式
        if base64_data.startswith('data:'):
            # 提取MIME类型和base64数据
            if ',' in base64_data:
                header, data = base64_data.split(',', 1)
                mime_type = header.split(';')[0].split(':')[1] if ':' in header else 'image/png'
            else:
                data = base64_data
                mime_type = 'image/png'
        else:
            data = base64_data
            mime_type = 'image/png'
        
        # 解码base64
        image_bytes = base64.b64decode(data)
        
        # 尝试打开图片以验证格式
        img = Image.open(io.BytesIO(image_bytes))
        image_format = img.format or 'PNG'
        
        logger.debug(f"[IMG_DECODE] 解码成功: 格式={image_format}, 大小={len(image_bytes)/1024:.2f}KB")
        
        return image_bytes, image_format, None
        
    except Exception as e:
        error_msg = f"解码base64图片失败: {type(e).__name__}: {e}"
        logger.error(f"[IMG_DECODE] {error_msg}")
        return None, None, error_msg


def merge_image_config(global_config: dict, model_config: dict) -> dict:
    """
    合并全局图片配置和模型级别配置
    模型级别配置优先级更高
    
    Args:
        global_config: 全局 image_optimization 配置
        model_config: 模型级别的 image_compression 配置
        
    Returns:
        合并后的配置字典
        
    模型配置示例 (在 model_endpoint_map.json 中):
    {
        "model_name": {
            "session_id": "...",
            "image_compression": {
                "enabled": true,
                "convert_png_to_jpg": true,
                "target_format": "jpg",
                "quality": 80,
                "target_size_kb": 500,
                "max_width": 1920,
                "max_height": 1080
            }
        }
    }
    """
    # 复制全局配置
    merged = global_config.copy() if global_config else {}
    
    if not model_config:
        return merged
    
    # 模型配置字段映射（模型配置使用更简洁的字段名）
    field_mapping = {
        'enabled': 'enabled',
        'target_format': 'target_format',  # png/jpg/webp
        'convert_png_to_jpg': 'convert_png_to_jpg',
        'convert_to_webp': 'convert_to_webp',
        'target_size_kb': 'target_size_kb',
        'quality': 'jpeg_quality',  # 简化字段名
        'jpeg_quality': 'jpeg_quality',
        'webp_quality': 'webp_quality',
        'max_width': 'max_width',
        'max_height': 'max_height',
        'strip_metadata': 'strip_metadata',
        'optimize_encoding': 'optimize_encoding',
    }
    
    for model_key, global_key in field_mapping.items():
        if model_key in model_config:
            value = model_config[model_key]
            merged[global_key] = value
            
            # 如果设置了通用quality，同时应用到jpeg和webp
            if model_key == 'quality':
                merged['jpeg_quality'] = value
                merged['webp_quality'] = value
    
    # 如果模型配置中启用了压缩，确保enabled为True
    if model_config.get('enabled', False):
        merged['enabled'] = True
    
    logger.debug(f"[IMG_CONFIG] 合并配置完成: {merged}")
    
    return merged
