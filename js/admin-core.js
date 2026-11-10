// admin-core.js - 核心功能和工具函数
// ==================== 全局会话失效处理 ====================
// admin 各页几十处裸 fetch 不检查 401，会话过期后只会显示空数据。
// 这里统一包装 window.fetch：遇到 401/403 弹一次横幅引导重新登录，
// 原始 Response 原样返回，各调用方原有错误分支不受影响。
(function() {
    if (window.__adminFetchPatched) return;
    window.__adminFetchPatched = true;
    const _origFetch = window.fetch.bind(window);
    let _sessionInvalid = false;
    window.fetch = async function(...args) {
        const resp = await _origFetch(...args);
        if ((resp.status === 401 || resp.status === 403) && !_sessionInvalid) {
            _sessionInvalid = true;
            if (typeof showMessage === 'function') {
                showMessage('danger', '登录会话已失效，请重新登录');
            }
            const banner = document.createElement('div');
            banner.id = 'admin-session-banner';
            banner.style.cssText = 'background:rgb(var(--danger-rgb) / .18);color:var(--danger);padding:10px 20px;text-align:center;font-size:0.85rem;position:sticky;top:0;z-index:9999;';
            banner.innerHTML = '会话已失效，<a href="/login?next=/admin" style="color:#fff;text-decoration:underline;">点此重新登录</a>';
            const header = document.querySelector('.container') || document.body;
            header.insertAdjacentElement('beforebegin', banner);
        }
        return resp;
    };
})();

// ==================== 全局变量 ====================
let currentEditingModel = null;
let currentEditingArchived = false;
let currentEditingTokenizer = null;
let selectedCaptureMode = 'direct_chat';
let selectedBattleTarget = 'A';
let currentConfigMode = 'form';
let currentConfigData = null;

// 日期过滤
let currentStartDate = null;
let currentEndDate = null;
let currentRequestStartDate = null;
let currentRequestEndDate = null;

// Token 单位只影响展示，默认沿用百万单位。
let currentTokenUnit = 'M';

// 捕获状态轮询
let capturePollingInterval = null;

// 拖动排序实例
let modelsSortable = null;

// ==================== 页面切换 ====================
// 当前页记在地址栏 # 后面：刷新留在原页，浏览器前进后退可用，也能直接给出某一页的链接。
// 形如 #models 或 #models/模型名，斜杠后面的部分交给对应页面处理。
function currentRoute() {
    const hash = location.hash.slice(1);
    const cut = hash.indexOf('/');
    const page = cut < 0 ? hash : hash.slice(0, cut);
    let detail = cut < 0 ? '' : hash.slice(cut + 1);
    try { detail = decodeURIComponent(detail); } catch { detail = ''; }
    if (!document.getElementById(page)?.classList.contains('page')) return { page: 'overview', detail: '' };
    return { page, detail };
}

function routeHash(page, detail = '') {
    return '#' + page + (detail ? '/' + encodeURIComponent(detail) : '');
}

function showPage(page, detail = '') {
    document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.page === page));
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === page));
    loadPageData(page, detail);
}

function navigateTo(page, detail = '') {
    const hash = routeHash(page, detail);
    if (location.hash !== hash) history.pushState(null, '', hash);
    showPage(page, detail);
}

async function applyRoute() {
    // 编辑窗里有没保存的修改、又不想放弃时留在原地，把地址改回去
    if (document.getElementById('model-modal').classList.contains('active') && !(await closeModelModal())) {
        history.pushState(null, '', routeHash('models', currentEditingModel || ''));
        return;
    }
    const { page, detail } = currentRoute();
    showPage(page, detail);
}

function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => navigateTo(item.dataset.page));
    });
    window.addEventListener('hashchange', applyRoute);
    const { page, detail } = currentRoute();
    showPage(page, detail);
}

let _overviewOpened = false;

function loadPageData(page, detail = '') {
    // 监控和 Token 计算器是整页嵌入的，第一次打开时才加载；切走只是隐藏，里面进行中的请求不会被取消
    const frame = document.querySelector(`#${page} > iframe[data-src]`);
    if (frame && !frame.getAttribute('src')) frame.src = frame.dataset.src;

    switch(page) {
        case 'gateway-workspace':
            window.gatewayWorkspace?.load();
            break;
        case 'overview':
            // 请求次数趋势只在第一次打开时读取，之后用它自己的刷新按钮
            if (!_overviewOpened) {
                _overviewOpened = true;
                refreshRequestStats();
            }
            refreshOverview();
            break;
        case 'models':
            loadModels().then(() => {
                const editing = document.getElementById('model-modal').classList.contains('active');
                const name = detail && resolveModelName(detail);
                if (name && !editing) openModelFromList(name);
            });
            break;
        case 'channels':
            loadChannels().then(() => { if (detail) openChannelModal(detail); });
            break;
        case 'tokenizer':
            refreshTokenizerInfo();
            loadTokenizerMappings();
            loadCustomTokenizers();
            break;
        case 'config':
            openConfigPage();
            break;
        case 'api-keys':
            loadApiKeys();
            break;
    }
}

// ==================== 工具函数 ====================
function formatNumber(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
}

function formatTokenNumber(num) {
    if (currentTokenUnit === 'B' && num >= 1000000000) {
        return (num / 1000000000).toFixed(2) + 'B';
    }
    return formatNumber(num);
}

function formatMonitorDuration(seconds) {
    if (!seconds || seconds < 0) return '0s';
    if (seconds < 1) return (seconds * 1000).toFixed(0) + 'ms';
    if (seconds < 60) return seconds.toFixed(1) + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    return Math.floor(seconds / 3600) + 'h';
}

function generateColors(count) {
    const colors = [
        'rgba(42, 168, 255, 0.8)',
        'rgba(16, 185, 129, 0.8)',
        'rgba(245, 158, 11, 0.8)',
        'rgba(239, 68, 68, 0.8)',
        'rgba(168, 85, 247, 0.8)',
        'rgba(236, 72, 153, 0.8)',
        'rgba(59, 130, 246, 0.8)',
        'rgba(34, 197, 94, 0.8)',
        'rgba(251, 146, 60, 0.8)',
        'rgba(244, 63, 94, 0.8)'
    ];
    return colors.slice(0, count);
}

// ==================== 请求 ====================
// 非 2xx 时把服务端给的 detail / message 取出来抛出，调用方只处理成功的情况
async function ensureOk(response) {
    if (response.ok) return response;
    const text = await response.text();
    let detail = text;
    try {
        const data = JSON.parse(text);
        detail = data.detail || data.message || text;
    } catch { /* 不是 JSON 就用原文 */ }
    throw new Error(`API错误 (${response.status}): ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
}

// ==================== 消息提示 ====================
// 同样的消息再出现时只累加次数；点击关闭，鼠标停留时不消失，错误多留一会
const TOAST_ICONS = { success: 'check', warning: 'warning', info: 'info' };
const _toasts = new Map();

// HTML转义（防止错误消息/服务端回显内容注入HTML）
function escapeHtml(unsafe) {
    if (unsafe === null || unsafe === undefined) return '';
    return String(unsafe)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// HTML 属性值转义。
// 放在 core 里：这个函数被 admin-apikeys.js / admin-models-edit.js 共用，
// 之前只定义在 admin-models-edit.js 中，能跑通纯粹靠 <script> 的加载顺序。
function escapeHtmlForAttr(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function showMessage(type, message) {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        stack.className = 'toast-stack';
        stack.setAttribute('role', 'status');
        stack.setAttribute('aria-live', 'polite');
        document.body.appendChild(stack);
    }
    const key = `${type}\n${message}`;
    let toast = _toasts.get(key);
    if (toast) {
        toast.count++;
        toast.element.querySelector('.toast-count').textContent = `×${toast.count}`;
        stack.appendChild(toast.element);
    } else {
        const element = document.createElement('div');
        element.className = `alert alert-${type}`;
        element.title = '点击关闭';
        element.innerHTML = `
        <svg class="ui-icon" aria-hidden="true"><use href="/css/icons.svg#${TOAST_ICONS[type] || 'x'}"/></svg>
        <div>${escapeHtml(message)}</div>
        <span class="toast-count"></span>
    `;
        const dismiss = () => { clearTimeout(toast.timer); element.remove(); _toasts.delete(key); };
        toast = { element, count: 1, dismiss };
        element.addEventListener('click', dismiss);
        element.addEventListener('mouseenter', () => clearTimeout(toast.timer));
        element.addEventListener('mouseleave', () => { toast.timer = setTimeout(dismiss, 2000); });
        _toasts.set(key, toast);
        stack.appendChild(element);
    }
    clearTimeout(toast.timer);
    toast.timer = setTimeout(toast.dismiss, type === 'danger' ? 8000 : 4500);
}

// ==================== 确认与输入对话框 ====================
// 代替浏览器自带的 confirm / prompt：样式跟界面一致，Esc 取消，Enter 确认，关掉后焦点回到原处。
// 确认框返回 true / false；带 input 时返回输入的文字，取消返回 null。
// input.options 给出 [值, 文字] 列表时是下拉选择，返回选中的值。
function openDialog({ title, message = '', confirmText = '确定', cancelText = '取消', danger = false, input = null }) {
    return new Promise(resolve => {
        const opener = document.activeElement;
        const overlay = document.createElement('div');
        overlay.className = 'modal active dialog-overlay';
        overlay.innerHTML = `
            <div class="modal-content dialog" role="alertdialog" aria-modal="true" aria-labelledby="dialog-title" aria-describedby="dialog-message">
                <div class="modal-header"><h3 id="dialog-title"></h3></div>
                <div class="modal-body"><p id="dialog-message"></p></div>
                <div class="modal-footer">
                    <button type="button" class="btn" data-dialog="cancel"></button>
                    <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-accent'}" data-dialog="confirm"></button>
                </div>
            </div>`;
        const cancelButton = overlay.querySelector('[data-dialog="cancel"]');
        const confirmButton = overlay.querySelector('[data-dialog="confirm"]');
        overlay.querySelector('#dialog-title').textContent = title;
        overlay.querySelector('#dialog-message').textContent = message;
        cancelButton.textContent = cancelText;
        confirmButton.textContent = confirmText;
        let field = null;
        if (input) {
            field = document.createElement(input.options ? 'select' : input.multiline ? 'textarea' : 'input');
            field.className = input.options ? 'form-select' : 'form-input';
            if (input.options) field.append(...input.options.map(([value, label]) => new Option(label, value)));
            field.value = input.value || '';
            field.placeholder = input.placeholder || '';
            field.setAttribute('aria-labelledby', 'dialog-title');
            overlay.querySelector('.modal-body').append(field);
        }
        const close = result => {
            document.removeEventListener('keydown', onKey, true);
            overlay.remove();
            if (opener?.isConnected) opener.focus();
            resolve(result);
        };
        const cancel = () => close(input ? null : false);
        const accept = () => close(input ? field.value : true);
        // 在捕获阶段接管按键，下面的编辑窗不会同时响应 Esc 和 Ctrl+S
        const onKey = event => {
            event.stopImmediatePropagation();
            if (event.key === 'Escape') {
                event.preventDefault();
                cancel();
            } else if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA' && event.target !== cancelButton) {
                event.preventDefault();
                accept();
            } else if (event.key === 'Tab') {
                event.preventDefault();
                const focusable = Array.from(overlay.querySelectorAll('button, input, select, textarea'));
                const index = focusable.indexOf(document.activeElement);
                focusable[(index + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus();
            }
        };
        document.addEventListener('keydown', onKey, true);
        overlay.addEventListener('click', event => {
            const action = event.target.closest('[data-dialog]')?.dataset.dialog;
            if (action === 'confirm') accept();
            else if (action === 'cancel' || event.target === overlay) cancel();
        });
        document.body.append(overlay);
        // 不可恢复的操作默认停在取消上，随手按回车不会误删
        (field || (danger ? cancelButton : confirmButton)).focus();
        field?.select?.();
    });
}

const confirmDialog = options => openDialog(options);
const promptDialog = options => openDialog({ ...options, input: options.input || {} });

// ==================== JSONC 解析器 ====================
// parseJsonc / updateJsoncValues are provided by jsonc-utils.js.

// ==================== 内存监控 ====================
async function refreshMemoryInfo() {
    try {
        const response = await fetch('/api/monitor/memory');
        const data = await response.json();
        
        const memoryValueEl = document.getElementById('memory-value');
        const memoryDetailsEl = document.getElementById('memory-details');
        const memoryBarFillEl = document.getElementById('memory-bar-fill');
        const tokenizerInfoEl = document.getElementById('tokenizer-info');
        const clearCacheBtn = document.getElementById('clear-cache-btn');
        
        if (data.error) {
            console.error('获取内存信息错误:', data.error);
            if (memoryValueEl) memoryValueEl.textContent = '错误';
            if (memoryDetailsEl) memoryDetailsEl.innerHTML = `<span style="color: var(--danger);">${escapeHtml(data.error)}</span>`;
            return;
        }
        
        // 更新进程内存
        const processMemoryMb = data.process?.rss_mb || 0;
        const systemPercent = data.system?.percent || 0;
        
        if (memoryValueEl) {
            memoryValueEl.textContent = `${processMemoryMb.toFixed(1)} MB`;
        }
        
        // 更新进度条
        if (memoryBarFillEl) {
            // 假设最大显示1GB
            const percent = Math.min((processMemoryMb / 1024) * 100, 100);
            memoryBarFillEl.style.width = `${percent}%`;
            // 根据内存使用量改变颜色
            if (processMemoryMb > 500) {
                memoryBarFillEl.style.background = 'var(--danger)'; // 红色
            } else if (processMemoryMb > 300) {
                memoryBarFillEl.style.background = 'var(--warn)'; // 黄色警示
            } else {
                memoryBarFillEl.style.background = 'var(--accent)'; // 常态用灰色
            }
        }
        
        // 更新分词器缓存信息
        const tokenizerData = data.tokenizers;
        if (tokenizerData && !tokenizerData.error) {
            const loadedCount = tokenizerData.loaded_count || 0;
            const estimatedMb = tokenizerData.estimated_memory_mb || 0;
            const loadedTokenizers = tokenizerData.loaded_tokenizers || [];
            
            // 更新详情文本
            if (memoryDetailsEl) {
                if (loadedCount > 0) {
                    memoryDetailsEl.innerHTML = `Tokenizer: ${loadedCount}个 (~${estimatedMb.toFixed(0)}MB)`;
                } else {
                    memoryDetailsEl.textContent = '无Tokenizer缓存';
                }
            }
            
            // 更新分词器详情列表
            if (tokenizerInfoEl) {
                if (loadedTokenizers.length > 0) {
                    let html = '';
                    for (const t of loadedTokenizers) {
                        const idleMinutes = t.idle_minutes || 0;
                        html += `<div>• ${escapeHtml(t.name)}: ${idleMinutes.toFixed(1)}分钟</div>`;
                    }
                    tokenizerInfoEl.innerHTML = html;
                    tokenizerInfoEl.style.display = 'block';
                } else {
                    tokenizerInfoEl.style.display = 'none';
                }
            }
            
            // 显示/隐藏清理按钮
            if (clearCacheBtn) {
                clearCacheBtn.style.display = loadedCount > 0 ? 'block' : 'none';
            }
        } else {
            if (memoryDetailsEl) {
                memoryDetailsEl.textContent = tokenizerData?.error || 'Tokenizer信息不可用';
            }
            if (tokenizerInfoEl) tokenizerInfoEl.style.display = 'none';
            if (clearCacheBtn) clearCacheBtn.style.display = 'none';
        }
    } catch (error) {
        console.error('刷新内存信息失败:', error);
        const memoryValueEl = document.getElementById('memory-value');
        const memoryDetailsEl = document.getElementById('memory-details');
        if (memoryValueEl) memoryValueEl.textContent = '错误';
        if (memoryDetailsEl) memoryDetailsEl.innerHTML = '<span style="color: var(--danger);">请求失败</span>';
    }
}

async function clearTokenizerCache() {
    try {
        const response = await fetch('/api/monitor/clear_tokenizer_cache', {
            method: 'POST'
        });
        const data = await response.json();
        
        if (data.error) {
            showMessage('danger', data.error);
        } else {
            const clearedCount = data.cleared_count || 0;
            const freedMb = data.memory_freed_mb || 0;
            showMessage('success', `已清理 ${clearedCount} 个分词器，释放 ${freedMb.toFixed(1)} MB`);
            refreshMemoryInfo();
        }
    } catch (error) {
        showMessage('danger', '清理缓存请求失败');
        console.error('清理分词器缓存失败:', error);
    }
}

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', () => {
    initializeUsageDateRange();
    // 按地址栏里的页面加载数据，各请求互不等待
    initNavigation();
    refreshMemoryInfo();
    
    // 🔧 B7 修复：使用 visibility API，页面不可见时暂停所有定时器
    let _overviewTimer = null;
    let _memoryTimer = null;

    function _startTimers() {
        if (!_overviewTimer) {
            _overviewTimer = setInterval(() => {
                if (document.getElementById('overview').classList.contains('active')) {
                    refreshOverview();
                }
            }, 300000); // 300秒刷新一次（从120秒降频）
        }
        if (!_memoryTimer) {
            _memoryTimer = setInterval(refreshMemoryInfo, 30000); // 30秒（从10秒降频）
        }
    }

    function _stopTimers() {
        if (_overviewTimer) { clearInterval(_overviewTimer); _overviewTimer = null; }
        if (_memoryTimer) { clearInterval(_memoryTimer); _memoryTimer = null; }
    }

    _startTimers();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            _stopTimers();
        } else {
            _startTimers();
            // 恢复可见时立即刷新一次
            refreshMemoryInfo();
        }
    });
});
