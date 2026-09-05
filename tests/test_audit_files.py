"""Isolated CSV/JSONC/file-bed regressions; never opens user configuration."""
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from routes.admin_routes import export_report
from utils.jsonc_edit import parse_jsonc


def test_jsonc_strings_comments_and_trailing_commas():
    source = '{/* comment */ "url":"https://host/x", "text":",}", "path":"C:\\\\", "n":0, // end\n}'
    assert parse_jsonc(source) == {'url': 'https://host/x', 'text': ',}', 'path': 'C:\\', 'n': 0}


@pytest.mark.parametrize('currency,expected', [('USD', 2.0), ('CNY', 14.4)])
def test_model_costs_convert_each_historical_currency(monkeypatch, tmp_path, currency, expected):
    from core import db_stats
    from modules.monitoring_sqlite import SQLiteLogger
    db_path = tmp_path / 'requests.db'
    logger = SQLiteLogger(db_path)
    for index, (unit, cost) in enumerate([('USD', 1), ('CNY', 7.2)]):
        logger.write_request({'type': 'request_end', 'request_id': str(index), 'model': 'demo',
                              'status': 'success', 'timestamp': 1700000000 + index,
                              'cost_info': {'currency': unit, 'input_cost': cost, 'total_cost': cost}})
    monkeypatch.setattr(db_stats, 'DB_PATH', db_path)
    monkeypatch.setattr(db_stats, 'CONFIG', {'exchange_rate': {'USD_TO_CNY': 7.2}})
    reader = db_stats.StatsDB()
    try:
        result = reader.get_token_stats(model_config={'demo': {'pricing': {'currency': currency}}})
        assert result['model_stats'][0]['input_cost'] == pytest.approx(expected)
        assert result['model_stats'][0]['total_cost'] == pytest.approx(expected)
    finally:
        reader._discard_connection()
        logger.close()


@pytest.mark.parametrize('text', ['=1+1', '+cmd', '-1+1', '@SUM(1)', '  =1'])
def test_csv_labels_cannot_be_formulas(text):
    from utils.csv_export import csv_safe_text
    assert csv_safe_text(text).startswith("'")


@pytest.mark.parametrize('enabled', [True, False])
def test_csv_has_actual_bom_and_does_not_mislabel_cny(enabled):
    db = SimpleNamespace(enabled=enabled, get_token_stats_async=AsyncMock(return_value={
        'model_stats': [{'model': '模型', 'currency': 'CNY', 'total_cost': 12}]}))
    monitor = SimpleNamespace(get_model_stats=lambda: [{'model': '模型'}])
    response = asyncio.run(export_report(db, monitor, {}))
    assert response.body.startswith(b'\xef\xbb\xbf')
    text = response.body.decode('utf-8-sig')
    assert '模型' in text
    assert '(USD)' not in text


@pytest.fixture
def file_bed(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'file_bed_server/main.py'
    spec = importlib.util.spec_from_file_location('file_bed_server.audit_main', source)
    module = importlib.util.module_from_spec(spec)
    with patch('builtins.open', mock_open(read_data='{}')), patch('os.makedirs'), \
            patch('fastapi.staticfiles.StaticFiles', MagicMock()):
        spec.loader.exec_module(module)
    module.UPLOAD_DIR = str(tmp_path)
    module.API_KEY = 'test-key'
    module.OPTIMIZATION_CONFIG = {'enabled': True, 'strip_metadata': True,
                                  'max_width': 2, 'max_height': 2, 'convert_to_webp': False}
    return module, TestClient(module.app), tmp_path


def test_file_bed_keeps_client_errors(file_bed):
    _, client, _ = file_bed
    for filename, data in [('bad.html', 'data:text/html;base64,SGk='),
                           ('bad.png', 'data:image/png;base64,%%%%')]:
        response = client.post('/upload', json={'api_key': 'test-key', 'file_name': filename, 'file_data': data})
        assert response.status_code == 400


def test_file_bed_strips_metadata_without_losing_image_format(file_bed):
    _, client, folder = file_bed
    image = Image.new('RGB', (4, 4), 'red')
    info = PngImagePlugin.PngInfo()
    info.add_text('private_metadata', 'remove me')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', pnginfo=info)
    response = client.post('/upload', json={'api_key': 'test-key', 'file_name': 'sample.png',
        'file_data': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()})
    assert response.status_code == 200
    with Image.open(folder / response.json()['filename']) as saved:
        assert saved.format == 'PNG'
        assert saved.size == (2, 2)
        assert 'private_metadata' not in saved.info
        assert saved.getpixel((0, 0)) == (255, 0, 0)
