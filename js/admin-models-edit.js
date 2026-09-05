'use strict';
function toggleCollapsible(headerElement) {
    const section = headerElement.parentElement;
    section.classList.toggle('collapsed');
}

function showAddModelModal() {
    resetModelEditorForm();
    modelEditor.originalConfig = {};
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
    document.getElementById('endpoint-path').value = '';
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
    
    beginModelEditing();
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
    
    beginModelEditing();
    
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
    
    beginModelEditing();
}

// 填充模型表单（editModel和copyModel共用）
function fillModelForm(config) {
    resetModelEditorForm();
    modelEditor.originalConfig = structuredClone(config);
    config = modelPrimaryConfig(config);
    
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
        document.getElementById('endpoint-path').value = config.endpoint_path || '';
        // Gemini 上游协议（generate_content 为默认值）
        document.getElementById('upstream-protocol').value = config.upstream_protocol || 'generate_content';
        document.getElementById('passthrough').checked = config.passthrough !== false;
        document.getElementById('model-provider').value = config.provider || '';
        document.getElementById('model-native-tools').value = (config.native_tools || []).join(', ');
        document.getElementById('model-native-tool-options').value = config.native_tool_options ? JSON.stringify(config.native_tool_options, null, 2) : '';
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
            document.getElementById('pricing-input').value = config.pricing.input ?? '';
            document.getElementById('pricing-output').value = config.pricing.output ?? '';
            document.getElementById('pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('pricing-currency').value = config.pricing.currency || 'USD';
            document.getElementById('pricing-cached-input').value = config.pricing.cached_input ?? '';
        } else {
            // 重置计费配置
            document.getElementById('pricing-input').value = '';
            document.getElementById('pricing-output').value = '';
            document.getElementById('pricing-unit').value = '1000000';
            document.getElementById('pricing-currency').value = 'USD';
            document.getElementById('pricing-cached-input').value = '';
        }
        
        // 加载最高温度限制
        document.getElementById('max-temperature').value = config.max_temperature ?? '';
        
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
            document.getElementById('lmarena-pricing-input').value = config.pricing.input ?? '';
            document.getElementById('lmarena-pricing-output').value = config.pricing.output ?? '';
            document.getElementById('lmarena-pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('lmarena-pricing-currency').value = config.pricing.currency || 'USD';
            document.getElementById('lmarena-pricing-cached-input').value = config.pricing.cached_input ?? '';
        } else {
            // 重置计费配置
            document.getElementById('lmarena-pricing-input').value = '';
            document.getElementById('lmarena-pricing-output').value = '';
            document.getElementById('lmarena-pricing-unit').value = '1000000';
            document.getElementById('lmarena-pricing-currency').value = 'USD';
            document.getElementById('lmarena-pricing-cached-input').value = '';
        }
        
        // 加载最高温度限制
        document.getElementById('lmarena-max-temperature').value = config.max_temperature ?? '';
        
        // 加载最大输出Token限制
        document.getElementById('lmarena-max-tokens').value = config.max_tokens || '';
        
        toggleBattleTarget();
        
        // 非 Direct API 模型重置自动重试字段，防止沿用上次编辑值
        resetAutoRetryFields();
    }
    
    toggleConfigType();
}

function closeModelModal(force = false) {
    return endModelEditing(force);
}

async function saveModel() {
    if (modelEditor.saving) return;
    if (!validateModelEditor()) return;
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
            sanitize_recursive_schemas: false,
            ...(document.getElementById('model-provider').value ? {provider: document.getElementById('model-provider').value} : {}),
            native_tools: document.getElementById('model-native-tools').value.split(',').map(value => value.trim()).filter(Boolean),
            native_tool_options: JSON.parse(document.getElementById('model-native-tool-options').value.trim() || '{}'),
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
            if (normalizedEndpointPath !== modelDefaultEndpoint(apiType)) {
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

    setModelSaving(true);
    try {
        // 🔧 修复：编辑模式下改名时传 old_model_name，让后端做重命名而非新增。
        // 旧版不论修改与否都发同名 POST，改名=新增重复模型，旧配置继续存活。
        const body = { model_name: modelName, config: mergeModelEditorConfig(config) };
        if (currentEditingModel) {
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
        
        closeModelModal(true);
        loadModels();
        showMessage('success', `模型 ${modelName} 保存成功`);
        
    } catch (error) {
        console.error('❌ 保存模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '保存失败: ' + error.message);
    } finally {
        setModelSaving(false);
    }
}
