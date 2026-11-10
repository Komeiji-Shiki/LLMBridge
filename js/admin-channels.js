// admin-channels.js - 渠道：多个模型共用的上游地址和 Key
// 模型配置里只留 channel 引用，地址、Key、鉴权方式和轮询策略都在渠道上，换 Key 只改一处。
// 渠道表放在 modelWorkspace.channels（模型列表和编辑窗也要用），这里只多记引用关系、整理建议和列表的显示状态。
'use strict';

// 排序方式记在浏览器里
const CHANNEL_SORT_KEY = 'llmbridge.channels.sort';
// 可排序的列：[排序键, 标题, 第一次点是否从大到小]
const CHANNEL_COLUMNS = [['name', '渠道', false], ['address', '服务地址', false], ['keys', 'Key', true], ['models', '模型', true]];

const channelWorkspace = { usage: {}, suggestions: [], editing: null, snapshot: '', expanded: new Set(), sort: loadChannelSort() };

function loadChannelSort() {
    try {
        const saved = JSON.parse(localStorage.getItem(CHANNEL_SORT_KEY));
        if (saved && CHANNEL_COLUMNS.some(([key]) => key === saved.key)) return { key: saved.key, desc: Boolean(saved.desc) };
    } catch { /* 存的内容不对就用默认顺序 */ }
    return { key: 'name', desc: false };
}

function channelHost(channel) {
    if (!channel.api_base_url) return '协议默认地址';
    try { return new URL(channel.api_base_url).host; } catch { return channel.api_base_url; }
}

function channelKeySummary(channel) {
    const count = (channel.api_keys || []).length;
    if (!count) return '无需认证';
    return `${count} 个 Key` + (count > 1 ? (channel.api_key_strategy === 'sticky' ? ' · 粘性轮询' : ' · 逐个轮询') : '');
}

// 只露头尾，够认出是哪一把
function channelKeyPreview(key) {
    return key.length > 10 ? `${key.slice(0, 6)}…${key.slice(-4)}` : '***';
}

function channelOptions() {
    return Object.entries(modelWorkspace.channels)
        .sort(([, a], [, b]) => a.name.localeCompare(b.name, 'zh'))
        .map(([id, channel]) => [id, `${channel.name} · ${channelHost(channel)} · ${channelKeySummary(channel)}`]);
}

async function postChannelApi(path, body = {}) {
    const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    await ensureOk(response);
    return response.json();
}

// ==================== 渠道列表 ====================

async function loadChannels() {
    try {
        const [listing, preview] = await Promise.all(['/api/admin/channels', '/api/admin/channels/suggest'].map(async path => {
            const response = await fetch(path);
            await ensureOk(response);
            return response.json();
        }));
        modelWorkspace.channels = listing.channels || {};
        channelWorkspace.usage = listing.usage || {};
        channelWorkspace.suggestions = preview.suggestions || [];
        renderChannels();
    } catch (error) {
        console.error('加载渠道失败:', error);
        showMessage('danger', '加载渠道失败: ' + error.message);
    }
}

function channelSortValue(id, channel, key) {
    if (key === 'keys') return (channel.api_keys || []).length;
    if (key === 'models') return (channelWorkspace.usage[id] || []).length;
    return key === 'address' ? (channel.api_base_url || '') : channel.name;
}

function sortedChannelEntries() {
    const { key, desc } = channelWorkspace.sort;
    return Object.entries(modelWorkspace.channels).sort(([idA, a], [idB, b]) => {
        const left = channelSortValue(idA, a, key), right = channelSortValue(idB, b, key);
        const order = typeof left === 'number' ? left - right : left.localeCompare(right, 'zh');
        // 数量相同的按名称排，顺序不会跳
        return (desc ? -order : order) || a.name.localeCompare(b.name, 'zh');
    });
}

function sortChannels(key) {
    const current = channelWorkspace.sort;
    channelWorkspace.sort = current.key === key
        ? { key, desc: !current.desc }
        : { key, desc: CHANNEL_COLUMNS.find(column => column[0] === key)[2] };
    localStorage.setItem(CHANNEL_SORT_KEY, JSON.stringify(channelWorkspace.sort));
    renderChannels();
}

// 渠道名、地址、用它的模型、Key 片段都能搜；多个词要同时命中
function channelMatches(id, channel, words) {
    if (!words.length) return true;
    const text = [channel.name, channel.api_base_url, ...(channel.api_keys || []), ...(channelWorkspace.usage[id] || [])].join('\n').toLowerCase();
    return words.every(word => text.includes(word));
}

function buildChannelHeader() {
    const { key, desc } = channelWorkspace.sort;
    return CHANNEL_COLUMNS.map(([column, label]) => {
        const active = column === key;
        const arrow = active ? (desc ? ' ▼' : ' ▲') : '';
        return `<th class="sortable-th${active ? ' active' : ''}" aria-sort="${active ? (desc ? 'descending' : 'ascending') : 'none'}" onclick="sortChannels('${column}')" title="点击排序，再点一次反向">${label}${arrow}</th>`;
    }).join('') + '<th>操作</th>';
}

function buildChannelRow(id, channel) {
    const attr = escapeHtml(id);
    const users = channelWorkspace.usage[id] || [];
    const keys = channel.api_keys || [];
    const open = users.length > 0 && channelWorkspace.expanded.has(id);
    const address = escapeHtml(channel.api_base_url || '协议默认地址');
    const previews = keys.slice(0, 2).map(channelKeyPreview).join('、') + (keys.length > 2 ? ' 等' : '');
    const row = `<tr data-channel-id="${attr}"${users.length ? '' : ' class="channel-idle"'}>
        <td><button class="model-name" data-channel-id="${attr}" onclick="openChannelModal(this.dataset.channelId)">${escapeHtml(channel.name)}</button></td>
        <td class="channel-address" title="${address}">${address}</td>
        <td>${channelKeySummary(channel)}${keys.length ? `<div class="model-subtitle channel-key-preview">${escapeHtml(previews)}</div>` : ''}</td>
        <td>${users.length
            ? `<button class="btn btn-sm" aria-expanded="${open}" data-channel-id="${attr}" onclick="toggleChannelModels(this.dataset.channelId)" title="展开看是哪些模型">${users.length} 个模型 ${open ? '▼' : '▶'}</button>`
            : '<span class="channel-unused">没有模型在用</span>'}</td>
        <td><div class="model-actions">
            <button class="btn btn-sm" data-channel-id="${attr}" onclick="openChannelModal(this.dataset.channelId)">编辑</button>
            <button class="btn btn-sm" data-channel-id="${attr}" onclick="openChannelModal(null, this.dataset.channelId)" title="照这个渠道新建一个，比如同一个地址换一组 Key">复制</button>
            <button class="btn btn-danger btn-sm" data-channel-id="${attr}" onclick="deleteChannel(this.dataset.channelId)">删除</button>
        </div></td></tr>`;
    if (!open) return row;
    return row + `<tr class="channel-models-row"><td colspan="5"><div class="channel-models">
        ${users.map(name => `<button class="badge badge-info" data-model-name="${escapeHtml(name)}" onclick="navigateTo('models', this.dataset.modelName)" title="打开这个模型的设置">${escapeHtml(name)}</button>`).join('')}
        <button class="btn btn-sm" data-channel-id="${attr}" onclick="showChannelModels(this.dataset.channelId)">在模型列表里查看</button>
    </div></td></tr>`;
}

function renderChannels() {
    const total = Object.keys(modelWorkspace.channels).length;
    const words = document.getElementById('channels-query').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const entries = sortedChannelEntries().filter(([id, channel]) => channelMatches(id, channel, words));

    const suggestions = channelWorkspace.suggestions;
    document.getElementById('channels-suggest').hidden = !suggestions.length;
    if (suggestions.length) {
        const models = suggestions.reduce((sum, item) => sum + item.models.length, 0);
        const created = suggestions.filter(item => !item.channel_id).length;
        const merged = suggestions.length - created;
        const plan = [created ? `新建 ${created} 个渠道` : '', merged ? `并入 ${merged} 个已有渠道` : ''].filter(Boolean).join('、');
        document.getElementById('channels-suggest-text').textContent =
            `有 ${models} 个模型的地址和 Key 跟别的模型完全相同，可以${plan}。整理后这些模型照常可用，换 Key 只改渠道一处。`;
    }

    const inUse = Object.values(channelWorkspace.usage).reduce((sum, users) => sum + users.length, 0);
    const idle = Object.keys(modelWorkspace.channels).filter(id => !(channelWorkspace.usage[id] || []).length).length;
    document.querySelector('.channels-toolbar').hidden = !total;
    document.getElementById('channels-summary').textContent = `${total} 个渠道 · ${inUse} 个模型在用`
        + (idle ? ` · ${idle} 个渠道没有模型在用` : '') + (words.length ? ` · 显示 ${entries.length} 个` : '');
    document.getElementById('channels-no-results').hidden = !total || entries.length > 0;
    document.getElementById('channels-list').innerHTML = entries.length
        ? `<table class="table">
            <thead><tr>${buildChannelHeader()}</tr></thead>
            <tbody>${entries.map(([id, channel]) => buildChannelRow(id, channel)).join('')}</tbody>
        </table>`
        : (total ? '' : '<div class="empty-state"><div class="empty-state-icon"><svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#link"/></svg></div><p>还没有渠道<br/>新建一个，或者把地址和 Key 相同的现有模型整理成渠道</p></div>');
}

function toggleChannelModels(id) {
    if (!channelWorkspace.expanded.delete(id)) channelWorkspace.expanded.add(id);
    renderChannels();
}

// 从渠道跳到模型列表，只看用这个渠道的模型
function showChannelModels(id) {
    for (const field of ['models-query', 'models-protocol']) document.getElementById(field).value = '';
    modelWorkspace.pendingGroup = 'ch:' + id;
    navigateTo('models');
}

// ==================== 整理现有模型 ====================

async function adoptChannelSuggestions() {
    const lines = channelWorkspace.suggestions.map(item => {
        const names = item.models.slice(0, 4).join('、') + (item.models.length > 4 ? ` 等 ${item.models.length} 个` : '');
        return `${item.channel_id ? '并入' : '新建'} ${item.name}（${item.key_count} 个 Key）：${names}`;
    });
    if (!(await confirmDialog({
        title: '把现有模型整理成渠道',
        message: `下面每一组模型的地址、Key、鉴权方式和轮询策略完全相同：\n\n${lines.join('\n')}\n\n渠道名先按地址取，之后可以改。整理前的配置会备份到 .migration_backups。`,
        confirmText: '整理成渠道'
    }))) return;
    try {
        const result = await postChannelApi('/api/admin/channels/adopt');
        showMessage('success', (result.message || '整理完成') + (result.backup ? `，整理前的配置备份在 ${result.backup}` : ''));
    } catch (error) {
        console.error('整理渠道失败:', error);
        showMessage('danger', '整理失败: ' + error.message);
    }
    loadChannels();
}

// ==================== 渠道编辑窗 ====================

function toggleChannelStrategyOptions() {
    const sticky = document.getElementById('channel-key-strategy').value === 'sticky';
    document.getElementById('channel-key-cooldown-group').hidden = !sticky;
}

function channelFormSnapshot() {
    // 测 Key 用哪个模型不属于渠道内容
    return JSON.stringify(Array.from(document.querySelectorAll('#channel-modal input, #channel-modal select'))
        .filter(el => el.id !== 'channel-test-model').map(el => el.value));
}

function updateChannelSaveStatus() {
    document.getElementById('channel-save-status').textContent =
        channelWorkspace.snapshot !== channelFormSnapshot() ? '有未保存的修改' : '修改后点击保存 · Ctrl / ⌘ + S';
}

// id 是要编辑的渠道；只给 copyFrom 时照着它新建一个
function openChannelModal(id = null, copyFrom = null) {
    const sourceId = id || copyFrom;
    if (sourceId && !modelWorkspace.channels[sourceId]) {
        showMessage('warning', '这个渠道已经不存在了');
        return;
    }
    const channel = sourceId ? modelWorkspace.channels[sourceId] : {};
    channelWorkspace.editing = id;
    document.getElementById('channel-modal-title').textContent = id ? `编辑渠道 · ${channel.name}` : (copyFrom ? `复制渠道 · ${channel.name}` : '新建渠道');
    const nameInput = document.getElementById('channel-name');
    nameInput.value = copyFrom ? `${channel.name} 副本` : (channel.name || '');
    document.getElementById('channel-base-url').value = channel.api_base_url || '';
    document.getElementById('channel-auth-mode').value = channel.api_key_auth_mode || 'auto';
    document.getElementById('channel-key-strategy').value = channel.api_key_strategy || 'round_robin';
    document.getElementById('channel-key-cooldown').value = channel.api_key_cooldown_seconds || '';
    toggleChannelStrategyOptions();
    clearApiKeyRows('channel-api-keys-list');
    const keys = channel.api_keys || [];
    (keys.length ? keys : ['']).forEach(key => addApiKeyRow(key, 'channel-api-keys-list'));

    // 测 Key 要发一次真实请求，得有个具体的模型：从用这个渠道（复制时是原渠道）的模型里选
    const testers = sourceId ? (channelWorkspace.usage[sourceId] || []) : [];
    document.getElementById('channel-test-model').replaceChildren(...testers.map(name => new Option(`用 ${name} 测试`, name)));
    for (const control of ['channel-test-keys-btn', 'channel-test-model']) document.getElementById(control).hidden = !testers.length;
    document.getElementById('channel-test-hint').hidden = testers.length > 0;
    document.getElementById('channel-key-test-results').style.display = 'none';

    const users = id ? (channelWorkspace.usage[id] || []) : [];
    document.getElementById('channel-usage-note').textContent = users.length
        ? `${users.length} 个模型在用这个渠道，保存后立即生效，不需要重启。`
        : '还没有模型用这个渠道。到模型列表勾选模型后点“改用渠道”，或者在模型设置里选它。';
    // 地址栏记下正在编辑的渠道，刷新后回到这里
    if (id) history.replaceState(null, '', routeHash('channels', id));
    channelWorkspace.snapshot = channelFormSnapshot();
    updateChannelSaveStatus();
    document.getElementById('channel-modal').classList.add('active');
    nameInput.focus();
    if (copyFrom) nameInput.select();
}

// 有没保存的修改时先问；返回 false 表示留在编辑窗里
async function closeChannelModal(force = false) {
    const modal = document.getElementById('channel-modal');
    if (!modal.classList.contains('active')) return true;
    if (!force && channelWorkspace.snapshot !== channelFormSnapshot() && !(await confirmDialog({ title: '放弃未保存的修改？', message: '这个渠道改过但还没有保存。', confirmText: '放弃修改', cancelText: '继续编辑', danger: true }))) return false;
    modal.classList.remove('active');
    if (location.hash.startsWith('#channels/')) history.replaceState(null, '', '#channels');
    return true;
}

async function saveChannel() {
    const button = document.getElementById('channel-save-btn');
    if (button.disabled) return;
    const nameInput = document.getElementById('channel-name');
    const name = nameInput.value.trim();
    if (!name) {
        showMessage('warning', '请输入渠道名称');
        nameInput.focus();
        return;
    }
    const channel = {
        name,
        api_base_url: document.getElementById('channel-base-url').value.trim(),
        api_keys: getApiKeys('channel-api-keys-list'),
        api_key_auth_mode: document.getElementById('channel-auth-mode').value,
        api_key_strategy: document.getElementById('channel-key-strategy').value,
    };
    const cooldown = parseInt(document.getElementById('channel-key-cooldown').value, 10);
    if (cooldown > 0) channel.api_key_cooldown_seconds = cooldown;

    button.disabled = true;
    try {
        const result = await postChannelApi('/api/admin/channels', { id: channelWorkspace.editing, channel });
        closeChannelModal(true);
        showMessage('success', result.message || '渠道已保存');
        loadChannels();
    } catch (error) {
        console.error('保存渠道失败:', error);
        showMessage('danger', '保存渠道失败: ' + error.message);
    } finally {
        button.disabled = false;
    }
}

async function testChannelKeys() {
    const apiKeys = getApiKeys('channel-api-keys-list');
    if (!apiKeys.length) {
        showMessage('warning', '请先填写至少一个 API Key');
        return;
    }
    // 直接打开渠道页时模型配置还没读过
    if (!Object.keys(modelWorkspace.models).length) await loadModels();
    const modelName = document.getElementById('channel-test-model').value;
    const config = modelPrimaryConfig(modelWorkspace.models[modelName]);
    if (!config.api_type) {
        showMessage('warning', `模型 ${modelName} 已经不在了，刷新后再试`);
        return;
    }
    const apiBaseUrl = document.getElementById('channel-base-url').value.trim();
    if (config.api_type !== 'gemini_native' && !apiBaseUrl) {
        showMessage('warning', '请先填写 API Base URL');
        return;
    }
    // 端点留空时由接口按协议取默认值
    await runKeyTest(document.getElementById('channel-test-keys-btn'), document.getElementById('channel-key-test-results'), {
        api_keys: apiKeys,
        api_base_url: apiBaseUrl,
        model_id: config.model_id || modelName,
        api_type: config.api_type,
        api_key_auth_mode: document.getElementById('channel-auth-mode').value,
        upstream_protocol: config.upstream_protocol || 'generate_content',
        endpoint_path: config.endpoint_path || '',
    });
}

async function deleteChannel(id) {
    const channel = modelWorkspace.channels[id];
    const users = channelWorkspace.usage[id] || [];
    if (!(await confirmDialog({
        title: '删除渠道',
        message: `确定要删除渠道 "${channel.name}" 吗？\n\n${users.length ? `还在用它的 ${users.length} 个模型会各自留一份现在的地址和 Key，照常可用。` : '没有模型在用它。'}`,
        confirmText: '删除',
        danger: true
    }))) return;
    try {
        const result = await postChannelApi('/api/admin/channels/delete', { id });
        showMessage('success', result.message || '渠道已删除');
    } catch (error) {
        console.error('删除渠道失败:', error);
        showMessage('danger', '删除失败: ' + error.message);
    }
    channelWorkspace.expanded.delete(id);
    loadChannels();
}

// ==================== 模型列表：批量改用渠道 ====================

async function assignSelectedModelsChannel() {
    const names = Array.from(document.querySelectorAll('#models-list .model-row .model-checkbox:checked'),
        box => box.closest('.model-row').dataset.modelName);
    if (!names.length) return;
    const options = channelOptions();
    if (!options.length) {
        showMessage('info', '还没有渠道。先到“上游渠道”页新建，或者把现有模型整理成渠道。');
        return;
    }
    const choice = await promptDialog({
        title: `${names.length} 个模型改用渠道`,
        message: '改用后这些模型的地址和 Key 以渠道为准，各自原来填的不再保留。',
        confirmText: '改用',
        input: { options: [...options, ['', '不用渠道（各自留一份现在的地址和 Key）']], value: options[0][0] }
    });
    if (choice === null) return;
    try {
        const result = await postChannelApi('/api/admin/models/channel', { model_names: names, channel: choice || null });
        showMessage('success', result.message || '已改用渠道');
    } catch (error) {
        console.error('改用渠道失败:', error);
        showMessage('danger', '改用渠道失败: ' + error.message);
    }
    loadModels();
}

// ==================== 模型编辑窗里的渠道 ====================

function refreshModelChannelOptions() {
    document.getElementById('model-channel').replaceChildren(
        new Option('不用渠道，这个模型自己填地址和 Key', ''),
        ...channelOptions().map(([id, label]) => new Option(label, id)));
}

// 选了渠道：地址、Key、鉴权方式和轮询策略显示渠道里的值并锁住（测 Key、从 API 获取模型照常能用）；
// 改回不用渠道时这些值留在表单里，相当于给模型留一份。
function applyModelChannel() {
    const channel = modelWorkspace.channels[document.getElementById('model-channel').value];
    if (channel) {
        document.getElementById('api-base-url').value = channel.api_base_url || '';
        document.getElementById('api-key-auth-mode').value = channel.api_key_auth_mode || 'auto';
        document.getElementById('api-key-strategy').value = channel.api_key_strategy || 'round_robin';
        document.getElementById('api-key-cooldown').value = channel.api_key_cooldown_seconds || '';
        toggleApiKeyStrategyOptions();
        clearApiKeyRows();
        (channel.api_keys || []).forEach(key => addApiKeyRow(key));
    }
    document.getElementById('settings-connection').classList.toggle('channel-locked', Boolean(channel));
    document.querySelectorAll('[data-channel-field] input, [data-channel-field] select').forEach(el => { el.disabled = Boolean(channel); });
    document.getElementById('model-channel-edit').hidden = !channel;
}

async function editModelChannel() {
    const id = document.getElementById('model-channel').value;
    if (await closeModelModal()) navigateTo('channels', id);
}

document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('channel-modal');
    for (const type of ['input', 'change', 'click']) modal.addEventListener(type, updateChannelSaveStatus);
    document.addEventListener('keydown', event => {
        if (!modal.classList.contains('active')) return;
        if (event.key === 'Escape') { event.preventDefault(); closeChannelModal(); }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveChannel(); }
    });
    window.addEventListener('beforeunload', event => {
        if (modal.classList.contains('active') && channelWorkspace.snapshot !== channelFormSnapshot()) { event.preventDefault(); event.returnValue = ''; }
    });
    // 按 / 跳到渠道搜索，Esc 清空
    bindSearchShortcut(document.getElementById('channels-query'), renderChannels);
});
