// admin-charts.js - 图表渲染功能

// Chart.js 实例
let tokenInputPieChart = null;
let tokenOutputPieChart = null;
let tokenInputBarChart = null;
let tokenOutputBarChart = null;
let tokenTrendChart = null;
let requestCountChart = null;
let outputAxisSeparate = true; // 输出Token是否使用独立Y轴

// 将模型统计聚合为 Top N + Other
function getTopModelsWithOther(modelStats, tokenField, topN = 10) {
    const sortedStats = [...(modelStats || [])].sort((a, b) => (b[tokenField] || 0) - (a[tokenField] || 0));
    const topModels = sortedStats.slice(0, topN);
    const otherModels = sortedStats.slice(topN);

    const otherValue = otherModels.reduce((sum, item) => sum + (item[tokenField] || 0), 0);
    const labels = topModels.map(s => s.model);
    const dataValues = topModels.map(s => s[tokenField] || 0);

    if (otherValue > 0) {
        labels.push('Other');
        dataValues.push(otherValue);
    }

    return { labels, dataValues };
}

function getTopModelsWithOtherColor(count) {
    if (count <= 0) return [];

    // 预留最后一个颜色给 Other（灰色）
    const hasOther = count > 10;
    if (!hasOther) {
        return generateColors(count);
    }

    const topColors = generateColors(count - 1);
    return [...topColors, 'rgba(148, 163, 184, 0.85)'];
}

// 将数值向上取整到"好看"的刻度值
function niceRound(value) {
    if (value <= 0) return 0.1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const normalized = value / magnitude;
    let nice;
    if (normalized <= 1) nice = 1;
    else if (normalized <= 1.5) nice = 1.5;
    else if (normalized <= 2) nice = 2;
    else if (normalized <= 3) nice = 3;
    else if (normalized <= 5) nice = 5;
    else if (normalized <= 7.5) nice = 7.5;
    else nice = 10;
    return +(nice * magnitude).toPrecision(3);
}

// ==================== Token 饼状图 ====================
function renderTokenInputPieChart(modelStats) {
    const ctx = document.getElementById('tokenInputPieChart');
    if (!ctx) return;
    
    if (tokenInputPieChart) tokenInputPieChart.destroy();
    
    const { labels, dataValues } = getTopModelsWithOther(modelStats, 'input_tokens', 10);

    // 有 Other 时 count 会是 11（前10+Other）
    const colors = getTopModelsWithOtherColor(labels.length);
    
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
    
    const { labels, dataValues } = getTopModelsWithOther(modelStats, 'output_tokens', 10);

    // 有 Other 时 count 会是 11（前10+Other）
    const colors = getTopModelsWithOtherColor(labels.length);
    
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
    
    const { labels, dataValues } = getTopModelsWithOther(modelStats, 'input_tokens', 10);
    const barColors = labels.map(label => label === 'Other' ? 'rgba(148, 163, 184, 0.85)' : 'rgba(42, 168, 255, 0.8)');
    
    tokenInputBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: '输入 Tokens',
                data: dataValues,
                backgroundColor: barColors,
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
    
    const { labels, dataValues } = getTopModelsWithOther(modelStats, 'output_tokens', 10);
    const barColors = labels.map(label => label === 'Other' ? 'rgba(148, 163, 184, 0.85)' : 'rgba(16, 185, 129, 0.8)');
    
    tokenOutputBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: '输出 Tokens',
                data: dataValues,
                backgroundColor: barColors,
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
    
    // 计算每日输出/输入比（保留4位精度）
    const ratioDataRaw = dailyStats.map(s => {
        const input = s.input_tokens || 0;
        return input > 0 ? +(s.output_tokens / input).toFixed(4) : null;
    });
    
    // 计算比率 Y 轴硬上限（基于 IQR 排除异常值，超出部分 clip 掉）
    const validRatios = ratioDataRaw.filter(v => v !== null).sort((a, b) => a - b);
    let ratioMax = undefined;
    if (validRatios.length >= 3) {
        const q1 = validRatios[Math.floor(validRatios.length * 0.25)];
        const q3 = validRatios[Math.floor(validRatios.length * 0.75)];
        const iqr = q3 - q1;
        ratioMax = Math.max(q3 + 2.5 * iqr, q3 * 3, 0.1);
        ratioMax = niceRound(ratioMax);
    } else if (validRatios.length > 0) {
        ratioMax = niceRound(Math.max(...validRatios) * 1.2);
    }
    
    // clip 显示数据到上限，但保留原始值用于 tooltip
    const ratioData = ratioDataRaw.map(v => {
        if (v === null) return null;
        return ratioMax !== undefined && v > ratioMax ? ratioMax : v;
    });
    
    tokenTrendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: dailyStats.map(s => s.date),
            datasets: [
                {
                    label: '输入 Tokens',
                    data: dailyStats.map(s => s.input_tokens),
                    borderColor: 'rgba(42, 168, 255, 1)',
                    backgroundColor: 'rgba(42, 168, 255, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4,
                    yAxisID: 'y'
                },
                {
                    label: '输出 Tokens',
                    data: dailyStats.map(s => s.output_tokens),
                    borderColor: 'rgba(16, 185, 129, 1)',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2, fill: true, tension: 0.4,
                    yAxisID: outputAxisSeparate ? 'y2' : 'y'
                },
                {
                    label: '缓存命中',
                    data: dailyStats.map(s => s.cached_tokens || 0),
                    borderColor: 'rgba(34, 197, 94, 1)',
                    backgroundColor: 'rgba(34, 197, 94, 0.08)',
                    borderWidth: 2, fill: false, tension: 0.4,
                    borderDash: [8, 4],
                    pointStyle: 'triangle',
                    pointRadius: 3, pointHoverRadius: 5,
                    yAxisID: 'y'
                },
                {
                    label: '输出/输入比',
                    data: ratioData,
                    borderColor: 'rgba(251, 191, 36, 1)',
                    backgroundColor: 'rgba(251, 191, 36, 0.08)',
                    borderWidth: 2, fill: false, tension: 0.4,
                    borderDash: [6, 3],
                    pointStyle: 'rectRot',
                    pointRadius: 4, pointHoverRadius: 6,
                    yAxisID: 'y1',
                    spanGaps: true
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            interaction: { mode: 'index', intersect: false },
            scales: {
                x: {
                    ticks: { color: '#8fa0bf', maxRotation: 45, minRotation: 45 },
                    grid: { color: 'rgba(34, 54, 80, 0.3)' }
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    beginAtZero: true,
                    ticks: {
                        color: 'rgba(42, 168, 255, 0.85)',
                        callback: function(value) { return formatNumber(value); }
                    },
                    grid: { color: 'rgba(34, 54, 80, 0.15)' },
                    title: { display: true, text: outputAxisSeparate ? '输入 Tokens' : 'Tokens', color: 'rgba(42, 168, 255, 0.85)' }
                },
                y2: {
                    type: 'linear',
                    position: 'right',
                    beginAtZero: true,
                    display: outputAxisSeparate,
                    ticks: {
                        color: 'rgba(16, 185, 129, 0.85)',
                        callback: function(value) { return formatNumber(value); }
                    },
                    grid: { drawOnChartArea: false },
                    title: { display: true, text: '输出 Tokens', color: 'rgba(16, 185, 129, 0.85)' }
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    beginAtZero: true,
                    max: ratioMax,
                    display: false,  // 隐藏轴刻度，避免右边太挤
                    grid: { drawOnChartArea: false }
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
                    padding: 12,
                    callbacks: {
                        label: function(context) {
                            if (context.dataset.yAxisID === 'y1') {
                                if (context.parsed.y === null) return context.dataset.label + ': N/A';
                                const rawVal = ratioDataRaw[context.dataIndex];
                                if (rawVal === null) return context.dataset.label + ': N/A';
                                const isClipped = ratioMax !== undefined && rawVal > ratioMax;
                                return context.dataset.label + ': ' + rawVal.toFixed(4) + 'x' + (isClipped ? ' ⚠️异常' : '');
                            }
                            return context.dataset.label + ': ' + formatNumber(context.parsed.y);
                        }
                    }
                }
            }
        }
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

// 切换输出Token的Y轴模式
function toggleOutputAxis() {
    outputAxisSeparate = !outputAxisSeparate;
    const btn = document.getElementById('toggle-output-axis-btn');
    if (btn) {
        btn.textContent = outputAxisSeparate ? '🔄 合并Y轴' : '🔄 独立Y轴';
    }
    // 直接更新图表配置，无需重新获取数据
    if (tokenTrendChart) {
        // 切换输出 Tokens 数据集的 yAxisID
        const outputDataset = tokenTrendChart.data.datasets.find(ds => ds.label === '输出 Tokens');
        if (outputDataset) {
            outputDataset.yAxisID = outputAxisSeparate ? 'y2' : 'y';
        }
        // 切换 y2 轴的显示
        tokenTrendChart.options.scales.y2.display = outputAxisSeparate;
        // 更新左轴标题
        tokenTrendChart.options.scales.y.title.text = outputAxisSeparate ? '输入 Tokens' : 'Tokens';
        tokenTrendChart.update();
    }
}

// ==================== 统计表格 ====================
// 排序状态：当前排序列与方向（null 表示保持后端默认顺序）
let tokenStatsSortKey = null;
let tokenStatsSortDir = 'desc';
// 缓存最近一次的数据，点击表头时直接用它重新渲染
let latestModelStatsCache = [];

// 可排序列配置：[排序键, 表头文案]
const TOKEN_STATS_COLUMNS = [
    ['model', '模型'],
    ['total_tokens', '总 Tokens'],
    ['input_tokens', '输入 Tokens'],
    ['output_tokens', '输出 Tokens'],
    ['cached_tokens', '命中缓存'],
    ['request_count', '请求数'],
    ['avg_tokens', '平均 Token/请求'],
    ['rpm', 'RPM'],
    ['tpm', 'TPM'],
    ['total_cost', '总消耗金额'],
];

// 提取用于排序的值（计算列和金额列需要特殊处理）
function getTokenStatSortValue(stat, key) {
    switch (key) {
        case 'model':
            return (stat.display_name || stat.model || '').toLowerCase();
        case 'avg_tokens':
            return stat.request_count > 0 ? stat.total_tokens / stat.request_count : 0;
        case 'total_cost': {
            // 不同模型可能记录不同货币，统一换算成 USD 后再比较
            const cost = stat.total_cost || 0;
            return (stat.currency === 'CNY') ? cost * EXCHANGE_RATE.CNY_TO_USD : cost;
        }
        default:
            return stat[key] || 0;
    }
}

// 点击表头排序：同列切换方向，换列时数值列默认倒序、模型列默认正序
function sortTokenStatsTable(key) {
    if (tokenStatsSortKey === key) {
        tokenStatsSortDir = tokenStatsSortDir === 'desc' ? 'asc' : 'desc';
    } else {
        tokenStatsSortKey = key;
        tokenStatsSortDir = key === 'model' ? 'asc' : 'desc';
    }
    renderTokenStatsTable(latestModelStatsCache);
}

function renderTokenStatsTable(modelStats) {
    const container = document.getElementById('token-stats-table');
    if (!container) return;
    
    modelStats = Array.isArray(modelStats) ? modelStats : [];
    latestModelStatsCache = modelStats;
    
    if (modelStats.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><p>暂无Token统计数据</p></div>';
        return;
    }
    
    // 记录当前勾选状态，重新渲染后恢复（排序/切换货币不丢失选中）
    const checkedModels = new Set(
        Array.from(container.querySelectorAll('.model-stat-checkbox:checked')).map(cb => cb.dataset.model)
    );
    
    // 按当前排序状态生成排序副本（不修改原数组）
    const sortedStats = [...modelStats];
    if (tokenStatsSortKey) {
        const dir = tokenStatsSortDir === 'asc' ? 1 : -1;
        sortedStats.sort((a, b) => {
            const va = getTokenStatSortValue(a, tokenStatsSortKey);
            const vb = getTokenStatSortValue(b, tokenStatsSortKey);
            if (typeof va === 'string' || typeof vb === 'string') {
                return String(va).localeCompare(String(vb)) * dir;
            }
            return (va - vb) * dir;
        });
    }
    
    // 生成可点击排序的表头
    const headerCells = TOKEN_STATS_COLUMNS.map(([key, label]) => {
        const active = tokenStatsSortKey === key;
        const arrow = active ? (tokenStatsSortDir === 'asc' ? ' ▲' : ' ▼') : '';
        return `<th class="sortable-th${active ? ' active' : ''}" onclick="sortTokenStatsTable('${key}')" title="点击排序">${label}${arrow}</th>`;
    }).join('');
    
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
                    ${headerCells}
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                ${sortedStats.map(stat => {
                    // 格式化RPM和TPM
                    const rpmDisplay = stat.rpm !== undefined && stat.rpm > 0
                        ? `<span style="color: #10b981;">${stat.rpm.toFixed(2)}</span>`
                        : '<span style="color: var(--text-dim);">-</span>';
                    
                    const tpmDisplay = stat.tpm !== undefined && stat.tpm > 0
                        ? `<span style="color: #3b82f6;">${stat.tpm >= 1000 ? (stat.tpm / 1000).toFixed(2) + 'K' : stat.tpm.toFixed(0)}</span>`
                        : '<span style="color: var(--text-dim);">-</span>';
                    
                    // 计算总消耗金额（按当前选中货币换算显示）
                    let costDisplay = '-';
                    let costTooltip = '';
                    
                    if (stat.total_cost !== undefined && stat.total_cost !== null && stat.total_cost > 0) {
                        const originalCurrency = stat.currency || 'USD';
                        const displayCurrency = (typeof currentCostCurrency !== 'undefined') ? currentCostCurrency : 'USD';
                        const displaySymbol = displayCurrency === 'CNY' ? '¥' : '$';
                        
                        // 换算成本到目标货币
                        let displayTotalCost = stat.total_cost;
                        let displayInputCost = stat.input_cost || 0;
                        let displayOutputCost = stat.output_cost || 0;
                        
                        if (originalCurrency !== displayCurrency) {
                            const rate = (originalCurrency === 'USD' && displayCurrency === 'CNY')
                                ? EXCHANGE_RATE.USD_TO_CNY
                                : (originalCurrency === 'CNY' && displayCurrency === 'USD')
                                    ? EXCHANGE_RATE.CNY_TO_USD
                                    : 1;
                            displayTotalCost *= rate;
                            displayInputCost *= rate;
                            displayOutputCost *= rate;
                        }
                        
                        costDisplay = `<span style="color: #f59e0b; font-weight: 600;">${displaySymbol}${displayTotalCost.toFixed(4)}</span>`;
                        
                        // 添加详细信息tooltip（显示原始货币和换算值）
                        const originalSymbol = originalCurrency === 'CNY' ? '¥' : '$';
                        if (originalCurrency !== displayCurrency) {
                            costTooltip = `原始: ${originalSymbol}${stat.total_cost.toFixed(4)} ${originalCurrency} | 输入: ${displaySymbol}${displayInputCost.toFixed(4)}, 输出: ${displaySymbol}${displayOutputCost.toFixed(4)}`;
                        } else {
                            costTooltip = `输入: ${displaySymbol}${displayInputCost.toFixed(4)}, 输出: ${displaySymbol}${displayOutputCost.toFixed(4)}`;
                        }
                    }
                    
                    // 缓存命中显示
                    const cachedTokens = stat.cached_tokens || 0;
                    const cachedDisplay = cachedTokens > 0
                        ? `<span style="color: #22c55e; font-weight: 500;" title="缓存命中的输入Token">${formatNumber(cachedTokens)}</span>`
                        : '<span style="color: var(--text-dim);">-</span>';
                    
                    return `
                        <tr>
                            <td>
                                <input type="checkbox" class="model-stat-checkbox" data-model="${escapeHtml(stat.model)}" onchange="updateSelectedCount()" style="cursor: pointer;">
                            </td>
                            <td>
                                <strong>${escapeHtml(stat.display_name || stat.model)}</strong>
                                ${stat.display_name && stat.display_name !== stat.model ? `<br><small style="color: var(--text-dim);">(${escapeHtml(stat.model)})</small>` : ''}
                            </td>
                            <td><span style="color: var(--accent);">${formatNumber(stat.total_tokens)}</span></td>
                            <td>${formatNumber(stat.input_tokens)}</td>
                            <td>${formatNumber(stat.output_tokens)}</td>
                            <td>${cachedDisplay}</td>
                            <td>${stat.request_count}</td>
                            <td>${stat.request_count > 0 ? formatNumber(Math.round(stat.total_tokens / stat.request_count)) : '-'}</td>
                            <td>${rpmDisplay}</td>
                            <td>${tpmDisplay}</td>
                            <td${costTooltip ? ` title="${costTooltip}"` : ''}>${costDisplay}</td>
                            <td>
                                <button class="btn btn-danger btn-sm" data-model="${escapeHtml(stat.model)}" onclick="deleteModelStats(this.dataset.model)">删除</button>
                            </td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
    `;
    // 恢复排序前的勾选状态
    if (checkedModels.size > 0) {
        container.querySelectorAll('.model-stat-checkbox').forEach(cb => {
            if (checkedModels.has(cb.dataset.model)) cb.checked = true;
        });
    }
    updateSelectedCount();
}
// ==================== 成本趋势图 ====================
let costTrendChart = null;
let costCurrencyDisplay = 'USD'; // 默认显示 USD

function renderCostTrendChart(dailyStats) {
    const ctx = document.getElementById('costTrendChart');
    if (!ctx) return;

    if (costTrendChart) costTrendChart.destroy();

    if (!dailyStats || dailyStats.length === 0) {
        costTrendChart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } } }
        });
        return;
    }

    dailyStats.sort((a, b) => a.date.localeCompare(b.date));

    const costField = costCurrencyDisplay === 'CNY' ? 'cost_cny' : 'cost_usd';
    const currencySymbol = costCurrencyDisplay === 'CNY' ? '¥' : '$';
    const costColor = costCurrencyDisplay === 'CNY' ? 'rgba(245, 158, 11, 1)' : 'rgba(34, 197, 94, 1)';
    const costBg = costCurrencyDisplay === 'CNY' ? 'rgba(245, 158, 11, 0.15)' : 'rgba(34, 197, 94, 0.15)';

    costTrendChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: dailyStats.map(s => s.date),
            datasets: [
                {
                    label: `每日成本 (${costCurrencyDisplay})`,
                    data: dailyStats.map(s => s[costField] || 0),
                    backgroundColor: costBg,
                    borderColor: costColor,
                    borderWidth: 2
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            scales: {
                x: {
                    ticks: { color: '#8fa0bf', maxRotation: 45, minRotation: 45 },
                    grid: { color: 'rgba(34, 54, 80, 0.3)' }
                },
                y: {
                    beginAtZero: true,
                    ticks: {
                        color: costColor,
                        callback: function(value) {
                            return currencySymbol + value.toFixed(4);
                        }
                    },
                    grid: { color: 'rgba(34, 54, 80, 0.15)' },
                    title: { display: true, text: `成本 (${costCurrencyDisplay})`, color: costColor }
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
                    padding: 12,
                    callbacks: {
                        label: function(context) {
                            return `成本: ` + currencySymbol + (context.parsed.y || 0).toFixed(6);
                        }
                    }
                }
            }
        }
    });
}

// 切换成本货币显示
function toggleCostCurrency() {
    costCurrencyDisplay = costCurrencyDisplay === 'USD' ? 'CNY' : 'USD';
    const btn = document.getElementById('toggle-cost-currency-btn');
    if (btn) {
        btn.textContent = costCurrencyDisplay === 'USD' ? '💱 切换CNY' : '💱 切换USD';
    }
    // 重新渲染（从缓存数据中取）
    if (typeof latestTokenStatsData !== 'undefined' && latestTokenStatsData) {
        renderCostTrendChart(latestTokenStatsData.daily_stats || []);
    }
}
