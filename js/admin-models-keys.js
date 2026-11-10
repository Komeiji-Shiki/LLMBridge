// admin-models-edit.js - 模型编辑功能（API Key、表单、保存、配置项）
'use strict';
// admin-models.js - 模型管理功能

// ==================== 多 API Key 轮询支持 ====================

let _apiKeyRowCounter = 0; // 用于生成唯一的行ID

/**
 * 添加一个 API Key 输入行
 * @param {string} value - 预填的 API Key 值（编辑时使用）
 * @param {string} listId - 放进哪个列表：模型编辑窗（默认）或渠道编辑窗
 */
function addApiKeyRow(value = '', listId = 'model-api-keys-list') {
    _apiKeyRowCounter++;
    const rowId = `api-key-row-${_apiKeyRowCounter}`;

    const container = document.getElementById(listId);
    if (!container) {
        console.error('[API-Key] 容器 #model-api-keys-list 未找到！请刷新页面重试。');
        showMessage('warning', 'API Key 输入容器未找到，请刷新页面重试。');
        return;
    }

    console.log(`[API-Key] 添加新行: rowId=${rowId}, value长度=${value ? value.length : 0}`);

    const row = document.createElement('div');
    row.id = rowId;
    row.style.cssText = 'display: flex; gap: 8px; margin-bottom: 8px; align-items: center;';
    row.innerHTML = `
        <input type="password" autocomplete="off" aria-label="上游 API Key" class="api-key-input form-input" value="${escapeHtmlForAttr(value)}"
            placeholder="sk-...（留空表示不使用此Key）"
            style="flex: 1; padding: 8px; font-size: 0.85rem; font-family: monospace;">
        <button type="button" class="btn btn-sm" aria-label="显示 API Key" onclick="const input = this.parentElement.querySelector('.api-key-input'); input.type = input.type === 'password' ? 'text' : 'password'; this.textContent = input.type === 'password' ? '显示' : '隐藏'; this.setAttribute('aria-label', this.textContent + ' API Key');">显示</button>
        <span class="key-balance-info" style="display: none; font-size: 0.8rem; white-space: nowrap; min-width: 100px; text-align: right;"></span>
        <button type="button" class="btn btn-sm btn-danger" onclick="removeApiKeyRow('${rowId}')"
            title="删除此 API Key" aria-label="删除此 API Key">
            <svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#trash"/></svg>
        </button>
    `;
    container.appendChild(row);
    updateApiKeyInputHints();
    console.log(`[API-Key] 行已添加到 DOM`);
}

// Google OAuth 模式下这一栏填的是凭据，提示跟着鉴权方式切换
function updateApiKeyInputHints() {
    for (const [listId, authModeId] of [['model-api-keys-list', 'api-key-auth-mode'], ['channel-api-keys-list', 'channel-auth-mode']]) {
        const oauth = document.getElementById(authModeId).value === 'google-oauth';
        document.querySelectorAll(`#${listId} .api-key-input`).forEach(input => {
            input.placeholder = oauth
                ? '凭据文件路径（如 C:/secrets/vertex-sa.json）或单行凭据 JSON'
                : 'sk-...（留空表示不使用此Key）';
            input.setAttribute('aria-label', oauth ? 'Google 凭据' : '上游 API Key');
        });
    }
}

/**
 * 删除一个 API Key 输入行
 */
function removeApiKeyRow(rowId) {
    const row = document.getElementById(rowId);
    if (row) {
        row.remove();
    }
}

/**
 * 清空所有 API Key 输入行
 */
function clearApiKeyRows(listId = 'model-api-keys-list') {
    const container = document.getElementById(listId);
    if (!container) {
        console.error('[API-Key] 容器 #model-api-keys-list 未找到（clearApiKeyRows）！');
        return;
    }
    // 行 ID 的计数不归零：模型和渠道两个列表共用，归零会撞 ID
    container.innerHTML = '';
}

/**
 * 切换轮询策略选项的显示（sticky 时显示冷却时长）
 */
function toggleApiKeyStrategyOptions() {
    const strategySelect = document.getElementById('api-key-strategy');
    const cooldownGroup = document.getElementById('api-key-cooldown-group');
    if (strategySelect && cooldownGroup) {
        cooldownGroup.style.display = strategySelect.value === 'sticky' ? 'flex' : 'none';
    }
}

/**
 * 查询所有 API Key 的余额（仅 DeepSeek 等支持 /user/balance 的 API）
 */
async function queryKeyBalances() {
    const apiBaseUrl = document.getElementById('api-base-url').value.trim();
    const keyRows = Array.from(document.querySelectorAll('#model-api-keys-list > div')).filter(row => row.querySelector('.api-key-input').value.trim());
    const keys = keyRows.map(row => row.querySelector('.api-key-input').value.trim());

    if (!keys.length) {
        showMessage('warning', '请先添加至少一个 API Key');
        return;
    }
    if (!apiBaseUrl) {
        showMessage('warning', '请先填写 API Base URL');
        return;
    }

    // 隐藏之前的余额显示
    document.querySelectorAll('.key-balance-info').forEach(el => {
        el.style.display = 'none';
        el.textContent = '';
    });

    const btn = document.getElementById('query-balance-btn');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '查询中...'; btn.disabled = true; }

    try {
        const resp = await fetch('/api/admin/query_key_balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_keys: keys,
                api_base_url: apiBaseUrl,
                provider: document.getElementById('model-provider').value
            })
        });
        const data = await resp.json();

        if (data.status === 'unsupported') {
            showMessage('warning', data.message || '当前 API 不支持余额查询');
            return;
        }

        if (data.status === 'error') {
            showMessage('danger', '查询失败: ' + (data.message || '未知错误'));
            return;
        }

        // 更新每行的余额显示
        const rows = keyRows;
        for (const result of (data.results || [])) {
            const idx = result.index;
            const row = rows[idx];
            if (!row) continue;
            const infoEl = row.querySelector('.key-balance-info');
            if (!infoEl) continue;

            if (result.status === 'ok' && result.balance) {
                const b = result.balance;
                const avail = b.is_available;
                const infos = b.infos || [];
                if (infos.length > 0) {
                    const main = infos[0];
                    const total = parseFloat(main.total).toFixed(2);
                    const color = avail ? 'var(--ok)' : 'var(--danger)';
                    infoEl.innerHTML = `<span style="color:${color}">${escapeHtml(main.currency)} ${total}</span>`;
                } else {
                    infoEl.innerHTML = '<span style="color:var(--danger)">无余额数据</span>';
                }
            } else if (result.status === 'timeout') {
                infoEl.innerHTML = '<span style="color:var(--warn)">超时</span>';
            } else {
                const err = result.error || '查询失败';
                infoEl.innerHTML = `<span style="color:var(--danger)" title="${escapeHtmlForAttr(err)}">失败</span>`;
            }
            infoEl.style.display = 'inline';
        }

        // 🔧 sticky 策略下：查完余额自动粘性到余额最高的 Key
        await autoStickyToBestBalanceKey(keys, data.results || [], rows);

    } catch (e) {
        showMessage('danger', '查询余额请求失败: ' + e.message);
    } finally {
        if (btn) { btn.textContent = origText; btn.disabled = false; }
    }
}

/**
 * 查余额后自动将 sticky 轮询的 current 设为余额最高的 Key（仅 sticky 策略生效）
 * @param {string[]} keys - 完整 API Key 数组（与 results 的 index 一一对应）
 * @param {Array} results - 余额查询结果列表
 * @param {NodeList} rows - #model-api-keys-list 下的 key 行 DOM
 */
async function autoStickyToBestBalanceKey(keys, results, rows) {
    const strategyEl = document.getElementById('api-key-strategy');
    if (!strategyEl || strategyEl.value !== 'sticky') {
        return; // 非 sticky 策略不处理
    }

    const modelName = (document.getElementById('model-name')?.value || '').trim();
    if (!modelName) {
        console.warn('[API-Key] 模型名称为空，跳过自动粘性设置');
        return;
    }

    // 找出余额最高的 key：优先 is_available，同可用性下按 total 比较
    let bestIdx = -1;
    let bestTotal = -Infinity;
    let bestAvail = false;
    for (const r of (results || [])) {
        if (r.status !== 'ok' || !r.balance) continue;
        const infos = r.balance.infos || [];
        if (!infos.length) continue;
        const total = parseFloat(infos[0].total);
        if (isNaN(total)) continue;
        const avail = !!r.balance.is_available;
        if (bestIdx === -1 || (avail && !bestAvail) || (avail === bestAvail && total > bestTotal)) {
            bestIdx = r.index;
            bestTotal = total;
            bestAvail = avail;
        }
    }

    if (bestIdx < 0 || bestIdx >= keys.length) {
        console.warn('[API-Key] 没有可用的余额数据，跳过自动粘性设置');
        return;
    }

    const bestKey = keys[bestIdx];
    const preview = bestKey.length > 10 ? bestKey.slice(0, 6) + '...' + bestKey.slice(-4) : '***';

    try {
        const resp = await fetch('/api/admin/set_sticky_key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_name: modelName, api_key: bestKey })
        });
        const data = await resp.json();

        if (data.status === 'done') {
            // 在余额最高的 key 行标记「已粘性」
            const row = rows && rows[bestIdx];
            const infoEl = row && row.querySelector('.key-balance-info');
            if (infoEl && !infoEl.querySelector('.sticky-marker')) {
                infoEl.insertAdjacentHTML('beforeend',
                    `<span class="sticky-marker" style="color:var(--warn); margin-left:6px;" title="已自动粘性到余额最高的 Key">已粘性</span>`);
            }
            console.log(`[API-Key] 🎯 已自动粘性到余额最高的 Key: ${preview}（余额 ${bestTotal}）`);
        } else {
            console.warn('[API-Key] 自动粘性设置未生效: ' + (data.message || '未知原因'));
        }
    } catch (e) {
        console.warn('[API-Key] 自动粘性设置请求失败: ' + e.message);
    }
}

/**
 * 获取所有非空的 API Key
 * @returns {string[]} 非空 API Key 数组
 */
function getApiKeys(listId = 'model-api-keys-list') {
    const inputs = document.querySelectorAll(`#${listId} .api-key-input`);
    const keys = [];
    inputs.forEach(input => {
        const value = input.value.trim();
        if (value) {
            keys.push(value);
        }
    });
    return keys;
}

// 一次粘贴多行：每行一把 Key，第一把填进当前输入框，其余各加一行；列表里已有的不重复加
document.addEventListener('paste', event => {
    const input = event.target instanceof Element ? event.target.closest('.api-key-input') : null;
    const list = input?.closest('#model-api-keys-list, #channel-api-keys-list');
    const pasted = (event.clipboardData?.getData('text') || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (!list || pasted.length < 2) return;
    event.preventDefault();
    const existing = new Set(getApiKeys(list.id));
    existing.delete(input.value.trim());
    const fresh = [...new Set(pasted)].filter(key => !existing.has(key));
    if (!fresh.length) return;
    input.value = fresh[0];
    fresh.slice(1).forEach(key => addApiKeyRow(key, list.id));
    input.dispatchEvent(new Event('input', { bubbles: true }));
});

// escapeHtmlForAttr 已上移到 admin-core.js（被本文件与 admin-apikeys.js 共用），
// 顺带补上单引号转义 —— 旧实现不转 ' ，属性用单引号包裹时可被闭合。

// 折叠面板切换函数
