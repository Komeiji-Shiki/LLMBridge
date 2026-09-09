"""管理面板多来源用量查询与导出。"""

import asyncio
import csv
import io
import logging

from fastapi import HTTPException, Response

from core.codex_usage import codex_usage_index
from core.combined_usage import combine_usage
from core.config_loader import CONFIG
from utils.csv_export import csv_safe_text


async def selected_usage(bridge, source, start=None, end=None, force=False):
    if source not in ('all', 'bridge', 'codex'):
        raise HTTPException(status_code=422, detail='用量来源必须是 all、bridge 或 codex')
    codex = None
    if source != 'bridge':
        try:
            # 经本网关转发的日志只在 Codex 单独视图保留，合计使用网关记录。
            providers = CONFIG.get('codex_usage', {}).get('bridge_providers', ['local-lmarenabridge']) if source == 'all' else []
            codex = await asyncio.to_thread(codex_usage_index.stats, start, end, force, providers)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail='日期格式无效') from error
        except Exception:
            logging.getLogger(__name__).exception('Codex 用量索引读取失败')
            codex = {'model_stats': [], 'daily_stats': [], 'status': {
                'available': False, 'errors': [{'error': 'Codex 用量读取失败，请查看服务日志'}]}}
    return combine_usage(bridge, codex, source)


def usage_csv(data):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['来源', '模型', '请求数', '用量事件数', '会话数', '输入Tokens', '输出Tokens',
                     '缓存命中Tokens', '推理Tokens', '缓存写入Tokens', '总Tokens', '输入成本(原币)',
                     '缓存成本(原币)', '输出成本(原币)', '总成本(原币)', '货币', '平均Token/请求'])
    for row in data['model_stats']:
        external = row.get('source') == 'codex'
        writer.writerow([
            row.get('source', 'bridge'), csv_safe_text(row.get('display_name', row['model'])),
            '' if external else row.get('request_count', 0), row.get('event_count', ''), row.get('session_count', ''),
            row.get('input_tokens', 0), row.get('output_tokens', 0), row.get('cached_tokens', 0),
            row.get('reasoning_tokens', ''), row.get('cache_write_tokens', ''), row.get('total_tokens', 0),
            *('' if external else round(row.get(key, 0) or 0, 6)
              for key in ('input_cost', 'cached_cost', 'output_cost', 'total_cost')),
            '' if external else csv_safe_text(row.get('currency', 'USD')),
            '' if external else round(row.get('total_tokens', 0) / row['request_count']) if row.get('request_count') else 0,
        ])
    return Response(content=output.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=token_report.csv'})
