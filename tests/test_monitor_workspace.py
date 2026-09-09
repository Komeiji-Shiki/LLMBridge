"""Real Chromium regressions with local assets and entirely simulated request logs."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from utils.prompt_cache_compare import compare

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('stale_failure', [False, True])
def test_log_queries_coalesce_and_ignore_cancelled_results(monitor_ui, stale_failure):
    page, _ = monitor_ui
    result = page.evaluate('''async staleFailure => {
        let resolveOld, rejectOld, resolveNew;
        const signals = [], pending = [new Promise((resolve, reject) => {resolveOld = resolve; rejectOld = reject;}),
            new Promise(resolve => {resolveNew = resolve;})];
        apiGet = (url, options) => {signals.push(options.signal); return pending.shift();};
        document.getElementById('filter-search').value = 'old';
        const first = refreshRequestLogs(), duplicate = refreshRequestLogs();
        document.getElementById('filter-search').value = 'new';
        const second = refreshRequestLogs();
        resolveNew({json: async () => ({total: 0, items: [], notice: 'CURRENT RESULT'})});
        await second;
        if (staleFailure) rejectOld(new Error('STALE FAILURE'));
        else resolveOld({json: async () => ({total: 0, items: [], notice: 'STALE RESULT'})});
        await first;
        return {calls: signals.length, same: first === duplicate, cancelled: signals[0].aborted,
            status: document.getElementById('log-load-state').textContent};
    }''', stale_failure)
    assert result == {'calls': 2, 'same': True, 'cancelled': True, 'status': 'CURRENT RESULT'}


def test_log_date_filter_pause_errors_and_summary_badges(monitor_ui):
    page, logs = monitor_ui
    queries = []
    logs[0]['has_reasoning'] = True
    logs[0]['has_tool_calls'] = True
    def query(route):
        queries.append(parse_qs(urlparse(route.request.url).query))
        route.fulfill(json={'items': logs, 'total': len(logs)})
    page.route('**/api/monitor/logs/requests/query?*', query)
    page.locator('#filter-from').fill('2026-09-09')
    page.locator('#filter-to').fill('2026-09-09')
    page.get_by_role('button', name='筛选', exact=True).click()
    page.wait_for_function("document.getElementById('log-load-state').textContent.startsWith('最近更新')")
    assert queries[-1]['start_date'] == queries[-1]['end_date'] == ['2026-09-09']
    assert queries[-1]['include_models'] == ['false']
    assert page.locator('#request-logs [title="含思维链内容"]').count() == 1
    assert page.locator('#request-logs [title="含工具调用"]').count() == 1
    page.locator('#log-refresh-toggle').click()
    previous = len(queries)
    page.evaluate('refreshLogs(true)')
    assert len(queries) == previous
    page.get_by_role('button', name='刷新日志', exact=True).click()
    page.wait_for_function("document.getElementById('log-load-state').textContent === '自动刷新已暂停。'")
    assert len(queries) > previous
    page.route('**/api/monitor/logs/requests/query?*', lambda route: route.fulfill(status=503, body='temporarily unavailable'))
    page.get_by_role('button', name='刷新日志', exact=True).click()
    page.wait_for_selector('#log-load-state.log-load-error')
    assert '503' in page.locator('#log-load-state').inner_text()
    assert page.locator('.compare-check').count() == len(logs)
    page.set_viewport_size({'width': 390, 'height': 900})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_log_validation_clears_loading_and_hidden_tab_cannot_replace_status(monitor_ui):
    page, _ = monitor_ui
    result = page.evaluate('''async () => {
        let finish;
        apiGet = () => new Promise(resolve => {finish = resolve;});
        document.getElementById('filter-search').value = 'request';
        const pending = refreshRequestLogs();
        currentTab = 'errors';
        logLoadState('ERROR LOG STATUS');
        finish({json: async () => ({total: 0, items: [], notice: 'REQUEST STATUS'})});
        await pending;
        const status = document.getElementById('log-load-state').textContent;
        currentTab = 'requests';
        document.getElementById('filter-from').value = '2026-09-10';
        document.getElementById('filter-to').value = '2026-09-09';
        applyLogFilters();
        clearTimeout(searchDebounceTimer);
        await refreshRequestLogs();
        return {status, busy: document.getElementById('requests-tab').getAttribute('aria-busy'),
            error: document.getElementById('log-load-state').textContent};
    }''')
    assert result['status'] == 'ERROR LOG STATUS'
    assert result['busy'] == 'false' and '开始日期不能晚于结束日期' in result['error']


@pytest.fixture
def monitor_ui():
    logs = [
        {'request_id': f'request-{index}-abcdef', 'model': 'Research / reasoning-model',
         'status': 'success', 'timestamp': 1700000000 + index * 70,
         'duration': 12, 'input_tokens': 8192, 'output_tokens': 384,
         'cached_tokens': 6144 if index == 1 else 1024, 'currency': 'USD', 'total_cost': .024,
         'caller_name': '研究客户端', 'caller_id': 'caller-a', 'conversation_id': 'session-a',
         'request_messages': [{'role': 'user', 'content': '分析这份文档的要点。'}],
         'response_message': {'role': 'assistant', 'content': '这是测试环境中的模拟回答。'},
         'tools': [], 'timings': {'first_business_ms': 2400, 'output_ms': 9600, 'total_ms': 12000}}
        for index in range(1, 4)
    ]
    errors = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('window.WebSocket = class {}; window.setInterval = () => 0;')

        def route(request_route):
            parsed = urlparse(request_route.request.url)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == '/api/monitor/logs/requests/query':
                return request_route.fulfill(json={'items': logs, 'total': len(logs), 'models': ['Research / reasoning-model']})
            if path == '/api/monitor/compare':
                old, new = [next(log for log in logs if log['request_id'] == query[key][0]) for key in ('a', 'b')]
                old, new = sorted((old, new), key=lambda log: log['timestamp'])
                return request_route.fulfill(json=compare(old, new))
            if path.startswith('/api/request/'):
                log = next(log for log in logs if log['request_id'] == unquote(path.split('/')[-1]))
                return request_route.fulfill(json=log)
            if path == '/api/monitor/stats':
                return request_route.fulfill(json={'stats': {'active_requests': 0, 'total_requests': 1248,
                    'successful_requests': 1224, 'avg_duration': 12, 'failed_requests': 24, 'uptime': 19200},
                    'active_requests': [], 'model_stats': [], 'browser_connected': False,
                    'mode': {'api_mode': 'direct'}})
            if path.startswith('/api/'):
                return request_route.fulfill(json={})
            relative = 'monitor.html' if path == '/monitor' else path.lstrip('/')
            asset = (ROOT / relative).resolve()
            if asset.is_relative_to(ROOT) and asset.is_file() and (relative == 'monitor.html' or relative.startswith(('js/', 'css/'))):
                return request_route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset)[0] or 'text/plain')
            request_route.fulfill(status=404, body='Not found')

        page.route('**/*', route)
        page.goto('http://bridge.test/monitor')
        page.wait_for_selector('.compare-check')
        yield page, logs
        browser.close()
    assert not errors, errors


def test_detail_replacement_syncs_selection_and_focus(monitor_ui):
    page, logs = monitor_ui
    page.locator('.compare-check').nth(0).check()
    page.locator('.compare-check').nth(1).check()
    page.locator('#request-logs .detail-btn').nth(2).click()
    page.locator('#modalBody button', has_text='加入对比选择').click()
    page.wait_for_selector('#compareBody .compare-conclusion')
    assert page.locator('.compare-check:checked').count() == 2
    assert not page.locator('.compare-check').first.is_checked()
    assert page.locator('#compare-slots .compare-slot').count() == 2
    assert page.locator('#compareModal .close').evaluate('(el) => el === document.activeElement')
    page.keyboard.press('Escape')
    assert not page.locator('#compareModal').is_visible()
    assert page.locator('#compare-btn').evaluate('(el) => el === document.activeElement')
    page.locator('#compare-select-all').uncheck()
    assert page.locator('.compare-check:checked').count() == 0
    assert page.locator('#compare-btn').is_disabled()


@pytest.mark.parametrize('stale_failure', [False, True])
def test_compare_ignores_stale_success_and_failure(monitor_ui, stale_failure):
    page, _ = monitor_ui
    result = page.evaluate('''async staleFailure => {
        let resolveOld, rejectOld, resolveNew;
        const pending = [new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; }),
            new Promise(resolve => { resolveNew = resolve; })];
        apiGet = () => pending.shift();
        const old = MonitorCompare.openCompareModal('old-a', 'old-b');
        MonitorCompare.closeCompareModal();
        const newer = MonitorCompare.openCompareModal('new-a', 'new-b');
        resolveNew({json: async () => ({models_match: true, inference: ['CURRENT RESULT']})});
        await newer;
        if (staleFailure) rejectOld(new Error('STALE FAILURE'));
        else resolveOld({json: async () => ({inference: ['STALE RESULT']})});
        await old;
        return document.getElementById('compareBody').textContent;
    }''', stale_failure)
    assert 'CURRENT RESULT' in result
    assert 'STALE' not in result


def test_selection_survives_refresh_and_can_be_removed_off_page(monitor_ui):
    page, _ = monitor_ui
    page.locator('.compare-check').first.check()
    assert page.locator('#compare-select-all').evaluate('(el) => el.indeterminate')
    page.evaluate('refreshRequestLogs()')
    assert page.locator('.compare-check').first.is_checked()
    page.evaluate('''async () => {
        apiGet = async () => ({json: async () => ({items: [], total: 0})});
        await refreshRequestLogs();
    }''')
    assert '筛选范围外' in page.locator('#compare-hint').inner_text()
    page.get_by_role('button', name='移除已选请求 1').click()
    assert page.locator('#compare-clear').is_disabled()
    page.evaluate("switchTab('errors')")
    assert not page.locator('#compare-toolbar').is_visible()


def test_untrusted_request_id_and_unicode_difference_render_safely(monitor_ui):
    page, logs = monitor_ui
    logs[0]['request_id'] = "quote');window.injected=true;//"
    logs[0]['request_messages'][0]['content'] = '😺' * 100 + '旧内容'
    logs[1]['request_messages'][0]['content'] = '😺' * 100 + '新内容'
    page.evaluate('refreshRequestLogs()')
    page.locator('#request-logs .detail-btn').first.click()
    page.locator('#modalBody button', has_text='加入对比选择').click()
    assert page.evaluate('MonitorCompare.count') == 1
    assert not page.evaluate('Boolean(window.injected)')
    page.keyboard.press('Escape')
    page.locator('.compare-check').nth(1).check()
    page.locator('#compare-btn').click()
    page.wait_for_selector('.diff-mark')
    assert page.locator('.compare-context').first.evaluate('(el) => el.querySelector(".diff-mark").nextSibling.textContent').startswith('旧内容')


@pytest.mark.parametrize('width', [1440, 390])
def test_compare_layout_and_nonstreaming_speed_note(monitor_ui, width):
    page, logs = monitor_ui
    page.set_viewport_size({'width': width, 'height': 900})
    logs[0]['streaming'] = False
    logs[0]['timings']['output_ms'] = 2000
    logs[0]['model'] = 'very-long-model-name-' * 20
    page.locator('#compare-select-all').check()
    page.locator('#compare-btn').click()
    page.wait_for_selector('.compare-tps')
    assert '非流式响应无法测得解码速度' in page.locator('#compareBody').inner_text()
    assert page.locator('#compareModal .modal-content').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('#compareModal .modal-content').evaluate('(el) => getComputedStyle(el).borderTopLeftRadius') == '0px'
    if width < 760:
        assert page.locator('.stats-grid').evaluate('(el) => getComputedStyle(el).gridTemplateColumns.split(" ").length') == 2
    page.locator('#compareModal .close').focus()
    page.keyboard.press('Shift+Tab')
    assert page.locator('#compareModal').evaluate('(el) => el.contains(document.activeElement)')
