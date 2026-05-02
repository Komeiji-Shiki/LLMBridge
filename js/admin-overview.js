// admin-overview.js - 概览页面功能

// 速率统计周期：'day' 或 'hour'
let currentRatePeriod = 'day';

// 成本显示货币：'USD' 或 'CNY'
let currentCostCurrency = 'USD';

// 缓存最新的 token stats 数据（用于货币切换时无需重新请求）
let latestTokenStatsData = null;

// 汇率常量（与后端保持一致）
const EXCHANGE_RATE = { USD_TO_CNY: 7.2, CNY_TO_USD: 1.0 / 7.2 };

// 切换成本显示货币
function switchCostCurrency(currency) {
    currentCostCurrency = currency;
    
    // 更新按钮样式
    document.getElementById('cost-currency-usd').className =
        currency === 'USD' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    document.getElementById('cost-currency-cny').className =
        currency === 'CNY' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    
    // 用缓存数据直接刷新显示，无需重新请求
    if (latestTokenStatsData) {
        updateCostDisplay(latestTokenStatsData);
        renderTokenStatsTable(latestTokenStatsData.model_stats);
    }
}

// 更新成本卡片显示（根据当前选中货币）
function updateCostDisplay(data) {
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
    
    document.getElementById('total-cost-value').textContent = symbol + totalCost.toFixed(4);
    document.getElementById('total-cost-currency').textContent = currLabel;
    document.getElementById('input-cost-value').textContent = symbol + inputCost.toFixed(4);
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

// 刷新总体速率统计
async function refreshOverallRates() {
    try {
        // 计算时间范围
        const now = new Date();
        const minutes = currentRatePeriod === 'day' ? 1440 : 60; // 一天1440分钟，一小时60分钟
        const startTime = new Date(now.getTime() - minutes * 60 * 1000);
        
        // 使用精确的时间范围查询 (ISO 8601 格式)
        let url = '/api/admin/token_stats';
        const params = new URLSearchParams();
        params.append('start_time', startTime.toISOString());
        params.append('end_time', now.toISOString());
        url += '?' + params.toString();
        
        const response = await fetch(url);
        
        // 检查HTTP状态
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
        
        if (!data.model_stats || data.model_stats.length === 0) {
            document.getElementById('overall-rpm-value').textContent = '-';
            document.getElementById('overall-tpm-value').textContent = '-';
            document.getElementById('rate-total-requests').textContent = '0';
            document.getElementById('rate-period-display').textContent =
                currentRatePeriod === 'day' ? '24小时' : '1小时';
            document.getElementById('rate-period-range').textContent = '暂无数据';
            return;
        }
        
        // 计算总体统计（这是时间段内的总数）
        let totalRequests = 0;
        let totalTokens = 0;
        
        data.model_stats.forEach(stat => {
            totalRequests += stat.request_count || 0;
            totalTokens += stat.total_tokens || 0;
        });
        
        // 根据周期计算RPM和TPM
        const actualMinutes = (now.getTime() - startTime.getTime()) / (1000 * 60);
        const rpm = totalRequests > 0 && actualMinutes > 0 ? (totalRequests / actualMinutes) : 0;
        const tpm = totalTokens > 0 && actualMinutes > 0 ? (totalTokens / actualMinutes) : 0;
        
        // 更新显示
        document.getElementById('overall-rpm-value').textContent = rpm.toFixed(2);
        document.getElementById('overall-tpm-value').textContent =
            tpm >= 1000 ? (tpm / 1000).toFixed(2) + 'K' : tpm.toFixed(0);
        document.getElementById('rate-total-requests').textContent = formatNumber(totalRequests);
        document.getElementById('rate-period-display').textContent =
            currentRatePeriod === 'day' ? '24小时' : '1小时';
        
        // 更新详细信息
        const timeRange = `${startTime.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} - ${now.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
        document.getElementById('rate-period-range').textContent = timeRange;
        
    } catch (error) {
        console.error('❌ 刷新总体速率统计失败:', error);
        console.error('错误详情:', error.message);
        // 显示错误状态
        document.getElementById('overall-rpm-value').textContent = '错误';
        document.getElementById('overall-tpm-value').textContent = '错误';
        document.getElementById('rate-period-range').textContent = `错误: ${error.message}`;
    }
}

// 🔧 优化：从缓存的 Token 统计数据中推算总体 RPM/TPM
// 避免重复发起 /api/admin/token_stats 请求
function updateOverallRatesFromCachedData() {
    try {
        const data = latestTokenStatsData;
        if (!data || !data.model_stats || data.model_stats.length === 0) {
            document.getElementById('overall-rpm-value').textContent = '-';
            document.getElementById('overall-tpm-value').textContent = '-';
            document.getElementById('rate-total-requests').textContent = '0';
            document.getElementById('rate-period-display').textContent =
                currentRatePeriod === 'day' ? '24小时' : '1小时';
            document.getElementById('rate-period-range').textContent = '暂无数据';
            return;
        }

        // 从后端返回的 model_stats 中汇总 RPM/TPM
        // 后端已根据 rpm_period 参数计算好每模型的 rpm 和 tpm
        let totalRpm = 0;
        let totalTpm = 0;
        let totalRequests = 0;

        data.model_stats.forEach(stat => {
            totalRpm += stat.rpm || 0;
            totalTpm += stat.tpm || 0;
            totalRequests += stat.request_count || 0;
        });

        // 更新显示
        document.getElementById('overall-rpm-value').textContent = totalRpm.toFixed(2);
        document.getElementById('overall-tpm-value').textContent =
            totalTpm >= 1000 ? (totalTpm / 1000).toFixed(2) + 'K' : totalTpm.toFixed(0);
        document.getElementById('rate-total-requests').textContent = formatNumber(totalRequests);
        document.getElementById('rate-period-display').textContent =
            currentRatePeriod === 'day' ? '24小时' : '1小时';

        // 更新时间范围显示
        const now = new Date();
        const minutes = currentRatePeriod === 'day' ? 1440 : 60;
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
                    <strong>运行模式:</strong> ${data.mode.mode}
                    ${data.mode.mode === 'battle' ? ` (Target: ${data.mode.target})` : ''}
                </div>
                <div><strong>浏览器状态:</strong> <span class="badge ${data.browser_connected ? 'badge-success' : 'badge-danger'}">${data.browser_connected ? '在线' : '离线'}</span></div>
                <div><strong>标签页数量:</strong> ${data.total_tabs}</div>
                <div><strong>失败请求:</strong> ${data.stats.failed_requests}</div>
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
                            <td style="font-family: monospace; font-size: 12px;">${req.request_id}</td>
                            <td>${req.model}</td>
                            <td><span class="badge badge-info">处理中</span></td>
                            <td>${new Date(req.timestamp * 1000).toLocaleString()}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`
            : '<div class="empty-state"><div class="empty-state-icon">📭</div><p>当前没有活跃请求</p></div>';
        
        document.getElementById('active-requests-list').innerHTML = requestsHtml;
        
        // 🔧 优化：refreshTokenStats 会同时更新 RPM/TPM 卡片
        if (includeRates) {
            refreshTokenStats();
        }
        
    } catch (error) {
        console.error('❌ 刷新概览失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新概览失败: ' + error.message);
    }
}

// ==================== Token 统计 ====================
function applyDateFilter() {
    const startDate = document.getElementById('token-start-date').value;
    const endDate = document.getElementById('token-end-date').value;
    
    if (startDate && endDate && startDate > endDate) {
        alert('开始日期不能晚于结束日期');
        return;
    }
    
    currentStartDate = startDate || null;
    currentEndDate = endDate || null;
    refreshTokenStats();
}

function clearDateFilter() {
    document.getElementById('token-start-date').value = '';
    document.getElementById('token-end-date').value = '';
    currentStartDate = null;
    currentEndDate = null;
    refreshTokenStats();
}

async function refreshTokenStats() {
    try {
        let url = '/api/admin/token_stats';
        const params = new URLSearchParams();
        
        // 日期筛选器用于 Token 统计、成本统计等
        if (currentStartDate) params.append('start_date', currentStartDate);
        if (currentEndDate) params.append('end_date', currentEndDate);
        
        // rpm_period 参数只影响 RPM/TPM 计算，不影响其他统计
        params.append('rpm_period', currentRatePeriod);
        
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
        renderTokenInputPieChart(data.model_stats);
        renderTokenOutputPieChart(data.model_stats);
        renderTokenInputBarChart(data.model_stats);
        renderTokenOutputBarChart(data.model_stats);
        renderTokenTrendChart(data.daily_stats || []);
        renderCostTrendChart(data.daily_stats || []);
        renderTokenStatsTable(data.model_stats);
        
        // 🔧 优化：从 Token 统计数据中直接推算总体 RPM/TPM，不再重复请求
        updateOverallRatesFromCachedData();
        
    } catch (error) {
        console.error('❌ 刷新Token统计失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新Token统计失败: ' + error.message);
    }

}

// ==================== 导出报告 ====================
function exportTokenReport() {
    const startDate = document.getElementById('token-start-date').value;
    const endDate = document.getElementById('token-end-date').value;
    
    let url = '/api/admin/export_report?';
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