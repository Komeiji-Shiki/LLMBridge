// 监控日志的筛选、分页和加载状态，与实时连接和详情渲染分开维护。
// 刷新日志
async function refreshLogs(automatic = false) {
    // 如果日志是隐藏的，不刷新
    if (!logsVisible || (automatic && logRefreshPaused)) {
        return;
    }

    if (currentTab === 'requests') {
        await refreshRequestLogs();
    } else {
        await refreshErrorLogs();
    }
}

// 当前日志限制
let currentLogLimit = 50;
let currentLogPage = 0;
let currentLogTotal = 0;
let searchDebounceTimer = null;
let logRefreshPaused = false;
const logQueryState = {key: null, pending: null, controller: null, modelsLoadedAt: 0};
const errorLogState = {key: null, pending: null, sequence: 0};

function logLoadState(message, failed = false) {
    const state = document.getElementById('log-load-state');
    state.textContent = message;
    state.classList.toggle('log-load-error', failed);
}

function toggleLogRefresh() {
    logRefreshPaused = !logRefreshPaused;
    const button = document.getElementById('log-refresh-toggle');
    button.textContent = logRefreshPaused ? '恢复自动刷新' : '暂停自动刷新';
    button.setAttribute('aria-pressed', String(logRefreshPaused));
    if (logRefreshPaused) logLoadState('自动刷新已暂停，仍可手动刷新和筛选。');
    else refreshLogs();
}

function resetLogFilters() {
    for (const name of ['model', 'status', 'search', 'from', 'to']) document.getElementById('filter-' + name).value = '';
    applyLogFilters();
}

// 模型目录附带在首次列表查询中，每分钟按需更新，并保留当前选择。
function updateLogModels(models) {
    const select = document.getElementById('filter-model'), previous = select.value;
    const values = [...new Set([...models, ...(previous ? [previous] : [])])];
    select.replaceChildren(new Option('全部模型', ''));
    for (const model of values) select.add(new Option(model, model));
    select.value = previous;
    logQueryState.modelsLoadedAt = Date.now();
}

// 改变日志显示数量
function changeLogLimit() {
    currentLogLimit = parseInt(document.getElementById('log-limit').value) || 50;
    currentLogPage = 0;
    refreshLogs();
}

// 应用筛选（防抖 300ms）
function applyLogFilters() {
    clearTimeout(searchDebounceTimer);
    ++_requestLogsSequence;
    logQueryState.controller?.abort();
    logQueryState.key = null;
    logQueryState.pending = null;
    document.getElementById('requests-tab').setAttribute('aria-busy', 'false');
    currentLogPage = 0;
    logLoadState('筛选条件已更改，正在等待查询…');
    searchDebounceTimer = setTimeout(() => {
        refreshLogs();
    }, 300);
}

// 翻页
function changePage(delta) {
    const maxPage = Math.max(0, Math.ceil(currentLogTotal / currentLogLimit) - 1);
    currentLogPage = Math.max(0, Math.min(currentLogPage + delta, maxPage));
    refreshLogs();
}

// 跳转到指定页（输入框回车/失焦触发）
function jumpToPage() {
    const input = document.getElementById('pagination-page-input');
    const totalPages = Math.max(1, Math.ceil(currentLogTotal / currentLogLimit));
    const page = Math.max(1, Math.min(parseInt(input.value, 10) || 1, totalPages));
    input.value = page;
    if (page - 1 === currentLogPage) return;
    currentLogPage = page - 1;
    refreshLogs();
}

function _renderPagination() {
    const totalPages = Math.max(1, Math.ceil(currentLogTotal / currentLogLimit));
    document.getElementById('log-pagination').style.display = currentLogTotal > currentLogLimit ? 'flex' : 'none';
    document.getElementById('pagination-prev').disabled = currentLogPage <= 0;
    document.getElementById('pagination-next').disabled = currentLogPage >= totalPages - 1;
    const pageInput = document.getElementById('pagination-page-input');
    pageInput.value = currentLogPage + 1;
    pageInput.max = totalPages;
    document.getElementById('pagination-total-pages').textContent = totalPages;
    document.getElementById('pagination-total-count').textContent = currentLogTotal.toLocaleString();
}

// 缓存命中率分档配色：低于 50% 红、50%~80% 黄、80%~90% 橙，90% 及以上保持绿色。
// 输入量不足 CACHE_COLOR_MIN_INPUT 的请求凑不满上游的最小缓存块，低命中属于正常，
// 不上色以免整列长期报警；命中数量与百分比仍然显示。
const CACHE_COLOR_MIN_INPUT = 10000;
function cacheRateColor(rate, input) {
    if (input < CACHE_COLOR_MIN_INPUT) return 'var(--text-dim)';
    if (rate < 50) return 'var(--danger)';
    if (rate < 80) return '#facc15';
    if (rate < 90) return '#f97316';
    return 'var(--ok)';
}

// 输入 token 单元格：主行是上游口径的输入量，副行是其中的缓存命中量与命中率。
// 个别上游的 cached_tokens 口径大于 input_tokens，命中率按输入封顶，精确值留在 title 里。
function renderInputTokens(log) {
    const input = Number(log.input_tokens) || 0;
    const cached = Number(log.cached_tokens) || 0;
    const main = input ? input.toLocaleString() : '-';
    if (!input || cached <= 0) return main;
    const rawRate = Math.min(cached, input) / input * 100;
    const rate = rawRate.toFixed(1);
    const title = `缓存命中 ${cached.toLocaleString()} / 输入 ${input.toLocaleString()}`;
    return `${main}<div style="font-size:11px;color:${cacheRateColor(rawRate, input)};opacity:.85;" title="${escapeHtml(title)}">${cached.toLocaleString()} (${rate}%)</div>`;
}

function renderOutputTokens(log) {
    const output = Number(log.output_tokens) || 0;
    const reasoning = Number(log.reasoning_tokens) || 0;
    const main = output ? output.toLocaleString() : '-';
    if (reasoning <= 0) return main;
    return `${main}<div style="font-size:11px;color:var(--text-dim);opacity:.85;">思考 ${reasoning.toLocaleString()}</div>`;
}

// 刷新请求日志
let _requestLogsSequence = 0;
function refreshRequestLogs() {
    const model = document.getElementById('filter-model').value;
    const status = document.getElementById('filter-status').value;
    const search = document.getElementById('filter-search').value.trim();
    const start = document.getElementById('filter-from').value;
    const end = document.getElementById('filter-to').value;
    if (start && end && start > end) {
        logLoadState('开始日期不能晚于结束日期。', true);
        return Promise.resolve();
    }
    const params = new URLSearchParams({limit: currentLogLimit, offset: currentLogPage * currentLogLimit,
        include_models: Date.now() - logQueryState.modelsLoadedAt > 60000});
    for (const [name, value] of Object.entries({model, status, search, start_date: start, end_date: end})) {
        if (value) params.set(name, value);
    }
    const key = params.toString();
    // 相同查询合并等待；筛选变更立即取消旧请求，旧结果和旧错误都不能覆盖页面。
    if (logQueryState.pending && logQueryState.key === key) return logQueryState.pending;
    logQueryState.controller?.abort();
    const controller = new AbortController(), sequence = ++_requestLogsSequence;
    logQueryState.controller = controller;
    logQueryState.key = key;
    logLoadState('正在读取请求日志…');
    document.getElementById('requests-tab').setAttribute('aria-busy', 'true');
    logQueryState.pending = (async () => {
      try {
        const response = await apiGet(`/api/monitor/logs/requests/query?${params}`, {signal: controller.signal});
        const data = await response.json();
        if (sequence !== _requestLogsSequence) return;
        if (Array.isArray(data.models)) updateLogModels(data.models);
        const lastPage = Math.max(0, Math.ceil((data.total || 0) / currentLogLimit) - 1);
        if (currentLogPage > lastPage) {
            currentLogPage = lastPage;
            return refreshRequestLogs();
        }
        if (currentTab === 'requests') logLoadState(data.notice || (logRefreshPaused ? '自动刷新已暂停。' : '最近更新 ' + new Date().toLocaleTimeString()));
        const logs = data.items || [];
        const exRate = data.exchange_rate || { USD_TO_CNY: 7.2, CNY_TO_USD: 1 / 7.2 };
        currentLogTotal = data.total || 0;

        // 更新计数
        document.getElementById('request-count').textContent = `(${currentLogTotal})`;
        document.getElementById('filter-total').textContent = currentLogTotal ? `共 ${currentLogTotal} 条` : '';
        _renderPagination();

        const tbody = document.getElementById('request-logs');

        if (logs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="11" style="text-align: center;">暂无匹配的请求日志</td></tr>';
            MonitorCompare.updateCompareButton();
            return;
        }

        tbody.innerHTML = logs.map(log => {
            const time = new Date(log.timestamp * 1000).toLocaleString();
            const statusClass = log.status === 'success' ? 'success' : 'failed';
            const stopReason = log.stop_reason || (log.cost_info && log.cost_info.stop_reason);
            const duration = log.duration ? formatDuration(log.duration) : '-';
            const inTokens = renderInputTokens(log);
            const outTokens = renderOutputTokens(log);
            // 按记录自身的货币显示符号，CNY 计价的费用不再被误标成美元
            const costCurrency = log.currency || 'USD';
            const costSymbol = costCurrency === 'CNY' ? '¥' : '$';
            const costVal = log.total_cost != null ? Number(log.total_cost).toFixed(6) : '';
            const costDisplay = costVal ? costSymbol + costVal : '-';
            const costTitle = (costVal && costCurrency === 'CNY')
                ? `${costVal} CNY ≈ $${(Number(costVal) * exRate.CNY_TO_USD).toFixed(6)} USD`
                : (costVal ? `${costVal} ${costCurrency}` : '');

            // 🔧 显示思维链/工具调用标记
            let featureBadges = '';
            if (log.has_reasoning || log.reasoning_content) {
                featureBadges += '<span style="display:inline-block;padding:1px 5px;background:rgb(var(--accent-rgb) / 0.15);color:var(--accent-hover);border-radius:0;font-size:10px;margin-right:3px;" title="含思维链内容" role="img" aria-label="含思维链内容"><svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#message"/></svg></span>';
            }
            const hasRequestToolCalls = log.request_messages && log.request_messages.some(m => m.tool_calls?.length);
            const hasResponseToolCalls = Boolean(
                log.has_tool_calls || log.response_tool_calls?.length || log.response_message?.tool_calls?.length
            );
            if (hasRequestToolCalls || hasResponseToolCalls) {
                featureBadges += '<span style="display:inline-block;padding:1px 5px;background:rgb(var(--warn-rgb) / 0.15);color:var(--warn);border-radius:0;font-size:10px;" title="含工具调用" role="img" aria-label="含工具调用"><svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#sliders"/></svg></span>';
            }

            return `
                <tr>
                    <td><input type="checkbox" class="compare-check" aria-label="加入缓存对比" data-request-id="${escapeHtml(log.request_id || '')}" onchange="MonitorCompare.toggleCompareSelection(this.dataset.requestId, this.checked)" ${log.request_id && MonitorCompare.has(log.request_id) ? 'checked' : ''} ${log.request_id ? '' : 'disabled title="该记录无请求ID"'}></td>
                    <td>${time}</td>
                    <td style="font-family: monospace; font-size: 12px;">${escapeHtml(log.request_id?.substring(0, 8) || 'N/A')}...</td>
                    <td>${escapeHtml(log.model)}${featureBadges ? ' ' + featureBadges : ''}<div style="font-size:11px;opacity:.7" title="${escapeHtml(log.caller_id || '')}">${escapeHtml(log.caller_name || '历史未归属')}</div></td>
                    <td><span class="status-badge ${statusClass}">${escapeHtml(log.status)}</span>${renderFilteredBadge(stopReason)}</td>
                    <td title="${escapeHtml(renderPhaseTimings(log.timings))}">${duration}${log.timings?.first_business_ms != null ? `<div style="font-size:11px;opacity:.7">首事件 ${(Number(log.timings.first_business_ms) / 1000).toFixed(2)}s</div>` : ""}</td>
                    <td>${inTokens}</td>
                    <td>${outTokens}</td>
                    <td style="white-space: nowrap;">${formatStopReason(stopReason)}</td>
                    <td style="font-family: monospace; font-size: 11px;" title="${escapeHtml(costTitle)}">${escapeHtml(costDisplay)}</td>
                    <td>
                        <button class="detail-btn" data-request-id="${escapeHtml(log.request_id || '')}" onclick="viewRequestDetails(this.dataset.requestId)">查看详细</button>
                    </td>
                </tr>
            `;
        }).join('');
        MonitorCompare.updateCompareButton();

      } catch (error) {
        if (sequence === _requestLogsSequence && currentTab === 'requests' && error.name !== 'AbortError') {
            logLoadState('读取请求日志失败：' + error.message + '。可以点击刷新重试。', true);
        }
      } finally {
        if (sequence === _requestLogsSequence) {
            logQueryState.pending = null;
            document.getElementById('requests-tab').setAttribute('aria-busy', 'false');
        }
      }
    })();
    return logQueryState.pending;
}

// 刷新错误日志
function refreshErrorLogs() {
    const limit = Math.min(currentLogLimit, 100);
    if (errorLogState.pending && errorLogState.key === limit) return errorLogState.pending;
    const sequence = ++errorLogState.sequence;
    errorLogState.key = limit;
    logLoadState('正在读取错误日志…');
    errorLogState.pending = (async () => {
      try {
        const response = await apiGet(`/api/monitor/logs/errors?limit=${limit}`);
        const logs = await response.json();
        if (sequence !== errorLogState.sequence || currentTab !== 'errors') return;
        logLoadState(logRefreshPaused ? '自动刷新已暂停。' : '最近更新 ' + new Date().toLocaleTimeString());

        // 更新计数显示
        var badge = document.getElementById('error-log-count');
        if (badge) badge.textContent = '(' + logs.length + ')';

        const container = document.getElementById('error-logs');

        if (logs.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无错误日志</div>';
            return;
        }

        container.innerHTML = logs.map(log => {
            const time = new Date(log.timestamp * 1000).toLocaleString();

            return `
                <div class="error-log">
                    <div class="error-message">${escapeHtml(log.error)}</div>
                    <div class="error-time">${time} - 模型: ${escapeHtml(log.model)} - 请求ID: ${escapeHtml(log.request_id || 'N/A')}</div>
                </div>
            `;
        }).join('');

      } catch (error) {
        if (sequence === errorLogState.sequence && currentTab === 'errors') logLoadState('读取错误日志失败：' + error.message, true);
      } finally {
        if (sequence === errorLogState.sequence) errorLogState.pending = null;
      }
    })();
    return errorLogState.pending;
}
