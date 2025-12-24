"""
图片处理模块 - 独立的图片优化和转换功能
支持根据配置进行图片优化、格式转换和base64编码
"""
import base64
import io
import logging
from typing import Tuple, Optional
from PIL import Image

logger = logging.getLogger(__name__)


def optimize_image(
    image_data: bytes,
    config: dict,
    original_format: Optional[str] = None
) -> Tuple[bytes, str, Optional[str]]:
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
            - progressive_encoding: 是否渐进式编码
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
        
        # 步骤1: 清除元数据
        if config.get('strip_metadata', True):
            logger.debug(f"[IMG_OPT] 清除EXIF元数据")
            img_data = list(img.getdata())
            img_clean = Image.new(img.mode, img.size)
            img_clean.putdata(img_data)
            img = img_clean
        
        # 步骤2: 调整尺寸
        max_w = config.get('max_width', 1920)
        max_h = config.get('max_height', 1080)
        if img.width > max_w or img.height > max_h:
            old_size = (img.width, img.height)
            img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            logger.info(f"[IMG_OPT] 调整尺寸: {old_size[0]}x{old_size[1]} -> {img.width}x{img.height}")
        
        # 步骤3: 确定输出格式
        output_format = original_format.upper()
        
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
        
        # 步骤4: 处理透明度（JPEG不支持透明）
        if output_format in ('JPEG', 'JPG') and img.mode in ('RGBA', 'LA', 'P'):
            logger.debug(f"[IMG_OPT] 转换透明背景为白色（JPEG不支持透明）")
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        
        # 步骤5: 获取初始质量参数
        if output_format in ('JPEG', 'JPG'):
            quality = config.get('jpeg_quality', 85)
        elif output_format == 'WEBP':
            quality = config.get('webp_quality', 85)
        else:
            quality = 95  # PNG等无损格式
        
        # 检查是否有目标大小限制
        target_size_kb = config.get('target_size_kb')
        
        if target_size_kb and target_size_kb > 0 and output_format in ('JPEG', 'JPG', 'WEBP'):
            # 使用二分法调整质量以达到目标大小
            optimized_data, final_quality = _compress_to_target_size(
                img, output_format, target_size_kb, quality, config
            )
            if optimized_data:
                optimized_size = len(optimized_data)
                reduction = (1 - optimized_size / original_size) * 100 if original_size > 0 else 0
                logger.info(f"[IMG_OPT] 目标大小压缩完成: {original_size/1024:.2f}KB -> {optimized_size/1024:.2f}KB ({reduction:.1f}% 压缩, 质量={final_quality})")
                return optimized_data, output_format, None
            else:
                logger.warning(f"[IMG_OPT] 无法达到目标大小 {target_size_kb}KB，使用最低质量")
        
        # 常规压缩流程
        output = io.BytesIO()
        save_kwargs = {}
        
        # 设置质量参数
        if output_format in ('JPEG', 'JPG'):
            save_kwargs['quality'] = quality
            logger.debug(f"[IMG_OPT] JPEG质量: {quality}")
        elif output_format == 'WEBP':
            save_kwargs['quality'] = quality
            logger.debug(f"[IMG_OPT] WEBP质量: {quality}")
        
        # 编码优化
        if config.get('optimize_encoding', True):
            save_kwargs['optimize'] = True
        
        # 渐进式编码
        if output_format == 'WEBP' and config.get('progressive_encoding', False):
            save_kwargs['method'] = 6  # 最慢但压缩率最高
        
        # 保存
        img.save(output, format=output_format, **save_kwargs)
        optimized_data = output.getvalue()
        optimized_size = len(optimized_data)
        
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
) -> Tuple[Optional[bytes], int]:
    """
    使用二分法压缩图片到目标大小
    
    Args:
        img: PIL Image对象
        output_format: 输出格式
        target_size_kb: 目标大小（KB）
        initial_quality: 初始质量
        config: 配置字典
        min_quality: 最低质量限制
        max_iterations: 最大迭代次数
        
    Returns:
        (压缩后的数据, 最终质量) 或 (None, 0) 如果无法达到目标
    """
    target_size_bytes = target_size_kb * 1024
    
    low_quality = min_quality
    high_quality = initial_quality
    best_data = None
    best_quality = initial_quality
    
    logger.info(f"[IMG_OPT] 开始目标大小压缩: 目标={target_size_kb}KB, 初始质量={initial_quality}")
    
    for iteration in range(max_iterations):
        mid_quality = (low_quality + high_quality) // 2
        
        output = io.BytesIO()
        save_kwargs = {'quality': mid_quality}
        
        if config.get('optimize_encoding', True):
            save_kwargs['optimize'] = True
        
        img.save(output, format=output_format, **save_kwargs)
        current_data = output.getvalue()
        current_size = len(current_data)
        
        logger.debug(f"[IMG_OPT] 迭代 {iteration+1}: 质量={mid_quality}, 大小={current_size/1024:.2f}KB")
        
        if current_size <= target_size_bytes:
            # 当前大小符合目标，尝试更高质量
            best_data = current_data
            best_quality = mid_quality
            low_quality = mid_quality + 1
        else:
            # 当前大小超出目标，降低质量
            high_quality = mid_quality - 1
        
        # 如果范围收敛，退出
        if low_quality > high_quality:
            break
    
    # 如果没有找到合适的，使用最低质量尝试一次
    if best_data is None:
        output = io.BytesIO()
        save_kwargs = {'quality': min_quality, 'optimize': True}
        img.save(output, format=output_format, **save_kwargs)
        current_data = output.getvalue()
        
        if len(current_data) <= target_size_bytes:
            best_data = current_data
            best_quality = min_quality
            logger.info(f"[IMG_OPT] 使用最低质量 {min_quality} 达到目标大小")
        else:
            logger.warning(f"[IMG_OPT] 即使最低质量 {min_quality} 也无法达到目标大小 {target_size_kb}KB (当前: {len(current_data)/1024:.2f}KB)")
            # 返回最低质量的结果
            best_data = current_data
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