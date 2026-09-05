"""Browser regressions against the real admin assets and an isolated fake API.

Run: python -m pytest tests/test_admin_workspace.py -q
Requires playwright and its Chromium browser. Never reads or modifies real config.
"""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ui():
    models = {
        'Alpha': {'api_type': 'direct_api', 'api_base_url': 'https://alpha.example/v1', 'model_id': 'upstream-alpha', 'api_key': 'test-key', 'max_temperature': 0, 'pricing': {'input': 0, 'output': 2, 'cached_input': 0, 'unit': 1000000}, 'sanitize_recursive_schemas': False},
        'Beta': {'api_type': 'responses_native', 'api_base_url': 'https://beta.example/v1', 'api_keys': ['test-a', 'test-b']},
        'Archived': {'api_type': 'anthropic_native', 'api_base_url': 'https://alpha.example/v1', 'archived': True, 'api_key': 'test-key'},
        'Legacy': {'session_id': 'test-session', 'mode': 'direct_chat'},
    }
    writes = []
    failures = {'save': False}
    errors = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request_route):
            request = request_route.request
            path = urlparse(request.url).path
            if path == '/api/admin/capabilities':
                return request_route.fulfill(json={'providers': {}, 'models': [{'model': 'Beta', 'endpoint': 0, 'provider': 'deepseek', 'protocol': 'responses', 'native_tools': ['web_search'], 'configured_tools': [], 'docs': 'https://api-docs.deepseek.com', 'issues': []}]})
            if path == '/api/admin/tokenizer_trust':
                return request_route.fulfill(json={'sources': []})
            if path == '/api/admin/playground/run':
                writes.append({'path': path, **request.post_data_json})
                return request_route.fulfill(content_type='text/event-stream', headers={'X-Bridge-Request-ID': 'run-id', 'X-Bridge-Session-ID': 'conversation'}, body='data: {"type":"response.output_text.delta","delta":"<img src=x>"}\n\ndata: {"type":"response.completed","response":{"id":"response-id"}}\n\n')
            if path == '/api/admin/playground/runs/run-id':
                return request_route.fulfill(json={'timings': {'prepare_ms': 20, 'upstream_wait_ms': 40, 'first_business_ms': 60, 'output_ms': 50, 'total_ms': 110, 'attempts': [{'attempt': 1, 'status': 200}]}, 'finished': True})
            if path == '/api/admin/current_price_analysis':
                return request_route.fulfill(json={'items': [{'model': 'Beta', 'requests': 1, 'historical_cost': 10, 'currency': 'USD', 'current_estimate': 20, 'current_currency': 'CNY'}]})
            if path == '/api/admin/overview':
                return request_route.fulfill(json={'active_requests': [], 'stats': {}, 'mode': {}, 'total_models': len(models), 'total_tabs': 0})
            if path == '/api/admin/config' and request.method == 'GET':
                return request_route.fulfill(json={'content': '// keep this comment\n{"server_port": 5102, "connection_pool": {"total_limit": 200}, "unknown": "literal ,} text"}'})
            if path == '/api/admin/token_stats':
                return request_route.fulfill(json={'model_stats': [], 'daily_stats': []})
            if path == '/api/admin/query_key_balance':
                return request_route.fulfill(json={'results': [{'index': 0, 'status': 'ok', 'balance': {'is_available': True, 'infos': [{'total': '12.5', 'currency': '<img src=x>'}]}}]})
            if path == '/api/admin/models':
                if request.method == 'POST':
                    data = request.post_data_json
                    writes.append(data)
                    if failures['save']:
                        return request_route.fulfill(status=500, json={'detail': 'Test save failure'})
                    models.pop(data.get('old_model_name', ''), None)
                    models[data['model_name']] = data['config']
                    return request_route.fulfill(json={'status': 'done'})
                return request_route.fulfill(json={'model_endpoint_map': models})
            if path.startswith('/api/'):
                if request.method == 'POST': writes.append({'path': path, **(request.post_data_json or {})})
                return request_route.fulfill(json={})
            relative = {'/admin': 'admin.html', '/monitor': 'monitor.html'}.get(path, path.lstrip('/'))
            asset = (ROOT / relative).resolve()
            if asset.is_relative_to(ROOT) and asset.is_file() and (relative in ('admin.html', 'monitor.html') or relative.startswith(('js/', 'css/'))):
                return request_route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset)[0] or 'text/plain')
            request_route.fulfill(status=404, body='Not found')

        page.route('**/*', route)
        page.goto('http://bridge.test/admin')
        page.locator('[data-page="models"]').click()
        page.wait_for_selector('.model-row')
        yield page, models, writes, failures
        browser.close()
    assert errors == [], errors


def test_filter_selection_and_protocol(ui):
    page, _, writes, _ = ui
    page.locator('#models-query').fill('upstream-alpha')
    assert page.locator('.model-row:visible').count() == 1
    page.locator('#select-all-active').check()
    assert page.locator('.model-checkbox:checked').count() == 1
    assert page.evaluate("modelsSortable.option('disabled')") is True
    page.locator('#models-query').fill('Beta')
    assert page.locator('.model-checkbox:checked').count() == 0
    page.locator('#models-query').fill('not found')
    assert page.locator('#models-no-results').is_visible()
    page.get_by_role('button', name='清除筛选').click()
    page.locator('#models-protocol').select_option('anthropic_native')
    assert page.locator('.model-row:visible').count() == 1
    assert page.locator('#archive-section-body').is_visible()
    assert writes == []


def test_config_mode_round_trip_preserves_edits_comments_and_literals(ui):
    page, _, _, _ = ui
    page.locator('[data-page="config"]').click()
    page.wait_for_selector('#form-server_port')
    page.locator('#form-server_port').fill('5200')
    page.locator('#mode-form-btn').click()
    assert page.locator('#form-server_port').input_value() == '5200'
    page.locator('#mode-jsonc-btn').click()
    text = page.locator('#config-editor').input_value()
    assert '// keep this comment' in text
    parsed = page.evaluate('text => parseJsonc(text)', text)
    assert parsed['server_port'] == 5200
    assert parsed['unknown'] == 'literal ,} text'
    page.locator('#mode-form-btn').click()
    assert page.locator('#form-server_port').input_value() == '5200'


def test_config_parser_escaped_backslashes_and_form_snapshot(ui):
    page, _, _, _ = ui
    text = '// comment\n' + json.dumps({'path': 'C:\\folder\\', 'literal': ',}', 'number': -1.25e3})
    assert page.evaluate('text => parseJsonc(text)', text) == json.loads(text.split('\n', 1)[1])
    page.locator('[data-page="config"]').click()
    page.wait_for_selector('#form-server_port')
    assert page.evaluate('''() => {
        const before = JSON.stringify(currentConfigData);
        document.getElementById('form-connection_pool_total_limit').value = '777';
        formToConfig();
        return before === JSON.stringify(currentConfigData);
    }''')


def test_edit_preserves_unrepresented_fields_and_other_endpoints(ui):
    page, models, writes, _ = ui
    primary = {**models['Alpha'], 'custom_header_policy': {'tenant': 'keep'}, 'prefill_content': 'clear me'}
    secondary = {'api_type': 'direct_api', 'api_base_url': 'https://backup.test', 'api_key': 'backup-key'}
    models['Alpha'] = [primary, secondary]
    page.evaluate('loadModels()')
    page.locator('.model-name', has_text='Alpha').click()
    page.locator('[data-settings-page="messages"]').click()
    page.locator('#prefill-content').fill('')
    page.locator('#model-save-btn').click()
    page.wait_for_selector('#model-modal', state='hidden')
    saved = writes[-1]
    assert saved['old_model_name'] == 'Alpha'
    assert saved['config'][0]['custom_header_policy'] == {'tenant': 'keep'}
    assert 'prefill_content' not in saved['config'][0]
    assert saved['config'][1] == secondary


def test_monitor_keeps_newest_query_and_supports_programmatic_tabs(ui):
    page, _, _, _ = ui
    page.add_init_script('window.WebSocket = class {}; window.setInterval = () => 0; window.setTimeout = () => 0;')
    page.goto('http://bridge.test/monitor')
    result = page.evaluate('''async () => {
        let olderResolve, newerResolve;
        const pending = [new Promise(resolve => olderResolve = resolve), new Promise(resolve => newerResolve = resolve)];
        apiGet = async () => { const response = pending.shift(); return {json: () => response}; };
        const older = refreshRequestLogs();
        const newer = refreshRequestLogs();
        newerResolve({total: 1, items: [{request_id: 'new', model: 'new', status: 'success', timestamp: 1,
            currency: 'USD" onmouseover="window.auditInjected=true', total_cost: 1}]});
        await newer;
        olderResolve({total: 999, items: []});
        await older;
        const count = document.getElementById('request-count').textContent;
        const injected = !!document.querySelector('[onmouseover]');
        apiGet = async () => ({json: async () => []});
        switchTab('errors');
        return {count, injected, active: document.querySelector('.tab.active').dataset.tab};
    }''')
    assert result == {'count': '(1)', 'injected': False, 'active': 'errors'}


def test_groups_zero_values_and_save(ui):
    page, _, writes, _ = ui
    page.locator('.model-name', has_text='Alpha').click()
    assert page.locator('.api-key-input').get_attribute('type') == 'password'
    assert page.locator('#settings-connection').is_visible()
    page.locator('[data-settings-page="generation"]').click()
    assert page.locator('#max-temperature').input_value() == '0'
    page.locator('[data-settings-page="pricing"]').click()
    assert page.locator('#pricing-input').input_value() == '0'
    assert page.locator('#pricing-cached-input').input_value() == '0'
    page.locator('#pricing-output').fill('3')
    page.locator('#model-save-btn').click()
    page.wait_for_selector('#model-modal', state='hidden')
    assert writes[-1]['config']['pricing']['cached_input'] == 0
    assert writes[-1]['config']['max_temperature'] == 0
    assert writes[-1]['config']['pricing']['output'] == 3


def test_dirty_guard_and_fresh_defaults(ui):
    page, _, _, _ = ui
    page.locator('.model-name', has_text='Alpha').click()
    page.locator('#model-name').fill('Changed')
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.keyboard.press('Escape')
    assert page.locator('#model-modal').is_visible()
    page.once('dialog', lambda dialog: dialog.accept())
    page.keyboard.press('Escape')
    page.get_by_role('button', name='添加模型').click()
    page.locator('[data-settings-page="messages"]').click()
    assert page.locator('#sanitize-recursive-schemas').count() == 0
    assert page.locator('#model-provider').input_value() == ''
    assert page.locator('#model-native-tools').input_value() == ''
    page.locator('[data-settings-page="reliability"]').click()
    assert page.locator('#img-compression-enabled').is_visible()
    assert page.locator('#auto-retry-enabled').is_visible()


def test_save_failure_keeps_form_and_allows_retry(ui):
    page, _, writes, failures = ui
    page.locator('.model-name', has_text='Alpha').click()
    page.locator('#model-name').fill('Renamed')
    failures['save'] = True
    page.locator('#model-save-btn').click()
    page.wait_for_function("!modelEditor.saving")
    assert page.locator('#model-modal').is_visible()
    assert page.locator('#model-name').input_value() == 'Renamed'
    assert page.locator('#model-save-btn').is_enabled()
    failures['save'] = False
    page.keyboard.press('Control+s')
    page.wait_for_selector('#model-modal', state='hidden')
    assert writes[-1]['old_model_name'] == 'Alpha'


def test_duplicate_copy_cannot_overwrite(ui):
    page, _, writes, _ = ui
    page.evaluate("openModelFromList('Alpha', true)")
    page.locator('#model-name').fill('Beta')
    page.locator('#model-save-btn').click()
    assert page.locator('#model-modal').is_visible()
    assert writes == []


def test_legacy_and_mobile_layout(ui):
    page, _, _, _ = ui
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('.model-name', has_text='Legacy').click()
    assert page.locator('#session-id').is_visible()
    assert page.locator('[data-settings-page]:visible').count() == 1
    page.locator('#config-type').select_option('direct_api')
    assert page.locator('[data-settings-page]:visible').count() == 6
    assert page.locator('#api-base-url').is_visible()
    page.locator('[data-settings-page="pricing"]').click()
    assert page.locator('#pricing-input').is_visible()
    assert page.evaluate("document.querySelector('#model-modal .modal-content').scrollWidth <= innerWidth")


def test_markup_preserves_unique_controls():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((ROOT / 'admin.html').read_text(encoding='utf-8'), 'html.parser')
    ids = [el['id'] for el in soup.select('[id]')]
    assert len(ids) == len(set(ids))
    assert len(soup.select('.model-settings-panel')) == 6


def test_archive_visible_selection_and_safe_rendering(ui):
    page, models, writes, _ = ui
    models['<img src=x onerror=alert(1)>'] = {'api_type': 'gemini_native'}
    page.get_by_role('button', name='刷新列表').click()
    page.wait_for_function("Object.keys(modelWorkspace.models).length === 5")
    assert page.locator('#models-list img').count() == 0
    assert page.locator('#models-list [data-model-config]').count() == 0
    page.locator('#models-query').fill('Alpha')
    page.locator('#select-all-active').check()
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('#archive-selected-btn').click()
    page.wait_for_timeout(100)
    assert writes[-1]['model_names'] == ['Alpha']


def test_visual_capture(ui, tmp_path):
    page, _, _, _ = ui
    import os
    destination = Path(os.environ.get('ADMIN_UI_SCREENSHOTS', tmp_path))
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / 'models-desktop.png'), full_page=True)
    page.locator('.model-name', has_text='Alpha').click()
    page.screenshot(path=str(destination / 'settings-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('[data-settings-page="generation"]').click()
    page.screenshot(path=str(destination / 'settings-mobile.png'))


def test_protocol_defaults_custom_endpoint_and_archived_save(ui):
    page, _, writes, _ = ui
    page.evaluate("openModelFromList('Archived')")
    assert page.locator('#endpoint-path').get_attribute('placeholder') == '/messages'
    page.locator('#api-type').select_option('responses_native')
    assert page.locator('#endpoint-path').get_attribute('placeholder') == '/responses'
    page.locator('#endpoint-path').fill('/custom/endpoint')
    page.locator('#api-type').select_option('direct_api')
    assert page.locator('#endpoint-path').input_value() == '/custom/endpoint'
    page.locator('#model-save-btn').click()
    page.wait_for_selector('#model-modal', state='hidden')
    assert writes[-1]['config']['archived'] is True
    assert writes[-1]['config']['endpoint_path'] == '/custom/endpoint'


def test_balance_rows_skip_empty_keys_and_escape_currency(ui):
    page, _, _, _ = ui
    page.locator('.model-name', has_text='Alpha').click()
    page.locator('#api-base-url').fill('https://api.deepseek.com')
    page.locator('.api-key-input').fill('')
    page.get_by_role('button', name='添加 API Key', exact=False).click()
    page.locator('.api-key-input').nth(1).fill('test-key')
    page.locator('#query-balance-btn').click()
    page.wait_for_selector('.key-balance-info:visible')
    rows = page.locator('#model-api-keys-list > div')
    assert not rows.nth(0).locator('.key-balance-info').is_visible()
    assert '12.50' in rows.nth(1).locator('.key-balance-info').inner_text()
    assert rows.locator('img').count() == 0


@pytest.mark.parametrize('width', [1440, 1024])
def test_square_spacious_navigation(ui, width):
    page, _, _, _ = ui
    page.set_viewport_size({'width': width, 'height': 1000})
    assert page.locator('.sidebar').bounding_box()['width'] >= 224
    for link in page.locator('.top-nav a').all():
        assert link.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert link.evaluate("el => getComputedStyle(el).whiteSpace") == 'nowrap'
        assert link.evaluate("el => getComputedStyle(el).borderRadius") == '0px'
    assert page.locator('#models > .card').evaluate("el => getComputedStyle(el).borderRadius") == '0px'


def test_native_playground_request_events_and_timings(ui):
    page, _, writes, _ = ui
    page.locator('[data-page="gateway-workspace"]').click()
    page.locator('#gw-tools input[value="web_search"]').check()
    page.locator('#gw-insert-tools').click()
    page.locator('#gw-run').click()
    page.wait_for_function("document.getElementById('gw-state').textContent.includes('传输结束')")
    assert writes[-1]['request']['tools'] == [{'type': 'web_search'}]
    assert 'response.completed' in page.locator('#gw-output').inner_text()
    assert page.locator('#gw-output img').count() == 0
    page.wait_for_function("document.getElementById('gw-timing').textContent.includes('0.110')")
    assert page.locator('#gw-session').input_value() == 'conversation'
    page.locator('#gw-prices').click()
    playwright.expect(page.locator('#gw-analysis').get_by_text('历史金额', exact=True)).to_be_visible()


def test_native_tool_model_configuration_survives_save(ui):
    page, _, writes, _ = ui
    page.locator('.model-name', has_text='Beta').click()
    page.locator('[data-settings-page="messages"]').click()
    page.locator('#model-provider').select_option('deepseek')
    page.locator('#model-native-tools').fill('web_search')
    page.locator('#model-native-tool-options').fill('{"web_search": {}}')
    page.locator('#model-save-btn').click()
    page.wait_for_selector('#model-modal', state='hidden')
    assert writes[-1]['config']['native_tools'] == ['web_search']
    assert writes[-1]['config']['provider'] == 'deepseek'
    assert writes[-1]['config']['sanitize_recursive_schemas'] is False


def test_monitor_large_detail_only_expands_on_demand(ui):
    page, _, _, _ = ui
    page.goto('http://bridge.test/monitor')
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<pre id=lazy-test>' + renderTruncatable('x'.repeat(100000) + '<img src=x>', 4000) + '</pre>')")
    assert len(page.locator('#lazy-test').inner_text()) < 4100
    page.locator('#lazy-test button').click()
    assert len(page.locator('#lazy-test').inner_text()) > 100000
    assert page.locator('#lazy-test img').count() == 0
    page.locator('#lazy-test button').click()
    assert len(page.locator('#lazy-test').inner_text()) < 4100
