"""配置加载和管理模块"""
import json
import logging
import os
import re
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# 配置文件修改时间跟踪（用于热更新）
CONFIG_FILE_MTIMES = {
    'config.jsonc': 0,
    'model_endpoint_map.json': 0,
    'models.json': 0
}
CONFIG_LOCK = Lock()  # 保护配置重载的线程锁

# 全局配置存储
CONFIG = {}
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}
DEFAULT_MODEL_ID = None

# 模型轮询索引
MODEL_ROUND_ROBIN_INDEX = {}
MODEL_ROUND_ROBIN_LOCK = Lock()


def _parse_jsonc(jsonc_string: str) -> dict:
    """
    稳健地解析 JSONC 字符串，移除注释。
    改进版：正确处理字符串内的 // 和 /* */
    """
    lines = jsonc_string.splitlines()
    no_comments_lines = []
    in_block_comment = False
    
    for line in lines:
        if in_block_comment:
            # 在块注释中，查找结束标记
            if '*/' in line:
                in_block_comment = False
                # 保留块注释结束后的内容
                line = line.split('*/', 1)[1]
            else:
                continue
        
        # 处理可能的块注释开始
        if '/*' in line:
            # 需要更智能地处理，避免删除字符串中的 /*
            before_comment, _, after_comment = line.partition('/*')
            if '*/' in after_comment:
                # 单行块注释
                _, _, after_block = after_comment.partition('*/')
                line = before_comment + after_block
            else:
                # 多行块注释开始
                line = before_comment
                in_block_comment = True
        
        # 处理单行注释 //，但要避免删除字符串中的 //
        # 使用更智能的方法：查找不在引号内的 //
        processed_line = ""
        in_string = False
        escape_next = False
        i = 0
        
        while i < len(line):
            char = line[i]
            
            if escape_next:
                processed_line += char
                escape_next = False
                i += 1
                continue
            
            if char == '\\':
                processed_line += char
                escape_next = True
                i += 1
                continue
            
            if char == '"' and not in_string:
                in_string = True
                processed_line += char
            elif char == '"' and in_string:
                in_string = False
                processed_line += char
            elif char == '/' and i + 1 < len(line) and line[i + 1] == '/' and not in_string:
                # 找到了真正的注释，停止处理这一行
                break
            else:
                processed_line += char
            
            i += 1
        
        # 只有非空行才添加
        if processed_line.strip():
            no_comments_lines.append(processed_line)

    return json.loads("\n".join(no_comments_lines))


def load_config(force_reload=False):
    """从 config.jsonc 加载配置，并处理 JSONC 注释。
    
    Args:
        force_reload: 是否强制重新加载，忽略文件修改时间检查
    """
    global CONFIG, CONFIG_FILE_MTIMES
    
    config_file = 'config.jsonc'
    
    # 检查文件是否被修改
    try:
        current_mtime = os.path.getmtime(config_file)
        if not force_reload and current_mtime == CONFIG_FILE_MTIMES[config_file]:
            # 文件未修改，无需重新加载
            return
    except FileNotFoundError:
        logger.error(f"配置文件 '{config_file}' 未找到。")
        CONFIG.clear()
        return
    
    # 使用锁保护配置重载
    with CONFIG_LOCK:
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                content = f.read()
            # 🔧 关键修复：使用 clear() + update() 而不是重新赋值
            # 这样可以保持字典对象不变，让所有导入的引用都能看到更新
            new_config = _parse_jsonc(content)
            CONFIG.clear()
            CONFIG.update(new_config)
            CONFIG_FILE_MTIMES[config_file] = current_mtime
            logger.info(f"✅ 已{'重新' if not force_reload else ''}加载配置文件 'config.jsonc'")
            # 打印关键配置状态
            logger.info(f"  - 酒馆模式 (Tavern Mode): {'✅ 启用' if CONFIG.get('tavern_mode_enabled') else '❌ 禁用'}")
            logger.info(f"  - 绕过模式 (Bypass Mode): {'✅ 启用' if CONFIG.get('bypass_enabled') else '❌ 禁用'}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"加载或解析 'config.jsonc' 失败: {e}。将使用默认配置。")
            CONFIG.clear()


def load_model_map():
    """从 models.json 加载模型映射（可选的备用配置），支持 'id:type' 格式。"""
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            content = f.read()
            # 允许空文件（这是正常的，因为这是可选配置）
            if not content.strip():
                logger.info("'models.json' 文件为空（这是正常的，该文件为可选的备用配置）。")
                MODEL_NAME_TO_ID_MAP.clear()
                return
            
            raw_map = json.loads(content)
            
        processed_map = {}
        for name, value in raw_map.items():
            if isinstance(value, str) and ':' in value:
                parts = value.split(':', 1)
                model_id = parts[0] if parts[0].lower() != 'null' else None
                model_type = parts[1]
                processed_map[name] = {"id": model_id, "type": model_type}
            else:
                # 默认或旧格式处理
                processed_map[name] = {"id": value, "type": "text"}

        # 🔧 关键修复：使用 clear() + update() 而不是重新赋值
        MODEL_NAME_TO_ID_MAP.clear()
        MODEL_NAME_TO_ID_MAP.update(processed_map)
        logger.info(f"成功从 'models.json' 加载并解析了 {len(MODEL_NAME_TO_ID_MAP)} 个备用模型配置。")

    except FileNotFoundError:
        logger.info("'models.json' 文件未找到（这是正常的，该文件为可选的备用配置）。")
        MODEL_NAME_TO_ID_MAP.clear()
    except json.JSONDecodeError as e:
        logger.warning(f"'models.json' 解析失败: {e}。将使用空模型列表。")
        MODEL_NAME_TO_ID_MAP.clear()


def load_model_endpoint_map(force_reload=False):
    """从 model_endpoint_map.json 加载模型到端点的映射。
    
    Args:
        force_reload: 是否强制重新加载，忽略文件修改时间检查
    """
    global MODEL_ENDPOINT_MAP, CONFIG_FILE_MTIMES
    
    config_file = 'model_endpoint_map.json'
    
    # 检查文件是否被修改
    try:
        current_mtime = os.path.getmtime(config_file)
        if not force_reload and current_mtime == CONFIG_FILE_MTIMES[config_file]:
            # 文件未修改，无需重新加载
            return
    except FileNotFoundError:
        logger.warning(f"'{config_file}' 文件未找到。将使用空映射。")
        MODEL_ENDPOINT_MAP.clear()
        return
    
    # 使用锁保护配置重载
    with CONFIG_LOCK:
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                content = f.read()
                # 允许空文件
                if not content.strip():
                    new_map = {}
                else:
                    new_map = json.loads(content)
            # 🔧 关键修复：使用 clear() + update() 而不是重新赋值
            MODEL_ENDPOINT_MAP.clear()
            MODEL_ENDPOINT_MAP.update(new_map)
            CONFIG_FILE_MTIMES[config_file] = current_mtime
            logger.info(f"✅ 已{'重新' if not force_reload else ''}加载 'model_endpoint_map.json' ({len(MODEL_ENDPOINT_MAP)} 个模型端点)")
        except FileNotFoundError:
            logger.warning("'model_endpoint_map.json' 文件未找到。将使用空映射。")
            MODEL_ENDPOINT_MAP.clear()
        except json.JSONDecodeError as e:
            logger.error(f"加载或解析 'model_endpoint_map.json' 失败: {e}。将使用空映射。")
            MODEL_ENDPOINT_MAP.clear()


def save_config():
    """将当前的 CONFIG 对象写回 config.jsonc 文件，保留注释。"""
    try:
        # 读取原始文件以保留注释等
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 使用正则表达式安全地替换值
        def replacer(key, value, content):
            # 这个正则表达式会找到 key，然后匹配它的 value 部分，直到逗号或右花括号
            pattern = re.compile(rf'("{key}"\s*:\s*").*?("?)(,?\s*)$', re.MULTILINE)
            replacement = rf'\g<1>{value}\g<2>\g<3>'
            if not pattern.search(content): # 如果 key 不存在，就添加到文件末尾（简化处理）
                 content = re.sub(r'}\s*$', f'  ,"{key}": "{value}"\n}}', content)
            else:
                 content = pattern.sub(replacement, content)
            return content

        content_str = "".join(lines)
        content_str = replacer("session_id", CONFIG["session_id"], content_str)
        content_str = replacer("message_id", CONFIG["message_id"], content_str)
        
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            f.write(content_str)
        logger.info("✅ 成功将会话信息更新到 config.jsonc。")
    except Exception as e:
        logger.error(f"❌ 写入 config.jsonc 时发生错误: {e}", exc_info=True)