/**
 * API Key 管理模块
 * 处理 API Key 的 CRUD 操作和 UI 交互
 */

// ==================== 状态 ====================
let _apikeyEditingId = null; // 正在编辑的 key ID，null 表示新建模式
let _allModelsForApiKey = []; // 缓存的模型列表

// ==================== 加载与渲染 ====================

async function loadApiKeys() {
    const container = document.getElementById('api-keys-list');
    if (!container) return;

    container.innerHTML = '<div style="text-align: center; padding: 30px; color: var(--text-dim);"><div class="loading-spinner" style="margin: 0 auto 10px;"></div>加载中...</div>';

    try {
        const resp = await fetch('/api/admin/api_keys');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const keys = data.keys || [];

        if (keys.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">🔑</div>
                    <p>尚未创建任何 API Key</p>
                    <p style="font-size: 0.875rem; margin-top: 10px;">点击上方"创建 API Key"按钮开始</p>
                </div>`;
            return;
        }

        let html = '<table class="table"><thead><tr>';
        html += '<th>名称</th><th>Key (脱敏)</th><th>允许模型</th><th>RPM 限制</th><th>当前 RPM</th><th>总请求</th><th>状态</th><th>操作</th>';
        html += '</tr></thead><tbody>';

        for (const key of keys) {
            const modelsDisplay = key.allowed_models && key.allowed_models.length > 0
                ? `<span title="${key.allowed_models.join(', ')}">${key.allowed_models.length} 个模型</span>`
                : '<span style="color: var(--text-dim);">全部模型</span>';

            const rpmDisplay = key.rpm_limit > 0 ? key.rpm_limit : '<span style="color: var(--text-dim);">不限</span>';
            const currentRpm = key.current_rpm || 0;
            const rpmColor = key.rpm_limit > 0 && currentRpm >= key.rpm_limit * 0.8 ? '#ef4444' : '#10b981';

            const statusBadge = key.enabled
                ? '<span class="badge badge-success">启用</span>'
                : '<span class="badge badge-danger">禁用</span>';

            const lastUsed = key.last_used_at
                ? new Date(key.last_used_at * 1000).toLocaleString('zh-CN')
                : '<span style="color: var(--text-dim);">从未使用</span>';

            html += `<tr>
                <td>
                    <div style="font-weight: 600;">${escapeHtml(key.name)}</div>
                    ${key.description ? `<div style="font-size: 0.75rem; color: var(--text-dim);">${escapeHtml(key.description)}</div>` : ''}
                    <div style="font-size: 0.7rem; color: var(--text-dim);">最后使用: ${lastUsed}</div>
                </td>
                <td><code style="font-size: 0.8rem; background: var(--surface-2); padding: 2px 6px; border-radius: 3px;">${escapeHtml(key.secret_masked)}</code></td>
                <td>${modelsDisplay}</td>
                <td>${rpmDisplay}</td>
                <td><span style="color: ${rpmColor}; font-weight: 600;">${currentRpm}</span></td>
                <td>${key.total_requests || 0}</td>
                <td>${statusBadge}</td>
                <td>
                    <div style="display: flex; gap: 6px;">
                        <button class="btn btn-sm btn-primary" onclick="editApiKey('${key.id}')">✏️</button>
                        <button class="btn btn-sm" onclick="toggleApiKeyEnabled('${key.id}', ${!key.enabled})">${key.enabled ? '⏸️' : '▶️'}</button>
                        <button class="btn btn-sm btn-danger" onclick="deleteApiKey('${key.id}', '${escapeHtml(key.name)}')">🗑️</button>
                    </div>
                </td>
            </tr>`;
        }

        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div class="alert alert-danger">加载 API Key 列表失败: ${escapeHtml(err.message)}</div>`;
    }
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ==================== 创建 / 编辑模态框 ====================

async function showCreateApiKeyModal() {
    _apikeyEditingId = null;
    document.getElementById('apikey-modal-title').textContent = '创建 API Key';
    document.getElementById('apikey-save-btn').textContent = '创建';

    // 清空表单
    document.getElementById('apikey-name').value = '';
    document.getElementById('apikey-description').value = '';
    document.getElementById('apikey-rpm').value = '0';
    document.getElementById('apikey-enabled').checked = true;

    await loadModelsForApiKeyModal([]);
    document.getElementById('apikey-modal').classList.add('active');
}

async function editApiKey(keyId) {
    _apikeyEditingId = keyId;
    document.getElementById('apikey-modal-title').textContent = '编辑 API Key';
    document.getElementById('apikey-save-btn').textContent = '保存';

    try {
        const resp = await fetch(`/api/admin/api_keys/${keyId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const key = data.key;

        document.getElementById('apikey-name').value = key.name || '';
        document.getElementById('apikey-description').value = key.description || '';
        document.getElementById('apikey-rpm').value = key.rpm_limit || 0;
        document.getElementById('apikey-enabled').checked = key.enabled !== false;

        await loadModelsForApiKeyModal(key.allowed_models || []);
        document.getElementById('apikey-modal').classList.add('active');
    } catch (err) {
        alert('加载 API Key 详情失败: ' + err.message);
    }
}

function closeApiKeyModal() {
    document.getElementById('apikey-modal').classList.remove('active');
    _apikeyEditingId = null;
}

// ==================== 模型列表复选框 ====================

async function loadModelsForApiKeyModal(selectedModels) {
    const container = document.getElementById('apikey-models-checklist');
    container.innerHTML = '<div style="text-align: center; color: var(--text-dim); padding: 10px;">加载中...</div>';

    try {
        const resp = await fetch('/api/admin/models');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const endpointMap = data.model_endpoint_map || {};

        // 过滤掉 archived 的模型
        _allModelsForApiKey = Object.entries(endpointMap)
            .filter(([name, config]) => {
                if (Array.isArray(config)) {
                    return config.length > 0 && !(config[0] && config[0].archived);
                }
                return !(config && config.archived);
            })
            .map(([name]) => name);

        renderApiKeyModelChecklist(selectedModels);
    } catch (err) {
        container.innerHTML = `<div style="color: #ef4444; padding: 10px;">加载模型列表失败: ${err.message}</div>`;
    }
}

function renderApiKeyModelChecklist(selectedModels) {
    const container = document.getElementById('apikey-models-checklist');
    const searchTerm = (document.getElementById('apikey-model-search')?.value || '').toLowerCase();

    const filtered = _allModelsForApiKey.filter(name => 
        !searchTerm || name.toLowerCase().includes(searchTerm)
    );

    if (filtered.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: var(--text-dim); padding: 15px;">没有匹配的模型</div>';
        return;
    }

    let html = '';
    for (const modelName of filtered) {
        const checked = selectedModels.includes(modelName) ? 'checked' : '';
        html += `<label style="display: flex; align-items: center; padding: 6px 4px; cursor: pointer; border-radius: 4px; transition: background 0.15s;"
                    onmouseover="this.style.background='rgba(255,255,255,0.03)'" onmouseout="this.style.background='transparent'">
            <input type="checkbox" class="apikey-model-cb" value="${escapeHtml(modelName)}" ${checked} style="margin-right: 10px;">
            <span style="font-size: 0.875rem;">${escapeHtml(modelName)}</span>
        </label>`;
    }
    container.innerHTML = html;
}

function filterApiKeyModels() {
    // 收集当前已选中的模型
    const selected = getSelectedApiKeyModels();
    renderApiKeyModelChecklist(selected);
}

function getSelectedApiKeyModels() {
    const checkboxes = document.querySelectorAll('.apikey-model-cb:checked');
    return Array.from(checkboxes).map(cb => cb.value);
}

function apiKeySelectAllModels() {
    document.querySelectorAll('.apikey-model-cb').forEach(cb => cb.checked = true);
}

function apiKeyDeselectAllModels() {
    document.querySelectorAll('.apikey-model-cb').forEach(cb => cb.checked = false);
}

// ==================== 保存 ====================

async function saveApiKey() {
    const name = document.getElementById('apikey-name').value.trim();
    if (!name) {
        alert('请输入 API Key 名称');
        return;
    }

    const rpm = parseInt(document.getElementById('apikey-rpm').value) || 0;
    const description = document.getElementById('apikey-description').value.trim();
    const enabled = document.getElementById('apikey-enabled').checked;
    const allowedModels = getSelectedApiKeyModels();

    const payload = {
        name,
        description,
        rpm_limit: rpm,
        enabled,
        allowed_models: allowedModels,
    };

    try {
        let resp;
        if (_apikeyEditingId) {
            // 编辑模式
            resp = await fetch(`/api/admin/api_keys/${_apikeyEditingId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        } else {
            // 创建模式
            resp = await fetch('/api/admin/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        }

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            throw new Error(errData.detail || `HTTP ${resp.status}`);
        }

        const data = await resp.json();
        closeApiKeyModal();

        if (!_apikeyEditingId && data.key && data.key.secret) {
            // 新建成功，显示 secret
            showApiKeySecret(data.key.secret);
        }

        loadApiKeys();
    } catch (err) {
        alert('保存失败: ' + err.message);
    }
}

// ==================== Secret 显示 ====================

function showApiKeySecret(secret) {
    document.getElementById('apikey-secret-display').value = secret;
    document.getElementById('apikey-copy-success').style.display = 'none';
    document.getElementById('apikey-secret-modal').classList.add('active');
}

function closeApiKeySecretModal() {
    document.getElementById('apikey-secret-modal').classList.remove('active');
}

async function copyApiKeySecret() {
    const input = document.getElementById('apikey-secret-display');
    try {
        await navigator.clipboard.writeText(input.value);
        document.getElementById('apikey-copy-success').style.display = 'block';
        setTimeout(() => {
            document.getElementById('apikey-copy-success').style.display = 'none';
        }, 3000);
    } catch {
        // 回退方案
        input.select();
        document.execCommand('copy');
        document.getElementById('apikey-copy-success').style.display = 'block';
    }
}

// ==================== 启用/禁用 ====================

async function toggleApiKeyEnabled(keyId, newState) {
    try {
        const resp = await fetch(`/api/admin/api_keys/${keyId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: newState }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        loadApiKeys();
    } catch (err) {
        alert('操作失败: ' + err.message);
    }
}

// ==================== 删除 ====================

async function deleteApiKey(keyId, keyName) {
    if (!confirm(`确定要删除 API Key "${keyName}" 吗？此操作不可恢复。`)) return;

    try {
        const resp = await fetch(`/api/admin/api_keys/${keyId}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        loadApiKeys();
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}

// ==================== 页面初始化 ====================

// 当切换到 api-keys 页面时自动加载
(function() {
    // 监听导航点击
    const origNavHandler = document.querySelector('.nav-item[data-page="api-keys"]');
    if (origNavHandler) {
        origNavHandler.addEventListener('click', function() {
            loadApiKeys();
        });
    }

    // 如果页面 hash 指向 api-keys，自动加载
    if (window.location.hash === '#api-keys') {
        setTimeout(loadApiKeys, 300);
    }
})();