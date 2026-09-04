// admin-models-edit.js - 模型编辑功能（API Key、表单、保存、配置项）
'use strict';
// admin-models.js - 模型管理功能

// ==================== 多 API Key 轮询支持 ====================

let _apiKeyRowCounter = 0; // 用于生成唯一的行ID

/**
 * 添加一个 API Key 输入行
 * @param {string} value - 预填的 API Key 值（编辑时使用）
 */
function addApiKeyRow(value = '') {
    _apiKeyRowCounter++;
    const rowId = `api-key-row-${_apiKeyRowCounter}`;
    
    const container = document.getElementById('model-api-keys-list');
    if (!container) {
        console.error('[API-Key] 容器 #model-api-keys-list 未找到！请刷新页面重试。');
        alert('❌ API Key 输入容器未找到，请刷新页面重试。');
        return;
    }
    
    console.log(`[API-Key] 添加新行: rowId=${rowId}, value长度=${value ? value.length : 0}`);
    
    const row = document.createElement('div');
    row.id = rowId;
    row.style.cssText = 'display: flex; gap: 8px; margin-bottom: 8px; align-items: center;';
    row.innerHTML = `
        <input type="text" class="api-key-input form-input" value="${escapeHtmlForAttr(value)}"
            placeholder="sk-...（留空表示不使用此Key）"
            style="flex: 1; padding: 8px; font-size: 0.85rem; font-family: monospace;">
        <span class="key-balance-info" style="display: none; font-size: 0.8rem; white-space: nowrap; min-width: 100px; text-align: right;"></span>
        <button type="button" class="btn btn-sm btn-danger" onclick="removeApiKeyRow('${rowId}')"
            title="删除此 API Key">
            🗑️
        </button>
    `;
    container.appendChild(row);
    console.log(`[API-Key] 行已添加到 DOM`);
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
function clearApiKeyRows() {
    const container = document.getElementById('model-api-keys-list');
    if (!container) {
        console.error('[API-Key] 容器 #model-api-keys-list 未找到（clearApiKeyRows）！');
        return;
    }
    container.innerHTML = '';
    _apiKeyRowCounter = 0;
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
    const keys = getApiKeys();
    
    if (!keys.length) {
        alert('请先添加至少一个 API Key');
        return;
    }
    if (!apiBaseUrl) {
        alert('请先填写 API Base URL');
        return;
    }
    if (!apiBaseUrl.toLowerCase().includes('deepseek')) {
        alert('当前仅 DeepSeek API 支持余额查询。\n\n检测到 API Base URL 不含 "deepseek"，无法使用 /user/balance 接口。');
        return;
    }
    
    // 隐藏之前的余额显示
    document.querySelectorAll('.key-balance-info').forEach(el => {
        el.style.display = 'none';
        el.textContent = '';
    });
    
    const btn = document.getElementById('query-balance-btn');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '⏳ 查询中...'; btn.disabled = true; }
    
    try {
        const resp = await fetch('/api/admin/query_key_balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_keys: keys,
                api_base_url: apiBaseUrl
            })
        });
        const data = await resp.json();
        
        if (data.status === 'unsupported') {
            alert(data.message || '当前 API 不支持余额查询');
            return;
        }
        
        if (data.status === 'error') {
            alert('查询失败: ' + (data.message || '未知错误'));
            return;
        }
        
        // 更新每行的余额显示
        const rows = document.querySelectorAll('#model-api-keys-list > div');
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
                    const color = avail ? '#34d399' : '#f87171';
                    const icon = avail ? '💰' : '⚠️';
                    infoEl.innerHTML = `<span style="color:${color}">${icon} ${main.currency} ${total}</span>`;
                } else {
                    infoEl.innerHTML = `<span style="color:#f87171">⚠️ 无余额数据</span>`;
                }
            } else if (result.status === 'timeout') {
                infoEl.innerHTML = '<span style="color:#fbbf24">⏱️ 超时</span>';
            } else {
                const err = result.error || '查询失败';
                infoEl.innerHTML = `<span style="color:#f87171" title="${escapeHtmlForAttr(err)}">❌ 失败</span>`;
            }
            infoEl.style.display = 'inline';
        }
        
        // 🔧 sticky 策略下：查完余额自动粘性到余额最高的 Key
        await autoStickyToBestBalanceKey(keys, data.results || [], rows);
        
    } catch (e) {
        alert('查询余额请求失败: ' + e.message);
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
                    `<span class="sticky-marker" style="color:#fbbf24; margin-left:6px;" title="已自动粘性到余额最高的 Key">⭐ 已粘性</span>`);
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
function getApiKeys() {
    const inputs = document.querySelectorAll('.api-key-input');
    const keys = [];
    inputs.forEach(input => {
        const value = input.value.trim();
        if (value) {
            keys.push(value);
        }
    });
    return keys;
}

// escapeHtmlForAttr 已上移到 admin-core.js（被本文件与 admin-apikeys.js 共用），
// 顺带补上单引号转义 —— 旧实现不转 ' ，属性用单引号包裹时可被闭合。

// 折叠面板切换函数
function toggleCollapsible(headerElement) {
    const section = headerElement.parentElement;
    section.classList.toggle('collapsed');
}

function showAddModelModal() {
    currentEditingModel = null;
    currentEditingArchived = false;
    document.getElementById('modal-title').textContent = '添加模型';
    document.getElementById('model-name').value = '';
    document.getElementById('model-name').disabled = false;
    
    // 重置所有字段
    document.getElementById('session-id').value = '';
    document.getElementById('mode').value = 'direct_chat';
    document.getElementById('battle-target').value = 'A';
    document.getElementById('battle-target-group').style.display = 'none';
    document.getElementById('model-type').value = 'text';
    document.getElementById('api-type').value = 'direct_api';
    document.getElementById('api-base-url').value = '';
    clearApiKeyRows();
    addApiKeyRow(); // 添加一个空行
    document.getElementById('api-key-strategy').value = 'round_robin';
    document.getElementById('api-key-cooldown').value = '';
    document.getElementById('api-key-cooldown-group').style.display = 'none';
    document.getElementById('endpoint-path').value = '/chat/completions';
    document.getElementById('upstream-protocol').value = 'generate_content';
    document.getElementById('responses-store').checked = false;
    document.getElementById('responses-reasoning-summary').value = '';
    document.getElementById('model-id').value = '';
    document.getElementById('display-name').value = '';
    document.getElementById('passthrough').checked = true;
    document.getElementById('force-stream').value = '';
    document.getElementById('convert-system-to-user').checked = false;
    document.getElementById('enable-prefix').checked = false;
    document.getElementById('enable-partial').checked = false;
    document.getElementById('prefill-content').value = '';
    document.getElementById('enable-thinking').value = '';
    document.getElementById('thinking-control').value = 'budget';
    document.getElementById('thinking-budget').value = '20000';
    document.getElementById('thinking-effort').value = '';
    document.getElementById('thinking-display').value = 'summarized';
    document.getElementById('oai-verbosity').value = '';
    document.getElementById('oai-thinking-type').value = '';
    document.getElementById('oai-thinking-effort').value = '';
    document.getElementById('thinking-separator').value = '';
    document.getElementById('anthropic-auto-cache').checked = false;
    toggleThinkingOptions();
    document.getElementById('pricing-input').value = '';
    document.getElementById('pricing-output').value = '';
    document.getElementById('pricing-unit').value = '1000000';
    document.getElementById('pricing-currency').value = 'USD';
    document.getElementById('pricing-cached-input').value = '';
    document.getElementById('token-stats-mode').value = 'api';
    document.getElementById('custom-params').value = '';
    document.getElementById('extra-body-params').value = '';
    document.getElementById('max-temperature').value = '';
    document.getElementById('lmarena-max-temperature').value = '';
    document.getElementById('max-tokens').value = '';
    document.getElementById('lmarena-max-tokens').value = '';
    document.getElementById('cached-tokens-mode').value = 'reverse';
    document.getElementById('completion-tokens-mode').checked = true;
    
    // 重置自动重试配置
    resetAutoRetryFields();

    // 重置图片压缩配置
    resetImageCompressionFields();
    
    // 重置系统提示词注入配置
    resetSystemInjectionFields();
    
    document.getElementById('config-type').value = 'direct_api';
    toggleConfigType();
    
    document.getElementById('model-modal').classList.add('active');
}

// 重置图片压缩字段
function resetImageCompressionFields() {
    document.getElementById('img-compression-enabled').checked = false;
    document.getElementById('img-target-format').value = '';
    document.getElementById('img-quality').value = '';
    document.getElementById('img-target-size-kb').value = '';
    document.getElementById('img-max-width').value = '';
    document.getElementById('img-max-height').value = '';
    document.getElementById('img-convert-png-to-jpg').checked = false;
    document.getElementById('img-compression-options').style.display = 'none';
}

// 切换图片压缩选项显示
function toggleImageCompressionOptions() {
    const enabled = document.getElementById('img-compression-enabled').checked;
    document.getElementById('img-compression-options').style.display = enabled ? 'block' : 'none';
}

// 重置自动重试字段
function resetAutoRetryFields() {
    document.getElementById('auto-retry-enabled').checked = false;
    document.getElementById('auto-retry-max-retries').value = '2';
    document.getElementById('auto-retry-delay-seconds').value = '2';
    document.getElementById('auto-retry-on-429').checked = true;
    document.getElementById('auto-retry-on-503').checked = true;
    document.getElementById('auto-retry-on-other-errors').checked = false;
    document.getElementById('auto-retry-options').style.display = 'none';
}

// 切换自动重试选项显示
function toggleAutoRetryOptions() {
    const enabled = document.getElementById('auto-retry-enabled').checked;
    document.getElementById('auto-retry-options').style.display = enabled ? 'block' : 'none';
}

// 加载自动重试配置到表单
function loadAutoRetryConfig(autoRetryConfig) {
    if (autoRetryConfig && autoRetryConfig.enabled) {
        document.getElementById('auto-retry-enabled').checked = true;
        document.getElementById('auto-retry-options').style.display = 'block';
        document.getElementById('auto-retry-max-retries').value = autoRetryConfig.max_retries ?? 2;
        document.getElementById('auto-retry-delay-seconds').value = autoRetryConfig.retry_delay_seconds ?? 2;
        document.getElementById('auto-retry-on-429').checked = autoRetryConfig.retry_on_429 !== false;
        document.getElementById('auto-retry-on-503').checked = autoRetryConfig.retry_on_503 !== false;
        document.getElementById('auto-retry-on-other-errors').checked = autoRetryConfig.retry_on_other_errors || false;
    } else {
        resetAutoRetryFields();
    }
}

// 获取自动重试配置
function getAutoRetryConfig() {
    const enabled = document.getElementById('auto-retry-enabled').checked;
    if (!enabled) {
        return null;
    }

    const maxRetries = Math.max(0, parseInt(document.getElementById('auto-retry-max-retries').value) || 0);
    const retryDelaySeconds = Math.max(0, parseFloat(document.getElementById('auto-retry-delay-seconds').value) || 0);

    return {
        enabled: true,
        max_retries: maxRetries,
        retry_delay_seconds: retryDelaySeconds,
        retry_on_429: document.getElementById('auto-retry-on-429').checked,
        retry_on_503: document.getElementById('auto-retry-on-503').checked,
        retry_on_other_errors: document.getElementById('auto-retry-on-other-errors').checked
    };
}

// 初始化图片压缩checkbox事件
document.addEventListener('DOMContentLoaded', function() {
    const imgCompressionCheckbox = document.getElementById('img-compression-enabled');
    if (imgCompressionCheckbox) {
        imgCompressionCheckbox.addEventListener('change', toggleImageCompressionOptions);
    }

    // 初始化自动重试checkbox事件
    const autoRetryCheckbox = document.getElementById('auto-retry-enabled');
    if (autoRetryCheckbox) {
        autoRetryCheckbox.addEventListener('change', toggleAutoRetryOptions);
    }
    
    // 初始化系统提示词注入checkbox事件
    const systemInjectionCheckbox = document.getElementById('system-injection-enabled');
    if (systemInjectionCheckbox) {
        systemInjectionCheckbox.addEventListener('change', toggleSystemInjectionOptions);
    }
});

// ==================== 系统提示词注入功能 ====================

// 切换系统提示词注入选项显示
function toggleSystemInjectionOptions() {
    const enabled = document.getElementById('system-injection-enabled').checked;
    document.getElementById('system-injection-options').style.display = enabled ? 'block' : 'none';
}

// 系统提示词预设模板
const SYSTEM_INJECTION_PRESETS = {
    antigravity: `You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.**Proactiveness**`
};

// 应用系统提示词预设
function applySystemInjectionPreset() {
    const preset = document.getElementById('system-injection-preset').value;
    const contentTextarea = document.getElementById('system-injection-content');
    
    if (preset && SYSTEM_INJECTION_PRESETS[preset]) {
        contentTextarea.value = SYSTEM_INJECTION_PRESETS[preset];
    } else if (preset === 'custom') {
        // 自定义模式，清空内容让用户输入
        contentTextarea.value = '';
        contentTextarea.focus();
    }
}

// ==================== 伪造对话历史注入（整轮/多轮 + 思维链） ====================

let _fakeConvRowCounter = 0;

/**
 * 添加一条伪造对话消息行
 * @param {string} role - user / assistant / system / tool
 * @param {string} content - 消息正文（可留空，例如 assistant 仅思考时）
 * @param {string} reasoning - 思维链 reasoning_content（仅 assistant 生效）
 * @param {string} toolId - tool_call_id（仅 tool 生效）
 */
function addFakeConvRow(role = 'user', content = '', reasoning = '', toolId = '') {
    _fakeConvRowCounter++;
    const rowId = `fake-conv-row-${_fakeConvRowCounter}`;
    const container = document.getElementById('fake-conversation-list');
    if (!container) {
        console.error('[FakeConv] 容器 #fake-conversation-list 未找到！');
        return;
    }

    const row = document.createElement('div');
    row.id = rowId;
    row.className = 'fake-conv-row';
    row.style.cssText = 'border: 1px solid var(--border, #444); border-radius: 6px; padding: 10px; margin-bottom: 10px; background: var(--bg-elevated, #1e1e2e);';
    row.innerHTML = `
        <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
            <select class="fake-conv-role form-select" onchange="toggleFakeConvRowFields('${rowId}')" style="flex: 0 0 130px;">
                <option value="user">user</option>
                <option value="assistant">assistant</option>
                <option value="system">system</option>
                <option value="tool">tool</option>
            </select>
            <span style="flex: 1;"></span>
            <button type="button" class="btn btn-sm btn-danger" onclick="removeFakeConvRow('${rowId}')" title="删除此消息">🗑️</button>
        </div>
        <textarea class="fake-conv-content form-input" rows="2" placeholder="消息内容（可留空，例如 assistant 仅思考时）" style="width: 100%; margin-bottom: 8px;"></textarea>
        <textarea class="fake-conv-reasoning form-input" rows="2" placeholder="reasoning_content 思维链（仅 assistant 生效）" style="width: 100%; margin-bottom: 8px; display: none;"></textarea>
        <input type="text" class="fake-conv-tool-id form-input" placeholder="tool_call_id（仅 tool 生效）" style="width: 100%; display: none;">
    `;

    row.querySelector('.fake-conv-role').value = role;
    row.querySelector('.fake-conv-content').value = content;
    row.querySelector('.fake-conv-reasoning').value = reasoning;
    row.querySelector('.fake-conv-tool-id').value = toolId;
    row.querySelector('.fake-conv-reasoning').style.display = role === 'assistant' ? 'block' : 'none';
    row.querySelector('.fake-conv-tool-id').style.display = role === 'tool' ? 'block' : 'none';

    container.appendChild(row);
}

/**
 * 删除一条伪造对话消息行
 */
function removeFakeConvRow(rowId) {
    const row = document.getElementById(rowId);
    if (row) row.remove();
}

/**
 * 切换单行内字段显示（reasoning_content 仅 assistant，tool_call_id 仅 tool）
 */
function toggleFakeConvRowFields(rowId) {
    const row = document.getElementById(rowId);
    if (!row) return;
    const role = row.querySelector('.fake-conv-role').value;
    const reasoningEl = row.querySelector('.fake-conv-reasoning');
    const toolIdEl = row.querySelector('.fake-conv-tool-id');
    if (reasoningEl) reasoningEl.style.display = role === 'assistant' ? 'block' : 'none';
    if (toolIdEl) toolIdEl.style.display = role === 'tool' ? 'block' : 'none';
}

/**
 * 添加一整轮对话（user + assistant 各一条）
 */
function addFakeConvTurn() {
    addFakeConvRow('user', '', '');
    addFakeConvRow('assistant', '', '');
}

/**
 * 清空所有伪造对话消息行
 */
function resetFakeConvRows() {
    const container = document.getElementById('fake-conversation-list');
    if (container) container.innerHTML = '';
    _fakeConvRowCounter = 0;
}

/**
 * 从 JSON 数组导入伪造对话（支持直接粘贴 DeepSeek 官方 messages 片段）
 */
function importFakeConversationFromJson() {
    const raw = prompt('请粘贴伪造对话的 JSON 数组，例如：\n[{"role":"user","content":"..."},{"role":"assistant","content":"...","reasoning_content":"..."}]');
    if (raw === null) return;
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (e) {
        alert('JSON 解析失败：' + e.message);
        return;
    }
    if (!Array.isArray(parsed)) {
        alert('请粘贴一个 JSON 数组');
        return;
    }
    let added = 0;
    for (const item of parsed) {
        if (!item || typeof item !== 'object') continue;
        const role = item.role || 'assistant';
        const content = typeof item.content === 'string' ? item.content : (item.content == null ? '' : JSON.stringify(item.content));
        const reasoning = typeof item.reasoning_content === 'string' ? item.reasoning_content : '';
        const toolId = typeof item.tool_call_id === 'string' ? item.tool_call_id : '';
        addFakeConvRow(role, content, reasoning, toolId);
        added++;
    }
    if (added === 0) alert('未导入任何有效消息');
}

/**
 * 收集伪造对话配置
 */
function getFakeConversation() {
    const rows = document.querySelectorAll('#fake-conversation-list .fake-conv-row');
    const list = [];
    rows.forEach(row => {
        const role = row.querySelector('.fake-conv-role')?.value || 'assistant';
        const content = row.querySelector('.fake-conv-content')?.value || '';
        const reasoning = row.querySelector('.fake-conv-reasoning')?.value || '';
        const toolId = row.querySelector('.fake-conv-tool-id')?.value || '';
        const item = { role, content };
        if (role === 'assistant' && reasoning) item.reasoning_content = reasoning;
        if (role === 'tool' && toolId) item.tool_call_id = toolId;
        list.push(item);
    });
    return list;
}

/**
 * 加载伪造对话配置到行编辑器
 */
function loadFakeConversation(list) {
    resetFakeConvRows();
    if (!Array.isArray(list) || list.length === 0) return;
    for (const item of list) {
        if (!item || typeof item !== 'object') continue;
        const role = item.role || 'assistant';
        const content = typeof item.content === 'string' ? item.content : (item.content == null ? '' : JSON.stringify(item.content));
        const reasoning = typeof item.reasoning_content === 'string' ? item.reasoning_content : '';
        const toolId = typeof item.tool_call_id === 'string' ? item.tool_call_id : '';
        addFakeConvRow(role, content, reasoning, toolId);
    }
}

// 重置系统提示词注入字段
function resetSystemInjectionFields() {
    document.getElementById('system-injection-enabled').checked = false;
    document.getElementById('system-injection-position').value = 'before_system';
    document.getElementById('system-injection-preset').value = '';
    document.getElementById('system-injection-content').value = '';
    document.getElementById('system-injection-options').style.display = 'none';
    resetFakeConvRows();
}

// 加载系统提示词注入配置到表单
function loadSystemInjectionConfig(injectionConfig) {
    if (injectionConfig && injectionConfig.enabled) {
        document.getElementById('system-injection-enabled').checked = true;
        document.getElementById('system-injection-options').style.display = 'block';
        
        document.getElementById('system-injection-position').value = injectionConfig.position || 'before_system';
        document.getElementById('system-injection-content').value = injectionConfig.content || '';
        
        // 检查是否匹配预设
        const content = injectionConfig.content || '';
        let matchedPreset = 'custom';
        for (const [key, presetContent] of Object.entries(SYSTEM_INJECTION_PRESETS)) {
            if (content === presetContent) {
                matchedPreset = key;
                break;
            }
        }
        document.getElementById('system-injection-preset').value = matchedPreset;

        // 加载伪造对话历史
        loadFakeConversation(injectionConfig.fake_conversation);
    } else {
        resetSystemInjectionFields();
    }
}

// 获取系统提示词注入配置
function getSystemInjectionConfig() {
    const enabled = document.getElementById('system-injection-enabled').checked;
    if (!enabled) {
        return null;
    }
    
    const content = document.getElementById('system-injection-content').value.trim();
    const fakeConversation = getFakeConversation();
    
    const config = {
        enabled: true,
        position: document.getElementById('system-injection-position').value,
        content: content
    };
    if (fakeConversation.length > 0) {
        config.fake_conversation = fakeConversation;
    }
    return config;
}

// 加载图片压缩配置到表单
function loadImageCompressionConfig(imgConfig) {
    if (imgConfig && imgConfig.enabled) {
        document.getElementById('img-compression-enabled').checked = true;
        document.getElementById('img-compression-options').style.display = 'block';
        
        document.getElementById('img-target-format').value = imgConfig.target_format || '';
        document.getElementById('img-quality').value = imgConfig.quality || imgConfig.jpeg_quality || '';
        document.getElementById('img-target-size-kb').value = imgConfig.target_size_kb || '';
        document.getElementById('img-max-width').value = imgConfig.max_width || '';
        document.getElementById('img-max-height').value = imgConfig.max_height || '';
        document.getElementById('img-convert-png-to-jpg').checked = imgConfig.convert_png_to_jpg || false;
    } else {
        resetImageCompressionFields();
    }
}

// 获取图片压缩配置
function getImageCompressionConfig() {
    const enabled = document.getElementById('img-compression-enabled').checked;
    if (!enabled) {
        return null;
    }
    
    const config = {
        enabled: true
    };
    
    const targetFormat = document.getElementById('img-target-format').value;
    if (targetFormat) config.target_format = targetFormat;
    
    const quality = document.getElementById('img-quality').value;
    if (quality) config.quality = parseInt(quality);
    
    const targetSizeKb = document.getElementById('img-target-size-kb').value;
    if (targetSizeKb) config.target_size_kb = parseInt(targetSizeKb);
    
    const maxWidth = document.getElementById('img-max-width').value;
    if (maxWidth) config.max_width = parseInt(maxWidth);
    
    const maxHeight = document.getElementById('img-max-height').value;
    if (maxHeight) config.max_height = parseInt(maxHeight);
    
    const convertPngToJpg = document.getElementById('img-convert-png-to-jpg').checked;
    if (convertPngToJpg) config.convert_png_to_jpg = true;
    
    return config;
}

// 复制模型配置
function copyModel(name, config) {
    currentEditingModel = null;  // 设为null表示新建模式
    currentEditingArchived = false;  // 复制出的新模型保持活跃，不带归档状态
    document.getElementById('modal-title').textContent = '复制模型';
    document.getElementById('model-name').value = name + '_copy';  // 默认添加_copy后缀
    document.getElementById('model-name').disabled = false;  // 允许修改名称
    
    // 复用editModel的配置填充逻辑
    fillModelForm(config);
    
    document.getElementById('model-modal').classList.add('active');
    
    // 聚焦到模型名称输入框，方便用户修改
    setTimeout(() => {
        const nameInput = document.getElementById('model-name');
        nameInput.focus();
        nameInput.select();
    }, 100);
}

function editModel(name, config) {
    currentEditingModel = name;
    // 编辑保存时保留归档状态（表单里没有归档选项，归档/恢复走列表页按钮）
    currentEditingArchived = !!((Array.isArray(config) ? config[0] : config) || {}).archived;
    document.getElementById('modal-title').textContent = '编辑模型';
    document.getElementById('model-name').value = name;
    // 允许在编辑时修改模型名称（重命名）
    document.getElementById('model-name').disabled = false;
    
    fillModelForm(config);
    
    document.getElementById('model-modal').classList.add('active');
}

// 填充模型表单（editModel和copyModel共用）
function fillModelForm(config) {
    
    if (config.api_type === 'direct_api' || config.api_type === 'responses_native' || config.api_type === 'gemini_native' || config.api_type === 'anthropic_native') {
        document.getElementById('config-type').value = 'direct_api';
        document.getElementById('api-type').value = config.api_type || 'direct_api';
        document.getElementById('api-base-url').value = config.api_base_url || '';
        
        // 🔧 多 API Key 轮询支持：兼容 api_keys 数组和 api_key 字符串
        clearApiKeyRows();
        if (config.api_keys && Array.isArray(config.api_keys) && config.api_keys.length > 0) {
            // 多个 key
            config.api_keys.forEach(key => addApiKeyRow(key));
        } else if (config.api_key && typeof config.api_key === 'string' && config.api_key.trim()) {
            // 单个 key（向后兼容）
            addApiKeyRow(config.api_key);
        } else {
            // 空行
            addApiKeyRow();
        }
        
        // 🔧 轮询策略
        const strategy = config.api_key_strategy || 'round_robin';
        document.getElementById('api-key-strategy').value = strategy;
        const cooldown = config.api_key_cooldown_seconds;
        document.getElementById('api-key-cooldown').value = (cooldown && cooldown > 0) ? cooldown : '';
        toggleApiKeyStrategyOptions();
        
        document.getElementById('model-id').value = config.model_id || '';
        document.getElementById('display-name').value = config.display_name || '';
        document.getElementById('endpoint-path').value = config.endpoint_path || '/chat/completions';
        // Gemini 上游协议（generate_content 为默认值）
        document.getElementById('upstream-protocol').value = config.upstream_protocol || 'generate_content';
        document.getElementById('passthrough').checked = config.passthrough !== false;
        document.getElementById('sanitize-recursive-schemas').checked = config.sanitize_recursive_schemas !== false;
        document.getElementById('force-stream').value = config.force_stream !== undefined ? String(config.force_stream) : '';
        document.getElementById('convert-system-to-user').checked = config.convert_system_to_user || false;
        document.getElementById('enable-prefix').checked = config.enable_prefix || false;
        document.getElementById('enable-partial').checked = config.enable_partial || false;
        document.getElementById('prefill-content').value = config.prefill_content || '';
        // 思维链模式：兼容旧的 thinking_mode 配置和布尔值 enable_thinking
        const et = config.enable_thinking;
        const tm = config.thinking_mode;
        if (et === true || et === 'enabled') {
            document.getElementById('enable-thinking').value = 'true';
        } else if (et === false || et === 'disabled') {
            document.getElementById('enable-thinking').value = 'false';
        } else if (et === 'adaptive' || tm === 'adaptive') {
            document.getElementById('enable-thinking').value = 'adaptive';
        } else {
            document.getElementById('enable-thinking').value = '';
        }
        document.getElementById('thinking-budget').value = config.thinking_budget || 20000;
        // 思考控制方式：配置了 reasoning_effort 则为强度等级模式，否则为 Token 预算模式
        document.getElementById('thinking-control').value = config.reasoning_effort ? 'effort' : 'budget';
        // thinking_effort（adaptive 模式）/ reasoning_effort（启用思考模式）共用同一个下拉框
        document.getElementById('thinking-effort').value = config.reasoning_effort || config.thinking_effort || '';
        document.getElementById('responses-store').checked = config.responses_store === true;
        document.getElementById('responses-reasoning-summary').value = config.responses_reasoning_summary || '';
        document.getElementById('oai-verbosity').value = config.verbosity || '';
        document.getElementById('oai-thinking-type').value = config.oai_thinking_type || '';
        document.getElementById('oai-thinking-effort').value = config.oai_thinking_effort || '';
        // 自动提示词缓存（仅 Anthropic 原生格式消费）
        document.getElementById('anthropic-auto-cache').checked = config.auto_cache === true;
        toggleThinkingOptions();
        document.getElementById('thinking-separator').value = config.thinking_separator || '';
        
        // 加载自定义参数
        if (config.custom_params) {
            document.getElementById('custom-params').value = JSON.stringify(config.custom_params, null, 2);
        } else {
            document.getElementById('custom-params').value = '';
        }
        
        // 加载附加主体参数
        if (config.extra_body_params) {
            document.getElementById('extra-body-params').value = JSON.stringify(config.extra_body_params, null, 2);
        } else {
            document.getElementById('extra-body-params').value = '';
        }
        
        if (config.pricing) {
            document.getElementById('pricing-input').value = config.pricing.input || '';
            document.getElementById('pricing-output').value = config.pricing.output || '';
            document.getElementById('pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('pricing-currency').value = config.pricing.currency || 'USD';
            document.getElementById('pricing-cached-input').value = config.pricing.cached_input || '';
        } else {
            // 重置计费配置
            document.getElementById('pricing-input').value = '';
            document.getElementById('pricing-output').value = '';
            document.getElementById('pricing-unit').value = '1000000';
            document.getElementById('pricing-currency').value = 'USD';
            document.getElementById('pricing-cached-input').value = '';
        }
        
        // 加载最高温度限制
        document.getElementById('max-temperature').value = config.max_temperature || '';
        
        // 加载最大输出Token限制
        document.getElementById('max-tokens').value = config.max_tokens || '';
        
        // 加载缓存Token统计模式
        document.getElementById('cached-tokens-mode').value = config.cached_tokens_mode || 'reverse';
        
        // 加载Token统计来源
        document.getElementById('token-stats-mode').value = config.token_stats_mode || 'api';

        // 加载思考Token是否计入输出Token（不写入配置时为默认相加）
        document.getElementById('completion-tokens-mode').checked = config.completion_tokens_mode !== 'separate';
        
        // 加载图片压缩配置
        loadImageCompressionConfig(config.image_compression);
        
        // 加载自动重试配置
        loadAutoRetryConfig(config.auto_retry);
        
        // 加载系统提示词注入配置
        loadSystemInjectionConfig(config.system_prompt_injection);
    } else {
        document.getElementById('config-type').value = 'lmarena';
        document.getElementById('session-id').value = config.session_id || '';
        document.getElementById('mode').value = config.mode || 'direct_chat';
        document.getElementById('battle-target').value = config.battle_target || 'A';
        document.getElementById('model-type').value = config.type || 'text';
        document.getElementById('lmarena-display-name').value = config.display_name || '';
        
        if (config.pricing) {
            document.getElementById('lmarena-pricing-input').value = config.pricing.input || '';
            document.getElementById('lmarena-pricing-output').value = config.pricing.output || '';
            document.getElementById('lmarena-pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('lmarena-pricing-currency').value = config.pricing.currency || 'USD';
            document.getElementById('lmarena-pricing-cached-input').value = config.pricing.cached_input || '';
        } else {
            // 重置计费配置
            document.getElementById('lmarena-pricing-input').value = '';
            document.getElementById('lmarena-pricing-output').value = '';
            document.getElementById('lmarena-pricing-unit').value = '1000000';
            document.getElementById('lmarena-pricing-currency').value = 'USD';
            document.getElementById('lmarena-pricing-cached-input').value = '';
        }
        
        // 加载最高温度限制
        document.getElementById('lmarena-max-temperature').value = config.max_temperature || '';
        
        // 加载最大输出Token限制
        document.getElementById('lmarena-max-tokens').value = config.max_tokens || '';
        
        toggleBattleTarget();
        
        // 非 Direct API 模型重置自动重试字段，防止沿用上次编辑值
        resetAutoRetryFields();
    }
    
    toggleConfigType();
}

function closeModelModal() {
    document.getElementById('model-modal').classList.remove('active');
}

async function saveModel() {
    const modelName = document.getElementById('model-name').value.trim();
    const configType = document.getElementById('config-type').value;
    
    if (!modelName) {
        alert('请输入模型名称');
        return;
    }
    
    let config = {};
    
    if (configType === 'direct_api') {
        const apiBaseUrl = document.getElementById('api-base-url').value.trim();
        const apiKeys = getApiKeys(); // 🔧 多 API Key 轮询：获取所有非空 key
        const endpointPathInput = document.getElementById('endpoint-path').value.trim();
        const apiType = document.getElementById('api-type').value;
        
        // 🔧 Responses 原生上游需要 URL，与普通 OpenAI/Anthropic 兼容上游一样
        if (['direct_api', 'responses_native', 'anthropic_native'].includes(apiType) && !apiBaseUrl) {
            const labels = {
                direct_api: 'OpenAI兼容格式',
                responses_native: 'Responses 原生格式',
                anthropic_native: 'Anthropic原生格式',
            };
            alert(`${labels[apiType]}需要填写 API Base URL`);
            return;
        }
        
        // 🔧 多 API Key 轮询：至少要有一个有效 key
        if (apiKeys.length === 0) {
            if (!confirm('未填写任何 API Key，将使用无认证模式访问上游 API。确定继续？')) {
                return;
            }
        }
        
        // Gemini原生格式既不需要api_base_url也不需要api_key（可以使用默认地址）
        // 但如果两者都没填，给个警告
        if (apiType === 'gemini_native' && !apiBaseUrl && apiKeys.length === 0) {
            if (!confirm('未填写API Base URL和API Key，将使用Google官方地址。确定继续？')) {
                return;
            }
        }
        
        const thinkingSeparator = document.getElementById('thinking-separator').value.trim();
        
        config = {
            api_type: apiType,
            model_id: document.getElementById('model-id').value.trim() || modelName,
            display_name: document.getElementById('display-name').value.trim() || modelName,
            // passthrough 仅对 api_type=direct_api 生效（gemini/responses/anthropic
            // 都在 direct_api_handler 的分发中先于该判断返回），所以这里不管当前选的是
            // 哪种协议都原样保存勾选值。旧版在 responses_native 下强制写 false，
            // 导致“切到 Responses 保存再切回 OpenAI 兼容”后透传开关被莫名关闭。
            passthrough: document.getElementById('passthrough').checked,
            sanitize_recursive_schemas: document.getElementById('sanitize-recursive-schemas').checked,
            convert_system_to_user: document.getElementById('convert-system-to-user').checked,
            enable_prefix: document.getElementById('enable-prefix').checked,
            enable_partial: document.getElementById('enable-partial').checked,
        };

        // 强制流式/非流式：仅在非空时写入配置
        const forceStreamVal = document.getElementById('force-stream').value;
        if (forceStreamVal === 'true') {
            config.force_stream = true;
        } else if (forceStreamVal === 'false') {
            config.force_stream = false;
        }

        // 思维链模式：四态处理
        // true/false 保存为布尔值（兼容 OpenRouter/OpenAI 模式），adaptive 保存为字符串
        const etVal = document.getElementById('enable-thinking').value;
        if (etVal === 'true') {
            config.enable_thinking = true;
            const controlMode = document.getElementById('thinking-control').value;
            const effortLevel = document.getElementById('thinking-effort').value;
            if (controlMode === 'effort' && effortLevel) {
                // 强度等级控制：保存 reasoning_effort，不写 thinking_budget
                config.reasoning_effort = effortLevel;
            } else {
                // Token 预算控制（默认，向后兼容）
                config.thinking_budget = parseInt(document.getElementById('thinking-budget').value) || 20000;
            }
        } else if (etVal === 'false') {
            config.enable_thinking = false;
        } else if (etVal === 'adaptive') {
            config.enable_thinking = 'adaptive';
            // thinking_effort 仅在显式选择时才保存，留空时不注入 output_config
            const effort = document.getElementById('thinking-effort').value;
            if (effort) {
                config.thinking_effort = effort;
            }
        }
        // etVal === '' 时不设置 enable_thinking 字段（透传客户端参数）

        // thinking_display 仅 Anthropic 原生上游消费（thinking.display 为 Messages API 专有字段）
        if ((etVal === 'true' || etVal === 'adaptive') && apiType === 'anthropic_native') {
            const displayVal = document.getElementById('thinking-display').value;
            if (displayVal) {
                config.thinking_display = displayVal;
            }
        }

        // Responses 原生上游专属配置
        if (apiType === 'responses_native') {
            config.responses_store = document.getElementById('responses-store').checked;
            const summary = document.getElementById('responses-reasoning-summary').value;
            if (summary) {
                config.responses_reasoning_summary = summary;
            }
        }

        // 自动提示词缓存：仅 Anthropic 原生格式生效
        if (apiType === 'anthropic_native' && document.getElementById('anthropic-auto-cache').checked) {
            config.auto_cache = true;
        }

        // verbosity 对 OpenAI Chat 和 Responses 原生上游均可转换
        if (apiType === 'direct_api' || apiType === 'responses_native') {
            const verbosityVal = document.getElementById('oai-verbosity').value;
            if (verbosityVal) {
                config.verbosity = verbosityVal;
            }
        }

        // OAI 兼容 Anthropic thinking 参数仅用于 Chat Completions 上游
        if (apiType === 'direct_api') {
            const oaiThinkingType = document.getElementById('oai-thinking-type').value;
            if (oaiThinkingType) {
                config.oai_thinking_type = oaiThinkingType;
            }
            const oaiThinkingEffort = document.getElementById('oai-thinking-effort').value;
            if (oaiThinkingEffort) {
                config.oai_thinking_effort = oaiThinkingEffort;
            }
        }

        // 预填充内容：只在非空时保存
        const prefillContent = document.getElementById('prefill-content').value;
        if (prefillContent && prefillContent.trim()) {
            config.prefill_content = prefillContent;
        }

        // 可选模型端点：默认 /chat/completions，留空或默认值则不写入配置
        if (endpointPathInput) {
            let normalizedEndpointPath = endpointPathInput;
            if (!normalizedEndpointPath.startsWith('/')) {
                normalizedEndpointPath = '/' + normalizedEndpointPath;
            }
            if (normalizedEndpointPath !== '/chat/completions') {
                config.endpoint_path = normalizedEndpointPath;
            }
        }

        // Gemini 上游协议：仅 gemini_native 且选择 interactions 时写入（generate_content 为默认值）
        if (apiType === 'gemini_native' && document.getElementById('upstream-protocol').value === 'interactions') {
            config.upstream_protocol = 'interactions';
        }
        
        // 🔧 只在有值时添加api_base_url字段
        if (apiBaseUrl) {
            config.api_base_url = apiBaseUrl;
        }
        
        // 🔧 多 API Key 轮询：根据 key 数量决定存储格式
        if (apiKeys.length > 1) {
            // 多个 key，使用 api_keys 数组
            config.api_keys = apiKeys;
        } else if (apiKeys.length === 1) {
            // 单个 key，使用 api_key 字符串（保持向后兼容）
            config.api_key = apiKeys[0];
        }
        // 0 个 key 时不添加任何字段（无认证模式）
        
        // 🔧 轮询策略：非默认值时才写入配置
        const apiKeyStrategy = document.getElementById('api-key-strategy').value;
        if (apiKeyStrategy && apiKeyStrategy !== 'round_robin') {
            config.api_key_strategy = apiKeyStrategy;
        }
        if (apiKeyStrategy === 'sticky') {
            const cooldownVal = parseInt(document.getElementById('api-key-cooldown').value, 10);
            if (cooldownVal > 0) {
                config.api_key_cooldown_seconds = cooldownVal;
            }
        }
        
        // 只在有值时添加thinking_separator字段
        if (thinkingSeparator) {
            config.thinking_separator = thinkingSeparator;
        }
        
        // 处理自定义参数
        const customParamsStr = document.getElementById('custom-params').value.trim();
        if (customParamsStr) {
            try {
                const customParams = JSON.parse(customParamsStr);
                config.custom_params = customParams;
            } catch (e) {
                alert('自定义参数格式错误，请输入有效的JSON格式\n错误: ' + e.message);
                return;
            }
        }
        
        // 处理附加主体参数
        const extraBodyParamsStr = document.getElementById('extra-body-params').value.trim();
        if (extraBodyParamsStr) {
            try {
                const extraBodyParams = JSON.parse(extraBodyParamsStr);
                config.extra_body_params = extraBodyParams;
            } catch (e) {
                alert('附加主体参数格式错误，请输入有效的JSON格式\n错误: ' + e.message);
                return;
            }
        }
        
        const pricingInput = document.getElementById('pricing-input').value;
        const pricingOutput = document.getElementById('pricing-output').value;
        
        if (pricingInput || pricingOutput) {
            const cachedInput = document.getElementById('pricing-cached-input').value;
            config.pricing = {
                input: parseFloat(pricingInput) || 0,
                output: parseFloat(pricingOutput) || 0,
                unit: parseInt(document.getElementById('pricing-unit').value) || 1000000,
                currency: document.getElementById('pricing-currency').value,
                ...(cachedInput ? { cached_input: parseFloat(cachedInput) || 0 } : {})
            };
        }
        
        // 保存最高温度限制
        const maxTemperature = document.getElementById('max-temperature').value.trim();
        if (maxTemperature) {
            config.max_temperature = parseFloat(maxTemperature);
        }
        
        // 保存最大输出Token限制
        const maxTokens = document.getElementById('max-tokens').value.trim();
        if (maxTokens) {
            config.max_tokens = parseInt(maxTokens);
        }
        
        // 保存缓存Token统计模式
        const cachedTokensMode = document.getElementById('cached-tokens-mode').value;
        if (cachedTokensMode && cachedTokensMode !== 'reverse') {
            config.cached_tokens_mode = cachedTokensMode;
        }
        
        // 保存Token统计来源（默认api不写入配置）
        const tokenStatsMode = document.getElementById('token-stats-mode').value;
        if (tokenStatsMode && tokenStatsMode !== 'api') {
            config.token_stats_mode = tokenStatsMode;
        }

        // 保存思考Token口径（勾选=相加为总输出，为默认值不写入配置）
        if (!document.getElementById('completion-tokens-mode').checked) {
            config.completion_tokens_mode = 'separate';
        }
        
        // 保存图片压缩配置
        const imageCompressionConfig = getImageCompressionConfig();
        if (imageCompressionConfig) {
            config.image_compression = imageCompressionConfig;
        }
        
        // 保存系统提示词注入配置
        const systemInjectionConfig = getSystemInjectionConfig();
        if (systemInjectionConfig) {
            config.system_prompt_injection = systemInjectionConfig;
        }

        // 保存自动重试配置
        const autoRetryConfig = getAutoRetryConfig();
        if (autoRetryConfig) {
            config.auto_retry = autoRetryConfig;
        }
    } else {
        const sessionId = document.getElementById('session-id').value.trim();
        const mode = document.getElementById('mode').value;
        
        if (!sessionId) {
            alert('请填写Session ID');
            return;
        }
        
        config = {
            session_id: sessionId,
            mode: mode,
            type: document.getElementById('model-type').value,
            display_name: document.getElementById('lmarena-display-name').value.trim() || modelName
        };
        
        if (mode === 'battle') {
            config.battle_target = document.getElementById('battle-target').value;
        }
        
        const lmarenaPricingInput = document.getElementById('lmarena-pricing-input').value;
        const lmarenaPricingOutput = document.getElementById('lmarena-pricing-output').value;
        
        if (lmarenaPricingInput || lmarenaPricingOutput) {
            const lmarenaCachedInput = document.getElementById('lmarena-pricing-cached-input').value;
            config.pricing = {
                input: parseFloat(lmarenaPricingInput) || 0,
                output: parseFloat(lmarenaPricingOutput) || 0,
                unit: parseInt(document.getElementById('lmarena-pricing-unit').value) || 1000000,
                currency: document.getElementById('lmarena-pricing-currency').value,
                ...(lmarenaCachedInput ? { cached_input: parseFloat(lmarenaCachedInput) || 0 } : {})
            };
        }
        
        // 保存最高温度限制
        const lmarenaMaxTemperature = document.getElementById('lmarena-max-temperature').value.trim();
        if (lmarenaMaxTemperature) {
            config.max_temperature = parseFloat(lmarenaMaxTemperature);
        }
        
        // 保存最大输出Token限制
        const lmarenaMaxTokens = document.getElementById('lmarena-max-tokens').value.trim();
        if (lmarenaMaxTokens) {
            config.max_tokens = parseInt(lmarenaMaxTokens);
        }
    }

    // 编辑已归档模型时保留归档状态（不因编辑保存而意外解除归档）
    if (currentEditingArchived) {
        config.archived = true;
    }

    try {
        // 🔧 修复：编辑模式下改名时传 old_model_name，让后端做重命名而非新增。
        // 旧版不论修改与否都发同名 POST，改名=新增重复模型，旧配置继续存活。
        const body = { model_name: modelName, config: config };
        if (currentEditingModel && currentEditingModel !== modelName) {
            body.old_model_name = currentEditingModel;
        }

        const response = await fetch('/api/admin/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
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
        
        closeModelModal();
        loadModels();
        showMessage('success', `模型 ${modelName} 保存成功`);
        
    } catch (error) {
        console.error('❌ 保存模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '保存失败: ' + error.message);
    }
}


// Responses 原生上游字段与端点联动
function syncResponsesApiOptions() {
    const apiType = document.getElementById('api-type')?.value || 'direct_api';
    const options = document.getElementById('responses-native-options');
    if (options) options.style.display = apiType === 'responses_native' ? 'block' : 'none';
    const passthroughConfig = document.getElementById('passthrough-config');
    if (passthroughConfig) passthroughConfig.style.display = apiType === 'responses_native' ? 'none' : 'block';

    const endpointInput = document.getElementById('endpoint-path');
    if (!endpointInput) return;
    const endpoint = endpointInput.value.trim();
    if (apiType === 'responses_native' && (!endpoint || endpoint === '/chat/completions')) {
        endpointInput.value = '/responses';
    } else if (apiType !== 'responses_native' && endpoint === '/responses') {
        endpointInput.value = apiType === 'anthropic_native' ? '/messages' : '/chat/completions';
    }
}

// 思维链模式下拉框联动：根据选择动态显示 budget或 effort
function toggleThinkingOptions() {
    syncResponsesApiOptions();
    const select = document.getElementById('enable-thinking');
    const optionsDiv = document.getElementById('thinking-options');
    const controlDiv = document.getElementById('thinking-control-config');
    const budgetDiv = document.getElementById('thinking-budget-config');
    const effortDiv = document.getElementById('thinking-effort-config');
    const displayDiv = document.getElementById('thinking-display-config');
    if (!select || !optionsDiv) return;
    const val = select.value;
    const apiType = document.getElementById('api-type')?.value || 'direct_api';
    // 启用思考或自适应思考时显示子选项区域
    const showOptions = (val === 'true' || val === 'adaptive');
    optionsDiv.style.display = showOptions ? 'block' : 'none';
    // verbosity 对 OpenAI Chat 和 Responses 原生上游均可转换
    const verbosityDiv = document.getElementById('verbosity-config');
    if (verbosityDiv) verbosityDiv.style.display = (apiType === 'direct_api' || apiType === 'responses_native') ? 'block' : 'none';
    // Gemini 上游协议选择仅 gemini_native 显示
    const upstreamProtocolDiv = document.getElementById('upstream-protocol-config');
    if (upstreamProtocolDiv) upstreamProtocolDiv.style.display = (apiType === 'gemini_native') ? 'block' : 'none';
    // OAI 兼容 thinking.type / output_config.effort 仅对 OpenAI 兼容上游显示，
    // Anthropic 原生格式下由顶层 enable_thinking 控制，显示这两项只会误导
    const oaiThinkingTypeDiv = document.getElementById('oai-thinking-type-config');
    const oaiThinkingEffortDiv = document.getElementById('oai-thinking-effort-config');
    if (oaiThinkingTypeDiv) oaiThinkingTypeDiv.style.display = (apiType === 'direct_api') ? 'block' : 'none';
    if (oaiThinkingEffortDiv) oaiThinkingEffortDiv.style.display = (apiType === 'direct_api') ? 'block' : 'none';
    // 自动提示词缓存仅对 Anthropic 原生上游显示
    const autoCacheDiv = document.getElementById('anthropic-auto-cache-config');
    if (autoCacheDiv) autoCacheDiv.style.display = (apiType === 'anthropic_native') ? 'block' : 'none';
    if (val === 'true') {
        // 启用思考：显示控制方式选择器，按控制方式切换 budget / effort
        const controlMode = document.getElementById('thinking-control')?.value || 'budget';
        if (controlDiv) controlDiv.style.display = 'block';
        if (budgetDiv) budgetDiv.style.display = (controlMode === 'budget') ? 'block' : 'none';
        if (effortDiv) effortDiv.style.display = (controlMode === 'effort') ? 'block' : 'none';
    } else {
        // 自适应思考（仅 Anthropic）：只有 effort，无 budget
        if (controlDiv) controlDiv.style.display = 'none';
        if (budgetDiv) budgetDiv.style.display = 'none';
        if (effortDiv) effortDiv.style.display = (val === 'adaptive') ? 'block' : 'none';
    }
    // thinking.display 为 Anthropic Messages API 专有字段，仅 anthropic_native 上游显示
    if (displayDiv) displayDiv.style.display = (showOptions && apiType === 'anthropic_native') ? 'block' : 'none';
}
