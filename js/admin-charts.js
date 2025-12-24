// admin-charts.js - 图表渲染功能

// Chart.js 实例
let tokenInputPieChart = null;
let tokenOutputPieChart = null;
let tokenInputBarChart = null;
let tokenOutputBarChart = null;
let tokenTrendChart = null;
let requestCountChart = null;

// ==================== Token 饼状图 ====================
function renderTokenInputPieChart(modelStats) {
    const ctx = document.getElementById('tokenInputPieChart');
    if (!ctx) return;
    
    if (tokenInputPieChart) tokenInputPieChart.destroy();
    
    const sortedStats = [...modelStats].sort((a, b) => b.input_tokens - a.input_tokens);
    const topModels = sortedStats.slice(0, 10);
    const labels = topModels.map(s => s.model);
    const dataValues = topModels.map(s => s.input_tokens);
    const colors = generateColors(topModels.length);
    
    tokenInputPieChart = new Chart(ctx, {
        type: 'pie',
        data: {
            labels: labels,
            datasets: [{
                data: dataValues,
                backgroundColor: colors,
                borderColor: 'rgba(42, 168, 255, 0.3)',
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { color: '#d9e5ff', font: { size: 11 }, padding: 10 }
                },
                tooltip: {
                    backgroundColor: 'rgba(14, 26, 45, 0.9)',
                    titleColor: '#2aa8ff',
                    bodyColor: '#d9e5ff',
                    borderColor: '#223650',
                    borderWidth: 1,
                    padding: 12,
                    callbacks: {
                        label: function(context) {
                            const label = context.label || '';
                            const value = formatNumber(context.parsed);
                            const total = context.dataset.data.reduce((a, b) => a + b, 0);
                            const percentage = ((context.parsed / total) * 100).toFixed(1);
                            return `${label}: ${value} 输入tokens (${percentage}%)`;
                        }
                    }
                }
            }
        }
    });
}

function renderTokenOutputPieChart(modelStats) {
    const ctx = document.getElementById('tokenOutputPieChart');
    if (!ctx) return;
    
    if (tokenOutputPieChart) tokenOutputPieChart.destroy();
    
    const sortedStats = [...modelStats].sort((a, b) => b.output_tokens - a.output_tokens);
    const topModels = sortedStats.slice(0, 10);
    const labels = topModels.map(s => s.model);
    const dataValues = topModels.map(s => s.output_tokens);
    const colors = generateColors(topModels.length);
    
    tokenOutputPieChart = new Chart(ctx, {
        type: 'pie',
        data: {
            labels: labels,
            datasets: [{
                data: dataValues,
                backgroundColor: colors,
                borderColor: 'rgba(16, 185, 129, 0.3)',
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { color: '#d9e5ff', font: { size: 11 }, padding: 10 }
                },
                tooltip: {
                    backgroundColor: 'rgba(14, 26, 45, 0.9)',
                    titleColor: '#10b981',
                    bodyColor: '#d9e5ff',
                    borderColor: '#223650',
                    borderWidth: 1,
                    padding: 12,
                    callbacks: {
                        label: function(context) {
                            const label = context.label || '';
                            const value = formatNumber(context.parsed);
                            const total = context.dataset.data.reduce((a, b) => a + b, 0);
                            const percentage = ((context.parsed / total) * 100).toFixed(1);
                            return `${label}: ${value} 输出tokens (${percentage}%)`;
                        }
                    }
                }
            }
        }
    });
}

// ==================== Token 条形图 ====================
function renderTokenInputBarChart(modelStats) {
    const ctx = document.getElementById('tokenInputBarChart');
    if (!ctx) return;
    
    if (tokenInputBarChart) tokenInputBarChart.destroy();
    
    const sortedStats = [...modelStats].sort((a, b) => b.input_tokens - a.input_tokens);
    const topModels = sortedStats.slice(0, 10);
    
    tokenInputBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: topModels.map(s => s.model),
            datasets: [{
                label: '输入 Tokens',
                data: topModels.map(s => s.input_tokens),
                backgroundColor: 'rgba(42, 168, 255, 0.8)',
                borderColor: 'rgba(42, 168, 255, 1)',
                borderWidth: 1
            }]
        },
        options: getBarChartOptions()
    });
}

function renderTokenOutputBarChart(modelStats) {
    const ctx = document.getElementById('tokenOutputBarChart');
    if (!ctx) return;
    
    if (tokenOutputBarChart) tokenOutputBarChart.destroy();
    
    const sortedStats = [...modelStats].sort((a, b) => b.output_tokens - a.output_tokens);
    const topModels = sortedStats.slice(0, 10);
    
    tokenOutputBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: topModels.map(s => s.model),
            datasets: [{
                label: '输出 Tokens',
                data: topModels.map(s => s.output_tokens),
                backgroundColor: 'rgba(16, 185, 129, 0.8)',
                borderColor: 'rgba(16, 185, 129, 1)',
                borderWidth: 1
            }]
        },
        options: getBarChartOptions()
    });
}

function getBarChartOptions() {
    return {
        responsive: true,
        maintainAspectRatio: true,
        scales: {
            x: {
                ticks: { color: '#8fa0bf', font: { size: 10 } },
                grid: { color: 'rgba(34, 54, 80, 0.3)' }
            },
            y: {
                beginAtZero: true,
                ticks: {
                    color: '#8fa0bf',
                    callback: function(value) { return formatNumber(value); }
                },
                grid: { color: 'rgba(34, 54, 80, 0.3)' }
            }
        },
        plugins: {
            legend: { display: false },
            tooltip: {
                backgroundColor: 'rgba(14, 26, 45, 0.9)',
                titleColor: '#2aa8ff',
                bodyColor: '#d9e5ff',
                borderColor: '#223650',
                borderWidth: 1,
                padding: 12
            }
        }
    };
}

// ==================== 趋势图 ====================
function renderTokenTrendChart(dailyStats) {
    const ctx = document.getElementById('tokenTrendChart');
    if (!ctx) return;
    
    if (tokenTrendChart) tokenTrendChart.destroy();
    
    if (!dailyStats || dailyStats.length === 0) {
        tokenTrendChart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } } }
        });
        return;
    }
    
    dailyStats.sort((a, b) => a.date.localeCompare(b.date));
    
    tokenTrendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: dailyStats.map(s => s.date),
            datasets: [
                {
                    label: '总 Tokens',
                    data: dailyStats.map(s => s.total_tokens),
                    borderColor: 'rgba(168, 85, 247, 1)',
                    backgroundColor: 'rgba(168, 85, 247, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4
                },
                {
                    label: '输入 Tokens',
                    data: dailyStats.map(s => s.input_tokens),
                    borderColor: 'rgba(42, 168, 255, 1)',
                    backgroundColor: 'rgba(42, 168, 255, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4
                },
                {
                    label: '输出 Tokens',
                    data: dailyStats.map(s => s.output_tokens),
                    borderColor: 'rgba(16, 185, 129, 1)',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4
                }
            ]
        },
        options: getLineChartOptions()
    });
}

function renderRequestCountChart(dailyStats) {
    const ctx = document.getElementById('requestCountChart');
    if (!ctx) return;
    
    if (requestCountChart) requestCountChart.destroy();
    
    if (!dailyStats || dailyStats.length === 0) {
        requestCountChart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } } }
        });
        return;
    }
    
    dailyStats.sort((a, b) => a.date.localeCompare(b.date));
    
    requestCountChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: dailyStats.map(s => s.date),
            datasets: [
                {
                    label: '总请求数',
                    data: dailyStats.map(s => s.total || 0),
                    borderColor: 'rgba(168, 85, 247, 1)',
                    backgroundColor: 'rgba(168, 85, 247, 0.1)',
                    borderWidth: 3, fill: true, tension: 0.4, pointRadius: 4, pointHoverRadius: 6
                },
                {
                    label: '成功请求',
                    data: dailyStats.map(s => s.success || 0),
                    borderColor: 'rgba(16, 185, 129, 1)',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4, pointRadius: 3, pointHoverRadius: 5
                },
                {
                    label: '失败请求',
                    data: dailyStats.map(s => s.failed || 0),
                    borderColor: 'rgba(239, 68, 68, 1)',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4, pointRadius: 3, pointHoverRadius: 5
                }
            ]
        },
        options: getLineChartOptions()
    });
}

function getLineChartOptions() {
    return {
        responsive: true,
        maintainAspectRatio: true,
        interaction: { mode: 'index', intersect: false },
        scales: {
            x: {
                ticks: { color: '#8fa0bf', maxRotation: 45, minRotation: 45 },
                grid: { color: 'rgba(34, 54, 80, 0.3)' }
            },
            y: {
                beginAtZero: true,
                ticks: {
                    color: '#8fa0bf',
                    callback: function(value) { return formatNumber(value); }
                },
                grid: { color: 'rgba(34, 54, 80, 0.3)' }
            }
        },
        plugins: {
            legend: { labels: { color: '#d9e5ff', font: { size: 12 } } },
            tooltip: {
                backgroundColor: 'rgba(14, 26, 45, 0.9)',
                titleColor: '#2aa8ff',
                bodyColor: '#d9e5ff',
                borderColor: '#223650',
                borderWidth: 1,
                padding: 12
            }
        }
    };
}

// ==================== 统计表格 ====================
function renderTokenStatsTable(modelStats) {
    const container = document.getElementById('token-stats-table');
    if (!container) return;
    
    if (!modelStats || modelStats.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><p>暂无Token统计数据</p></div>';
        return;
    }
    
    container.innerHTML = `
        <div style="margin-bottom: 15px; display: flex; gap: 10px; align-items: center;">
            <button class="btn btn-sm" onclick="toggleAllModelStats()">
                <span id="toggle-all-text">全选</span>
            </button>
            <button class="btn btn-primary btn-sm" onclick="mergeSelectedModelStats()">
                🔗 合并选中
            </button>
            <button class="btn btn-danger btn-sm" onclick="deleteSelectedModelStats()">
                🗑️ 删除选中
            </button>
            <span id="selected-count-display" style="color: var(--text-dim); margin-left: auto;">已选择: 0</span>
        </div>
        <table class="table">
            <thead>
                <tr>
                    <th style="width: 40px;">
                        <input type="checkbox" id="select-all-checkbox" onchange="toggleAllModelStats()" style="cursor: pointer;">
                    </th>
                    <th>模型</th>
                    <th>总 Tokens</th>
                    <th>输入 Tokens</th>
                    <th>输出 Tokens</th>
                    <th>请求数</th>
                    <th>平均 Token/请求</th>
                    <th>RPM</th>
                    <th>TPM</th>
                    <th>总消耗金额</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                ${modelStats.map(stat => {
                    // 格式化RPM和TPM
                    const rpmDisplay = stat.rpm !== undefined && stat.rpm > 0
                        ? `<span style="color: #10b981;">${stat.rpm.toFixed(2)}</span>`
                        : '<span style="color: var(--text-dim);">-</span>';
                    
                    const tpmDisplay = stat.tpm !== undefined && stat.tpm > 0
                        ? `<span style="color: #3b82f6;">${stat.tpm >= 1000 ? (stat.tpm / 1000).toFixed(2) + 'K' : stat.tpm.toFixed(0)}</span>`
                        : '<span style="color: var(--text-dim);">-</span>';
                    
                    // 计算总消耗金额
                    let costDisplay = '-';
                    let costTooltip = '';
                    
                    if (stat.total_cost !== undefined && stat.total_cost !== null && stat.total_cost > 0) {
                        const currency = stat.currency || 'USD';
                        const currencySymbol = currency === 'CNY' ? '¥' : '$';
                        costDisplay = `<span style="color: #f59e0b; font-weight: 600;">${currencySymbol}${stat.total_cost.toFixed(4)}</span>`;
                        
                        // 添加详细信息tooltip
                        if (stat.input_cost || stat.output_cost) {
                            costTooltip = `输入: ${currencySymbol}${(stat.input_cost || 0).toFixed(4)}, 输出: ${currencySymbol}${(stat.output_cost || 0).toFixed(4)}`;
                        }
                    }
                    
                    return `
                        <tr>
                            <td>
                                <input type="checkbox" class="model-stat-checkbox" data-model="${stat.model}" onchange="updateSelectedCount()" style="cursor: pointer;">
                            </td>
                            <td>
                                <strong>${stat.display_name || stat.model}</strong>
                                ${stat.display_name && stat.display_name !== stat.model ? `<br><small style="color: var(--text-dim);">(${stat.model})</small>` : ''}
                            </td>
                            <td><span style="color: var(--accent);">${formatNumber(stat.total_tokens)}</span></td>
                            <td>${formatNumber(stat.input_tokens)}</td>
                            <td>${formatNumber(stat.output_tokens)}</td>
                            <td>${stat.request_count}</td>
                            <td>${stat.request_count > 0 ? formatNumber(Math.round(stat.total_tokens / stat.request_count)) : '-'}</td>
                            <td>${rpmDisplay}</td>
                            <td>${tpmDisplay}</td>
                            <td${costTooltip ? ` title="${costTooltip}"` : ''}>${costDisplay}</td>
                            <td>
                                <button class="btn btn-danger btn-sm" onclick="deleteModelStats('${stat.model.replace(/'/g, "\\'")}')">删除</button>
                            </td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
    `;
    updateSelectedCount();
}