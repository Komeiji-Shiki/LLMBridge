// ==UserScript==
// @name         网页扭扭字解析插件 - AST重构版 3.0
// @namespace    http://tampermonkey.net/
// @version      3.0
// @description  利用抽象语法树完美解析任意嵌套的LaTeX格式扭扭字；同时自动渲染被代码块包裹的HTML可视化片段
// @author       GrayWill
// @match        *://*/*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // ============================================================
    //  1. 词法分析器 (Lexer)：把输入的字符串切成一个个标记 (Token)
    // ============================================================
    function tokenize(text) {
        let tokens = [];
        let i = 0;
        // 先把用来标记公式的 \( 和 \) 给剥离掉
        let cleanText = text.replace(/\\\(/g, '').replace(/\\\)/g, '');

        while (i < cleanText.length) {
            let char = cleanText[i];

            // 匹配反斜杠开头的指令
            if (char === '\\') {
                let cmd = '';
                i++;
                while (i < cleanText.length && /[a-zA-Z]/.test(cleanText[i])) {
                    cmd += cleanText[i];
                    i++;
                }
                if (cmd) tokens.push({ type: 'command', value: cmd });
                continue;
            }

            // 匹配花括号
            if (char === '{') {
                tokens.push({ type: 'brace_open' });
                i++;
                continue;
            }
            if (char === '}') {
                tokens.push({ type: 'brace_close' });
                i++;
                continue;
            }

            // 匹配普通文本（包括参数如数字、颜色代码或汉字）
            let str = '';
            while (i < cleanText.length && cleanText[i] !== '\\' && cleanText[i] !== '{' && cleanText[i] !== '}') {
                str += cleanText[i];
                i++;
            }
            if (str.length > 0) {
                tokens.push({ type: 'text', value: str });
            }
        }
        return tokens;
    }

    // ============================================================
    //  2. 语法分析器 (Parser)：把线性的 Token 变成嵌套的抽象语法树 (AST)
    // ============================================================
    function parse(tokens) {
        let current = 0;

        function walk() {
            if (current >= tokens.length) return null;
            let token = tokens[current];

            if (token.type === 'text') {
                current++;
                return { type: 'Text', value: token.value };
            }

            if (token.type === 'command') {
                let node = { type: 'Command', name: token.value, args: [] };
                current++;

                // 贪婪匹配后面紧跟的所有被 {} 包裹的参数块
                while (current < tokens.length && tokens[current].type === 'brace_open') {
                    current++; // 跳过 '{'
                    let argChildren = [];
                    while (current < tokens.length && tokens[current].type !== 'brace_close') {
                        let child = walk();
                        if (child) argChildren.push(child);
                    }
                    if (current < tokens.length && tokens[current].type === 'brace_close') {
                        current++; // 跳过 '}'
                    }
                    node.args.push(argChildren);
                }
                return node;
            }

            // 处理异常情况直接当作文本跳过
            current++;
            return { type: 'Text', value: token.value || '' };
        }

        let ast = [];
        while (current < tokens.length) {
            let node = walk();
            if (node) ast.push(node);
        }
        return ast;
    }

    // ============================================================
    //  3. 渲染器 (Renderer)：遍历 AST 生成 HTML 字符串
    // ============================================================
    function renderAST(nodes) {
        if (!nodes) return '';
        let html = '';

        for (let node of nodes) {
            if (node.type === 'Text') {
                html += node.value;
            } else if (node.type === 'Command') {
                // 递归渲染子节点
                let args = node.args.map(arg => renderAST(arg));
                let styles = [];
                let content = '';

                // 指令分发逻辑
                if (node.name === 'rotatebox' && args.length >= 2) {
                    styles.push(`transform: rotate(${args[0]}deg)`, 'display: inline-block', 'margin: 0 0.05em');
                    content = args[1];
                } else if (node.name === 'scalebox' && args.length >= 2) {
                    styles.push(`font-size: ${args[0]}em`, 'display: inline-block');
                    content = args[1];
                } else if (node.name === 'textcolor' && args.length >= 2) {
                    styles.push(`color: ${args[0]}`);
                    content = args[1];
                } else if (node.name === 'colorbox' && args.length >= 2) {
                    styles.push(`background-color: ${args[0]}`);
                    content = args[1];
                } else {
                    // 如果遇到未定义的指令，直接输出里面的内容，防止吞字
                    content = args.join('');
                }

                if (styles.length > 0) {
                    html += `<span style="${styles.join('; ')}">${content}</span>`;
                } else {
                    html += content;
                }
            }
        }
        return html;
    }

    // ============================================================
    //  4. HTML 可视化片段检测与渲染（3.0 新增）
    // ============================================================

    /**
     * 判断一段文本是否是 "HTML 可视化片段"（而非全页面框架或普通代码）
     * 满足以下条件：
     *   1. 以 < 开头
     *   2. 不含 DOCTYPE / html / head / body 等全页面标签
     *   3. 包含 div/span/section 等结构性 HTML 标签
     *   4. 优先匹配有内联样式或 flex 布局的片段
     */
    function isVisualHTMLBlock(text) {
        const trimmed = text.trim();
        if (!trimmed) return false;
        if (!/^\s*</.test(trimmed)) return false;
        if (/<!DOCTYPE|<html[\s>]|<head[\s>]|<body[\s>]/i.test(trimmed)) return false;
        if (!/<(div|span|section|article|table|ul|ol|dl|nav|header|footer|main|aside|figure|details|summary)[\s>]/i.test(trimmed)) return false;
        return true;
    }

    // HTML闭合检测（栈匹配），排除 void elements 并校验标签嵌套
    function isHTMLClosed(html) {
        const clean = html.replace(/<[^>]*\/>/g, '').replace(/<!--[\s\S]*?-->/g, '');
        const voidElements = /^(br|hr|img|input|meta|link|area|base|col|embed|param|source|track|wbr)$/i;
        const stack = [];
        const tagRegex = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>/g;
        let match;
        while ((match = tagRegex.exec(clean)) !== null) {
            const isClose = match[1] === '/';
            const tagName = match[2].toLowerCase();
            if (voidElements.test(tagName)) continue;
            if (isClose) {
                if (stack.length === 0 || stack[stack.length - 1] !== tagName) return false;
                stack.pop();
            } else {
                stack.push(tagName);
            }
        }
        return stack.length === 0;
    }

    // 非破坏性渲染：隐藏原节点，在后面插入渲染容器
    function nonDestructiveRender(target, content) {
        // 查找是否已经存在我们插入的渲染容器
        let container = target.nextElementSibling;
        if (!container || container.dataset.vizSource !== 'rendered') {
            container = document.createElement('div');
            container.style.cssText = 'display: contents;';
            container.dataset.vizSource = 'rendered';
            target.insertAdjacentElement('afterend', container);
        }
        
        // 隐藏原始节点，但不从DOM树中移除，保护前端框架的虚拟DOM
        target.style.display = 'none';
        target.dataset.vizProcessed = 'true';
        
        // 更新渲染容器的内容
        container.innerHTML = content;
        return container;
    }

    /**
     * 判断代码块的语言标注是否是 HTML
     */
    function isHTMLLanguageBlock(codeEl) {
        const cls = codeEl.className || '';
        if (/language-html|lang-html/i.test(cls)) return true;
        const pre = codeEl.closest('pre');
        if (pre) {
            const preCls = pre.className || '';
            if (/language-html|lang-html/i.test(preCls)) return true;
        }
        return false;
    }

    function processCodeBlock(codeEl) {
        if (codeEl.dataset.vizProcessed === 'true') return false;
        const content = codeEl.textContent || codeEl.innerText || '';
        if (!content.trim()) return false;

        const byLanguage = isHTMLLanguageBlock(codeEl);
        const byContent = isVisualHTMLBlock(content);
        if (!byLanguage && !byContent) return false;

        // 流式输出关键：如果HTML没有闭合，说明还没输出完，跳过本次渲染
        if (!isHTMLClosed(content)) {
            return false; 
        }

        try {
            const pre = codeEl.closest('pre');
            const target = pre || codeEl;
            const container = nonDestructiveRender(target, content);
            // 对新插入的渲染容器递归处理（解析其中的 LaTeX 等）
            if (container) walkDOM(container);
            return true;
        } catch (e) {
            console.debug('[扭扭字插件] HTML 可视化块渲染失败:', e.message);
        }
        return false;
    }

    /**
     * 批量处理容器内所有代码块
     */
    function processAllCodeBlocks(root) {
        // 用 querySelectorAll 收集所有 code 元素（静态 NodeList，不受 DOM 变化影响）
        const codeElements = root.querySelectorAll ? root.querySelectorAll('code') : [];
        const toProcess = [];

        for (const codeEl of codeElements) {
            if (codeEl.dataset.vizProcessed === 'true') continue;
            if (codeEl.closest('script, style, textarea, input, [data-viz-source]')) continue;
            toProcess.push(codeEl);
        }

        for (const codeEl of toProcess) {
            processCodeBlock(codeEl);
        }
    }

    // ============================================================
    //  4.5 DeepSeek ds-markdown-html 处理（3.0 新增）
    //  检测 <span class="ds-markdown-html"> 并将其中转义的 HTML 还原渲染
    // ============================================================

    /**
     * 还原 HTML 实体编码
     */
    function unescapeHTML(text) {
        return text
            .replace(/&lt;/g, '<')
            .replace(/&gt;/g, '>')
            .replace(/&amp;/g, '&')
            .replace(/&quot;/g, '"')
            .replace(/&#39;/g, "'");
    }

    /**
     * 处理 DeepSeek 的 ds-markdown-html span：
     * 收集所有相邻的同类 span 合并后统一还原渲染（流式输出可能拆成多个 span）
     */
    function processDSMarkdownHTMLSpan(spanEl) {
        const cls = spanEl.className || '';
        if (!/ds-markdown-html/i.test(cls)) return false;
        if (spanEl.dataset.vizProcessed === 'true') return false;

        // 收集所有相邻的 ds-markdown-html span，合并处理
        const group = [spanEl];
        
        // 向前收集
        let prev = spanEl.previousSibling;
        while (prev) {
            if (prev.nodeType === 1 && prev.tagName === 'SPAN' && /ds-markdown-html/i.test(prev.className || '')) {
                if (prev.dataset.vizProcessed !== 'true') {
                    group.unshift(prev);
                }
                prev = prev.previousSibling;
            } else {
                break;
            }
        }
        
        // 向后收集
        let next = spanEl.nextSibling;
        while (next) {
            if (next.nodeType === 1 && next.tagName === 'SPAN' && /ds-markdown-html/i.test(next.className || '')) {
                if (next.dataset.vizProcessed !== 'true') {
                    group.push(next);
                }
                next = next.nextSibling;
            } else {
                break;
            }
        }

        // 拼接所有span的内容
        let combinedRaw = '';
        for (const s of group) {
            combinedRaw += s.textContent || s.innerText || '';
        }
        
        if (!combinedRaw.trim()) return false;

        const unescaped = unescapeHTML(combinedRaw);
        if (!isVisualHTMLBlock(unescaped)) return false;
        
        // 等待HTML闭合
        if (!isHTMLClosed(unescaped)) {
            return false;
        }

        try {
            const container = document.createElement('div');
            container.style.cssText = 'display: contents;';
            container.innerHTML = unescaped;
            container.dataset.vizSource = 'rendered';

            // 隐藏组内所有span，保护前端虚拟DOM
            for (const s of group) {
                s.style.display = 'none';
                s.dataset.vizProcessed = 'true';
            }

            // 在最后一个span后面插入渲染容器
            const lastSpan = group[group.length - 1];
            lastSpan.insertAdjacentElement('afterend', container);

            // 对新容器递归处理（解析其中的 LaTeX 等）
            walkDOM(container);
            return true;
        } catch (e) {
            console.debug('[扭扭字插件] ds-markdown-html 渲染失败:', e.message);
            return false;
        }
    }

    // ============================================================
    //  5. 转义 HTML 还原（3.0 新增）
    //  检测文本节点中 &lt;div ... &gt; 等转义标签并还原为真实 DOM
    // ============================================================
    function processEscapedHTMLInText(node) {
        const text = node.nodeValue;
        // 快速检测：必须包含转义的 HTML 标签起始符
        if (!/&lt;\s*(div|span|section|article|table|nav|header|footer|details|summary)\b/i.test(text)) return false;

        // 尝试还原转义
        let unescaped = unescapeHTML(text);

        // 如果还原后和原文一样，说明没有实际变化
        if (unescaped === text) return false;

        // 检查还原后的内容是否是 HTML 可视化片段
        if (!isVisualHTMLBlock(unescaped)) return false;

        try {
            const container = document.createElement('span');
            container.innerHTML = unescaped;
            node.parentNode.replaceChild(container, node);
            return true;
        } catch (e) {
            console.debug('[扭扭字插件] 转义 HTML 还原失败:', e.message);
            return false;
        }
    }

    // ============================================================
    //  6. DOM 操作与监听逻辑
    // ============================================================

    // 用于追踪已处理的文本节点，避免 MutationObserver 触发无限循环
    const processedNodes = new WeakSet();

    function processTextNode(node) {
        // 跳过已处理过的节点
        if (processedNodes.has(node)) return;

        const text = node.nodeValue;
        // 先快速判断是否包含目标指令特征，避免无谓的性能损耗
        if (text.includes('\\rotatebox') || text.includes('\\textcolor') ||
            text.includes('\\scalebox') || text.includes('\\colorbox')) {
            const tokens = tokenize(text);
            const ast = parse(tokens);
            const html = renderAST(ast);

            // 如果生成的 html 相比原文本发生了实质性变化，则替换节点
            if (html !== text && html !== text.replace(/\\\(/g, '').replace(/\\\)/g, '')) {
                const span = document.createElement('span');
                span.innerHTML = html;
                node.parentNode.replaceChild(span, node);
                return; // 节点已被替换，不需要再检查转义
            }
        }

        // 3.0 新增：检查转义 HTML
        processEscapedHTMLInText(node);

        // 标记为已处理
        processedNodes.add(node);
    }

    function walkDOM(node) {
        // 跳过不需要处理的标签
        if (node.nodeType === 1) { // 元素节点
            const tagName = node.tagName;
            if (tagName === 'TEXTAREA' || tagName === 'INPUT' ||
                tagName === 'SCRIPT' || tagName === 'STYLE' ||
                tagName === 'NOSCRIPT' || tagName === 'IFRAME') {
                return;
            }

            // 3.0：优先处理 DeepSeek ds-markdown-html span
            if (tagName === 'SPAN') {
                const cls = node.className || '';
                if (/ds-markdown-html/i.test(cls)) {
                    processDSMarkdownHTMLSpan(node);
                    return; // 已被替换为真实 DOM，不再递归
                }
            }

            // 跳过已经被本脚本渲染过的容器
            if (node.dataset && node.dataset.vizSource === 'rendered') {
                return;
            }

            // 3.0：先处理该元素内的所有代码块（在遍历文本节点之前）
            processAllCodeBlocks(node);
        }

        if (node.nodeType === 3) { // 文本节点
            processTextNode(node);
        } else if (node.childNodes) {
            // 遍历子节点（使用快照避免 DOM 变化导致的问题）
            const children = Array.from(node.childNodes);
            for (let i = 0; i < children.length; i++) {
                // 如果节点在遍历过程中已被移除（如被替换），跳过
                if (children[i].parentNode) {
                    walkDOM(children[i]);
                }
            }
        }
    }

    // ============================================================
    //  7. 防抖与 MutationObserver（流式输出适配）
    // ============================================================

    // ============ 累积节点批量 flush 机制（替代覆盖式 debounce） ============
    const pendingRoots = new Set();
    let flushTimer = null;

    function scheduleProcess(node) {
        if (!node) return;
        // textNode → 向上找父元素
        let target = node;
        if (node.nodeType === 3) target = node.parentNode;
        if (!target || target.nodeType !== 1) return;

        // 向上找到 ds-markdown-html 祖先（如果有的话）
        const dsHtmlAncestor = target.closest && target.closest('span.ds-markdown-html');
        if (dsHtmlAncestor) target = dsHtmlAncestor;

        pendingRoots.add(target);

        if (flushTimer) return;
        flushTimer = setTimeout(() => {
            flushTimer = null;
            const roots = Array.from(pendingRoots);
            pendingRoots.clear();
            for (const r of roots) {
                if (r.isConnected) walkDOM(r);
            }
        }, 150);
    }

    // 修改后的 MutationObserver（累积+批量flush，修复流式输出漏节点问题）
    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            // 处理新增节点
            mutation.addedNodes.forEach(scheduleProcess);
            // 处理文本内容变化（characterData）
            if (mutation.type === 'characterData') {
                scheduleProcess(mutation.target);
            }
            // 处理子节点替换（childList）——当框架直接替换 ds-markdown-html 子节点时
            if (mutation.type === 'childList' && mutation.target.nodeType === 1) {
                if (mutation.target.tagName === 'SPAN' && /ds-markdown-html/i.test(mutation.target.className)) {
                    scheduleProcess(mutation.target);
                }
            }
        }
    });

    // ============================================================
    //  8. 启动
    // ============================================================
    setTimeout(() => {
        walkDOM(document.body);
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true
        });
    }, 1500);

})();
