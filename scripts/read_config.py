"""给启动脚本读取 config.jsonc 的顶层配置：python scripts/read_config.py <键名>

不导入项目代码：kill_server 这类脚本要在依赖不全时也能运行。
"""
import re
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.jsonc'
# 与 core.constants.ServerDefaults 保持一致（有测试校验）
DEFAULTS = {'server_port': 5102}


def read_value(key, text=None):
    if text is None:
        text = CONFIG_PATH.read_text(encoding='utf-8') if CONFIG_PATH.exists() else ''
    # 只匹配行首的键：被注释掉的旧值以 // 开头，不会命中
    match = re.search(rf'^\s*"{re.escape(key)}"\s*:\s*"?([^",\s]+)', text, re.MULTILINE)
    return match.group(1) if match else str(DEFAULTS.get(key, ''))


if __name__ == '__main__':
    print(read_value(sys.argv[1]))
