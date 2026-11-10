"""前端预览：在临时目录里起一个只监听本机的网关实例。

页面、脚本和样式取自仓库，改完刷新就能看到；配置、日志和统计都在临时目录里，
不读写正在运行的实例。模型列表来自 model_endpoint_map.json，去掉了所有密钥，
上游地址全部换成无效域名，预览实例发不出真实请求。统计只读复制最近 30 天。

    python scripts/preview_ui.py --port 5180
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SECRET_FIELD = re.compile(r'key|token|secret|credential|password|header|cookie|session', re.I)
_KEPT_FIELDS = {'api_key_strategy', 'api_key_auth_mode', 'tokenizer'}


def _scrub(value):
    """去掉密钥类字段；上游地址换成无效域名。"""
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {}
    for name, item in value.items():
        if name == 'api_base_url':
            cleaned[name] = 'https://preview.invalid/v1'
        elif name in ('api_key', 'api_keys'):
            cleaned[name] = 'preview-key' if name == 'api_key' else ['preview-key-a', 'preview-key-b']
        elif _SECRET_FIELD.search(name) and isinstance(item, (str, list, dict)) and name not in _KEPT_FIELDS:
            continue
        elif isinstance(item, str) and item.startswith(('http://', 'https://')):
            cleaned[name] = 'https://preview.invalid/v1'
        else:
            cleaned[name] = _scrub(item)
    return cleaned


def _copy_recent_requests(source: Path, target: Path, days: int) -> int:
    if not source.exists():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    uri = 'file:' + source.resolve().as_posix() + '?mode=ro'
    reader = sqlite3.connect(uri, uri=True)
    try:
        schema = reader.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()
    finally:
        reader.close()
    if not schema:
        return 0
    # ATTACH 用只读 URI，要求主连接也以 URI 方式打开
    writer = sqlite3.connect('file:' + target.resolve().as_posix() + '?mode=rwc', uri=True)
    try:
        writer.execute(schema[0])
        writer.execute('ATTACH DATABASE ? AS live', (uri,))
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        copied = writer.execute('INSERT INTO requests SELECT * FROM live.requests WHERE date >= ?', (since,)).rowcount
        writer.commit()
        writer.execute('DETACH DATABASE live')
        return copied
    finally:
        writer.close()


def prepare_runtime(port: int, days: int) -> Path:
    runtime = Path(tempfile.mkdtemp(prefix='llmbridge-preview-'))
    config = (REPO / 'config.jsonc.example').read_text(encoding='utf-8')
    config = re.sub(r'("server_port"\s*:\s*)\d+', lambda match: match.group(1) + str(port), config, count=1)
    config = re.sub(r'("web_access_key"\s*:\s*)"[^"]*"', r'\1""', config, count=1)
    (runtime / 'config.jsonc').write_text(config, encoding='utf-8')

    source_map = REPO / 'model_endpoint_map.json'
    if source_map.exists():
        models = _scrub(json.loads(source_map.read_text(encoding='utf-8')))
        (runtime / 'model_endpoint_map.json').write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        shutil.copy(REPO / 'model_endpoint_map.example.json', runtime / 'model_endpoint_map.json')
    for name in ('models.json', 'custom_tokenizers.json'):
        if (REPO / name).exists():
            shutil.copy(REPO / name, runtime / name)

    copied = _copy_recent_requests(REPO / 'logs' / 'requests.db', runtime / 'logs' / 'requests.db', days)
    if (REPO / 'logs' / 'stats.json').exists():
        shutil.copy(REPO / 'logs' / 'stats.json', runtime / 'logs' / 'stats.json')
    print(f'预览目录 {runtime}，复制了最近 {days} 天的 {copied} 条请求统计', flush=True)
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=5180)
    parser.add_argument('--days', type=int, default=30)
    args = parser.parse_args()

    os.chdir(prepare_runtime(args.port, args.days))
    sys.path.insert(0, str(REPO))
    if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    import api_server_new
    api_server_new.load_config(force_reload=True)
    server = Config()
    server.bind = [f'127.0.0.1:{args.port}']
    print(f'预览地址 http://127.0.0.1:{args.port}/admin', flush=True)
    asyncio.run(serve(api_server_new.app, server))


if __name__ == '__main__':
    main()
