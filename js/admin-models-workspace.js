// Model browsing and editor interaction; option serialization lives in admin-models-edit.
'use strict';

const modelWorkspace = { models: {}, channels: {}, pendingGroup: '', request: 0, archiveOpen: false };
// 这些输入框边输入边检查 JSON，保存时由 validateModelEditor 拦下并跳到所在页
const MODEL_JSON_FIELDS = ['custom-params', 'extra-body-params', 'model-native-tool-options'];
const modelEditor = { snapshot: '', saving: false, opener: null, page: 'connection', originalConfig: {} };
// 接了渠道的模型不保存这些字段，和后端 core/model_channels.CHANNEL_FIELDS 一致
const MODEL_CHANNEL_FIELDS = ['api_base_url', 'api_key', 'api_keys', 'api_key_auth_mode', 'api_key_strategy', 'api_key_cooldown_seconds'];

function modelPrimaryConfig(config) {
    return (Array.isArray(config) ? config[0] : config) || {};
}

function mergeModelEditorConfig(config) {
    const original = structuredClone(modelEditor.originalConfig);
    const primary = modelPrimaryConfig(original);
    // 表单编辑基础单价时保留按请求长度定义的阶梯与未展示的计费字段。
    if (config.pricing && primary.pricing) {
        const preservedPricing = { ...primary.pricing };
        for (const key of ['input', 'output', 'unit', 'currency', 'cached_input', 'cache_write', 'cache_write_1h']) {
            delete preservedPricing[key];
        }
        config.pricing = { ...preservedPricing, ...config.pricing };
    }
    // Only fields represented by this form can be removed by clearing a control.
    const managedFields = [
        'api_type', 'model_id', 'display_name', 'passthrough', 'sanitize_recursive_schemas',
        'provider', 'model_family', 'first_chunk_timeout_seconds', 'native_tools', 'native_tool_options',
        'convert_system_to_user', 'enable_prefix', 'enable_partial', 'force_stream',
        'strip_claude_agent_sdk_prefix',
        'enable_thinking', 'reasoning_effort', 'thinking_budget', 'thinking_effort', 'thinking_display',
        'responses_store', 'responses_reasoning_summary', 'auto_cache', 'tool_name_rewrite', 'verbosity',
        'oai_thinking_type', 'oai_thinking_effort', 'prefill_content', 'prefill_reasoning_content', 'endpoint_path', 'upstream_protocol',
        'channel', 'api_base_url', 'api_key', 'api_keys', 'api_key_auth_mode', 'api_key_strategy', 'api_key_cooldown_seconds',
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

// 接了渠道的模型，地址和 Key 以渠道为准
function modelConnection(config) {
    return modelWorkspace.channels[config.channel] || config;
}

// 模型实际生效的配置：渠道的默认设置打底，模型自己写的盖在上面，写成 null 的项表示明确不设置
function modelEffective(config) {
    const defaults = modelWorkspace.channels[config.channel]?.defaults;
    if (!defaults) return config;
    const merged = { ...defaults, ...config };
    for (const field of Object.keys(defaults)) if (merged[field] === null) delete merged[field];
    return merged;
}

function modelService(config) {
    return modelConnection(config).api_base_url || (config.api_type === 'gemini_native' ? 'Google 官方默认地址' : '未指定地址');
}

// 筛选用的分组：接了渠道的按渠道，其余按服务地址
function modelGroup(config) {
    return modelWorkspace.channels[config.channel] ? 'ch:' + config.channel : modelService(config);
}

// 地址栏和监控页给的可能是显示名称，这里换回配置里的模型名
function resolveModelName(label) {
    if (Object.hasOwn(modelWorkspace.models, label)) return label;
    return Object.keys(modelWorkspace.models).find(name => modelPrimaryConfig(modelWorkspace.models[name]).display_name === label);
}

// 在编辑窗里直接换到列表中的上一个 / 下一个模型（按当前筛选结果），不用关掉再点
function modelEditorNeighbors() {
    const names = Array.from(document.querySelectorAll('#models-list .model-row:not([hidden])'), row => row.dataset.modelName);
    const index = names.indexOf(currentEditingModel);
    return index < 0 ? [] : [names[index - 1], names[index + 1]];
}

function updateModelStepper() {
    const [previous, next] = modelEditorNeighbors();
    document.getElementById('model-stepper').hidden = !currentEditingModel;
    document.getElementById('model-prev').disabled = !previous;
    document.getElementById('model-next').disabled = !next;
}

async function stepModelEditor(delta) {
    const target = modelEditorNeighbors()[delta < 0 ? 0 : 1];
    if (target && await closeModelModal()) openModelFromList(target);
}

function openModelFromList(name, copy = false) {
    const config = modelWorkspace.models[name];
    if (copy) copyModel(name, config);
    else editModel(name, config);
}

function refreshModelProviders() {
    const select = document.getElementById('models-provider');
    // 从渠道页跳过来时带着要看的渠道
    const selected = modelWorkspace.pendingGroup || select.value;
    modelWorkspace.pendingGroup = '';
    const groups = new Set(Object.values(modelWorkspace.models).map(config => modelGroup(modelPrimaryConfig(config))));
    const channels = Object.entries(modelWorkspace.channels)
        .filter(([id]) => groups.has('ch:' + id))
        .sort(([, a], [, b]) => a.name.localeCompare(b.name))
        .map(([id, channel]) => new Option(channel.name, 'ch:' + id));
    const services = [...groups].filter(group => !group.startsWith('ch:')).sort().map(service => new Option(service, service));
    const section = (label, options) => {
        const group = document.createElement('optgroup');
        group.label = label;
        group.append(...options);
        return group;
    };
    // 没有渠道时和以前一样，只是一列服务地址
    select.replaceChildren(new Option('全部渠道和地址', ''),
        ...(channels.length ? [section('渠道', channels), section('没接渠道的地址', services)] : services));
    if (Array.from(select.options).some(option => option.value === selected)) select.value = selected;
}

function applyModelFilters() {
    const query = document.getElementById('models-query').value.trim().toLocaleLowerCase();
    const protocol = document.getElementById('models-protocol').value;
    const provider = document.getElementById('models-provider').value;
    let active = 0, archived = 0, visible = 0;
    document.querySelectorAll('#models-list .model-row').forEach(row => {
        const name = row.dataset.modelName;
        const config = modelPrimaryConfig(modelWorkspace.models[name]);
        const matches = (!protocol || modelProtocol(config) === protocol) && (!provider || modelGroup(config) === provider)
            && [name, config.model_id, config.display_name, modelService(config), modelWorkspace.channels[config.channel]?.name].some(value => String(value || '').toLocaleLowerCase().includes(query));
        row.hidden = !matches;
        if (!matches) row.querySelector('.model-checkbox').checked = false;
        else { visible++; if (isModelConfigArchived(modelWorkspace.models[name])) archived++; else active++; }
    });
    const filtered = !!(query || protocol || provider);
    if (modelsSortable) modelsSortable.option('disabled', filtered);
    document.getElementById('models-summary').textContent = `${Object.keys(modelWorkspace.models).length} 个模型 · 显示 ${active} 个可用 / ${archived} 个已归档${filtered ? ' · 清除筛选后可拖动排序' : ''}`;
    document.getElementById('models-no-results').hidden = visible > 0 || !Object.keys(modelWorkspace.models).length;
    const archiveBody = document.getElementById('archive-section-body');
    if (filtered && archived && archiveBody) {
        archiveBody.style.display = 'block';
        document.getElementById('archive-toggle-icon').textContent = '▼';
        document.querySelector('.archive-header').setAttribute('aria-expanded', 'true');
    }
    syncModelsStickyHeight();
    updateArchiveButtons();
}

function syncModelsStickyHeight() {
    document.getElementById('models').style.setProperty('--models-sticky-height', document.querySelector('.models-sticky').offsetHeight + 'px');
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
    // 渠道可能刚改过，选项每次重建；先回到不用渠道并解锁，填表时再按模型的配置选上
    refreshModelChannelOptions();
    applyModelChannel();
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

// 紧凑列表开关，状态记在浏览器里
const MODELS_COMPACT_KEY = 'llmbridge.models.compact';

function setModelsCompact(compact) {
    document.getElementById('models-list').classList.toggle('models-compact', compact);
    const button = document.getElementById('models-density');
    button.setAttribute('aria-pressed', String(compact));
    button.classList.toggle('btn-primary', compact);
}

function toggleModelsCompact() {
    const compact = !document.getElementById('models-list').classList.contains('models-compact');
    setModelsCompact(compact);
    localStorage.setItem(MODELS_COMPACT_KEY, compact ? '1' : '0');
}

function checkModelJsonField(input) {
    const text = input.value.trim();
    let message = '';
    if (text) {
        try { JSON.parse(text); } catch (error) { message = `不是有效的 JSON：${error.message}`; }
    }
    input.setCustomValidity(message);
    input.classList.toggle('json-invalid', Boolean(message));
}

function beginModelEditing() {
    MODEL_JSON_FIELDS.forEach(id => checkModelJsonField(document.getElementById(id)));
    modelEditor.opener = document.activeElement;
    // LMArena 模式已弃用，只有本来就是这种配置的模型才显示切换项
    const configType = document.getElementById('config-type');
    configType.closest('.form-group').classList.toggle('legacy-lmarena', configType.value === 'direct_api');
    // 地址栏跟着记下正在编辑的模型，刷新后回到这里
    if (currentEditingModel) history.replaceState(null, '', routeHash('models', currentEditingModel));
    modelEditor.snapshot = modelFormSnapshot();
    document.getElementById('model-modal').classList.add('active');
    document.body.classList.add('model-editor-open');
    selectModelSettings('connection');
    updateModelSaveStatus();
    updateModelStepper();
    document.getElementById('model-name').focus();
}

async function endModelEditing(force) {
    if (modelEditor.saving && !force) return false;
    if (!force && modelEditor.snapshot !== modelFormSnapshot() && !(await confirmDialog({ title: '放弃未保存的修改？', message: '这个模型的设置改过但还没有保存。', confirmText: '放弃修改', cancelText: '继续编辑', danger: true }))) return false;
    document.getElementById('model-modal').classList.remove('active');
    document.body.classList.remove('model-editor-open');
    modelEditor.snapshot = '';
    if (location.hash.startsWith('#models/')) history.replaceState(null, '', '#models');
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
    setModelsCompact(localStorage.getItem(MODELS_COMPACT_KEY) === '1');
    // 表头吸在筛选栏下面；筛选栏换行时高度会变，跟着更新
    new ResizeObserver(syncModelsStickyHeight).observe(document.querySelector('.models-sticky'));
    MODEL_JSON_FIELDS.forEach(id => {
        const input = document.getElementById(id);
        input.addEventListener('input', () => checkModelJsonField(input));
    });
    // 按 / 跳到模型搜索，Esc 清空
    bindSearchShortcut(document.getElementById('models-query'), applyModelFilters);
    modal.addEventListener('change', updateModelSaveStatus);
    modal.addEventListener('click', () => { if (modal.classList.contains('active')) updateModelSaveStatus(); });
    document.addEventListener('keydown', event => {
        if (!modal.classList.contains('active')) return;
        if (event.key === 'Escape') { event.preventDefault(); closeModelModal(); }
        // 下拉框里 Alt + 方向键是展开选项，不抢
        if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown') && event.target.tagName !== 'SELECT') {
            event.preventDefault();
            stepModelEditor(event.key === 'ArrowUp' ? -1 : 1);
        }
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
