// 模型可用性时间线：按模型、按时间段显示上游每次尝试的成功率，请求内部重试几次就算几次；
// 点模型名或格子把下面的请求日志筛到这个模型。客户端自己断开的不算失败，只在提示里单独列出。
const MonitorAvailability = (() => {
    'use strict';
    let data = null;

    function level(item) {
        const decided = item.success + item.failed;
        if (!decided) return 'idle';
        const rate = item.success / decided;
        return rate >= 0.95 ? 'ok' : rate >= 0.5 ? 'warn' : 'bad';
    }

    function rateText(item) {
        const decided = item.success + item.failed;
        // 向下取整到一位小数：有失败时不会显示成 100%
        return decided ? `${Math.floor(item.success / decided * 1000) / 10}%` : '—';
    }

    function clock(seconds) {
        return new Date(seconds * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
    }

    function describe(cell) {
        const begin = data.start + cell.slot * data.bucket_seconds;
        const lines = [`${clock(begin)}–${clock(begin + data.bucket_seconds)}`,
            `${cell.total} 次上游调用：成功 ${cell.success}，失败 ${cell.failed}${cell.cancelled ? `，客户端断开 ${cell.cancelled}` : ''}`];
        // 有重试时，上游调用数和调用方实际发出的请求数不同，两个口径都列出来
        if (cell.requests !== cell.total) lines.push(`来自 ${cell.requests} 个请求，最终失败 ${cell.request_failed}`);
        if (cell.error) lines.push(`最多的错误（${cell.error_count} 次）：${cell.error}`);
        return lines.join('\n');
    }

    function renderRow(row) {
        const cells = new Map(row.cells.map(cell => [cell.slot, cell]));
        const model = escapeHtml(row.model);
        const squares = Array.from({length: data.buckets}, (_, slot) => {
            const cell = cells.get(slot);
            if (!cell) return '<span class="availability-cell"></span>';
            return `<button type="button" class="availability-cell ${level(cell)}" data-model="${model}" `
                + `data-failed="${cell.request_failed}" title="${escapeHtml(describe(cell))}"></button>`;
        }).join('');
        return `<div class="availability-row">
            <button type="button" class="availability-model" data-model="${model}" title="在请求日志里只看这个模型">${model}</button>
            <span class="availability-rate ${level(row)}">${rateText(row)}</span>
            <div class="availability-cells" style="--buckets: ${data.buckets}">${squares}</div>
            <span class="availability-count">${row.success}/${row.success + row.failed}${row.cancelled ? ` · 断开 ${row.cancelled}` : ''}</span>
        </div>`;
    }

    function render() {
        const list = document.getElementById('availability-list');
        if (!data.models.length) {
            list.innerHTML = '<div class="empty-state">这段时间没有请求</div>';
            return;
        }
        const span = data.buckets * data.bucket_seconds;
        list.innerHTML = data.models.map(renderRow).join('') + `<div class="availability-row availability-axis">
            <span></span><span></span>
            <div class="availability-ticks"><span>${clock(data.start)}</span><span>${clock(data.start + span / 2)}</span><span>现在</span></div>
            <span></span>
        </div>`;
    }

    async function refresh() {
        const [hours, minutes] = document.getElementById('availability-range').value.split(':');
        try {
            const response = await apiGet(`/api/monitor/availability?hours=${hours}&bucket_minutes=${minutes}`);
            data = await response.json();
            render();
        } catch (error) {
            document.getElementById('availability-list').innerHTML =
                `<div class="empty-state">加载失败：${escapeHtml(error.message)}</div>`;
        }
    }

    // 有失败的格子只看失败的请求，其余看这个模型的全部请求
    function showInLogs(model, failedOnly) {
        const select = document.getElementById('filter-model');
        if (![...select.options].some(option => option.value === model)) select.add(new Option(model, model));
        select.value = model;
        document.getElementById('filter-status').value = failedOnly ? 'failed' : '';
        if (!logsVisible) toggleLogs();
        if (currentTab !== 'requests') switchTab('requests');
        applyLogFilters();
        document.getElementById('request-log-section').scrollIntoView({behavior: 'smooth', block: 'start'});
    }

    document.getElementById('availability-list').addEventListener('click', event => {
        const target = event.target.closest('[data-model]');
        if (target) showInLogs(target.dataset.model, Number(target.dataset.failed) > 0);
    });
    refresh();
    setInterval(() => { if (isPageActive()) refresh(); }, 60000);
    return {refresh};
})();
