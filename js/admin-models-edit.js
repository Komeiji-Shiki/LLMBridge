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

/**
 * HTML 属性转义
 */
function escapeHtmlForAttr(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&').replace(/"/g, '"').replace(/</g, '<').replace(/>/g, '>');
}

// 折叠面板切换函数
function toggleCollapsible(headerElement) {
    const section = headerElement.parentElement;
    section.classList.toggle('collapsed');
}

function showAddModelModal() {
    currentEditingModel = null;
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
    document.getElementById('endpoint-path').value = '/chat/completions';
    document.getElementById('model-id').value = '';
    document.getElementById('display-name').value = '';
    document.getElementById('passthrough').checked = true;
    document.getElementById('convert-system-to-user').checked = false;
    document.getElementById('enable-prefix').checked = false;
    document.getElementById('enable-partial').checked = false;
    document.getElementById('prefill-content').value = '';
    document.getElementById('enable-thinking').value = '';
    document.getElementById('thinking-budget').value = '20000';
    document.getElementById('thinking-effort').value = '';
    document.getElementById('thinking-display').value = 'summarized';
    document.getElementById('thinking-separator').value = '';
    toggleThinkingOptions();
    document.getElementById('pricing-input').value = '';
    document.getElementById('pricing-output').value = '';
    document.getElementById('pricing-unit').value = '1000000';
    document.getElementById('pricing-currency').value = 'USD';
    document.getElementById('custom-params').value = '';
    document.getElementById('max-temperature').value = '';
    document.getElementById('lmarena-max-temperature').value = '';
    document.getElementById('max-tokens').value = '';
    document.getElementById('lmarena-max-tokens').value = '';
    document.getElementById('cached-tokens-mode').value = 'reverse';
    
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

// 重置系统提示词注入字段
function resetSystemInjectionFields() {
    document.getElementById('system-injection-enabled').checked = false;
    document.getElementById('system-injection-position').value = 'before_system';
    document.getElementById('system-injection-preset').value = '';
    document.getElementById('system-injection-content').value = '';
    document.getElementById('system-injection-options').style.display = 'none';
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
    if (!content) {
        return null;
    }
    
    return {
        enabled: true,
        position: document.getElementById('system-injection-position').value,
        content: content
    };
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
    document.getElementById('modal-title').textContent = '编辑模型';
    document.getElementById('model-name').value = name;
    // 允许在编辑时修改模型名称（重命名）
    document.getElementById('model-name').disabled = false;
    
    fillModelForm(config);
    
    document.getElementById('model-modal').classList.add('active');
}

// 填充模型表单（editModel和copyModel共用）
function fillModelForm(config) {
    
    if (config.api_type === 'direct_api' || config.api_type === 'gemini_native' || config.api_type === 'anthropic_native') {
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
        
        document.getElementById('model-id').value = config.model_id || '';
        document.getElementById('display-name').value = config.display_name || '';
        document.getElementById('endpoint-path').value = config.endpoint_path || '/chat/completions';
        document.getElementById('passthrough').checked = config.passthrough !== false;
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
        document.getElementById('thinking-display').value = config.thinking_display || 'summarized';
        toggleThinkingOptions();
        document.getElementById('thinking-separator').value = config.thinking_separator || '';
        
        // 加载自定义参数
        if (config.custom_params) {
            document.getElementById('custom-params').value = JSON.stringify(config.custom_params, null, 2);
        } else {
            document.getElementById('custom-params').value = '';
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
        document.getElementById('cached-tokens-mode').value = config.cached_tokens_mode || 'off';
        
        // 加载Token统计来源
        document.getElementById('token-stats-mode').value = config.token_stats_mode || 'api';
        
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
        
        // 🔧 修复：API Key变为可选（支持本地反代等无需认证的场景）
        // OpenAI 兼容和 Anthropic 原生格式都需要 api_base_url，Gemini 原生可选
        if ((apiType === 'direct_api' || apiType === 'anthropic_native') && !apiBaseUrl) {
            alert(apiType === 'anthropic_native' ? 'Anthropic原生格式需要填写 API Base URL' : 'OpenAI兼容格式需要填写 API Base URL');
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
            passthrough: document.getElementById('passthrough').checked,
            convert_system_to_user: document.getElementById('convert-system-to-user').checked,
            enable_prefix: document.getElementById('enable-prefix').checked,
            enable_partial: document.getElementById('enable-partial').checked,
        };

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

        // thinking_display 在启用思考（true/adaptive）时生效
        if (etVal === 'true' || etVal === 'adaptive') {
            const displayVal = document.getElementById('thinking-display').value;
            if (displayVal) {
                config.thinking_display = displayVal;
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
        if (cachedTokensMode && cachedTokensMode !== 'off') {
            config.cached_tokens_mode = cachedTokensMode;
        }
        
        // 保存Token统计来源（默认api不写入配置）
        const tokenStatsMode = document.getElementById('token-stats-mode').value;
        if (tokenStatsMode && tokenStatsMode !== 'api') {
            config.token_stats_mode = tokenStatsMode;
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
    
    try {
        const response = await fetch('/api/admin/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_name: modelName, config: config })
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


// 思维链模式下拉框联动：根据选择动态显示 budget 或 effort
function toggleThinkingOptions() {
    const select = document.getElementById('enable-thinking');
    const optionsDiv = document.getElementById('thinking-options');
    const controlDiv = document.getElementById('thinking-control-config');
    const budgetDiv = document.getElementById('thinking-budget-config');
    const effortDiv = document.getElementById('thinking-effort-config');
    const displayDiv = document.getElementById('thinking-display-config');
    if (!select || !optionsDiv) return;
    const val = select.value;
    // 启用思考或自适应思考时显示子选项区域
    const showOptions = (val === 'true' || val === 'adaptive');
    optionsDiv.style.display = showOptions ? 'block' : 'none';
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
    if (displayDiv) displayDiv.style.display = showOptions ? 'block' : 'none';
}
