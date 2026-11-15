/**
 * 用量额度：访客 Key、模型、渠道共用的设置表单和用量展示
 *
 * 表单写成 <fieldset data-quota-form="前缀" data-quota-legend="标题">，页面加载时生成内容；
 * 额度和用量的格式见 core/usage_quota.py。
 */

const QUOTA_PERIOD_LABELS = { total: '累计', day: '今日', week: '本周', month: '本月' };
const QUOTA_KIND_LABELS = { cost: '费用', tokens: 'Token', requests: '请求' };
// 管理面板只显示设了额度的模型和渠道：{ models: {名称: 用量}, channels: {渠道 id: 用量} }
let quotaOverview = { models: {}, channels: {} };

function initQuotaForms() {
    document.querySelectorAll('fieldset[data-quota-form]').forEach(fieldset => {
        const prefix = fieldset.dataset.quotaForm;
        const periods = [['total', '累计'], ['day', '每天'], ['week', '每周'], ['month', '每月']];
        fieldset.classList.add('quota-form');
        fieldset.innerHTML = `
            <legend class="form-label">${escapeHtml(fieldset.dataset.quotaLegend || '用量额度')}</legend>
            <div class="quota-period" role="radiogroup" aria-label="统计周期">
                ${periods.map(([value, label]) => `<label><input type="radio" name="${prefix}-quota-period" value="${value}"${value === 'total' ? ' checked' : ''}><span>${label}</span></label>`).join('')}
            </div>
            <div class="quota-grid">
                <div>
                    <label class="form-label" for="${prefix}-cost-limit">费用上限</label>
                    <div class="quota-cost-input">
                        <input type="number" id="${prefix}-cost-limit" class="form-input" value="0" min="0" step="0.01">
                        <select id="${prefix}-cost-currency" class="form-select" aria-label="费用货币">
                            <option value="USD">USD</option>
                            <option value="CNY">CNY</option>
                        </select>
                    </div>
                </div>
                <div>
                    <label class="form-label" for="${prefix}-token-limit">Token 上限</label>
                    <input type="number" id="${prefix}-token-limit" class="form-input" value="0" min="0" step="1">
                </div>
                <div>
                    <label class="form-label" for="${prefix}-request-limit">请求次数上限</label>
                    <input type="number" id="${prefix}-request-limit" class="form-input" value="0" min="0" step="1">
                </div>
            </div>`;
    });
}

function fillQuotaForm(prefix, quota) {
    quota = quota || {};
    document.querySelector(`input[name="${prefix}-quota-period"][value="${quota.period || 'total'}"]`).checked = true;
    document.getElementById(`${prefix}-cost-limit`).value = quota.cost_limit || 0;
    document.getElementById(`${prefix}-cost-currency`).value = quota.cost_currency || 'USD';
    document.getElementById(`${prefix}-token-limit`).value = quota.token_limit || 0;
    document.getElementById(`${prefix}-request-limit`).value = quota.request_limit || 0;
}

// 没设任何上限的累计额度等于不限额，返回 null
function readQuotaForm(prefix) {
    const quota = {
        period: document.querySelector(`input[name="${prefix}-quota-period"]:checked`).value,
        cost_limit: parseFloat(document.getElementById(`${prefix}-cost-limit`).value) || 0,
        cost_currency: document.getElementById(`${prefix}-cost-currency`).value,
        token_limit: parseInt(document.getElementById(`${prefix}-token-limit`).value) || 0,
        request_limit: parseInt(document.getElementById(`${prefix}-request-limit`).value) || 0,
    };
    return quota.period === 'total' && !quota.cost_limit && !quota.token_limit && !quota.request_limit ? null : quota;
}

function formatQuotaTime(timestamp) {
    return new Date(timestamp * 1000).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function formatQuotaAmount(kind, value, currency) {
    if (kind === 'cost') {
        return (currency === 'CNY' ? '¥' : '$') + Number(value || 0).toFixed(4).replace(/\.?0+$/, '');
    }
    return Math.round(value || 0).toLocaleString('en-US');
}

// 每个设了上限的项目一根用量条；没设上限的只把本期已用的费用和 Token 列在下面
function renderQuotaUsage(usage) {
    if (!usage) return '';
    const { used, limits, currency } = usage;
    const head = usage.resets_at
        ? `${QUOTA_PERIOD_LABELS[usage.period]} · ${formatQuotaTime(usage.resets_at)} 重置`
        : `累计 · 自 ${formatQuotaTime(usage.period_start)} 起`;
    let rows = '';
    const extras = [];
    for (const kind of ['cost', 'tokens', 'requests']) {
        const limit = limits[kind] || 0;
        const value = formatQuotaAmount(kind, used[kind], currency);
        if (!limit) {
            if (kind !== 'requests' && used[kind] > 0) extras.push(`${QUOTA_KIND_LABELS[kind]} ${value}`);
            continue;
        }
        // Chromium 把恰好等于 high 的值算作次优（黄色），用尽时把 high 压到 low，整根变红
        const high = used[kind] >= limit ? limit * 0.8 : limit;
        rows += `<div class="quota-usage-row">
            <span>${QUOTA_KIND_LABELS[kind]}</span>
            <span>${value} / ${formatQuotaAmount(kind, limit, currency)}</span>
            <meter min="0" max="${limit}" low="${limit * 0.8}" high="${high}" optimum="0" value="${Math.min(used[kind], limit)}"
                aria-label="${QUOTA_KIND_LABELS[kind]}用量"></meter>
        </div>`;
    }
    if (!rows) rows = '<div style="color: var(--text-dim);">不限</div>';
    const extra = extras.length ? `<div class="quota-usage-head">已用 ${extras.join(' · ')}</div>` : '';
    const inFlight = usage.in_flight ? `<div class="quota-usage-head">进行中 ${usage.in_flight} 个请求</div>` : '';
    return `<div class="quota-usage"><div class="quota-usage-head">${head}</div>${rows}${extra}${inFlight}</div>`;
}

// 列表里的一行小字：用得最满的那一项占上限的百分比
function quotaListBadge(usage) {
    if (!usage) return '';
    if (usage.exhausted.length) return '<span class="badge badge-danger">额度用尽</span>';
    const ratios = ['cost', 'tokens', 'requests'].filter(kind => usage.limits[kind]).map(kind => usage.used[kind] / usage.limits[kind]);
    if (!ratios.length) return '';
    const ratio = Math.max(...ratios);
    return `<span class="badge ${ratio >= 0.8 ? 'badge-warning' : 'badge-info'}" title="${QUOTA_PERIOD_LABELS[usage.period]}额度已用 ${Math.floor(ratio * 100)}%">额度 ${Math.floor(ratio * 100)}%</span>`;
}

// 用量只是附加信息：取不到或格式不对时列表照常显示，只是不标额度
async function loadQuotaOverview() {
    try {
        const response = await fetch('/api/admin/quotas');
        await ensureOk(response);
        const data = await response.json();
        quotaOverview = { models: data.models || {}, channels: data.channels || {} };
    } catch (error) {
        console.error('加载额度用量失败:', error);
    }
    return quotaOverview;
}

// 编辑窗里显示这份额度的本期用量和重置按钮；subject 形如 model:名称、channel:id
function showQuotaUsage(containerId, subject, usage) {
    const container = document.getElementById(containerId);
    if (!usage) {
        container.innerHTML = '';
        return;
    }
    container.innerHTML = `${renderQuotaUsage(usage)}
        <button type="button" class="btn btn-sm" data-subject="${escapeHtml(subject)}" onclick="resetQuotaUsage(this.dataset.subject, '${containerId}')">
            <svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#refresh"/></svg> 重置用量
        </button>`;
}

async function resetQuotaUsage(subject, containerId) {
    if (!(await confirmDialog({ title: '重置用量', message: '确定要清零这份额度当前周期的费用、Token 和请求次数吗？额度用尽时会立即恢复访问。', confirmText: '重置' }))) return;
    try {
        const response = await fetch('/api/admin/quotas/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subject }),
        });
        await ensureOk(response);
        await loadQuotaOverview();
        const [kind, name] = [subject.slice(0, subject.indexOf(':')), subject.slice(subject.indexOf(':') + 1)];
        showQuotaUsage(containerId, subject, quotaOverview[kind === 'model' ? 'models' : 'channels'][name]);
        showMessage('success', '用量已重置');
    } catch (error) {
        showMessage('danger', '重置失败: ' + error.message);
    }
}

initQuotaForms();
