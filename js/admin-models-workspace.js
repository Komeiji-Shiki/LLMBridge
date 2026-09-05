// Model browsing and editor interaction; option serialization lives in admin-models-edit.
'use strict';

const modelWorkspace = { models: {}, request: 0 };
const modelEditor = { snapshot: '', saving: false, opener: null, page: 'connection', originalConfig: {} };

function modelPrimaryConfig(config) {
    return (Array.isArray(config) ? config[0] : config) || {};
}

function mergeModelEditorConfig(config) {
    const original = structuredClone(modelEditor.originalConfig);
    const primary = modelPrimaryConfig(original);
    // Only fields represented by this form can be removed by clearing a control.
    const managedFields = [
        'api_type', 'model_id', 'display_name', 'passthrough', 'sanitize_recursive_schemas',
        'convert_system_to_user', 'enable_prefix', 'enable_partial', 'force_stream',
        'enable_thinking', 'reasoning_effort', 'thinking_budget', 'thinking_effort', 'thinking_display',
        'responses_store', 'responses_reasoning_summary', 'auto_cache', 'verbosity',
        'oai_thinking_type', 'oai_thinking_effort', 'prefill_content', 'endpoint_path', 'upstream_protocol',
        'api_base_url', 'api_key', 'api_keys', 'api_key_strategy', 'api_key_cooldown_seconds',
        'thinking_separator', 'custom_params', 'extra_body_params', 'pricing', 'max_temperature',
        'max_tokens', 'cached_tokens_mode', 'token_stats_mode', 'completion_tokens_mode',
        'image_compression', 'system_prompt_injection', 'auto_retry', 'session_id', 'mode',
        'type', 'battle_target', 'archived'
    ];
    for (const field of managedFields) delete primary[field];
    Object.assign(primary, config);
    if (!Array.isArray(original)) return primary;
    original[0] = primary;
    if (!currentEditingModel) original.forEach(endpoint => { delete endpoint.archived; });
    return original;
}

function modelProtocol(config) {
    return ['direct_api', 'responses_native', 'anthropic_native', 'gemini_native'].includes(config.api_type) ? config.api_type : 'legacy';
}

function modelService(config) {
    return config.api_base_url || (config.api_type === 'gemini_native' ? 'Google 官方默认地址' : '未指定地址');
}

function openModelFromList(name, copy = false) {
    const config = modelWorkspace.models[name];
    if (copy) copyModel(name, config);
    else editModel(name, config);
}

function refreshModelProviders() {
    const select = document.getElementById('models-provider');
    const selected = select.value;
    const services = [...new Set(Object.values(modelWorkspace.models).map(config => modelService(modelPrimaryConfig(config))))];
    select.replaceChildren(new Option('全部服务地址', ''), ...services.sort().map(service => new Option(service, service)));
    if (services.includes(selected)) select.value = selected;
}

function applyModelFilters() {
    const query = document.getElementById('models-query').value.trim().toLocaleLowerCase();
    const protocol = document.getElementById('models-protocol').value;
    const provider = document.getElementById('models-provider').value;
    let active = 0, archived = 0, visible = 0;
    document.querySelectorAll('#models-list .model-row').forEach(row => {
        const name = row.dataset.modelName;
        const config = modelPrimaryConfig(modelWorkspace.models[name]);
        const matches = (!protocol || modelProtocol(config) === protocol) && (!provider || modelService(config) === provider)
            && [name, config.model_id, config.display_name, modelService(config)].some(value => String(value || '').toLocaleLowerCase().includes(query));
        row.hidden = !matches;
        if (!matches) row.querySelector('.model-checkbox').checked = false;
        else { visible++; if (isModelConfigArchived(modelWorkspace.models[name])) archived++; else active++; }
    });
    const filtered = !!(query || protocol || provider);
    if (modelsSortable) modelsSortable.option('disabled', filtered);
    document.getElementById('models-summary').textContent = `${Object.keys(modelWorkspace.models).length} 个模型 · 当前显示 ${active} 个可用 / ${archived} 个已归档${filtered ? ' · 清除筛选后可拖动排序' : ' · 拖动手柄调整可用模型顺序'}`;
    document.getElementById('models-no-results').hidden = visible > 0 || !Object.keys(modelWorkspace.models).length;
    const archiveBody = document.getElementById('archive-section-body');
    if (filtered && archived && archiveBody) {
        archiveBody.style.display = 'block';
        document.getElementById('archive-toggle-icon').textContent = '▼';
        document.querySelector('.archive-header').setAttribute('aria-expanded', 'true');
    }
    updateArchiveButtons();
}

function resetModelFilters() {
    for (const id of ['models-query', 'models-protocol', 'models-provider']) document.getElementById(id).value = '';
    applyModelFilters();
}

function modelFormSnapshot() {
    // Transient API model picker / search controls are not part of the saved configuration.
    return JSON.stringify(Array.from(document.querySelectorAll('#model-modal input, #model-modal select, #model-modal textarea'))
        .filter(el => !['model-search', 'model-select'].includes(el.id))
        .map(el => [el.id, el.type === 'checkbox' ? el.checked : el.value]));
}

function resetModelEditorForm() {
    document.querySelectorAll('#model-modal input, #model-modal select, #model-modal textarea').forEach(el => {
        if (el.id === 'model-name') return;
        if (el.type === 'checkbox') el.checked = el.defaultChecked;
        else if (el.tagName === 'SELECT') {
            el.selectedIndex = Math.max(0, Array.from(el.options).findIndex(option => option.defaultSelected));
        } else el.value = el.defaultValue;
    });
    for (const id of ['model-select-container', 'key-test-results']) document.getElementById(id).style.display = 'none';
}

function selectModelSettings(page) {
    const legacy = document.getElementById('config-type').value !== 'direct_api';
    if (legacy) page = 'connection';
    modelEditor.page = page;
    document.querySelectorAll('.model-settings-panel').forEach(panel => { panel.hidden = panel.id !== `settings-${page}`; });
    document.querySelectorAll('[data-settings-page]').forEach(button => {
        const selected = button.dataset.settingsPage === page;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-current', selected ? 'page' : 'false');
        button.hidden = legacy && button.dataset.settingsPage !== 'connection';
    });
    document.querySelector('#model-modal .modal-body').scrollTop = 0;
}

function syncModelSettingsType() {
    const direct = document.getElementById('config-type').value === 'direct_api';
    document.querySelectorAll('[data-direct-setting]').forEach(el => el.classList.toggle('protocol-hidden', !direct));
    selectModelSettings(direct ? modelEditor.page : 'connection');
}

function beginModelEditing() {
    modelEditor.opener = document.activeElement;
    modelEditor.snapshot = modelFormSnapshot();
    document.getElementById('model-modal').classList.add('active');
    document.body.classList.add('model-editor-open');
    selectModelSettings('connection');
    updateModelSaveStatus();
    document.getElementById('model-name').focus();
}

function endModelEditing(force) {
    if (modelEditor.saving && !force) return false;
    if (!force && modelEditor.snapshot !== modelFormSnapshot() && !confirm('模型设置尚未保存，确定放弃修改吗？')) return false;
    document.getElementById('model-modal').classList.remove('active');
    document.body.classList.remove('model-editor-open');
    modelEditor.snapshot = '';
    if (modelEditor.opener?.isConnected) modelEditor.opener.focus();
    return true;
}

function updateModelSaveStatus() {
    document.getElementById('model-save-status').textContent = modelEditor.saving ? '正在保存，请稍候…' :
        (modelEditor.snapshot !== modelFormSnapshot() ? '有未保存的修改' : '修改后点击保存 · Ctrl / ⌘ + S');
}

function setModelSaving(saving) {
    modelEditor.saving = saving;
    const modal = document.getElementById('model-modal');
    modal.setAttribute('aria-busy', String(saving));
    modal.querySelectorAll('button, input, select, textarea').forEach(el => {
        if (saving) { el.dataset.beforeSaveDisabled = String(el.disabled); el.disabled = true; }
        else if (el.dataset.beforeSaveDisabled !== undefined) { el.disabled = el.dataset.beforeSaveDisabled === 'true'; delete el.dataset.beforeSaveDisabled; }
    });
    document.getElementById('model-save-btn').textContent = saving ? '保存中…' : '保存模型';
    updateModelSaveStatus();
}

function validateModelEditor() {
    const name = document.getElementById('model-name');
    const existing = Object.hasOwn(modelWorkspace.models, name.value.trim());
    if (existing && name.value.trim() !== currentEditingModel) {
        selectModelSettings('connection'); name.focus();
        showMessage('danger', '这个模型名称已存在，请使用其他名称，避免覆盖已有配置。'); return false;
    }
    const direct = document.getElementById('config-type').value === 'direct_api';
    const controls = document.querySelectorAll(direct ? '[data-direct-setting] input, [data-direct-setting] textarea' : '#lmarena-config input');
    for (const input of controls) {
        if (!input.checkValidity()) {
            selectModelSettings(input.closest('.model-settings-panel').id.replace('settings-', ''));
            input.reportValidity(); return false;
        }
    }
    return true;
}

document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('model-modal');
    modal.addEventListener('input', updateModelSaveStatus);
    modal.addEventListener('change', updateModelSaveStatus);
    modal.addEventListener('click', () => { if (modal.classList.contains('active')) updateModelSaveStatus(); });
    document.addEventListener('keydown', event => {
        if (!modal.classList.contains('active')) return;
        if (event.key === 'Escape') { event.preventDefault(); closeModelModal(); }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveModel(); }
        if (event.key === 'Tab') {
            const focusable = Array.from(modal.querySelectorAll('button, input, select, textarea, a[href]')).filter(el => !el.disabled && el.getClientRects().length);
            const first = focusable[0], last = focusable.at(-1);
            if (!modal.contains(document.activeElement)) { event.preventDefault(); first?.focus(); }
            else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
    });
    window.addEventListener('beforeunload', event => {
        if (modal.classList.contains('active') && modelEditor.snapshot !== modelFormSnapshot()) { event.preventDefault(); event.returnValue = ''; }
    });
    document.querySelectorAll('.nav-item').forEach(item => {
        item.tabIndex = 0; item.setAttribute('role', 'button');
        item.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); item.click(); } });
    });
});
