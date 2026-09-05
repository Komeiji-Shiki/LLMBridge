'use strict';
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

// Responses 原生上游字段与端点联动
function syncResponsesApiOptions() {
    const apiType = document.getElementById('api-type')?.value || 'direct_api';
    const options = document.getElementById('responses-native-options');
    if (options) options.style.display = apiType === 'responses_native' ? 'block' : 'none';
    const passthroughConfig = document.getElementById('passthrough-config');
    if (passthroughConfig) passthroughConfig.style.display = apiType === 'direct_api' ? 'block' : 'none';

    const endpointInput = document.getElementById('endpoint-path');
    if (!endpointInput) return;
    endpointInput.placeholder = modelDefaultEndpoint(apiType) || '由 Gemini 上游协议决定';
    endpointInput.dataset.apiType = apiType;

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

function modelDefaultEndpoint(apiType) {
    return {direct_api: '/chat/completions', responses_native: '/responses', anthropic_native: '/messages', gemini_native: ''}[apiType] || '';
}

function changeModelApiType() {
    const endpoint = document.getElementById('endpoint-path');
    if (endpoint.value.trim() === modelDefaultEndpoint(endpoint.dataset.apiType)) endpoint.value = '';
    toggleThinkingOptions();
}
