"""把 Gemini 原生端点的主机根地址补上 /v1beta。

c897c49 起版本路径完全由 api_base_url 决定，程序不再自动补 /v1beta。
此前主机根地址（如 https://generativelanguage.googleapis.com、
http://localhost:8317）实际请求的都是 <根地址>/v1beta，升级后会变成
<根地址>/models/... 而 404。本脚本只改路径为空的地址，把它们写成升级前
实际使用的完整地址；已带任意路径（/v1、/v1beta、自定义前缀）的地址不动。

默认只读列出待迁移端点；--apply 先备份到 .migration_backups/ 再原子写入。
运行中的服务会按文件修改时间自动重新加载。不输出任何密钥。
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.jsonc_edit import atomic_write_json  # noqa: E402


def migrated_base_url(endpoint):
    if not isinstance(endpoint, dict) or endpoint.get('api_type') != 'gemini_native':
        return None
    base = endpoint.get('api_base_url')
    if not isinstance(base, str) or not base.strip():
        return None
    if urlsplit(base.strip()).path not in ('', '/'):
        return None
    return base.strip().rstrip('/') + '/v1beta'


def plan(model_map):
    changes = []
    for name, raw in model_map.items():
        for index, endpoint in enumerate(raw if isinstance(raw, list) else [raw]):
            target = migrated_base_url(endpoint)
            if target:
                changes.append((name, index, endpoint, target))
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map', default=str(ROOT / 'model_endpoint_map.json'))
    parser.add_argument('--apply', action='store_true', help='备份后写入迁移结果')
    args = parser.parse_args()

    path = Path(args.map)
    model_map = json.loads(path.read_text(encoding='utf-8'))
    changes = plan(model_map)
    for name, index, endpoint, target in changes:
        archived = '（已归档）' if endpoint.get('archived') else ''
        print(f'{name}[{index}]{archived}: {endpoint["api_base_url"]} -> {target}')
    if not changes:
        print('没有需要迁移的 Gemini 端点')
        return 0
    if not args.apply:
        print(f'共 {len(changes)} 个端点待迁移；加 --apply 执行')
        return 0

    backup_dir = ROOT / '.migration_backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)
    for _, _, endpoint, target in changes:
        endpoint['api_base_url'] = target
    atomic_write_json(str(path), model_map)
    print(f'已迁移 {len(changes)} 个端点，原文件备份在 {backup_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
