import httpx
import logging
import base64
import ssl
from typing import Tuple, Optional, Any

logger = logging.getLogger(__name__)

def _get_value_from_json_path(json_data: dict, path: str) -> Optional[str]:
    """根据点分隔的路径从JSON数据中获取值，支持数组索引。"""
    keys = path.split('.')
    current_data = json_data
    for key in keys:
        if isinstance(current_data, dict):
            current_data = current_data.get(key)
        elif isinstance(current_data, list) and key.isdigit():
            try:
                current_data = current_data[int(key)]
            except IndexError:
                current_data = None
        else:
            current_data = None
        
        if current_data is None:
            return None
    return str(current_data) if current_data is not None else None

async def upload_to_file_bed(
        file_name: str,
        file_data: str,
        endpoint: dict
) -> Tuple[Optional[str], Optional[str]]:
    """
    将 base64 编码的文件上传到指定的文件床端点 (通用版本)。
    """
    upload_url = endpoint.get("url")
    endpoint_name = endpoint.get("name", "Unknown Endpoint")

    if not upload_url:
        return None, f"图床 '{endpoint_name}' 未配置 'url'"

    # 从配置中读取API具体要求
    file_field_name = endpoint.get("form_file_field", "file")
    extra_data_fields = endpoint.get("form_data_fields", {})
    response_type = endpoint.get("response_type", "json")  # "json" 或 "text"
    json_url_key = endpoint.get("json_url_key", "url")  # e.g., "data.url" or "image"
    api_key = endpoint.get("api_key")
    api_key_field = endpoint.get("api_key_field", "key")

    if ',' in file_data:
        _, file_data = file_data.split(',', 1)

    try:
        decoded_file_data = base64.b64decode(file_data)
        
        files_payload = {file_field_name: (file_name, decoded_file_data)}
        
        # 将 api_key 添加到额外数据中（如果存在）
        if api_key:
            extra_data_fields[api_key_field] = api_key

        # 创建一个更宽松的SSL上下文以解决 "sslv3 alert handshake failure" 问题
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')

        # 允许并处理重定向，以兼容 filepush.co 等端点
        async with httpx.AsyncClient(timeout=60.0, verify=ssl_context, follow_redirects=True) as client:
            response = await client.post(upload_url, data=extra_data_fields, files=files_payload)
            # 对于非200状态码，也尝试处理，因为某些图床在成功时返回非2xx状态码
            # response.raise_for_status() # 暂时注释掉，以处理更多情况

            final_url = None
            if response.status_code == 200:
                # 优先检查响应头中的 Location 字段，适用于某些重定向场景
                if 'Location' in response.headers and response.headers['Location'].startswith("http"):
                    final_url = response.headers['Location']
                elif response_type == 'text':
                    url = response.text.strip()
                    # 兼容 bashupload.com 返回的 "wget <url>" 格式
                    if "wget" in url:
                        import re
                        match = re.search(r'https?://\S+', url)
                        if match:
                            final_url = match.group(0)
                    elif url.startswith("http"):
                        final_url = url
                else:  # 默认为 json
                    try:
                        result = response.json()
                        final_url = _get_value_from_json_path(result, json_url_key)
                    except Exception:
                        # 如果JSON解析失败（如 temp.sh），则回退到文本模式
                        url = response.text.strip()
                        if url.startswith("http"):
                            final_url = url
            
            if final_url:
                logger.info(f"✅ 成功上传 '{file_name}' 到 '{endpoint_name}'，URL: {final_url}")
                return final_url, None
            else:
                # 改进错误消息，包含状态码
                error_msg = f"图床返回了非预期的响应 (HTTP {response.status_code}): {response.text[:200]}"
                logger.error(f"❌ 上传到 '{endpoint_name}' 失败: {error_msg}")
                return None, error_msg

    except httpx.HTTPStatusError as e:
        error_details = f"HTTP 错误 {e.response.status_code} - {e.response.text[:200]}"
        return None, error_details
    except httpx.RequestError as e:
        # 改进日志记录，包含异常类型
        error_details = f"连接错误 ({type(e).__name__}): {e}. 请检查网络和 '{endpoint_name}' 的URL配置。"
        return None, error_details
    except Exception as e:
        # 改进日志记录，包含异常类型
        error_details = f"未知上传错误 ({type(e).__name__}): {e}"
        return None, error_details