// admin-channels.js - 渠道：多个模型共用的上游地址和 Key
// 模型配置里只留 channel 引用，地址、Key、鉴权方式和轮询策略都在渠道上，换 Key 只改一处。
// 渠道表放在 modelWorkspace.channels（模型列表和编辑窗也要用），这里只多记引用关系和整理建议。
'use strict';

const channelWorkspace = { usage: {}, suggestions: [], editing: null };

function channelHost(channel) {
    if (!channel.api_base_url) return '协议默认地址';
    try { return new URL(channel.api_base_url).host; } catch { return channel.api_base_url; }
}

function channelKeySummary(channel) {
    const count = (channel.api_keys || []).length;
    if (!count) return '无需认证';
    return `${count} 个 Key` + (count > 1 ? (channel.api_key_strategy === 'sticky' ? ' · 粘性轮询' : ' · 逐个轮询') : '');
}

function channelOptions() {
    return Object.entries(modelWorkspace.channels)
        .sort(([, a], [, b]) => a.name.localeCompare(b.name))
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

function buildChannelRow(id, channel) {
    const attr = escapeHtml(id);
    const users = channelWorkspace.usage[id] || [];
    const address = escapeHtml(channel.api_base_url || '协议默认地址');
    return `<tr data-channel-id="${attr}">
        <td><button class="model-name" data-channel-id="${attr}" onclick="openChannelModal(this.dataset.channelId)">${escapeHtml(channel.name)}</button></td>
        <td class="channel-address" title="${address}">${address}</td>
        <td>${channelKeySummary(channel)}</td>
        <td>${users.length
            ? `<button class="btn btn-sm" data-channel-id="${attr}" onclick="showChannelModels(this.dataset.channelId)" title="到模型列表里只看这个渠道的模型">${users.length} 个模型</button>`
            : '<span class="channel-unused">没有模型在用</span>'}</td>
        <td><div class="model-actions">
            <button class="btn btn-sm" data-channel-id="${attr}" onclick="openChannelModal(this.dataset.channelId)">编辑</button>
            <button class="btn btn-danger btn-sm" data-channel-id="${attr}" onclick="deleteChannel(this.dataset.channelId)">删除</button>
        </div></td></tr>`;
}

function renderChannels() {
    const entries = Object.entries(modelWorkspace.channels).sort(([, a], [, b]) => a.name.localeCompare(b.name));
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
    document.getElementById('channels-list').innerHTML = entries.length
        ? `<table class="table">
            <thead><tr><th>渠道</th><th>服务地址</th><th>Key</th><th>模型</th><th>操作</th></tr></thead>
            <tbody>${entries.map(([id, channel]) => buildChannelRow(id, channel)).join('')}</tbody>
        </table>`
        : '<div class="empty-state"><div class="empty-state-icon"><svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#link"/></svg></div><p>还没有渠道<br/>新建一个，或者把地址和 Key 相同的现有模型整理成渠道</p></div>';
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

function openChannelModal(id = null) {
    if (id && !modelWorkspace.channels[id]) {
        showMessage('warning', '这个渠道已经不存在了');
        return;
    }
    const channel = id ? modelWorkspace.channels[id] : {};
    channelWorkspace.editing = id;
    document.getElementById('channel-modal-title').textContent = id ? `编辑渠道 · ${channel.name}` : '新建渠道';
    document.getElementById('channel-name').value = channel.name || '';
    document.getElementById('channel-base-url').value = channel.api_base_url || '';
    document.getElementById('channel-auth-mode').value = channel.api_key_auth_mode || 'auto';
    document.getElementById('channel-key-strategy').value = channel.api_key_strategy || 'round_robin';
    document.getElementById('channel-key-cooldown').value = channel.api_key_cooldown_seconds || '';
    toggleChannelStrategyOptions();
    clearApiKeyRows('channel-api-keys-list');
    const keys = channel.api_keys || [];
    (keys.length ? keys : ['']).forEach(key => addApiKeyRow(key, 'channel-api-keys-list'));
    const users = id ? (channelWorkspace.usage[id] || []) : [];
    document.getElementById('channel-usage-note').textContent = users.length
        ? `${users.length} 个模型在用这个渠道，保存后立即生效，不需要重启。`
        : '还没有模型用这个渠道。到模型列表勾选模型后点“改用渠道”，或者在模型设置里选它。';
    // 地址栏记下正在编辑的渠道，刷新后回到这里
    if (id) history.replaceState(null, '', routeHash('channels', id));
    document.getElementById('channel-modal').classList.add('active');
    document.getElementById('channel-name').focus();
}

function closeChannelModal() {
    document.getElementById('channel-modal').classList.remove('active');
    if (location.hash.startsWith('#channels/')) history.replaceState(null, '', '#channels');
}

async function saveChannel() {
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

    const button = document.getElementById('channel-save-btn');
    button.disabled = true;
    try {
        const result = await postChannelApi('/api/admin/channels', { id: channelWorkspace.editing, channel });
        closeChannelModal();
        showMessage('success', result.message || '渠道已保存');
        loadChannels();
    } catch (error) {
        console.error('保存渠道失败:', error);
        showMessage('danger', '保存渠道失败: ' + error.message);
    } finally {
        button.disabled = false;
    }
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

document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.getElementById('channel-modal').classList.contains('active')) closeChannelModal();
});
// 浏览器后退离开了正在编辑的渠道时，编辑窗跟着关掉
window.addEventListener('hashchange', () => {
    if (!location.hash.startsWith('#channels/')) document.getElementById('channel-modal').classList.remove('active');
});
