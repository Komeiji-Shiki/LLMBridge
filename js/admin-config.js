// admin-config.js - 配置编辑功能

// ==================== 配置编辑模式切换 ====================
function switchConfigMode(mode) {
    currentConfigMode = mode;
    
    // 更新按钮状态
    document.getElementById('mode-jsonc-btn').className = mode === 'jsonc' ? 'btn btn-primary' : 'btn';
    document.getElementById('mode-form-btn').className = mode === 'form' ? 'btn btn-primary' : 'btn';
    
    // 切换编辑器显示
    if (mode === 'jsonc') {
        document.getElementById('config-jsonc-editor').style.display = 'block';
        document.getElementById('config-form-editor').style.display = 'none';
    } else {
        // 从JSONC转换到表单模式时，先解析当前内容
        try {
            const jsonContent = document.getElementById('config-editor').value;
            currentConfigData = parseJsonc(jsonContent);
            buildConfigForm();
            document.getElementById('config-jsonc-editor').style.display = 'none';
            document.getElementById('config-form-editor').style.display = 'block';
        } catch (error) {
            showConfigMessage('danger', '解析配置失败: ' + error.message);
            // 切换回JSONC模式
            currentConfigMode = 'jsonc';
            document.getElementById('mode-jsonc-btn').className = 'btn btn-primary';
            document.getElementById('mode-form-btn').className = 'btn';
        }
    }
}

// ==================== 构建配置表单 ====================
function buildConfigForm() {
    const container = document.getElementById('config-form-content');
    if (!currentConfigData) {
        container.innerHTML = '<p style="color: var(--text-dim);">无法加载配置数据</p>';
        return;
    }
    
    let html = '';
    
    // 基础配置
    html += buildBasicConfig();
    
    // ID 更新器配置
    html += buildIdUpdaterConfig();
    
    // 重试配置
    html += buildRetryConfig();
    
    // Bypass配置
    html += buildBypassConfig();
    
    // 图像配置
    html += buildImageConfig();
    
    // 消息转换配置
    html += buildMessageConfig();
    
    // 图床配置
    html += buildFileBedConfig();
    
    // 连接和性能配置
    html += buildPerformanceConfig();
    
    // Reasoning配置
    html += buildReasoningConfig();
    
    // 其他设置
    html += buildOtherConfig();
    
    container.innerHTML = html;
}

// ==================== 配置卡片构建函数 ====================

function buildBasicConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>基础配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">版本号</label>
                    <input type="text" class="form-input" id="form-version" value="${currentConfigData.version || ''}" readonly>
                    <small style="color: var(--text-dim);">用于程序更新检查，请不要手动修改</small>
                </div>
                <div class="form-group">
                    <label class="form-label">Session ID</label>
                    <input type="text" class="form-input" id="form-session_id" value="${currentConfigData.session_id || ''}">
                    <small style="color: var(--text-dim);">当前 LMArena 页面的会话 ID</small>
                </div>
            </div>
        </div>
    `;
}

function buildIdUpdaterConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>ID 更新器专用配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">上次使用的模式</label>
                    <select class="form-select" id="form-id_updater_last_mode">
                        <option value="direct_chat" ${currentConfigData.id_updater_last_mode === 'direct_chat' ? 'selected' : ''}>Direct Chat</option>
                        <option value="battle" ${currentConfigData.id_updater_last_mode === 'battle' ? 'selected' : ''}>Battle</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Battle 模式目标</label>
                    <select class="form-select" id="form-id_updater_battle_target">
                        <option value="A" ${currentConfigData.id_updater_battle_target === 'A' ? 'selected' : ''}>A</option>
                        <option value="B" ${currentConfigData.id_updater_battle_target === 'B' ? 'selected' : ''}>B</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">自动保存模式</label>
                    <select class="form-select" id="form-id_updater_auto_save_mode">
                        <option value="model" ${currentConfigData.id_updater_auto_save_mode === 'model' ? 'selected' : ''}>model - 保存到特定模型（推荐）</option>
                        <option value="global" ${currentConfigData.id_updater_auto_save_mode === 'global' ? 'selected' : ''}>global - 保存到全局配置</option>
                        <option value="ask" ${currentConfigData.id_updater_auto_save_mode === 'ask' ? 'selected' : ''}>ask - 每次询问</option>
                    </select>
                </div>
            </div>
        </div>
    `;
}

function buildRetryConfig() {
    const retryConfig = currentConfigData.empty_response_retry || {};
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>重试配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-enable_auto_retry" ${currentConfigData.enable_auto_retry ? 'checked' : ''} style="margin-right: 8px;">
                        启用自动重试
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">重试超时时间（秒）</label>
                    <input type="number" class="form-input" id="form-retry_timeout_seconds" value="${currentConfigData.retry_timeout_seconds || 60}">
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-empty_response_retry_enabled" ${retryConfig.enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用空响应重试
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">最大重试次数</label>
                    <input type="number" class="form-input" id="form-empty_response_retry_max_retries" value="${retryConfig.max_retries || 5}">
                </div>
                <div class="form-group">
                    <label class="form-label">基础延迟（毫秒）</label>
                    <input type="number" class="form-input" id="form-empty_response_retry_base_delay_ms" value="${retryConfig.base_delay_ms || 100}">
                </div>
                <div class="form-group">
                    <label class="form-label">最大延迟（毫秒）</label>
                    <input type="number" class="form-input" id="form-empty_response_retry_max_delay_ms" value="${retryConfig.max_delay_ms || 3000}">
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-empty_response_retry_show_retry_info_to_client" ${retryConfig.show_retry_info_to_client ? 'checked' : ''} style="margin-right: 8px;">
                        向客户端显示重试信息
                    </label>
                </div>
            </div>
        </div>
    `;
}

function buildBypassConfig() {
    const bypassSettings = currentConfigData.bypass_settings || {};
    const attachmentBypassSettings = currentConfigData.attachment_bypass_settings || {};
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>Bypass 配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-bypass_enabled" ${currentConfigData.bypass_enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用 Bypass 模式
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">Bypass 设置</label>
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;">
                        <label>
                            <input type="checkbox" id="form-bypass_settings_text" ${bypassSettings.text ? 'checked' : ''} style="margin-right: 8px;">
                            文本模型
                        </label>
                        <label>
                            <input type="checkbox" id="form-bypass_settings_search" ${bypassSettings.search ? 'checked' : ''} style="margin-right: 8px;">
                            搜索模型
                        </label>
                        <label>
                            <input type="checkbox" id="form-bypass_settings_image" ${bypassSettings.image ? 'checked' : ''} style="margin-right: 8px;">
                            图像模型
                        </label>
                    </div>
                </div>
                <div class="form-group">
                    <label class="form-label">附件 Bypass 设置</label>
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;">
                        <label>
                            <input type="checkbox" id="form-attachment_bypass_settings_text" ${attachmentBypassSettings.text ? 'checked' : ''} style="margin-right: 8px;">
                            文本附件
                        </label>
                        <label>
                            <input type="checkbox" id="form-attachment_bypass_settings_search" ${attachmentBypassSettings.search ? 'checked' : ''} style="margin-right: 8px;">
                            搜索附件
                        </label>
                        <label>
                            <input type="checkbox" id="form-attachment_bypass_settings_image" ${attachmentBypassSettings.image ? 'checked' : ''} style="margin-right: 8px;">
                            图像附件
                        </label>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function buildImageConfig() {
    const localSaveFormat = currentConfigData.local_save_format || {};
    const imageReturnFormat = currentConfigData.image_return_format || {};
    const base64Conversion = imageReturnFormat.base64_conversion || {};
    const imageOptimization = currentConfigData.image_optimization || {};
    const processedImageCache = currentConfigData.processed_image_cache || {};
    
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>图像处理配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-save_images_locally" ${currentConfigData.save_images_locally ? 'checked' : ''} style="margin-right: 8px;">
                        本地保存图像
                    </label>
                </div>
                
                <h4 style="margin: 20px 0 10px 0;">本地保存格式</h4>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-local_save_format_enabled" ${localSaveFormat.enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用格式转换
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">保存格式</label>
                    <select class="form-select" id="form-local_save_format_format">
                        <option value="png" ${localSaveFormat.format === 'png' ? 'selected' : ''}>PNG</option>
                        <option value="jpeg" ${localSaveFormat.format === 'jpeg' ? 'selected' : ''}>JPEG</option>
                        <option value="webp" ${localSaveFormat.format === 'webp' ? 'selected' : ''}>WebP</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">JPEG 质量 (1-100)</label>
                    <input type="number" class="form-input" id="form-local_save_format_jpeg_quality" value="${localSaveFormat.jpeg_quality || 100}" min="1" max="100">
                </div>
                
                <h4 style="margin: 20px 0 10px 0;">返回格式配置</h4>
                <div class="form-group">
                    <label class="form-label">返回模式</label>
                    <select class="form-select" id="form-image_return_format_mode">
                        <option value="base64" ${imageReturnFormat.mode === 'base64' ? 'selected' : ''}>Base64</option>
                        <option value="url" ${imageReturnFormat.mode === 'url' ? 'selected' : ''}>URL</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-image_return_format_base64_conversion_enabled" ${base64Conversion.enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用 Base64 转换
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">目标格式</label>
                    <select class="form-select" id="form-image_return_format_base64_conversion_target_format">
                        <option value="png" ${base64Conversion.target_format === 'png' ? 'selected' : ''}>PNG</option>
                        <option value="jpeg" ${base64Conversion.target_format === 'jpeg' ? 'selected' : ''}>JPEG</option>
                        <option value="webp" ${base64Conversion.target_format === 'webp' ? 'selected' : ''}>WebP</option>
                    </select>
                </div>
                
                <h4 style="margin: 20px 0 10px 0;">图像优化</h4>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-image_optimization_enabled" ${imageOptimization.enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用图像优化
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-image_optimization_strip_metadata" ${imageOptimization.strip_metadata ? 'checked' : ''} style="margin-right: 8px;">
                        移除元数据
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-image_optimization_convert_to_webp" ${imageOptimization.convert_to_webp ? 'checked' : ''} style="margin-right: 8px;">
                        转换为 WebP
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">WebP 质量 (1-100)</label>
                    <input type="number" class="form-input" id="form-image_optimization_webp_quality" value="${imageOptimization.webp_quality || 70}" min="1" max="100">
                </div>
                <div class="form-group">
                    <label class="form-label">最大宽度（像素）</label>
                    <input type="number" class="form-input" id="form-image_optimization_max_width" value="${imageOptimization.max_width || 4096}">
                </div>
                <div class="form-group">
                    <label class="form-label">最大高度（像素）</label>
                    <input type="number" class="form-input" id="form-image_optimization_max_height" value="${imageOptimization.max_height || 4096}">
                </div>
                
                <h4 style="margin: 20px 0 10px 0;">图像缓存</h4>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-processed_image_cache_enabled" ${processedImageCache.enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用处理图像缓存
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">缓存 TTL（秒）</label>
                    <input type="number" class="form-input" id="form-processed_image_cache_ttl_seconds" value="${processedImageCache.ttl_seconds || 3600}">
                </div>
                <div class="form-group">
                    <label class="form-label">最大缓存大小</label>
                    <input type="number" class="form-input" id="form-processed_image_cache_max_size" value="${processedImageCache.max_size || 200}">
                </div>
            </div>
        </div>
    `;
}

function buildMessageConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>消息转换配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">消息角色转换模式</label>
                    <select class="form-select" id="form-message_role_conversion_mode">
                        <option value="system_merge" ${currentConfigData.message_role_conversion_mode === 'system_merge' ? 'selected' : ''}>system_merge - 合并系统消息</option>
                        <option value="preserve" ${currentConfigData.message_role_conversion_mode === 'preserve' ? 'selected' : ''}>preserve - 保留原始角色</option>
                        <option value="convert" ${currentConfigData.message_role_conversion_mode === 'convert' ? 'selected' : ''}>convert - 转换角色</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-merge_preserve_role_labels" ${currentConfigData.merge_preserve_role_labels ? 'checked' : ''} style="margin-right: 8px;">
                        合并时保留角色标签
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-tavern_mode_enabled" ${currentConfigData.tavern_mode_enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用 Tavern 模式
                    </label>
                </div>
            </div>
        </div>
    `;
}

function buildFileBedConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>图床配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-file_bed_enabled" ${currentConfigData.file_bed_enabled ? 'checked' : ''} style="margin-right: 8px;">
                        启用图床上传
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">选择策略</label>
                    <select class="form-select" id="form-file_bed_selection_strategy">
                        <option value="round_robin" ${currentConfigData.file_bed_selection_strategy === 'round_robin' ? 'selected' : ''}>round_robin - 轮询</option>
                        <option value="random" ${currentConfigData.file_bed_selection_strategy === 'random' ? 'selected' : ''}>random - 随机</option>
                        <option value="priority" ${currentConfigData.file_bed_selection_strategy === 'priority' ? 'selected' : ''}>priority - 优先级</option>
                    </select>
                </div>
                <small style="color: var(--text-dim);">图床端点配置请使用 JSONC 模式编辑</small>
            </div>
        </div>
    `;
}

function buildPerformanceConfig() {
    const connectionPool = currentConfigData.connection_pool || {};
    const downloadDelay = currentConfigData.download_delay || {};
    const downloadTimeout = currentConfigData.download_timeout || {};
    const memoryManagement = currentConfigData.memory_management || {};
    const performanceMonitoring = currentConfigData.performance_monitoring || {};
    
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>连接和性能配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-use_default_ids_if_mapping_not_found" ${currentConfigData.use_default_ids_if_mapping_not_found ? 'checked' : ''} style="margin-right: 8px;">
                        映射未找到时使用默认 ID
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">流响应超时（秒）</label>
                    <input type="number" class="form-input" id="form-stream_response_timeout_seconds" value="${currentConfigData.stream_response_timeout_seconds || 600}">
                </div>
                <div class="form-group">
                    <label class="form-label">最大并发下载数</label>
                    <input type="number" class="form-input" id="form-max_concurrent_downloads" value="${currentConfigData.max_concurrent_downloads || 3}">
                </div>
                <small style="color: var(--text-dim);">更多详细配置请使用 JSONC 模式编辑</small>
            </div>
        </div>
    `;
}

function buildReasoningConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>Reasoning 配置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-enable_lmarena_reasoning" ${currentConfigData.enable_lmarena_reasoning ? 'checked' : ''} style="margin-right: 8px;">
                        启用 LMArena Reasoning
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">Reasoning 输出模式</label>
                    <select class="form-select" id="form-reasoning_output_mode">
                        <option value="openai" ${currentConfigData.reasoning_output_mode === 'openai' ? 'selected' : ''}>OpenAI</option>
                        <option value="anthropic" ${currentConfigData.reasoning_output_mode === 'anthropic' ? 'selected' : ''}>Anthropic</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-preserve_streaming" ${currentConfigData.preserve_streaming ? 'checked' : ''} style="margin-right: 8px;">
                        保留流式传输
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-strip_reasoning_from_history" ${currentConfigData.strip_reasoning_from_history ? 'checked' : ''} style="margin-right: 8px;">
                        从历史中剥离 Reasoning
                    </label>
                </div>
            </div>
        </div>
    `;
}

function buildOtherConfig() {
    return `
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header"><h3>其他设置</h3></div>
            <div style="padding: 20px;">
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-debug_show_full_urls" ${currentConfigData.debug_show_full_urls ? 'checked' : ''} style="margin-right: 8px;">
                        调试时显示完整 URL
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">URL 显示长度</label>
                    <input type="number" class="form-input" id="form-url_display_length" value="${currentConfigData.url_display_length || 200}">
                </div>
                <div class="form-group">
                    <label class="form-label">
                        <input type="checkbox" id="form-enable_idle_restart" ${currentConfigData.enable_idle_restart ? 'checked' : ''} style="margin-right: 8px;">
                        启用空闲重启
                    </label>
                </div>
                <div class="form-group">
                    <label class="form-label">空闲重启超时（秒，-1 表示禁用）</label>
                    <input type="number" class="form-input" id="form-idle_restart_timeout_seconds" value="${currentConfigData.idle_restart_timeout_seconds || -1}">
                </div>
                <div class="form-group">
                    <label class="form-label">API Key</label>
                    <input type="password" class="form-input" id="form-api_key" value="${currentConfigData.api_key || ''}" placeholder="留空则不启用认证">
                </div>
                <small style="color: var(--text-dim);">Tokenizer 配置请在专门的 Tokenizer 页面配置</small>
            </div>
        </div>
    `;
}

// ==================== 从表单收集数据 ====================
function formToConfig() {
    // 这个函数需要从所有表单字段收集数据
    // 由于字段太多，这里使用简化版本，保留原配置并更新表单字段
    const config = Object.assign({}, currentConfigData);
    
    // 基础配置
    config.version = document.getElementById('form-version').value;
    config.session_id = document.getElementById('form-session_id').value;
    
    // ID更新器配置
    config.id_updater_last_mode = document.getElementById('form-id_updater_last_mode').value;
    config.id_updater_battle_target = document.getElementById('form-id_updater_battle_target').value;
    config.id_updater_auto_save_mode = document.getElementById('form-id_updater_auto_save_mode').value;
    
    // 重试配置
    config.enable_auto_retry = document.getElementById('form-enable_auto_retry').checked;
    config.retry_timeout_seconds = parseInt(document.getElementById('form-retry_timeout_seconds').value);
    
    config.empty_response_retry = config.empty_response_retry || {};
    config.empty_response_retry.enabled = document.getElementById('form-empty_response_retry_enabled').checked;
    config.empty_response_retry.max_retries = parseInt(document.getElementById('form-empty_response_retry_max_retries').value);
    config.empty_response_retry.base_delay_ms = parseInt(document.getElementById('form-empty_response_retry_base_delay_ms').value);
    config.empty_response_retry.max_delay_ms = parseInt(document.getElementById('form-empty_response_retry_max_delay_ms').value);
    config.empty_response_retry.show_retry_info_to_client = document.getElementById('form-empty_response_retry_show_retry_info_to_client').checked;
    
    // Bypass配置
    config.bypass_enabled = document.getElementById('form-bypass_enabled').checked;
    config.bypass_settings = config.bypass_settings || {};
    config.bypass_settings.text = document.getElementById('form-bypass_settings_text').checked;
    config.bypass_settings.search = document.getElementById('form-bypass_settings_search').checked;
    config.bypass_settings.image = document.getElementById('form-bypass_settings_image').checked;
    
    config.attachment_bypass_settings = config.attachment_bypass_settings || {};
    config.attachment_bypass_settings.text = document.getElementById('form-attachment_bypass_settings_text').checked;
    config.attachment_bypass_settings.search = document.getElementById('form-attachment_bypass_settings_search').checked;
    config.attachment_bypass_settings.image = document.getElementById('form-attachment_bypass_settings_image').checked;
    
    // 图像配置
    config.save_images_locally = document.getElementById('form-save_images_locally').checked;
    
    // 消息转换
    config.message_role_conversion_mode = document.getElementById('form-message_role_conversion_mode').value;
    config.merge_preserve_role_labels = document.getElementById('form-merge_preserve_role_labels').checked;
    config.tavern_mode_enabled = document.getElementById('form-tavern_mode_enabled').checked;
    
    // 图床
    config.file_bed_enabled = document.getElementById('form-file_bed_enabled').checked;
    config.file_bed_selection_strategy = document.getElementById('form-file_bed_selection_strategy').value;
    
    // 性能配置
    config.use_default_ids_if_mapping_not_found = document.getElementById('form-use_default_ids_if_mapping_not_found').checked;
    config.stream_response_timeout_seconds = parseInt(document.getElementById('form-stream_response_timeout_seconds').value);
    config.max_concurrent_downloads = parseInt(document.getElementById('form-max_concurrent_downloads').value);
    
    // Reasoning
    config.enable_lmarena_reasoning = document.getElementById('form-enable_lmarena_reasoning').checked;
    config.reasoning_output_mode = document.getElementById('form-reasoning_output_mode').value;
    config.preserve_streaming = document.getElementById('form-preserve_streaming').checked;
    config.strip_reasoning_from_history = document.getElementById('form-strip_reasoning_from_history').checked;
    
    // 其他
    config.debug_show_full_urls = document.getElementById('form-debug_show_full_urls').checked;
    config.url_display_length = parseInt(document.getElementById('form-url_display_length').value);
    config.enable_idle_restart = document.getElementById('form-enable_idle_restart').checked;
    config.idle_restart_timeout_seconds = parseInt(document.getElementById('form-idle_restart_timeout_seconds').value);
    config.api_key = document.getElementById('form-api_key').value;
    
    return config;
}

// ==================== 加载和保存配置 ====================
async function loadConfig() {
    try {
        const response = await fetch('/api/admin/config');
        
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
        
        const data = await response.json();
        document.getElementById('config-editor').value = data.content;
        
        try {
            currentConfigData = parseJsonc(data.content);
        } catch (e) {
            console.error('解析配置失败:', e);
            showConfigMessage('danger', '解析配置失败，请检查JSON格式');
        }
        
        if (currentConfigData) {
            buildConfigForm();
        }
    } catch (error) {
        console.error('❌ 加载配置失败:', error);
        console.error('错误详情:', error.message);
        showConfigMessage('danger', '加载配置失败: ' + error.message);
    }
}

async function saveConfig() {
    let content;
    
    try {
        if (currentConfigMode === 'jsonc') {
            content = document.getElementById('config-editor').value;
        } else {
            const configObj = formToConfig();
            content = JSON.stringify(configObj, null, 2);
        }
        
        const response = await fetch('/api/admin/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
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
        
        showConfigMessage('success', '配置已保存并重新加载');
        await loadConfig();
        
    } catch (error) {
        console.error('❌ 保存配置失败:', error);
        console.error('错误详情:', error.message);
        showConfigMessage('danger', '保存失败: ' + error.message);
    }
}

// ==================== 监控页面初始化 ====================
function initMonitorPage() {
    // iframe会自动加载monitor.html
}