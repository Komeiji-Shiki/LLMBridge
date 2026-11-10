// 快速跳转：Ctrl / ⌘ + K 打开，输入页面名或模型名，回车直达。
// 模型有几百个的时候，比先进模型页再搜索少两步。
(() => {
    'use strict';
    let overlay = null, items = [], active = 0;

    function candidates() {
        const pages = Array.from(document.querySelectorAll('.nav-item:not(.legacy-lmarena)'), item => {
            const label = item.textContent.trim();
            return { kind: '页面', label, search: label.toLowerCase(), open: () => navigateTo(item.dataset.page) };
        });
        const models = Object.entries(modelWorkspace.models).map(([name, config]) => {
            const primary = modelPrimaryConfig(config);
            return {
                kind: isModelConfigArchived(config) ? '已归档' : '模型',
                label: name,
                hint: primary.model_id && primary.model_id !== name ? primary.model_id : '',
                search: `${name} ${primary.model_id || ''} ${primary.display_name || ''}`.toLowerCase(),
                open: () => navigateTo('models', name),
            };
        });
        const channels = Object.entries(modelWorkspace.channels).map(([id, channel]) => ({
            kind: '渠道',
            label: channel.name,
            hint: channel.api_base_url || '',
            search: `${channel.name} ${channel.api_base_url || ''}`.toLowerCase(),
            open: () => navigateTo('channels', id),
        }));
        return pages.concat(channels, models);
    }

    function render() {
        const words = overlay.querySelector('input').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
        items = candidates().filter(item => words.every(word => item.search.includes(word))).slice(0, 40);
        active = Math.min(active, Math.max(0, items.length - 1));
        const list = overlay.querySelector('.palette-list');
        list.replaceChildren(...items.map((item, index) => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'palette-item';
            row.setAttribute('role', 'option');
            row.setAttribute('aria-selected', String(index === active));
            row.dataset.index = index;
            for (const [className, text] of [['palette-kind', item.kind], ['palette-label', item.label], ['palette-hint', item.hint]]) {
                if (!text) continue;
                const part = document.createElement('span');
                part.className = className;
                part.textContent = text;
                row.append(part);
            }
            return row;
        }));
        overlay.querySelector('.palette-empty').hidden = items.length > 0;
        list.children[active]?.scrollIntoView({ block: 'nearest' });
    }

    function open() {
        if (overlay || document.querySelector('.modal.active')) return;
        overlay = document.createElement('div');
        overlay.className = 'modal active palette-overlay';
        overlay.innerHTML = `<div class="modal-content palette" role="dialog" aria-modal="true" aria-label="快速跳转">
            <input class="form-input" type="search" placeholder="跳到页面，或输入模型名直接打开设置" aria-label="搜索页面和模型" autocomplete="off" spellcheck="false">
            <div class="palette-list" role="listbox"></div>
            <div class="palette-empty" hidden>没有匹配的页面、渠道或模型</div>
            <div class="palette-foot">↑ ↓ 选择 · Enter 打开 · Esc 关闭</div>
        </div>`;
        const input = overlay.querySelector('input');
        input.addEventListener('input', () => { active = 0; render(); });
        overlay.addEventListener('click', event => {
            const row = event.target.closest('.palette-item');
            if (row) choose(Number(row.dataset.index));
            else if (event.target === overlay) close();
        });
        document.body.append(overlay);
        active = 0;
        render();
        input.focus();
        // 还没进过模型页时模型列表是空的，先读一次，读到后补进结果
        if (!Object.keys(modelWorkspace.models).length) loadModels().then(() => { if (overlay) render(); });
    }

    function close() {
        overlay?.remove();
        overlay = null;
    }

    function choose(index) {
        const item = items[index];
        if (!item) return;
        close();
        item.open();
    }

    document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault();
            if (overlay) close(); else open();
            return;
        }
        if (!overlay) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (items.length) active = (active + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
            render();
        } else if (event.key === 'Enter') {
            event.preventDefault();
            choose(active);
        }
    });

    window.commandPalette = { open, close };
})();
