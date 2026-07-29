"""配置加载和管理模块"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# 配置文件修改时间跟踪（用于热更新）
CONFIG_FILE_MTIMES = {
    'config.jsonc': 0.0,
    'model_endpoint_map.json': 0.0,
    'models.json': 0.0
}
CONFIG_LOCK = Lock()  # 保护配置重载的线程锁

# 全局配置存储
CONFIG = {}
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}
DEFAULT_MODEL_ID = None

# 🔧 鉴权 fail-closed：仅在首次成功解析 config.jsonc 后置 True。
# 启动期配置解析失败时保持 False，WebAccessKeyMiddleware 据此对受保护路径
# 返回 503（拒绝服务）而不是放行（旧版「保留当前配置」= 空配置 = 鉴权失效）。
CONFIG_LOADED = False

# 模型轮询索引
MODEL_ROUND_ROBIN_INDEX = {}
# 🔧 性能修复：改为 asyncio.Lock，避免在 async 函数中阻塞事件循环
MODEL_ROUND_ROBIN_LOCK = asyncio.Lock()



def get_setting(path: str, default=None):
    """按点分路径读取配置项，缺失或为 null 时回退到 default。

    统一入口的意义：配置读取点散落在各模块，历史上反复出现两类问题——
    管理面板能改、后端却读死常量（配置形同虚设），以及读错嵌套层级
    （如把 empty_response_retry.show_retry_info_to_client 当顶层键读）。
    所有可调参数一律走本函数，配置项与使用点就有了唯一对应关系。

    每次调用都从当前 CONFIG 取值，因此天然跟随热重载生效。

    Args:
        path: 点分路径，如 "background_tasks.memory_monitor_interval"
        default: 配置缺失时的回退值（通常取自 core.constants）
    """
    node = CONFIG
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def get_float_setting(path: str, default: float) -> float:
    """读取数值型配置项，非法值（非数字/负数/布尔）回退到默认值。

    配置由管理面板与手改文件两条路径写入，清空输入框会写入空串，
    直接拿去做 timeout/sleep 会抛 TypeError 或让任务永久挂起。
    """
    value = get_setting(path, default)
    if isinstance(value, bool):
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning(f"配置项 '{path}' 的值 {value!r} 不是合法数字，回退到默认值 {default}")
        return float(default)
    if parsed <= 0:
        logger.warning(f"配置项 '{path}' 的值 {parsed} 必须为正数，回退到默认值 {default}")
        return float(default)
    return parsed


def _replace_dict_no_gap(target: dict, new_data: dict) -> None:
    """就地替换 dict 内容，且任何时刻不出现“空配置”窗口。

    🔧 竞态修复：旧版 clear()+update() 两步之间，其他线程/协程读到的是
    空字典（所有 CONFIG.get() 突然变成默认值）。改为先覆盖/新增、再删除
    过期键：读者最多短暂看到新旧混合值，不会读到缺失的配置。
    """
    stale_keys = set(target) - set(new_data)
    target.update(new_data)
    for key in stale_keys:
        target.pop(key, None)


def _parse_jsonc(jsonc_string: str) -> dict:
    """
    稳健地解析 JSONC 字符串，移除 // 和 /* */ 注释。

    使用单遍字符状态机，正确处理字符串字面量内出现的
    //、/*、*/ 与转义引号，避免误删字符串内容。
    """
    result_chars = []
    i = 0
    n = len(jsonc_string)
    in_string = False

    while i < n:
        char = jsonc_string[i]

        if in_string:
            result_chars.append(char)
            if char == '\\' and i + 1 < n:
                # 保留转义序列的下一个字符，避免 \" 被误判为字符串结束
                result_chars.append(jsonc_string[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result_chars.append(char)
            i += 1
            continue

        if char == '/' and i + 1 < n:
            next_char = jsonc_string[i + 1]
            if next_char == '/':
                # 单行注释：跳到行尾（保留换行符以维持行号）
                newline_pos = jsonc_string.find('\n', i)
                i = n if newline_pos == -1 else newline_pos
                continue
            if next_char == '*':
                # 块注释：跳到 */ 之后
                end_pos = jsonc_string.find('*/', i + 2)
                i = n if end_pos == -1 else end_pos + 2
                continue

        result_chars.append(char)
        i += 1

    return json.loads(''.join(result_chars))


def load_config(force_reload=False):
    """从 config.jsonc 加载配置，并处理 JSONC 注释。

    Args:
        force_reload: 是否强制重新加载，忽略文件修改时间检查
    """
    global CONFIG, CONFIG_FILE_MTIMES, CONFIG_LOADED

    config_file = 'config.jsonc'
    
    # 检查文件是否被修改（锁外快速路径，避免未变更时的锁竞争）
    try:
        current_mtime = os.path.getmtime(config_file)
        if not force_reload and current_mtime == CONFIG_FILE_MTIMES[config_file]:
            # 文件未修改，无需重新加载
            return
    except FileNotFoundError:
        # 🔧 修复：文件短暂不可见（编辑器保存瞬间等）不再清空运行中配置，
        # 否则服务会立刻失去全部配置；首次启动时 CONFIG 本来就是空的。
        logger.error(f"配置文件 '{config_file}' 未找到，保留当前运行配置。")
        return
    
    # 使用锁保护配置重载
    with CONFIG_LOCK:
        # 🔧 竞态修复：锁内二次确认 mtime，避免多个线程同时通过锁外检查后
        # 重复重载同一版本的配置
        try:
            current_mtime = os.path.getmtime(config_file)
        except FileNotFoundError:
            logger.error(f"配置文件 '{config_file}' 未找到，保留当前运行配置。")
            return
        if not force_reload and current_mtime == CONFIG_FILE_MTIMES[config_file]:
            return
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                content = f.read()
            # 保持字典对象不变（所有导入的引用都能看到更新），
            # 且无“空配置”窗口（见 _replace_dict_no_gap）
            new_config = _parse_jsonc(content)
            _replace_dict_no_gap(CONFIG, new_config)
            CONFIG_LOADED = True
            CONFIG_FILE_MTIMES[config_file] = current_mtime
            logger.info(f"✅ 已{'重新' if not force_reload else ''}加载配置文件 'config.jsonc'")
            # 打印关键配置状态
            logger.info(f"  - 酒馆模式 (Tavern Mode): {'✅ 启用' if CONFIG.get('tavern_mode_enabled') else '❌ 禁用'}")
            logger.info(f"  - 绕过模式 (Bypass Mode): {'✅ 启用' if CONFIG.get('bypass_enabled') else '❌ 禁用'}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            # 解析失败（文件被改坏）保留旧配置继续运行，而不是清空让服务立刻失能。
            # 🔧 鉴权 fail-closed：但这是首次启动（CONFIG_LOADED 仍为 False，旧配置
            # 就是空 dict）时例外——空配置会让 WebAccessKeyMiddleware 把所有受保护
            # 路径当作「未配置密钥」直接放行，必须抛出让进程退出而不是带病启动。
            if not CONFIG_LOADED:
                logger.critical(f"首次加载 'config.jsonc' 失败且无任何可用配置: {e}。拒绝启动。")
                raise
            logger.error(f"加载或解析 'config.jsonc' 失败: {e}。保留当前运行配置。")


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

        # 无空窗替换，保持字典对象不变（见 _replace_dict_no_gap）
        _replace_dict_no_gap(MODEL_NAME_TO_ID_MAP, processed_map)
        logger.info(f"成功从 'models.json' 加载并解析了 {len(MODEL_NAME_TO_ID_MAP)} 个备用模型配置。")

    except FileNotFoundError:
        logger.info("'models.json' 文件未找到（这是正常的，该文件为可选的备用配置）。")
        MODEL_NAME_TO_ID_MAP.clear()
    except json.JSONDecodeError as e:
        # 🔧 修复：解析失败保留旧配置，避免文件被改坏时模型列表瞬间清空
        logger.warning(f"'models.json' 解析失败: {e}。保留当前模型列表。")


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
        # 🔧 竞态修复：锁内二次确认 mtime，避免多线程重复重载
        if not force_reload and current_mtime == CONFIG_FILE_MTIMES[config_file]:
            return
        loaded_new_mtime = None
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                content = f.read()
                # 允许空文件
                if not content.strip():
                    new_map = {}
                else:
                    new_map = json.loads(content)
            # 无空窗替换，保持字典对象不变（见 _replace_dict_no_gap）
            _replace_dict_no_gap(MODEL_ENDPOINT_MAP, new_map)
            CONFIG_FILE_MTIMES[config_file] = current_mtime
            logger.info(f"✅ 已{'重新' if not force_reload else ''}加载 'model_endpoint_map.json' ({len(MODEL_ENDPOINT_MAP)} 个模型端点)")
        except FileNotFoundError:
            # 🔧 修复：不再清空运行中映射（避免文件短暂不可见时模型列表瞬间清空），
            # 但 mtime 已在锁外记录过，不重复触碰
            logger.warning("'model_endpoint_map.json' 文件未找到。将使用空映射。")
            if not MODEL_ENDPOINT_MAP:
                CONFIG_FILE_MTIMES[config_file] = current_mtime
        except json.JSONDecodeError as e:
            # 🔧 修复：解析失败（文件被改坏）保留旧映射继续运行
            logger.error(f"加载或解析 'model_endpoint_map.json' 失败: {e}。保留当前映射。")


def save_config():
    """将当前的 CONFIG 对象写回 config.jsonc 文件，保留注释。
    
    注意：只保存存在于 CONFIG 中的字段，避免 KeyError。

    🔧 竞态修复：读-改-写全程持有 CONFIG_LOCK，避免与 load_config 热重载
    或并发保存交错时写坏 config.jsonc。
    """
    def replacer(key, value, content):
        """安全替换 JSONC 中某个 key 的值。

        🔧 转义修复：旧版把 value 直接拼进 re.sub 的 replacement 字符串，
        value 含反斜杠或 \\g 序列时会报错/写坏文件，含引号会破坏 JSONC 结构。
        现在值统一用 json.dumps 序列化，替换用回调函数（返回值不再参与
        反向引用/转义解释），从根上消除注入面；同时支持字符串与裸值。
        """
        value_json = json.dumps(value, ensure_ascii=False)
        # 匹配 "key": <转义感知的字符串 或 裸值（数字/布尔/null）>
        pattern = re.compile(
            rf'("{re.escape(key)}"\s*:\s*)("(?:\\.|[^"\\])*"|[^,\r\n}}]+)'
        )
        if pattern.search(content):
            return pattern.sub(lambda m: m.group(1) + value_json, content, count=1)
        # key 不存在：添加到文件末尾（简化处理）
        return re.sub(r'}\s*$', lambda m: f'  ,"{key}": {value_json}\n}}', content)

    try:
        with CONFIG_LOCK:
            # 读取原始文件以保留注释等（与下方写回同在一个锁窗口内）
            with open('config.jsonc', 'r', encoding='utf-8') as f:
                content_str = f.read()

            # 🔧 修复：只保存存在于 CONFIG 中的字段
            if "session_id" in CONFIG:
                content_str = replacer("session_id", CONFIG["session_id"], content_str)

            # message_id 字段已不再使用，移除此保存逻辑
            # 如果将来需要保存其他字段，在此添加类似检查

            with open('config.jsonc', 'w', encoding='utf-8') as f:
                f.write(content_str)
        logger.info("✅ 成功将会话信息更新到 config.jsonc。")
    except KeyError as e:
        logger.warning(f"⚠️ 保存配置时字段不存在: {e}")
    except Exception as e:
        logger.error(f"❌ 写入 config.jsonc 时发生错误: {e}", exc_info=True)