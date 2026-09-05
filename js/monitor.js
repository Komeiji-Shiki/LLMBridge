/**
 * monitor.js - 监控面板前端逻辑
 * 🔧 重构：从 monitor.html 内嵌 <script> 拆分而来，行为保持不变
 */
let ws = null;
let currentTab = 'requests';
let browserConnected = false;
let currentMode = null;

// 显示/隐藏状态变量
let modelStatsVisible = false;  // 模型统计默认隐藏
let logsVisible = true;  // 🔧 日志默认显示
let lastModelStats = [];  // 保存最后的模型统计数据

// 页面是否处于"值得刷新"的状态。
// 监控页会被 admin 以 iframe 内嵌，切到其它页时父页面把它 display:none ——
// 此时 iframe 里的 setInterval 依然全速运行，最快 1 秒一次地打接口。
// display:none 的 iframe 其 innerHeight 为 0，据此和标签页可见性一起判断。
function isPageActive() {
    if (document.hidden) return false;
    if (window.innerHeight === 0 || window.innerWidth === 0) return false;
    return true;
}

// 🔧 会话失效检测：所有 fetch 统一走 apiGet 封装。
// 旧版六处裸 fetch 不检查 response.ok，会话失效后监控页显示字面量
// "undefined" 和全空数据，且从不提示重新登录。
let _sessionInvalid = false;

async function apiGet(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
        if (resp.status === 401 || resp.status === 403) {
            if (!_sessionInvalid) {
                _sessionInvalid = true;
                const statusEl = document.getElementById('ws-status-text');
                if (statusEl) {
                    statusEl.textContent = '会话已失效';
                    statusEl.style.color = '#ef4444';
                }
                const banner = document.createElement('div');
                banner.style.cssText = 'background:#7f1d1d;color:#fca5a5;padding:10px 20px;text-align:center;font-size:0.85rem;';
                banner.innerHTML = `会话已失效，<a href="/login?next=/monitor" style="color:#fff;text-decoration:underline;">点此重新登录</a>`;
                const header = document.querySelector('header') || document.body;
                header.insertAdjacentElement('afterend', banner);
            }
            throw new Error('HTTP ' + resp.status + ' — 会话已失效');
        }
        const text = await resp.text().catch(function() { return ''; });
        throw new Error('HTTP ' + resp.status + ': ' + text.substring(0, 200));
    }
    return resp;
}

// 连接WebSocket
let _wsReconnectTimer = null;
let _wsRetryDelay = 5000;          // 断线重连退避：5s 起，最长 60s
const _WS_RETRY_MAX = 60000;

function connectWebSocket() {
    if (_wsReconnectTimer) {
        clearTimeout(_wsReconnectTimer);
        _wsReconnectTimer = null;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws/monitor`);

    ws.onopen = () => {
        // 静默连接，不输出日志
        _wsRetryDelay = 5000;   // 连上了就把退避重置回最小值
        _sessionInvalid = false;
        updateConnectionStatus(true);
    };

    ws.onmessage = (event) => {
        try {
            handleWebSocketMessage(JSON.parse(event.data));
        } catch (e) {
            // 单条坏消息不该让整个 onmessage 处理器炸掉
            console.warn('监控消息解析失败:', e);
        }
    };

    ws.onclose = (event) => {
        // 静默断开，不输出日志
        updateConnectionStatus(false);
        // 🔧 修复：旧版只判 code === 1008，但浏览器在 WebSocket 握手中被服务端拒绝时
        // 永远得不到服务端送出的 1008 —— 握手被拒后浏览器拿到的 code 恒为 1006
        // （CLOSE_ABNORMAL）。对所有非 1000 的关闭做 /auth/check 探测会话有效性。
        const code = event ? event.code : 1006;
        if (code !== 1000) {
            fetch('/auth/check').then(function(r) { return r.json(); }).then(function(d) {
                if (!d.authenticated) {
                    const text = document.getElementById('ws-status-text');
                    if (text) { text.textContent = '需要登录'; text.style.color = '#ef4444'; }
                    const banner = document.createElement('div');
                    banner.style.cssText = 'background:#7f1d1d;color:#fca5a5;padding:10px 20px;text-align:center;font-size:0.85rem;';
                    banner.innerHTML = `会话已失效，<a href="/login?next=/monitor" style="color:#fff;text-decoration:underline;">点此重新登录</a>`;
                    var header = document.querySelector('header') || document.body;
                    header.insertAdjacentElement('afterend', banner);
                    return;  // 不再重连
                }
                scheduleReconnect();
            }).catch(function() {
                scheduleReconnect();
            });
            return;
        }
        scheduleReconnect();
    };
    function scheduleReconnect() {
        if (_wsReconnectTimer) return;
        _wsReconnectTimer = setTimeout(() => {
            _wsReconnectTimer = null;
            connectWebSocket();
        }, _wsRetryDelay);
        _wsRetryDelay = Math.min(_wsRetryDelay * 2, _WS_RETRY_MAX);
    }

    ws.onerror = (error) => {
        // 只在开发环境输出错误
        if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
            console.error('WebSocket错误:', error);
        }
    };
}

// 日志刷新节流：密集的 request_end 事件合并成一次查询
let _logsRefreshTimer = null;
const _LOGS_REFRESH_DEBOUNCE_MS = 1500;

function scheduleLogsRefresh() {
    if (_logsRefreshTimer) return;
    _logsRefreshTimer = setTimeout(() => {
        _logsRefreshTimer = null;
        if (isPageActive()) refreshLogs();
    }, _LOGS_REFRESH_DEBOUNCE_MS);
}

// 处理WebSocket消息
function handleWebSocketMessage(data) {
    switch(data.type) {
        case 'initial_data':
            updateAllData(data);
            break;
        case 'request_start':
            addActiveRequest(data);
            break;
        case 'request_end':
            removeActiveRequest(data.request_id);
            // 🔧 节流：旧版每收到一个 request_end 就全量刷一次日志。
            // 并发稍高时（几十个请求同时结束）会瞬间打出几十次日志查询，
            // 每次都要走 SQLite COUNT + 分页查询，把监控自己变成负载源。
            scheduleLogsRefresh();
            break;
        case 'stats_update':
            updateStats(data.stats);
            // 如果WebSocket消息包含模型统计，也更新它
            if (data.model_stats) {
                updateModelStats(data.model_stats);
            }
            break;
        case 'browser_status':
            updateBrowserStatus(data.connected);
            break;
        case 'tab_connection':
            // 标签页连接状态变化时刷新
            refreshTabConnections();
            break;
    }
}

// 更新连接状态
function updateConnectionStatus(connected) {
    const dot = document.getElementById('ws-status');
    const text = document.getElementById('ws-status-text');

    if (connected) {
        dot.classList.add('connected');
        text.textContent = '已连接';
    } else {
        dot.classList.remove('connected');
        text.textContent = '未连接';
    }
}

// 更新浏览器状态
function updateBrowserStatus(connected) {
    browserConnected = connected;
    document.getElementById('browser-status').textContent =
        connected ? '油猴脚本已连接' : '油猴脚本未连接';
}

// 更新所有数据
function updateAllData(data) {
    if (data.stats) {
        updateStats(data.stats);
    }
    if (data.active_requests) {
        updateActiveRequests(data.active_requests);
    }
    if (data.mode) {
        updateMode(data.mode);
    }
    refreshStats();
    refreshLogs();
}

// 更新模式显示
function updateMode(modeData) {
    currentMode = modeData;
    const modeElement = document.getElementById('current-mode');
    const detailsElement = document.getElementById('mode-details');

    if (modeData) {
        modeElement.textContent = modeData.mode || 'direct_chat';
        if (modeData.mode === 'battle') {
            detailsElement.textContent = `Battle 目标: ${modeData.target || 'A'}`;
        } else {
            detailsElement.textContent = 'Direct Chat 模式';
        }
    }
}

// 格式化时间
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '0s';
    if (seconds < 1) return (seconds * 1000).toFixed(0) + 'ms';
    if (seconds < 60) return seconds.toFixed(1) + 's';
    if (seconds < 3600) {
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return secs > 0 ? `${mins}m${secs}s` : `${mins}m`;
    }
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return mins > 0 ? `${hours}h${mins}m` : `${hours}h`;
}

// 格式化数字
function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

// 更新统计数据
function updateStats(stats) {
    if (!stats) return;

    document.getElementById('active-requests').textContent = stats.active_requests || 0;
    // 总请求数显示具体数字，不使用K/M格式化
    document.getElementById('total-requests').textContent = (stats.total_requests || 0).toLocaleString();
    document.getElementById('avg-duration').textContent = formatDuration(stats.avg_duration || 0);
    document.getElementById('error-count').textContent = stats.failed_requests || 0;
    document.getElementById('uptime').textContent = formatDuration(stats.uptime || 0);

    const successRate = stats.total_requests > 0
        ? ((stats.successful_requests / stats.total_requests) * 100).toFixed(1)
        : 0;
    document.getElementById('success-rate-text').textContent = `成功率: ${successRate}%`;

    if (stats.uptime) {
        document.getElementById('server-info').textContent =
            `服务器运行时间: ${formatDuration(stats.uptime)}`;
    }
}

// 更新活跃请求列表
let _activeRequestSignature = '';
function updateActiveRequests(requests) {
    const container = document.getElementById('active-requests-list');
    const signature = JSON.stringify(requests || []);
    if (signature === _activeRequestSignature) {
        container.querySelectorAll('[data-started]').forEach(node => {
            node.textContent = Math.max(0, Date.now() / 1000 - Number(node.dataset.started)).toFixed(1) + 's';
        });
        return;
    }
    _activeRequestSignature = signature;

    if (!requests || requests.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无活跃请求</div>';
        return;
    }

    container.innerHTML = requests.map(req => {
        const duration = ((Date.now() / 1000) - req.timestamp).toFixed(1);
        return `
            <div class="active-request">
                <div class="request-info">
                    <div class="request-id">${escapeHtml(req.request_id.substring(0, 8))}...</div>
                    <div class="request-model">${escapeHtml(req.model)}
                        ${req.mode ? `<span class="mode-indicator">${escapeHtml(req.mode)}</span>` : ''}
                    </div>
                </div>
                <div class="request-duration" data-started="${Number(req.timestamp)}">${duration}s</div>
                <span class="status-badge active">处理中</span>
            </div>
        `;
    }).join('');
}

// 活跃请求刷新节流：密集的 request_start/request_end 合并为一次查询
let _activeRefreshTimer = null;
const _ACTIVE_REFRESH_DEBOUNCE_MS = 500;

function scheduleActiveRefresh() {
    if (_activeRefreshTimer) return;
    _activeRefreshTimer = setTimeout(() => {
        _activeRefreshTimer = null;
        if (isPageActive()) refreshActiveRequests();
    }, _ACTIVE_REFRESH_DEBOUNCE_MS);
}

// 添加活跃请求
function addActiveRequest(data) {
    const count = parseInt(document.getElementById('active-requests').textContent) + 1;
    document.getElementById('active-requests').textContent = count;
    scheduleActiveRefresh();
}

// 移除活跃请求
function removeActiveRequest(requestId) {
    const count = Math.max(0, parseInt(document.getElementById('active-requests').textContent) - 1);
    document.getElementById('active-requests').textContent = count;
    scheduleActiveRefresh();
}

// 刷新统计数据
async function refreshStats() {
    try {
        const response = await apiGet('/api/monitor/stats');
        const data = await response.json();

        updateStats(data.stats);
        updateModelStats(data.model_stats);
        updateBrowserStatus(data.browser_connected);

        if (data.mode) {
            updateMode(data.mode);
        }
    } catch (error) {
        console.error('获取统计数据失败:', error);
    }
}

// 存储上一次的模型统计数据用于比较
let lastModelStatsJSON = '';

// 更新模型统计 - 优化版本，只在数据变化时更新DOM
function updateModelStats(modelStats) {
    // 保存数据供显示时使用
    lastModelStats = modelStats || [];

    // 如果模型统计是隐藏的，不更新DOM
    if (!modelStatsVisible) {
        return;
    }

    const container = document.getElementById('model-stats-list');

    // 检查数据是否真的发生了变化
    const currentStatsJSON = JSON.stringify(modelStats);
    const dataChanged = currentStatsJSON !== lastModelStatsJSON;

    // 如果数据没有变化，直接返回，避免重复渲染
    if (!dataChanged) {
        return;
    }

    lastModelStatsJSON = currentStatsJSON;

    if (!modelStats || modelStats.length === 0) {
        container.innerHTML = '<div class="empty-state" style="grid-column: 1 / -1;">暂无模型使用数据</div>';
        return;
    }

    // 清空容器并重新填充
    container.innerHTML = '';

    // 为每个模型创建卡片
    modelStats.forEach(stat => {
        const successRate = stat.success_rate || 0;
        const avgDuration = stat.avg_duration || 0;
        const modelName = stat.model || 'unknown';

        const card = document.createElement('div');
        card.className = 'model-card';
        card.innerHTML = `
            <div class="model-name" title="${escapeHtml(modelName)}">${escapeHtml(modelName)}</div>
            <div class="model-stats">
                <div>
                    <span class="model-stat-label">总请求:</span>
                    <span class="model-stat-value">${formatNumber(stat.total_requests || 0)}</span>
                </div>
                <div>
                    <span class="model-stat-label">成功率:</span>
                    <span class="model-stat-value">${successRate.toFixed(1)}%</span>
                </div>
                <div>
                    <span class="model-stat-label">平均耗时:</span>
                    <span class="model-stat-value">${formatDuration(avgDuration)}</span>
                </div>
                <div>
                    <span class="model-stat-label">失败数:</span>
                    <span class="model-stat-value">${stat.failed_requests || 0}</span>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// 刷新日志
async function refreshLogs() {
    // 如果日志是隐藏的，不刷新
    if (!logsVisible) {
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

// 加载模型下拉列表
async function loadModelFilter() {
    try {
        const resp = await apiGet('/api/monitor/logs/requests/query?limit=1&offset=0');
        const data = await resp.json();
        const models = data.models || [];
        const sel = document.getElementById('filter-model');
        while (sel.options.length > 1) sel.remove(1);
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            sel.appendChild(opt);
        });
    } catch (e) {
        console.error('加载模型列表失败:', e);
    }
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
    searchDebounceTimer = setTimeout(() => {
        currentLogPage = 0;
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

// 刷新请求日志
let _requestLogsSequence = 0;
async function refreshRequestLogs() {
    const sequence = ++_requestLogsSequence;
    try {
        const model = document.getElementById('filter-model')?.value || '';
        const status = document.getElementById('filter-status')?.value || '';
        const search = document.getElementById('filter-search')?.value.trim() || '';
        const limit = currentLogLimit;
        const offset = currentLogPage * limit;
        const params = new URLSearchParams({ limit, offset });
        if (model) params.set('model', model);
        if (status) params.set('status', status);
        if (search) params.set('search', search);

        const response = await apiGet(`/api/monitor/logs/requests/query?${params}`);
        const data = await response.json();
        if (sequence !== _requestLogsSequence) return;
        const logs = data.items || [];
        const exRate = data.exchange_rate || { USD_TO_CNY: 7.2, CNY_TO_USD: 1 / 7.2 };
        currentLogTotal = data.total || 0;

        // 更新计数
        document.getElementById('request-count').textContent = `(${currentLogTotal})`;
        document.getElementById('filter-total').textContent = currentLogTotal ? `共 ${currentLogTotal} 条` : '';
        _renderPagination();

        const tbody = document.getElementById('request-logs');

        if (logs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" style="text-align: center;">暂无匹配的请求日志</td></tr>';
            return;
        }

        tbody.innerHTML = logs.map(log => {
            const time = new Date(log.timestamp * 1000).toLocaleString();
            const statusClass = log.status === 'success' ? 'success' : 'failed';
            const duration = log.duration ? formatDuration(log.duration) : '-';
            const inTokens = log.input_tokens ? log.input_tokens.toLocaleString() : '-';
            const outTokens = log.output_tokens ? log.output_tokens.toLocaleString() : '-';
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
            if (log.reasoning_content) {
                featureBadges += '<span style="display:inline-block;padding:1px 5px;background:rgba(42,168,255,0.15);color:#7dd3fc;border-radius:3px;font-size:10px;margin-right:3px;" title="含思维链内容">🧠</span>';
            }
            const hasRequestToolCalls = log.request_messages && log.request_messages.some(m => m.tool_calls);
            const hasResponseToolCalls = Boolean(
                log.response_tool_calls || (log.response_message && log.response_message.tool_calls)
            );
            if (hasRequestToolCalls || hasResponseToolCalls) {
                featureBadges += '<span style="display:inline-block;padding:1px 5px;background:rgba(245,158,11,0.15);color:#fcd34d;border-radius:3px;font-size:10px;" title="含工具调用">🔧</span>';
            }

            return `
                <tr>
                    <td>${time}</td>
                    <td style="font-family: monospace; font-size: 12px;">${escapeHtml(log.request_id?.substring(0, 8) || 'N/A')}...</td>
                    <td>${escapeHtml(log.model)}${featureBadges ? ' ' + featureBadges : ''}<div style="font-size:11px;opacity:.7" title="${escapeHtml(log.caller_id || '')}">${escapeHtml(log.caller_name || '历史未归属')}</div></td>
                    <td><span class="status-badge ${statusClass}">${escapeHtml(log.status)}</span></td>
                    <td title="${escapeHtml(renderPhaseTimings(log.timings))}">${duration}${log.timings?.first_business_ms != null ? `<div style="font-size:11px;opacity:.7">首事件 ${(Number(log.timings.first_business_ms) / 1000).toFixed(2)}s</div>` : ""}</td>
                    <td>${inTokens}</td>
                    <td>${outTokens}</td>
                    <td style="white-space: nowrap;">${formatStopReason(log.stop_reason || (log.cost_info && log.cost_info.stop_reason))}</td>
                    <td style="font-family: monospace; font-size: 11px;" title="${escapeHtml(costTitle)}">${escapeHtml(costDisplay)}</td>
                    <td>
                        <button class="detail-btn" data-request-id="${escapeHtml(log.request_id || '')}" onclick="viewRequestDetails(this.dataset.requestId)">查看详细</button>
                    </td>
                </tr>
            `;
        }).join('');

    } catch (error) {
        console.error('获取请求日志失败:', error);
    }
}

// 刷新错误日志
async function refreshErrorLogs() {
    try {
        const limit = currentLogLimit === 10000 ? 1000 : Math.min(currentLogLimit, 100);
        const response = await apiGet(`/api/monitor/logs/errors?limit=${limit}`);
        const logs = await response.json();

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
        console.error('获取错误日志失败:', error);
    }
}

// 刷新活跃请求
async function refreshActiveRequests() {
    try {
        const response = await apiGet('/api/monitor/active');
        const requests = await response.json();
        updateActiveRequests(requests);
    } catch (error) {
        console.error('获取活跃请求失败:', error);
    }
}

// 切换标签页
function switchTab(tab) {
    currentTab = tab;

    // 更新标签样式
    document.querySelectorAll('.tab').forEach(t => {
        t.classList.remove('active');
    });
    // 🔧 修复：旧版依赖全局 event.target，嵌套元素点击时可能拿到子元素而非 .tab
    var targetTab = document.querySelector(`.tab[data-tab="${tab}"]`);
    if (targetTab) targetTab.classList.add('active');

    // 切换内容
    document.getElementById('requests-tab').style.display = tab === 'requests' ? 'block' : 'none';
    document.getElementById('errors-tab').style.display = tab === 'errors' ? 'block' : 'none';

    refreshLogs();
}

// 刷新数据
function refreshData() {
    refreshStats();
    refreshLogs();
    refreshActiveRequests();
}

// 定时刷新（全部先过 isPageActive 门禁：页面在后台或被父页面隐藏时不打接口）
setInterval(() => {
    if (!isPageActive()) return;
    refreshStats();
}, 5000); // 每5秒刷新统计

setInterval(() => {
    if (!isPageActive()) return;
    if (logsVisible && currentTab === 'requests') {
        refreshRequestLogs();
    }
}, 10000); // 每10秒刷新日志（仅在显示状态）

setInterval(() => {
    if (!isPageActive()) return;
    // 仅当存在活跃请求时才刷新计时器
    const activeContainer = document.getElementById('active-requests-list');
    if (activeContainer && activeContainer.children.length > 0 &&
        !activeContainer.querySelector('.empty-state')) {
        refreshActiveRequests();
    }
}, 1000); // 每秒刷新活跃请求计时器

// HTML转义函数 - 防止XSS攻击和渲染问题
function escapeHtml(unsafe) {
    if (unsafe === null || unsafe === undefined) {
        return '';
    }
    // 转换为字符串并转义HTML特殊字符
    return String(unsafe)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// 停止原因（直接显示上游原始值，不做翻译）
function formatStopReason(reason) {
    return reason ? escapeHtml(reason) : '-';
}

// 🔧 修复：截断长内容到指定字符数，添加"展开"按钮
// 旧版全量渲染导致包含大量内容的模态框渲染卡顿甚至页面崩溃
let _expandCounter = 0;
const _expandedTextValues = new Map();
function renderTruncatable(text, maxLen = 4000) {
    text = String(text || '');
    if (text.length <= maxLen) return escapeHtml(text);
    const id = '_trunc_' + (++_expandCounter);
    _expandedTextValues.set(id, {text, maxLen, expanded: false});
    return `<span id="${id}">${escapeHtml(text.slice(0, maxLen))}…</span> <button type="button" onclick="toggleLongText('${id}', this)">展开</button>`;
}
function toggleLongText(id, button) {
    const entry = _expandedTextValues.get(id), node = document.getElementById(id);
    if (!entry || !node) return;
    entry.expanded = !entry.expanded;
    node.textContent = entry.expanded ? entry.text : entry.text.slice(0, entry.maxLen) + '…';
    button.textContent = entry.expanded ? '收起' : '展开';
}
function renderPhaseTimings(timings) {
    if (!timings) return '该记录没有阶段耗时';
    const labels = {prepare_ms: '准备', upstream_wait_ms: '等待上游首字节', first_business_ms: '首业务事件', output_ms: '输出', total_ms: '总计'};
    return Object.entries(labels).map(([key, name]) => `${name}：${timings[key] == null ? '无数据' : (Number(timings[key]) / 1000).toFixed(3) + ' 秒'}`).join(' · ');
}

// 格式化JSON内容
function formatJson(obj) {
    if (!obj) return 'null';
    try {
        if (typeof obj === 'string') {
            // 尝试解析字符串为JSON
            try {
                obj = JSON.parse(obj);
            } catch {
                // 如果不是JSON，直接返回转义后的字符串
                return escapeHtml(obj);
            }
        }
        // 格式化JSON并转义
        return escapeHtml(JSON.stringify(obj, null, 2));
    } catch (e) {
        console.error('格式化JSON失败:', e);
        return escapeHtml(String(obj));
    }
}


function messageContentToText(msg) {
    if (!msg || !msg.content) {
        // 空字符串/ null / undefined 统一显示占位，避免消息框整块空白
        return '(空消息)';
    }

    if (typeof msg.content === 'string') {
        return msg.content;
    }

    if (Array.isArray(msg.content)) {
        return msg.content.map(part => {
            if (typeof part === 'string') {
                return part;
            }
            if (!part || typeof part !== 'object') {
                return String(part);
            }
            if (part.type === 'text') {
                return part.text || '';
            }
            if (part.type === 'image_url') {
                return '[图片内容]';
            }
            return JSON.stringify(part, null, 2);
        }).join('\n');
    }

    if (typeof msg.content === 'object') {
        return JSON.stringify(msg.content, null, 2);
    }

    return String(msg.content);
}

function coalesceResponsesRequestMessages(messages) {
    if (!Array.isArray(messages)) return [];

    const merged = [];
    let pendingAssistant = null;
    const mergedFields = new Set([
        'role', 'content', 'reasoning_content', 'reasoning_signature', 'tool_calls'
    ]);

    function flushAssistant() {
        if (pendingAssistant) {
            merged.push(pendingAssistant);
            pendingAssistant = null;
        }
    }

    function cloneAssistant(message) {
        const cloned = { ...message };
        if (Array.isArray(message.tool_calls)) cloned.tool_calls = [...message.tool_calls];
        if (Array.isArray(message.reasoning_signature)) {
            cloned.reasoning_signature = [...message.reasoning_signature];
        }
        return cloned;
    }

    function mergeTextLikeField(target, source, field, separator) {
        const incoming = source[field];
        if (incoming === undefined || incoming === null || incoming === '') return;
        const existing = target[field];
        if (existing === undefined || existing === null || existing === '') {
            target[field] = Array.isArray(incoming) ? [...incoming] : incoming;
        } else if (typeof existing === 'string' && typeof incoming === 'string') {
            target[field] = `${existing}${separator}${incoming}`;
        } else {
            const existingParts = Array.isArray(existing) ? existing : [existing];
            const incomingParts = Array.isArray(incoming) ? incoming : [incoming];
            target[field] = [...existingParts, ...incomingParts];
        }
    }

    function mergeListField(target, source, field) {
        const incoming = source[field];
        if (incoming === undefined || incoming === null) return;
        const incomingItems = Array.isArray(incoming) ? incoming : [incoming];
        if (incomingItems.length === 0) return;
        const existing = target[field];
        if (existing === undefined || existing === null) {
            target[field] = Array.isArray(incoming) ? [...incoming] : incoming;
            return;
        }
        const existingItems = Array.isArray(existing) ? existing : [existing];
        target[field] = [...existingItems, ...incomingItems];
    }

    for (const message of messages) {
        if (!message || typeof message !== 'object' || Array.isArray(message) || message.role !== 'assistant') {
            flushAssistant();
            merged.push(message);
            continue;
        }

        if (!pendingAssistant) {
            pendingAssistant = cloneAssistant(message);
            continue;
        }

        mergeTextLikeField(pendingAssistant, message, 'content', '');
        mergeTextLikeField(pendingAssistant, message, 'reasoning_content', '\n');
        mergeListField(pendingAssistant, message, 'reasoning_signature');
        mergeListField(pendingAssistant, message, 'tool_calls');

        for (const [key, value] of Object.entries(message)) {
            if (!mergedFields.has(key) && pendingAssistant[key] === undefined) {
                pendingAssistant[key] = value;
            }
        }
    }

    flushAssistant();
    return merged;
}


function renderMessageBox(msg) {
    const content = messageContentToText(msg);
    const toolCalls = msg && msg.tool_calls;
    const reasoning = msg && msg.reasoning_content;
    const reasoningSignature = msg && msg.reasoning_signature;
    let extraBadges = '';

    if (reasoning) {
        extraBadges += '<span class="status-badge" style="background:rgba(42,168,255,0.15);color:#7dd3fc;font-size:10px;margin-left:4px;">含思维链</span>';
    }
    if (reasoningSignature) {
        extraBadges += '<span class="status-badge" style="background:rgba(139,92,246,0.15);color:#c4b5fd;font-size:10px;margin-left:4px;">含思维链签名</span>';
    }
    if (toolCalls) {
        extraBadges += '<span class="status-badge" style="background:rgba(245,158,11,0.15);color:#fcd34d;font-size:10px;margin-left:4px;">含工具调用</span>';
    }

    let reasoningHtml = '';
    if (reasoning) {
        const rc = typeof reasoning === 'string' ? reasoning : JSON.stringify(reasoning, null, 2);
        reasoningHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(42,168,255,0.06);border:1px solid rgba(42,168,255,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#7dd3fc;font-weight:600;margin-bottom:4px;">💭 思维链 (Reasoning):</div>
                <div style="white-space:pre-wrap;word-break:break-word;color:#bae6fd;">${escapeHtml(rc.substring(0, 2000))}${rc.length > 2000 ? '... (截断)' : ''}</div>
            </div>`;
    }

    let signatureHtml = '';
    if (reasoningSignature) {
        const sig = typeof reasoningSignature === 'string' ? reasoningSignature : JSON.stringify(reasoningSignature, null, 2);
        signatureHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(139,92,246,0.06);border:1px solid rgba(139,92,246,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#c4b5fd;font-weight:600;margin-bottom:4px;">🔏 思维链签名 (Signature):</div>
                <div style="white-space:pre-wrap;word-break:break-word;color:#ddd6fe;">${escapeHtml(sig.substring(0, 500))}${sig.length > 500 ? '... (截断)' : ''}</div>
            </div>`;
    }

    let toolCallsHtml = '';
    if (toolCalls) {
        toolCallsHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#fcd34d;font-weight:600;margin-bottom:4px;">🔧 工具调用:</div>
                <pre style="white-space:pre-wrap;word-break:break-word;color:#fde68a;margin:0;font-size:11px;">${escapeHtml(JSON.stringify(toolCalls, null, 2))}</pre>
            </div>`;
    }

    const hasStructuredPayload = Boolean(
        reasoning || reasoningSignature || (Array.isArray(toolCalls) ? toolCalls.length : toolCalls)
    );
    const contentHtml = content === '(空消息)' && hasStructuredPayload
        ? ''
        : `<div class="message-content">${escapeHtml(content)}</div>`;

    return `
        <div class="message-box">
            <div class="message-role">${escapeHtml((msg && msg.role) || 'assistant')}${extraBadges}</div>
            ${reasoningHtml}
            ${signatureHtml}
            ${contentHtml}
            ${toolCallsHtml}
        </div>`;
}
// 查看请求详情
let _detailRequestVersion = 0;
async function viewRequestDetails(requestId) {
    const version = ++_detailRequestVersion;
    const modal = document.getElementById('detailModal');
    const modalBody = document.getElementById('modalBody');

    // 添加调试日志
    console.log(`[DEBUG] 查看请求详情: ${requestId}`);

    modal.style.display = 'block';
    if (!modal._returnFocus) modal._returnFocus = document.activeElement;
    document.body.classList.add('detail-open');
    modal.querySelector('.close').focus();
    modal.querySelector('.modal-content').scrollTop = 0;
    _expandedTextValues.clear();
    modalBody.innerHTML = '<div class="empty-state">加载中...</div>';

    try {
        const response = await apiGet(`/api/request/${requestId}`);

        const details = await response.json();
        if (version !== _detailRequestVersion) return;
        if (!response.ok) throw new Error(details.detail || '读取详情失败');

        // 统一构造结构化响应消息：有它就只渲染“模型响应消息”一块，
        // 避免与单独的“思维链”“响应内容”区块重复展示
        let responseMsg = null;
        if (details.response_message) {
            responseMsg = { ...details.response_message };
        } else if (details.response_tool_calls) {
            responseMsg = {
                role: 'assistant',
                content: details.response_content || null,
                reasoning_content: details.reasoning_content || null,
                tool_calls: details.response_tool_calls
            };
        }
        if (responseMsg) {
            // 补齐结构化消息里缺失的字段，保证信息不丢
            if (!responseMsg.reasoning_content && details.reasoning_content) {
                responseMsg.reasoning_content = details.reasoning_content;
            }
            if ((responseMsg.content === null || responseMsg.content === undefined || responseMsg.content === '') && details.response_content) {
                responseMsg.content = details.response_content;
            }
        }

        const displayRequestMessages = details.mode === 'responses_native_passthrough'
            ? coalesceResponsesRequestMessages(details.request_messages)
            : details.request_messages;

        modalBody.innerHTML = `
            <div class="detail-section detail-overview">
                <h3>基本信息</h3>
                <div class="detail-item">
                    <div class="detail-label">请求ID:</div>
                    <div class="detail-value">${escapeHtml(details.request_id)}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">时间:</div>
                    <div class="detail-value">${new Date(details.timestamp * 1000).toLocaleString()}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">模型:</div>
                    <div class="detail-value">${escapeHtml(details.model)}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">状态:</div>
                    <div class="detail-value"><span class="status-badge ${details.status === 'success' ? 'success' : 'failed'}">${escapeHtml(details.status)}</span></div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">耗时:</div>
                    <div class="detail-value">${formatDuration(details.duration || 0)}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Token使用:</div>
                    <div class="detail-value">输入: ${(details.input_tokens || 0).toLocaleString()}${details.cached_tokens > 0 ? ` (缓存命中: ${(details.cached_tokens || 0).toLocaleString()}, ${((details.cached_tokens / details.input_tokens) * 100).toFixed(1)}%)` : ''}, 输出: ${(details.output_tokens || 0).toLocaleString()}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">停止原因:</div>
                    <div class="detail-value">${formatStopReason(details.stop_reason || (details.cost_info && details.cost_info.stop_reason))}</div>
                </div>
                ${details.upstream_usage && Object.keys(details.upstream_usage).length > 0 ? `
                <details class="data-disclosure"><summary>上游原始用量 <span>JSON</span></summary><pre>${formatJson(details.upstream_usage)}</pre></details>
                ` : ''}
                ${details.error ? `
                <div class="detail-item">
                    <div class="detail-label">错误:</div>
                    <div class="detail-value" style="color: #dc2626;">${escapeHtml(details.error)}</div>
                </div>
                ` : ''}
            </div>

            <div class="detail-section"><h3>调用方与阶段耗时</h3>
                <div class="caller-meta"><div><span>调用方</span><strong>${escapeHtml(details.caller_name || '历史未归属')}</strong><code>${escapeHtml(details.caller_id || 'unattributed')}</code></div>
                <div><span>会话 ID</span><code>${escapeHtml(details.conversation_id || '未记录')}</code></div></div>
                <div class="timing-grid">${Object.entries({prepare_ms: '准备', upstream_wait_ms: '等待上游首字节', first_business_ms: '首业务事件', output_ms: '输出', total_ms: '总计'}).map(([key, label]) => `<div class="timing-card"><span>${label}</span><strong>${details.timings?.[key] == null ? '—' : (Number(details.timings[key]) / 1000).toFixed(3)}<small>${details.timings?.[key] == null ? '无数据' : '秒'}</small></strong></div>`).join('')}</div>
                ${details.timings?.attempts?.length ? `<details class="data-disclosure"><summary>请求尝试记录 <span>${details.timings.attempts.length} 次</span></summary><pre>${renderTruncatable(JSON.stringify(details.timings.attempts, null, 2), 2000)}</pre></details>` : ''}
                ${details.pricing_snapshot ? `<details class="data-disclosure"><summary>调用时价格快照 <span>JSON</span></summary><pre>${escapeHtml(JSON.stringify(details.pricing_snapshot, null, 2))}</pre></details>` : ''}
                ${details.gateway_request_id ? `<a class="archive-link" href="/api/admin/exchanges/${encodeURIComponent(details.gateway_request_id)}">下载完整原生请求与响应归档 <span aria-hidden="true">↗</span></a>` : ''}
            </div>

            ${details.request_params ? `
            <div class="detail-section">
                <h3>请求参数</h3>
                <div class="detail-item">
                    <div class="detail-label">流式输出:</div>
                    <div class="detail-value">${(details.request_params.streaming ?? details.request_params.stream) ? '是' : '否'}</div>
                </div>
                <pre class="response-content" style="white-space:pre-wrap;word-break:break-word;margin-top:10px;">${renderTruncatable(JSON.stringify(details.request_params, null, 2), 4000)}</pre>
            </div>
            ` : ''}

            ${displayRequestMessages && displayRequestMessages.length > 0 ? `
            <div class="detail-section">
                <h3>请求消息</h3>
                ${displayRequestMessages.map(msg => renderMessageBox(msg)).join('')}
            </div>
            ` : ''}

            ${!responseMsg && details.reasoning_content ? `
            <div class="detail-section">
                <h3>💭 思维链 (Reasoning)</h3>
                <div class="response-content" style="background: rgba(42,168,255,0.06); border: 1px solid rgba(42,168,255,0.2);">
                    ${renderTruncatable(details.reasoning_content, 4000)}
                </div>
            </div>
            ` : ''}

            ${!responseMsg && details.response_content ? `
            <div class="detail-section">
                <h3>响应内容</h3>
                <div class="response-content">${(() => {
                    // 处理响应内容，可能是字符串或对象
                    if (typeof details.response_content === 'string') {
                        return renderTruncatable(details.response_content, 4000);
                    } else if (typeof details.response_content === 'object') {
                        // 如果是对象，美化JSON显示
                        return renderTruncatable(JSON.stringify(details.response_content, null, 2), 4000);
                    } else {
                        return renderTruncatable(String(details.response_content), 4000);
                    }
                })()}</div>
            </div>
            ` : ''}

            ${responseMsg ? `
            <div class="detail-section">
                <h3>模型响应消息</h3>
                ${renderMessageBox(responseMsg)}
            </div>
            ` : ''}
        `;

    } catch (error) {
        if (version !== _detailRequestVersion) return;
        modalBody.innerHTML = `<div class="empty-state">加载失败: ${escapeHtml(error.message)}</div>`;
    }
}

// 关闭模态框
function closeModal() {
    ++_detailRequestVersion;
    _expandedTextValues.clear();
    const modal = document.getElementById('detailModal');
    modal.style.display = 'none';
    document.body.classList.remove('detail-open');
    modal._returnFocus?.focus();
    modal._returnFocus = null;
}

// 保持键盘焦点在详情窗口内，关闭后回到原入口。
document.addEventListener('keydown', event => {
    const modal = document.getElementById('detailModal');
    if (modal.style.display !== 'block') return;
    if (event.key === 'Escape') closeModal();
    if (event.key !== 'Tab') return;
    const focusable = [...modal.querySelectorAll('button, a[href], summary, input, select, textarea, [tabindex="0"]')].filter(el => el.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

// 点击模态框外部关闭
window.onclick = function(event) {
    const modal = document.getElementById('detailModal');
    if (event.target == modal) {
        closeModal();
    }
}

// 初始化
connectWebSocket();
refreshData();
loadModelFilter();

// 初始化时设置各个面板的隐藏状态
document.addEventListener('DOMContentLoaded', function() {
    // 模型统计默认隐藏
    const modelStatsContainer = document.getElementById('model-stats-container');
    const modelStatsBtn = document.getElementById('toggle-model-stats-btn');
    if (modelStatsContainer && modelStatsBtn) {
        modelStatsContainer.classList.add('model-stats-hidden');
        modelStatsBtn.textContent = '显示统计';
    }

    // 🔧 日志默认显示
    const logsContainer = document.getElementById('logs-container');
    const logsBtn = document.getElementById('toggle-logs-btn');
    if (logsContainer && logsBtn) {
        logsContainer.classList.remove('logs-hidden');
        logsBtn.textContent = '隐藏日志';
    }
});

// 切换API文档显示
function toggleApiDocs() {
    const content = document.getElementById('api-docs-content');
    if (content.style.display === 'none') {
        content.style.display = 'block';
    } else {
        content.style.display = 'none';
    }
}

// API 文档示例语言切换（原内嵌于文档区域的脚本）
function switchExample(lang) {
    // 隐藏所有示例
    document.querySelectorAll('.example-content').forEach(el => {
        el.style.display = 'none';
    });
    // 移除所有激活状态
    document.querySelectorAll('.example-tab').forEach(el => {
        el.classList.remove('active');
    });
    // 显示选中的示例
    document.getElementById('example-' + lang).style.display = 'block';
    // 激活选中的标签
    event.target.classList.add('active');
}

// 显示/隐藏模型统计
function toggleModelStats() {
    const container = document.getElementById('model-stats-container');
    const btn = document.getElementById('toggle-model-stats-btn');

    modelStatsVisible = !modelStatsVisible;

    if (modelStatsVisible) {
        container.classList.remove('model-stats-hidden');
        btn.textContent = '隐藏统计';
        updateModelStats(lastModelStats); // 显示时更新数据
    } else {
        container.classList.add('model-stats-hidden');
        btn.textContent = '显示统计';
    }
}

// 显示/隐藏日志
function toggleLogs() {
    const container = document.getElementById('logs-container');
    const btn = document.getElementById('toggle-logs-btn');

    logsVisible = !logsVisible;

    if (logsVisible) {
        container.classList.remove('logs-hidden');
        btn.textContent = '隐藏日志';
        loadModelFilter().catch(e => console.error('模型筛选下拉加载失败:', e)); // 加载模型筛选下拉
        refreshLogs(); // 显示时刷新日志
    } else {
        container.classList.add('logs-hidden');
        btn.textContent = '显示日志';
    }
}

// 刷新标签页连接状态
async function refreshTabConnections() {
    try {
        const response = await apiGet('/api/monitor/tabs');
        const data = await response.json();

        updateTabConnectionsUI(data);
    } catch (error) {
        console.error('获取标签页连接状态失败:', error);
    }
}

// 更新标签页连接UI
function updateTabConnectionsUI(data) {
    // 更新汇总信息
    document.getElementById('total-tabs').textContent = data.total_tabs;
    document.getElementById('total-capacity').textContent = data.total_capacity + ' 个请求';
    document.getElementById('total-used').textContent = data.total_active_requests;

    const usagePercentage = data.total_capacity > 0
        ? ((data.total_active_requests / data.total_capacity) * 100).toFixed(1)
        : 0;
    document.getElementById('capacity-usage').textContent = `使用率: ${usagePercentage}%`;

    // 更新标签页列表
    const grid = document.getElementById('tabs-grid');

    if (!data.tabs || data.tabs.length === 0) {
        grid.innerHTML = '<div class="empty-state" style="grid-column: 1 / -1;">暂无标签页连接</div>';
        return;
    }

    grid.innerHTML = data.tabs.map((tab, index) => {
        const statusColor = tab.status === 'busy' ? '#f87171' : '#34d399';
        const statusText = tab.status === 'busy' ? '繁忙' : '空闲';
        const statusBg = tab.status === 'busy' ? 'rgba(220,38,38,0.18)' : 'rgba(16,185,129,0.15)';

        // 生成一个简短的显示ID
        const shortId = tab.tab_id.split('_').pop().substring(0, 6);

        // 格式化连接时长
        const connectedDuration = formatDuration(tab.connected_duration || 0);

        return `
            <div style="border: 1px solid var(--line-weak); border-radius: 8px; padding: 16px; background: var(--card-bg);">
                <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 12px;">
                    <div style="flex: 1;">
                        <div style="font-size: 14px; font-weight: 600; color: var(--text-main); margin-bottom: 4px;">
                            标签页 #${index + 1}
                        </div>
                        <div style="font-size: 11px; font-family: monospace; color: var(--text-dim);">
                            ID: ${escapeHtml(shortId)}
                        </div>
                        <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">
                            已连接: ${connectedDuration}
                        </div>
                    </div>
                    <div style="padding: 4px 12px; background: ${statusBg}; color: ${statusColor}; border-radius: 12px; font-size: 12px; font-weight: 500;">
                        ${statusText}
                    </div>
                </div>

                <div style="margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px; color: var(--text-dim); margin-bottom: 4px;">
                        <span>请求负载</span>
                        <span style="font-weight: 600; color: var(--text-main);">${tab.active_requests}/${tab.max_concurrent}</span>
                    </div>
                    <div style="height: 8px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: ${tab.load_percentage >= 100 ? '#dc2626' : tab.load_percentage >= 66 ? '#f59e0b' : '#10b981'}; width: ${tab.load_percentage}%; transition: width 0.3s;"></div>
                    </div>
                    <div style="font-size: 11px; color: var(--text-dim); margin-top: 4px;">
                        ${tab.load_percentage.toFixed(0)}% 已使用
                    </div>
                </div>

                <div style="display: flex; gap: 8px; font-size: 12px; margin-bottom: 8px;">
                    <div style="flex: 1; padding: 8px; background: rgba(255,255,255,0.05); border-radius: 4px; text-align: center;">
                        <div style="color: var(--text-dim);">活跃请求</div>
                        <div style="font-weight: 600; color: var(--text-main); margin-top: 2px;">${tab.active_requests}</div>
                    </div>
                    <div style="flex: 1; padding: 8px; background: rgba(255,255,255,0.05); border-radius: 4px; text-align: center;">
                        <div style="color: var(--text-dim);">剩余容量</div>
                        <div style="font-weight: 600; color: var(--text-main); margin-top: 2px;">${tab.max_concurrent - tab.active_requests}</div>
                    </div>
                </div>

                <div style="padding: 6px 10px; background: ${tab.load_percentage >= 80 ? 'rgba(245,158,11,0.15)' : 'rgba(16,185,129,0.12)'}; border-radius: 4px; font-size: 11px; color: ${tab.load_percentage >= 80 ? '#fbbf24' : '#34d399'}; text-align: center;">
                    ${tab.load_percentage >= 80 ? '⚠️ 负载较高' : '✅ 运行良好'}
                </div>
            </div>
        `;
    }).join('');
}

// 初始化时加载标签页状态
setTimeout(() => {
    refreshTabConnections();
}, 1000);

// 定期刷新标签页状态（每3秒；页面不可见时跳过）
setInterval(() => {
    if (!isPageActive()) return;
    refreshTabConnections();
}, 3000);

// 从后台/隐藏状态回到前台时立即补一次刷新，避免看到过期数据
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && isPageActive()) {
        refreshStats();
        refreshActiveRequests();
    }
});
