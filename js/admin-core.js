// admin-core.js - 核心功能和工具函数

// ==================== 全局变量 ====================
let currentEditingModel = null;
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

// 捕获状态轮询
let capturePollingInterval = null;

// 拖动排序实例
let modelsSortable = null;

// ==================== 页面切换 ====================
function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const page = item.dataset.page;
            
            document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.getElementById(page).classList.add('active');
            
            loadPageData(page);
        });
    });
}

function loadPageData(page) {
    switch(page) {
        case 'overview':
            refreshOverview();
            break;
        case 'models':
            loadModels();
            break;
        case 'tokenizer':
            refreshTokenizerInfo();
            loadTokenizerMappings();
            break;
        case 'config':
            loadConfig();
            break;
        case 'monitor':
            initMonitorPage();
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

// ==================== 消息提示 ====================
function showMessage(type, message) {
    const notification = document.createElement('div');
    notification.className = `alert alert-${type}`;
    notification.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 2000; min-width: 300px;';
    notification.innerHTML = `
        <span>${type === 'success' ? '✅' : '❌'}</span>
        <div>${message}</div>
    `;
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 5000);
}

function showQuietMessage(type, message) {
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        z-index: 2000;
        padding: 10px 20px;
        background: ${type === 'success' ? 'rgba(16, 185, 129, 0.9)' : 'rgba(239, 68, 68, 0.9)'};
        color: white;
        border-radius: 6px;
        font-size: 0.875rem;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        opacity: 0;
        transition: opacity 0.3s;
    `;
    notification.textContent = message;
    document.body.appendChild(notification);
    
    setTimeout(() => notification.style.opacity = '1', 10);
    setTimeout(() => {
        notification.style.opacity = '0';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

function showConfigMessage(type, message) {
    const msgEl = document.getElementById('config-message');
    msgEl.innerHTML = `
        <div class="alert alert-${type}">
            <span>${type === 'success' ? '✅' : '❌'}</span>
            <div>${message}</div>
        </div>
    `;
    setTimeout(() => msgEl.innerHTML = '', 5000);
}

// ==================== JSONC 解析器 ====================
function parseJsonc(content) {
    let result = '';
    let i = 0;
    let inString = false;
    let stringChar = '';
    
    while (i < content.length) {
        const char = content[i];
        const nextChar = content[i + 1];
        
        if (!inString && (char === '"' || char === "'")) {
            inString = true;
            stringChar = char;
            result += char;
            i++;
            continue;
        } else if (inString && char === stringChar && content[i - 1] !== '\\') {
            inString = false;
            result += char;
            i++;
            continue;
        } else if (inString) {
            result += char;
            i++;
            continue;
        }
        
        if (char === '/' && nextChar === '/') {
            while (i < content.length && content[i] !== '\n') {
                i++;
            }
            continue;
        }
        
        if (char === '/' && nextChar === '*') {
            i += 2;
            while (i < content.length - 1) {
                if (content[i] === '*' && content[i + 1] === '/') {
                    i += 2;
                    break;
                }
                i++;
            }
            continue;
        }
        
        result += char;
        i++;
    }
    
    result = result.replace(/,(\s*[}\]])/g, '$1');
    return JSON.parse(result);
}

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    refreshOverview();
    refreshTokenStats();
    refreshRequestStats();
    
    const tokenizerPage = document.getElementById('tokenizer');
    if (tokenizerPage) {
        refreshTokenizerInfo();
        loadTokenizerMappings();
    }
    
    // 🔧 优化：将自动刷新间隔从5秒改为30秒，减少对SQLite的频繁查询
    // 用户可以随时点击"刷新"按钮手动刷新
    setInterval(() => {
        if (document.getElementById('overview').classList.contains('active')) {
            refreshOverview();
        }
    }, 120000); // 30秒刷新一次
});