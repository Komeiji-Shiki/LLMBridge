# file_bed_server/main.py
import base64
import os
import uuid
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import logging
from apscheduler.schedulers.background import BackgroundScheduler
import json
from PIL import Image
import io

# --- 基础配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 路径配置 ---
# 将上传目录定位到 main.py 文件的同级目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
API_KEY = "your_secret_api_key"  # 简单的认证密钥
CLEANUP_INTERVAL_MINUTES = 1 # 清理任务运行频率（分钟）
FILE_MAX_AGE_MINUTES = 10 # 文件最大保留时间（分钟）

# --- 图片优化配置 ---
def load_optimization_config():
    """加载优化配置"""
    try:
        # 假设 config.jsonc 在 file_bed_server 目录的上一级
        config_path = os.path.join(os.path.dirname(BASE_DIR), 'config.jsonc')
        with open(config_path, 'r', encoding='utf-8') as f:
            # 简单处理jsonc注释
            content = '\n'.join(line for line in f if not line.strip().startswith('//'))
            config = json.loads(content)
            return config.get('image_optimization', {})
    except Exception as e:
        logger.error(f"加载图片优化配置失败: {e}", exc_info=True)
        return {'enabled': False}

OPTIMIZATION_CONFIG = load_optimization_config()
if OPTIMIZATION_CONFIG.get('enabled'):
    logger.info("图片优化功能已启用。")

# --- 清理函数 ---
def cleanup_old_files():
    """遍历上传目录并删除超过指定时间的文件。"""
    now = time.time()
    cutoff = now - (FILE_MAX_AGE_MINUTES * 60)
    
    logger.info(f"正在运行清理任务，删除早于 {datetime.fromtimestamp(cutoff).strftime('%Y-%m-%d %H:%M:%S')} 的文件...")
    
    deleted_count = 0
    try:
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime < cutoff:
                        os.remove(file_path)
                        logger.info(f"已删除过期文件: {filename}")
                        deleted_count += 1
                except OSError as e:
                    logger.error(f"删除文件 '{file_path}' 时出错: {e}")
    except Exception as e:
        logger.error(f"清理旧文件时发生未知错误: {e}", exc_info=True)

    if deleted_count > 0:
        logger.info(f"清理任务完成，共删除了 {deleted_count} 个文件。")
    else:
        logger.info("清理任务完成，没有找到需要删除的文件。")


# --- FastAPI 生命周期事件 ---
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """在服务器启动时启动后台任务，在关闭时停止。"""
    # 启动调度器并添加任务
    scheduler.add_job(cleanup_old_files, 'interval', minutes=CLEANUP_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"后台文件清理任务已启动，每 {CLEANUP_INTERVAL_MINUTES} 分钟运行一次。")
    yield
    # 关闭调度器
    scheduler.shutdown()
    logger.info("后台文件清理任务已停止。")


app = FastAPI(lifespan=lifespan)

# --- 确保上传目录存在 ---
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)
    logger.info(f"上传目录 '{UPLOAD_DIR}' 已创建。")

# --- 挂载静态文件目录以提供文件访问 ---
app.mount(f"/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- Pydantic 模型定义 ---
class UploadRequest(BaseModel):
    file_name: str
    file_data: str # 接收完整的 base64 data URI
    api_key: str | None = None

# --- API 端点 ---
@app.post("/upload")
async def upload_file(request: UploadRequest, http_request: Request):
    """
    接收 base64 编码的文件并保存，返回可访问的 URL。
    """
    # 简单的 API Key 认证
    if API_KEY and request.api_key != API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")

    try:
        # 1. 解析 base64 data URI
        header, encoded_data = request.file_data.split(',', 1)
        
        # 2. 解码 base64 数据
        file_data = base64.b64decode(encoded_data)

        # 3. 生成唯一文件名以避免冲突
        file_extension = os.path.splitext(request.file_name)[1]
        if not file_extension:
            # 尝试从 header 中获取 mime 类型来猜测扩展名
            import mimetypes
            mime_type = header.split(';')[0].split(':')[1]
            guessed_extension = mimetypes.guess_extension(mime_type)
            file_extension = guessed_extension if guessed_extension else '.bin'

        # 图片优化（如果启用）
        if OPTIMIZATION_CONFIG.get('enabled') and file_extension.lower() in ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff']:
            try:
                img = Image.open(io.BytesIO(file_data))
                original_size = len(file_data)

                # 步骤1: 清除元数据
                if OPTIMIZATION_CONFIG.get('strip_metadata'):
                    # 创建一个没有exif数据的新图像
                    img_data = list(img.getdata())
                    img_clean = Image.new(img.mode, img.size)
                    img_clean.putdata(img_data)
                    img = img_clean

                # 步骤2: 调整尺寸
                max_w = OPTIMIZATION_CONFIG.get('max_width', 1920)
                max_h = OPTIMIZATION_CONFIG.get('max_height', 1080)
                if img.width > max_w or img.height > max_h:
                    img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

                # 步骤3: 优化保存
                output = io.BytesIO()
                save_kwargs = {}
                
                # 确定输出格式
                output_format = img.format
                if OPTIMIZATION_CONFIG.get('convert_to_webp'):
                    output_format = 'WEBP'
                    file_extension = '.webp'
                
                # 处理透明度以兼容JPEG
                if output_format == 'JPEG' and img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1] if 'A' in img.mode else None)
                    img = background

                if output_format in ('JPEG', 'WEBP'):
                    save_kwargs['quality'] = OPTIMIZATION_CONFIG.get(f'{output_format.lower()}_quality', 85)
                
                if OPTIMIZATION_CONFIG.get('optimize_encoding'):
                    save_kwargs['optimize'] = True
                
                if output_format == 'WEBP' and OPTIMIZATION_CONFIG.get('progressive_encoding'):
                    save_kwargs['method'] = 6 # method 6 is the slowest but gives the best compression

                img.save(output, format=output_format, **save_kwargs)
                
                optimized_data = output.getvalue()
                optimized_size = len(optimized_data)
                
                logger.info(f"图片优化: {original_size/1024:.2f}KB → {optimized_size/1024:.2f}KB "
                           f"({(1-optimized_size/original_size)*100:.1f}% 压缩)")
                
                file_data = optimized_data
                
            except Exception as e:
                logger.warning(f"图片优化失败，使用原图: {e}", exc_info=True)

        unique_filename = f"{uuid.uuid4()}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)

        # 4. 保存文件
        with open(file_path, "wb") as f:
            f.write(file_data)
        
        # 5. 返回成功信息和唯一文件名
        logger.info(f"文件 '{request.file_name}' 已成功保存为 '{unique_filename}'。")
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "filename": unique_filename}
        )

    except (ValueError, IndexError) as e:
        logger.error(f"解析 base64 数据时出错: {e}")
        raise HTTPException(status_code=400, detail=f"无效的 base64 data URI 格式: {e}")
    except Exception as e:
        logger.error(f"处理文件上传时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"内部服务器错误: {e}")

@app.get("/")
def read_root():
    return {"message": "LMArena Bridge 文件床服务器正在运行。"}

# --- 主程序入口 ---
if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 文件床服务器正在启动...")
    logger.info("   - 监听地址: https://api.spinsnow.fun/file-bed-server")
    logger.info(f"   - 上传端点: https://api.spinsnow.fun/file-bed-server/upload")
    logger.info(f"   - 文件访问路径: /uploads")
    uvicorn.run(app, host="0.0.0.0", port=5180)