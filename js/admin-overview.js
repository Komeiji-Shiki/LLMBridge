// admin-overview.js - 概览页面功能

// 速率统计周期：'day' 或 'hour'
let currentRatePeriod = 'day';

// 切换速率统计周期
function switchRatePeriod(period) {
    currentRatePeriod = period;
    
    // 更新按钮样式
    document.getElementById('rate-period-day').className =
        period === 'day' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    document.getElementById('rate-period-hour').className =
        period === 'hour' ? 'btn btn-primary btn-sm' : 'btn btn-sm';
    
    // 刷新总体速率统计
    refreshOverallRates();
    
    // 同时刷新Token统计表格（使用相同的时间范围）
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

async function refreshOverview() {
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
        
        // 刷新总体速率统计
        refreshOverallRates();
        
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
        
        if (currentStartDate) params.append('start_date', currentStartDate);
        if (currentEndDate) params.append('end_date', currentEndDate);
        
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
        
        // 更新成本信息
        const totalCost = data.total_cost || 0;
        const inputCost = data.input_cost || 0;
        const outputCost = data.output_cost || 0;
        const currency = data.currency || 'USD';
        
        document.getElementById('total-cost-value').textContent = totalCost.toFixed(4);
        document.getElementById('total-cost-currency').textContent = currency;
        document.getElementById('input-cost-value').textContent = inputCost.toFixed(4);
        document.getElementById('output-cost-value').textContent = outputCost.toFixed(4);
        
        // 渲染图表
        renderTokenInputPieChart(data.model_stats);
        renderTokenOutputPieChart(data.model_stats);
        renderTokenInputBarChart(data.model_stats);
        renderTokenOutputBarChart(data.model_stats);
        renderTokenTrendChart(data.daily_stats || []);
        renderTokenStatsTable(data.model_stats);
        
        // 🔧 优化：不再重复调用 refreshOverallRates()
        // refreshOverallRates() 已经在 refreshOverview() 中调用了
        // 避免重复查询 SQLite 数据库
        
    } catch (error) {
        console.error('❌ 刷新Token统计失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新Token统计失败: ' + error.message);
    }
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