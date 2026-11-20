"""发往上游的请求体压缩。

Agent 类客户端每轮都带着完整历史，发往上游的请求体是流量的大头。渠道或模型设了
upstream_compression（zstd / br / gzip）时，请求体在线程里压缩后带上 Content-Encoding 发出；
上游回 400 / 415 且错误正文像是解码失败时，同一次尝试内改发明文，明文成功就在一段时间内
记住这个地址不接受该算法。只压请求体，不碰响应；没设或设成 off 时和原来完全一样。

全局配置 upstream_compression 只放调优参数，见 _DEFAULTS。
"""
import asyncio
import gzip
import re
import time
from urllib.parse import urlsplit

import brotli
import zstandard

from core.config_loader import CONFIG

ALGORITHMS = ('zstd', 'br', 'gzip')
_DEFAULTS = {
    'min_bytes': 4096,
    'zstd_level': 3,
    'gzip_level': 1,
    'br_quality': 2,
    'fallback_statuses': [400, 415],
    # 只在错误正文像是解码、解析失败时重发，“prompt is too long”这类真实的 400 不该再明文传一遍
    'fallback_pattern': r'json|pars|decod|encod|compress|gzip|zstd|brotli|invalid character|unexpected token'
                        r'|unsupported (media|content|encoding)',
    # 0 表示一直记到重启
    'remember_minutes': 360,
}
_rejected = {}


def options() -> dict:
    configured = CONFIG.get('upstream_compression')
    return {**_DEFAULTS, **(configured if isinstance(configured, dict) else {})}


def _target(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}{parts.path}'


def _label(algorithm: str, settings: dict) -> str:
    level = {'zstd': 'zstd_level', 'gzip': 'gzip_level', 'br': 'br_quality'}[algorithm]
    return f'{algorithm}-{settings[level]}'


def _compress(algorithm: str, payload: bytes, settings: dict) -> bytes:
    if algorithm == 'zstd':
        return zstandard.ZstdCompressor(level=int(settings['zstd_level'])).compress(payload)
    if algorithm == 'br':
        return brotli.compress(payload, quality=int(settings['br_quality']))
    return gzip.compress(payload, compresslevel=int(settings['gzip_level']), mtime=0)


def decompress(encoding, data: bytes) -> bytes:
    """还原压缩后的请求体；转换预览展示真正要发的正文时用。"""
    if encoding == 'zstd':
        return zstandard.ZstdDecompressor().decompress(data)
    if encoding == 'br':
        return brotli.decompress(data)
    if encoding == 'gzip':
        return gzip.decompress(data)
    return data


async def prepare(url: str, payload: bytes, endpoint: dict):
    """返回 (发送的正文, 追加的请求头, 压缩明细)；渠道没开压缩时明细为 None。"""
    algorithm = (endpoint or {}).get('upstream_compression')
    if algorithm not in ALGORITHMS:
        return payload, {}, None
    settings = options()
    report = {'encoding': _label(algorithm, settings), 'raw_bytes': len(payload), 'sent_bytes': len(payload)}
    if len(payload) < int(settings['min_bytes']):
        report['status'] = 'below_min'
        return payload, {}, report
    if _rejected.get((_target(url), algorithm), 0) > time.time():
        report['status'] = 'remembered_unsupported'
        return payload, {}, report
    started = time.perf_counter()
    try:
        data = await asyncio.to_thread(_compress, algorithm, payload, settings)
    except Exception as error:
        report.update(status='compress_error', note=str(error)[:300])
        return payload, {}, report
    report.update(status='compressed', sent_bytes=len(data), compress_ms=round((time.perf_counter() - started) * 1000, 2))
    return data, {'Content-Encoding': algorithm}, report


async def rejected(response, report) -> bool:
    """压缩发出后上游的回应像是解不开正文：状态码在回退列表里，且错误正文匹配回退规则。"""
    settings = options()
    if response.status not in set(settings['fallback_statuses']):
        return False
    text = (await response.text(errors='replace'))[:2000]
    pattern = settings['fallback_pattern']
    if pattern and not re.search(pattern, text, re.I):
        return False
    report['note'] = text[:300]
    return True


def remember(url: str, report: dict) -> None:
    """明文重发成功后记住这个地址不接受该算法，记忆期内直接发明文。"""
    minutes = float(options()['remember_minutes'])
    algorithm = report['encoding'].split('-', 1)[0]
    _rejected[(_target(url), algorithm)] = time.time() + minutes * 60 if minutes > 0 else float('inf')

