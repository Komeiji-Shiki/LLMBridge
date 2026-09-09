// admin-overview.js - 概览页面功能

// 速率统计周期：'day' 或 'hour'
let currentRatePeriod = 'day';

// 成本显示货币：'USD' 或 'CNY'
let currentCostCurrency = 'USD';

// 缓存最新的 token stats 数据（用于货币切换时无需重新请求）
let latestTokenStatsData = null;
const tokenStatsRefresh = { serial: 0, controller: null, pollTimer: null };

// 汇率常量（与后端保持一致）
const EXCHANGE_RATE = { USD_TO_CNY: 7.2, CNY_TO_USD: 1.0 / 7.2 };

// 切换成本显示货币
function switchCostCurrency(currency) {
    currentCostCurrency = currency;
    costCurrencyDisplay = currency;
    
    // 更新按钮样式
    document.getElementById('cost-currency-usd').className =
        currency === 'USD' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    document.getElementById('cost-currency-cny').className =
        currency === 'CNY' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    
    // 用缓存数据直接刷新显示，无需重新请求
    if (latestTokenStatsData) {
        updateCostDisplay(latestTokenStatsData);
        renderTokenStatsTable(latestTokenStatsData.model_stats);
        renderCostTrendChart(latestTokenStatsData.daily_stats || []);
    }
    const title = document.getElementById('cost-trend-title');
    if (title) title.textContent = `每日金额 (${currency})`;
}

// 更新成本卡片显示（根据当前选中货币）
function updateCostDisplay(data) {
    if (data.cost_scope === 'unavailable') {
        for (const id of ['total-cost-value', 'input-cost-value', 'cached-cost-value', 'output-cost-value']) {
            document.getElementById(id).textContent = '未定价';
        }
        document.getElementById('total-cost-currency').textContent = '没有可用的公开标准价格';
        return;
    }
    const symbol = currentCostCurrency === 'CNY' ? '¥' : '$';
    const currLabel = currentCostCurrency;
    
    let totalCost, inputCost, outputCost;
    
    // 优先使用后端预计算的换算值
    if (currentCostCurrency === 'CNY' && data.cost_cny) {
        totalCost = data.cost_cny.total_cost;
        inputCost = data.cost_cny.input_cost;
        outputCost = data.cost_cny.output_cost;
    } else if (currentCostCurrency === 'USD' && data.cost_usd) {
        totalCost = data.cost_usd.total_cost;
        inputCost = data.cost_usd.input_cost;
        outputCost = data.cost_usd.output_cost;
    } else {
        // 回退：用旧字段 + 本地换算
        totalCost = data.total_cost || 0;
        inputCost = data.input_cost || 0;
        outputCost = data.output_cost || 0;
        if (currentCostCurrency === 'CNY' && (data.currency || 'USD') === 'USD') {
            totalCost *= EXCHANGE_RATE.USD_TO_CNY;
            inputCost *= EXCHANGE_RATE.USD_TO_CNY;
            outputCost *= EXCHANGE_RATE.USD_TO_CNY;
        }
    }
    
    const qualifier = data.unpriced_tokens ? '≥ ' : data.cost_scope?.includes('estimate') ? '≈ ' : '';
    document.getElementById('total-cost-value').textContent = qualifier + symbol + totalCost.toFixed(4);
    document.getElementById('total-cost-currency').textContent = currLabel;
    document.getElementById('input-cost-value').textContent = symbol + inputCost.toFixed(4);
    const cachedCost = (currentCostCurrency === 'CNY' ? data.cost_cny?.cached_cost : data.cost_usd?.cached_cost) || 0;
    document.getElementById('cached-cost-value').textContent = symbol + cachedCost.toFixed(4);
    document.getElementById('output-cost-value').textContent = symbol + outputCost.toFixed(4);
}

// 切换速率统计周期
function switchRatePeriod(period) {
    currentRatePeriod = period;
    
    // 更新按钮样式
    document.getElementById('rate-period-day').className =
        period === 'day' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    document.getElementById('rate-period-hour').className =
        period === 'hour' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    
    // 🔧 优化：只需刷新一次 Token 统计，RPM/TPM 会从返回数据中自动计算
    refreshTokenStats();
}

// 从缓存的 Token 统计数据中刷新总体速率卡片
// （旧的 refreshOverallRates 会为同一份数据再发一次 /api/admin/token_stats，
//  自被本函数取代后已无调用方，故删除）
function updateOverallRatesFromCachedData() {
    try {
        const data = latestTokenStatsData;
        if (!data || !data.rate_stats) {
            document.getElementById('overall-rpm-value').textContent = '-';
            document.getElementById('overall-tpm-value').textContent = '-';
            document.getElementById('rate-total-requests').textContent = '0';
            document.getElementById('rate-period-display').textContent =
                currentRatePeriod === 'day' ? '24小时' : '1小时';
            document.getElementById('rate-period-range').textContent = '暂无数据';
            return;
        }

        // “总体速率统计”卡片只统计当前 24小时/1小时周期，不能使用 model_stats 的全量 request_count
        const rateStats = data.gateway_rate_stats || data.rate_stats;
        const minutes = rateStats.minutes || (currentRatePeriod === 'day' ? 1440 : 60);
        const totalRequests = rateStats.request_count || 0;
        const totalTokens = rateStats.total_tokens || 0;
        const totalRpm = minutes > 0 ? totalRequests / minutes : 0;
        const totalTpm = minutes > 0 ? totalTokens / minutes : 0;

        // 更新显示
        document.getElementById('overall-rpm-value').textContent = totalRpm.toFixed(2);
        document.getElementById('overall-tpm-value').textContent =
            totalTpm >= 1000 ? (totalTpm / 1000).toFixed(2) + 'K' : totalTpm.toFixed(0);
        document.getElementById('rate-total-requests').textContent = formatNumber(totalRequests);
        document.getElementById('rate-period-display').textContent =
            currentRatePeriod === 'day' ? '24小时' : '1小时';

        // 更新时间范围显示
        const now = new Date();
        const startTime = new Date(now.getTime() - minutes * 60 * 1000);
        const timeRange = `${startTime.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} - ${now.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
        document.getElementById('rate-period-range').textContent = timeRange;

    } catch (error) {
        console.error('❌ 更新总体速率统计失败:', error);
        document.getElementById('overall-rpm-value').textContent = '错误';
        document.getElementById('overall-tpm-value').textContent = '错误';
    }
}

async function refreshOverview(options = {}) {
    const { includeRates = true } = options;
    if (includeRates) refreshTokenStats();
    try {
        const response = await fetch('/api/admin/overview');
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        const data = await response.json();
        
        document.querySelector('#browser-stat .stat-card-value').textContent =
            data.browser_connected ? '✅ 已连接' : '❌ 未连接';
        document.querySelector('#browser-stat .stat-card-detail').textContent =
            `${data.total_tabs} 个标签页`;
        
        document.querySelector('#models-stat .stat-card-value').textContent = data.total_models;
        document.querySelector('#requests-stat .stat-card-value').textContent = data.active_requests.length;
        
        const totalReqs = data.stats.total_requests || 0;
        const successReqs = data.stats.success_requests || 0;
        const successRate = totalReqs > 0
            ? ((successReqs / totalReqs) * 100).toFixed(1)
            : '0';
        
        document.querySelector('#total-requests-stat .stat-card-value').textContent = totalReqs;
        document.querySelector('#total-requests-stat .stat-card-detail').textContent = `成功率: ${successRate}%`;
        
        const statusHtml = `
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; color: var(--text-main);">
                <div>
                    <strong>运行模式:</strong> ${escapeHtml(data.mode.mode)}
                    ${data.mode.mode === 'battle' ? ` (Target: ${escapeHtml(data.mode.target)})` : ''}
                </div>
                <div><strong>浏览器状态:</strong> <span class="badge ${data.browser_connected ? 'badge-success' : 'badge-danger'}">${data.browser_connected ? '在线' : '离线'}</span></div>
                <div><strong>标签页数量:</strong> ${data.total_tabs}</div>
                <div><strong>失败请求:</strong> ${data.stats.failed_requests || 0}</div>
            </div>
        `;
        document.getElementById('status-details').innerHTML = statusHtml;
        
        const requestsHtml = data.active_requests.length > 0 
            ? `<table class="table">
                <thead>
                    <tr>
                        <th>请求ID</th>
                        <th>模型</th>
                        <th>状态</th>
                        <th>开始时间</th>
                    </tr>
                </thead>
                <tbody>
                    ${data.active_requests.map(req => `
                        <tr>
                            <td style="font-family: monospace; font-size: 12px;">${escapeHtml(req.request_id)}</td>
                            <td>${escapeHtml(req.model)}</td>
                            <td><span class="badge badge-info">处理中</span></td>
                            <td>${new Date(req.timestamp * 1000).toLocaleString()}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`
            : '<div class="empty-state"><div class="empty-state-icon">📭</div><p>当前没有活跃请求</p></div>';
        
        document.getElementById('active-requests-list').innerHTML = requestsHtml;
        
        
    } catch (error) {
        console.error('❌ 刷新概览失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新概览失败: ' + error.message);
    }
}

// ==================== Token 统计 ====================
function usageLocalDate(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function setUsageRange(days, refresh = true) {
    const end = new Date();
    const start = new Date(end);
    if (days !== 'all') start.setDate(start.getDate() - Number(days) + 1);
    currentStartDate = days === 'all' ? null : usageLocalDate(start);
    currentEndDate = days === 'all' ? null : usageLocalDate(end);
    document.getElementById('token-start-date').value = currentStartDate || '';
    document.getElementById('token-end-date').value = currentEndDate || '';
    document.querySelectorAll('.usage-range button').forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.days === String(days)));
    });
    if (refresh) refreshTokenStats();
}

function initializeUsageDateRange() {
    setUsageRange(30, false);
    currentRequestStartDate = currentStartDate;
    currentRequestEndDate = currentEndDate;
    document.getElementById('request-start-date').value = currentRequestStartDate;
    document.getElementById('request-end-date').value = currentRequestEndDate;
}

function applyDateFilter() {
    const startDate = document.getElementById('token-start-date').value;
    const endDate = document.getElementById('token-end-date').value;
    
    if (startDate && endDate && startDate > endDate) {
        alert('开始日期不能晚于结束日期');
        return;
    }
    
    currentStartDate = startDate || null;
    currentEndDate = endDate || null;
    document.querySelectorAll('.usage-range button').forEach(button => button.setAttribute('aria-pressed', 'false'));
    refreshTokenStats();
}

function clearDateFilter() {
    setUsageRange('all');
}

async function refreshTokenStats(force = false) {
    const serial = ++tokenStatsRefresh.serial;
    clearTimeout(tokenStatsRefresh.pollTimer);
    tokenStatsRefresh.controller?.abort();
    tokenStatsRefresh.controller = new AbortController();
    for (const id of ['usage-refresh', 'usage-export']) document.getElementById(id).disabled = true;
    const source = document.getElementById('usage-source')?.value || 'all';
    try {
        let url = '/api/admin/token_stats';
        const params = new URLSearchParams();
        params.set('source', source);
        params.set('background', 'true');
        if (force) params.set('force', 'true');
        const status = document.getElementById('codex-usage-status');
        if (status && source !== 'bridge') status.textContent = '正在读取 Codex 用量，首次扫描历史日志可能需要一些时间…';
        
        // 日期筛选器用于 Token 统计、成本统计等
        if (currentStartDate) params.append('start_date', currentStartDate);
        if (currentEndDate) params.append('end_date', currentEndDate);
        
        // rpm_period 参数只影响“总体速率统计”卡片，不影响下方 Token/成本/概览总数
        params.append('rpm_period', currentRatePeriod);
        
        if (params.toString()) url += '?' + params.toString();
        
        const response = await fetch(url, { signal: tokenStatsRefresh.controller.signal });
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        const data = await response.json();
        // 来源切换后忽略旧请求，避免较慢的扫描覆盖当前选择。
        if (serial !== tokenStatsRefresh.serial) return;
        if (status) {
            const usage = data.codex_usage;
            if (!usage) status.textContent = '';
            else if (usage.status?.errors?.length) {
                status.textContent = 'Codex 部分数据读取失败，当前统计可能不完整。' + usage.status.errors.map(item => item.error).join('；');
            } else if (usage.status?.refreshing && !usage.status?.available) {
                status.textContent = 'Codex 正在首次导入，已先显示网关统计；导入完成后自动补齐。';
            } else if (!usage.status?.available) {
                status.textContent = '服务所在机器未发现 Codex 会话日志。可通过 CODEX_HOME 或 CODEX_USAGE_HOMES 指定目录。';
            } else {
                const updated = usage.status.scanned_at ? new Date(usage.status.scanned_at * 1000).toLocaleString() : '已有本地索引';
                status.textContent = `Codex · ${usage.session_count || 0} 个会话 · ${usage.event_count || 0} 条用量事件 · ${updated}\n缓存输入 ${formatNumber(usage.cached_tokens || 0)} · 推理输出 ${formatNumber(usage.reasoning_tokens || 0)}`;
                if (usage.status.refreshing) status.textContent += ' · 正在后台检查更新';
                status.title = (usage.status.directories || []).join('\n');
                if (usage.excluded_usage?.event_count) {
                    status.textContent += `\n合计已排除经 LLMBridge 转发的 ${usage.excluded_usage.event_count} 条事件，共 ${formatNumber(usage.excluded_usage.total_tokens)} Tokens；Codex 单独视图保留完整用量。`;
                }
            }
        }
        if (data.codex_usage?.status?.refreshing) {
            tokenStatsRefresh.pollTimer = setTimeout(() => {
                if (!document.hidden && document.getElementById('overview').classList.contains('active')) refreshTokenStats();
            }, 1500);
        }
        const costNote = document.getElementById('usage-cost-note');
        if (costNote) costNote.textContent = source === 'bridge' ? '金额采用网关记录中的历史价格。' : 'Codex 按当前公开标准 API 单价估算，不代表订阅实际扣费；快模式、工具和地区附加费未计入。未定价模型保留 Token，金额合计仅含已知价格部分。';
        document.getElementById('total-cost-label').textContent = source === 'all' ? '已记录 + Codex 估算' : source === 'codex' ? 'Codex 估算金额' : '网关已记录金额';
        renderCodexPricing(data);
        
        // 更新总计卡片
        const totalTokens = data.total_tokens || 0;
        const inputTokens = data.total_input_tokens || 0;
        const outputTokens = data.total_output_tokens || 0;
        
        document.getElementById('total-tokens-value').textContent = formatNumber(totalTokens);
        
        if (totalTokens > 0) {
            document.getElementById('token-ratio').textContent =
                `输入: ${formatNumber(inputTokens)} / 输出: ${formatNumber(outputTokens)}`;
        } else {
            document.getElementById('token-ratio').textContent = '暂无数据';
        }
        
        // 缓存数据并更新汇率（后端可能返回更新的汇率）
        latestTokenStatsData = data;
        if (data.exchange_rate) {
            EXCHANGE_RATE.USD_TO_CNY = data.exchange_rate.USD_TO_CNY;
            EXCHANGE_RATE.CNY_TO_USD = data.exchange_rate.CNY_TO_USD;
        }
        
        // 更新成本信息（根据当前选中货币）
        updateCostDisplay(data);
        
        // 渲染图表
        renderTokenDistribution(data.model_stats);
        renderTokenTrendChart(data.daily_stats || []);
        const costPanel = document.getElementById('cost-trend-panel');
        if (costPanel) costPanel.hidden = data.cost_scope === 'unavailable';
        if (!costPanel?.hidden) renderCostTrendChart(data.daily_stats || []);
        renderTokenStatsTable(data.model_stats);
        
        // 🔧 优化：从 Token 统计数据中直接推算总体 RPM/TPM，不再重复请求
        updateOverallRatesFromCachedData();
        
    } catch (error) {
        if (error.name === 'AbortError' || serial !== tokenStatsRefresh.serial) return;
        const status = document.getElementById('codex-usage-status');
        if (status) status.textContent = '用量加载失败：' + error.message;
        console.error('❌ 刷新Token统计失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新Token统计失败: ' + error.message);
    } finally {
        if (serial === tokenStatsRefresh.serial) {
            for (const id of ['usage-refresh', 'usage-export']) document.getElementById(id).disabled = false;
        }
    }

}

// ==================== 导出报告 ====================
function exportTokenReport() {
    const startDate = document.getElementById('token-start-date').value;
    const endDate = document.getElementById('token-end-date').value;
    
    let url = '/api/admin/export_report?';
    url += 'source=' + encodeURIComponent(document.getElementById('usage-source')?.value || 'all') + '&';
    if (startDate) url += 'start_date=' + encodeURIComponent(startDate) + '&';
    if (endDate) url += 'end_date=' + encodeURIComponent(endDate) + '&';
    
    // 触发下载
    const a = document.createElement('a');
    a.href = url;
    a.download = 'token_report.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    
    showMessage('success', '📥 报告下载已开始...');
}


// ==================== 请求统计 ====================
function applyRequestDateFilter() {
    const startDate = document.getElementById('request-start-date').value;
    const endDate = document.getElementById('request-end-date').value;
    
    if (startDate && endDate && startDate > endDate) {
        alert('开始日期不能晚于结束日期');
        return;
    }
    
    currentRequestStartDate = startDate || null;
    currentRequestEndDate = endDate || null;
    refreshRequestStats();
}

function clearRequestDateFilter() {
    document.getElementById('request-start-date').value = '';
    document.getElementById('request-end-date').value = '';
    currentRequestStartDate = null;
    currentRequestEndDate = null;
    refreshRequestStats();
}

async function refreshRequestStats() {
    try {
        let url = '/api/admin/request_stats';
        const params = new URLSearchParams();
        
        if (currentRequestStartDate) params.append('start_date', currentRequestStartDate);
        if (currentRequestEndDate) params.append('end_date', currentRequestEndDate);
        
        if (params.toString()) url += '?' + params.toString();
        
        const response = await fetch(url);
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        const data = await response.json();
        
        renderRequestCountChart(data.daily_stats || []);
        renderRequestStatsSummary(data);
        
    } catch (error) {
        console.error('❌ 刷新请求统计失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新请求统计失败: ' + error.message);
    }
}

function renderRequestStatsSummary(data) {
    const container = document.getElementById('request-stats-summary');
    if (!container) return;
    
    const totalRequests = data.total_requests || 0;
    const successRequests = data.success_requests || 0;
    const failedRequests = data.failed_requests || 0;
    const successRate = totalRequests > 0
        ? ((successRequests / totalRequests) * 100).toFixed(1)
        : '0';
    
    container.innerHTML = `
        <div style="text-align: center;">
            <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 5px;">总请求数</div>
            <div style="font-size: 1.5rem; font-weight: bold; color: var(--accent);">${totalRequests}</div>
        </div>
        <div style="text-align: center;">
            <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 5px;">成功请求</div>
            <div style="font-size: 1.5rem; font-weight: bold; color: #10b981;">${successRequests}</div>
        </div>
        <div style="text-align: center;">
            <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 5px;">失败请求</div>
            <div style="font-size: 1.5rem; font-weight: bold; color: #ef4444;">${failedRequests}</div>
        </div>
        <div style="text-align: center;">
            <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 5px;">成功率</div>
            <div style="font-size: 1.5rem; font-weight: bold; color: ${successRate >= 90 ? '#10b981' : successRate >= 70 ? '#f59e0b' : '#ef4444'};">${successRate}%</div>
        </div>
    `;
}

// ==================== 模型统计操作 ====================
function updateSelectedCount() {
    const checkboxes = document.querySelectorAll('.model-stat-checkbox');
    const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
    const countDisplay = document.getElementById('selected-count-display');
    if (countDisplay) {
        countDisplay.textContent = `已选择: ${checkedCount}`;
    }
    
    const selectAllCheckbox = document.getElementById('select-all-checkbox');
    if (selectAllCheckbox) {
        if (checkedCount === 0) {
            selectAllCheckbox.checked = false;
            selectAllCheckbox.indeterminate = false;
        } else if (checkedCount === checkboxes.length) {
            selectAllCheckbox.checked = true;
            selectAllCheckbox.indeterminate = false;
        } else {
            selectAllCheckbox.checked = false;
            selectAllCheckbox.indeterminate = true;
        }
    }
}

function toggleAllModelStats() {
    const selectAllCheckbox = document.getElementById('select-all-checkbox');
    const checkboxes = document.querySelectorAll('.model-stat-checkbox');
    const shouldCheck = selectAllCheckbox ? selectAllCheckbox.checked : true;
    
    checkboxes.forEach(cb => cb.checked = shouldCheck);
    updateSelectedCount();
}

async function mergeSelectedModelStats() {
    const checkboxes = document.querySelectorAll('.model-stat-checkbox:checked');
    const selectedModels = Array.from(checkboxes).map(cb => cb.getAttribute('data-model'));
    
    if (selectedModels.length < 2) {
        alert('请至少选择两个模型进行合并');
        return;
    }
    
    const targetName = prompt(`请输入合并后的模型名称（将合并 ${selectedModels.length} 个模型）:`, selectedModels[0]);
    if (!targetName || !targetName.trim()) return;
    
    if (!confirm(`确定要将以下模型合并为 "${targetName.trim()}" 吗？\n\n${selectedModels.join('\n')}\n\n合并后原模型的统计数据将被删除。`)) {
        return;
    }
    
    try {
        const response = await fetch('/api/admin/merge_model_stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_models: selectedModels,
                target_model: targetName.trim()
            })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            showMessage('success', `✅ 成功合并 ${selectedModels.length} 个模型到 "${targetName.trim()}"`);
            refreshTokenStats();
        } else {
            throw new Error(result.detail || '合并失败');
        }
    } catch (error) {
        console.error('合并模型统计失败:', error);
        showMessage('danger', '合并失败: ' + error.message);
    }
}

async function deleteSelectedModelStats() {
    const checkboxes = document.querySelectorAll('.model-stat-checkbox:checked');
    const selectedModels = Array.from(checkboxes).map(cb => cb.getAttribute('data-model'));
    
    if (selectedModels.length === 0) {
        alert('请至少选择一个模型');
        return;
    }
    
    if (!confirm(`确定要删除以下 ${selectedModels.length} 个模型的统计数据吗？\n\n${selectedModels.join('\n')}\n\n此操作不可恢复！`)) {
        return;
    }
    
    try {
        const response = await fetch('/api/admin/delete_model_stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models: selectedModels })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            showMessage('success', `✅ 成功删除 ${selectedModels.length} 个模型的统计数据`);
            refreshTokenStats();
        } else {
            throw new Error(result.detail || '删除失败');
        }
    } catch (error) {
        console.error('删除模型统计失败:', error);
        showMessage('danger', '删除失败: ' + error.message);
    }
}

async function deleteModelStats(modelName) {
    if (!confirm(`确定要删除模型 "${modelName}" 的统计数据吗？\n\n此操作不可恢复！`)) {
        return;
    }
    
    try {
        const response = await fetch('/api/admin/delete_model_stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models: [modelName] })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            showMessage('success', `✅ 成功删除模型 "${modelName}" 的统计数据`);
            refreshTokenStats();
        } else {
            throw new Error(result.detail || '删除失败');
        }
    } catch (error) {
        console.error('删除模型统计失败:', error);
        showMessage('danger', '删除失败: ' + error.message);
    }
}

function renderCodexPricing(data) {
    const container = document.getElementById('codex-pricing-details');
    const pricing = data.pricing;
    container.parentElement.hidden = !pricing?.rates;
    if (!pricing?.rates) return;
    const unknown = pricing.unpriced_models || [];
    const rows = Object.entries(pricing.rates).map(([model, rate]) => `
        <tr><td><a href="https://developers.openai.com/api/docs/models/${encodeURIComponent(model)}" target="_blank" rel="noopener noreferrer">${escapeHtml(model)}</a></td>
        <td>${rate.input}</td><td>${rate.cached_input}</td><td>${rate.output}</td><td>${rate.long_context ? '>272K' : '—'}</td></tr>`).join('');
    container.innerHTML = `<p>价格核对日期：${escapeHtml(pricing.verified_at)}，单位 USD / 百万 Token。长上下文按模型规则计价，缓存属于输入、推理属于输出，不重复相加。</p>
        ${unknown.length ? `<p>未定价：${unknown.map(escapeHtml).join('、')}，共 ${formatNumber(data.unpriced_tokens || 0)} Tokens。</p>` : '<p>当前所选范围的 Codex 模型均有公开价格。</p>'}
        <table class="table"><thead><tr><th>模型 / 官方来源</th><th>普通输入</th><th>缓存输入</th><th>输出</th><th>长上下文</th></tr></thead><tbody>${rows}</tbody></table>`;
}
