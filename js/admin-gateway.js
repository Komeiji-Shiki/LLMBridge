/* Native request workspace; values from providers are always rendered as text. */
(() => {
    'use strict';
    const nav = document.createElement('li');
    nav.className = 'nav-item';
    nav.dataset.page = 'gateway-workspace';
    nav.textContent = '🧪 能力与 Playground';
    document.querySelector('.nav-menu').append(nav);
    const page = document.createElement('section');
    page.id = 'gateway-workspace';
    page.className = 'page';
    page.innerHTML = `<div class="page-header"><h2>能力与请求 Playground</h2><p>检查官方协议配置，发送原生请求，查看完整工具事件与阶段耗时。</p></div>
      <div class="card gateway-controls">
        <label>模型与端点 <select id="gw-model" class="form-select"></select></label>
        <button type="button" class="btn" id="gw-refresh">刷新能力</button>
        <a id="gw-docs" target="_blank" rel="noopener noreferrer">官方文档</a>
        <p id="gw-diagnosis" role="status"></p><div id="gw-tools"></div>
        <label>会话 ID（可选，跨轮次沿用）<input id="gw-session" class="form-input" maxlength="128"></label>
        <label><input type="checkbox" id="gw-stream" checked> 流式响应</label>
        <label for="gw-body">原生 JSON 请求，可编辑所有供应商字段</label>
        <textarea id="gw-body" class="form-textarea gateway-json" spellcheck="false" rows="14"></textarea>
        <div class="gateway-actions"><button class="btn" id="gw-template">恢复协议示例</button><button class="btn" id="gw-insert-tools">把选中工具写入请求</button><button class="btn btn-primary" id="gw-run">发送请求</button><button class="btn" id="gw-cancel" disabled>中断请求</button><button class="btn" id="gw-download" disabled>下载完整响应</button></div>
        <p id="gw-state" role="status">发送会调用真实上游，并按供应商规则产生费用。</p>
        <div id="gw-timing" class="gateway-metrics"></div>
        <pre id="gw-output" class="gateway-output" aria-label="原始响应"></pre>
      </div>
      <div class="card gateway-controls"><h3>调用方用量与价格分析</h3>
        <div class="gateway-actions"><label>开始日期 <input type="date" id="gw-from"></label><label>结束日期 <input type="date" id="gw-to"></label><button class="btn" id="gw-usage">读取用量</button><button class="btn" id="gw-prices">按当前价格分析</button></div>
        <p>历史金额保持调用时的价格；不同币种分别展示。旧记录没有调用方信息时显示为未归属。</p><div id="gw-analysis" class="table-container"></div>
      </div>
      <div class="card gateway-controls"><h3>Tokenizer 来源信任</h3><p>普通 tokenizer 无需开启。只有需要执行自定义远程代码的来源才逐项加入；每行一个完整仓库名或本地路径。</p>
        <textarea id="gw-trust" class="form-textarea" rows="3" aria-label="已信任的 tokenizer 来源"></textarea><button class="btn" id="gw-save-trust">保存来源列表</button><p id="gw-trust-state" role="status"></p>
      </div>`;
    document.querySelector('.main-content').append(page);
    const el = id => document.getElementById('gw-' + id);
    let models = [], controller = null, raw = '', renderFrame = 0, loadVersion = 0, analysisVersion = 0;
    const selected = () => models[Number(el('model').value)];
    async function json(url, options) {
        const response = await fetch(url, options);
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result));
        return result;
    }
    function showCapabilities() {
        const model = selected();
        el('tools').replaceChildren();
        if (!model) { el('diagnosis').textContent = '没有可用模型'; return; }
        el('diagnosis').textContent = `${model.provider} · ${model.protocol} · 文档与配置诊断（尚未实测）\n` + model.issues.map(issue => issue.message).join('\n');
        el('docs').hidden = !model.docs;
        if (model.docs) el('docs').href = model.docs;
        for (const name of model.native_tools) {
            const label = document.createElement('label'), input = document.createElement('input');
            input.type = 'checkbox'; input.value = name; input.checked = model.configured_tools.includes(name);
            label.append(input, ' ' + name); el('tools').append(label);
        }
    }
    function template() {
        const protocol = selected()?.protocol;
        const body = protocol === 'gemini' ? {contents: [{role: 'user', parts: [{text: '你好，请介绍你能提供的帮助。'}]}]} :
            protocol === 'interactions' || protocol === 'responses' ? {input: '你好，请介绍你能提供的帮助。'} :
            {messages: [{role: 'user', content: '你好，请介绍你能提供的帮助。'}], ...(protocol === 'anthropic' ? {max_tokens: 1024} : {})};
        el('body').value = JSON.stringify(body, null, 2);
    }
    function insertTools() {
        try {
            const body = JSON.parse(el('body').value), model = selected();
            if (!model) throw new Error('请先选择模型');
            const names = [...el('tools').querySelectorAll('input:checked')].map(input => input.value);
            const mapping = {google_search: 'googleSearch', url_context: 'urlContext', code_execution: 'codeExecution', google_maps: 'googleMaps', file_search: 'fileSearch'};
            if (model.protocol === 'chat' && model.provider === 'qwen') body.enable_search = names.includes('web_search');
            else body.tools = names.map(type => model.protocol === 'gemini' ? {[mapping[type]]: {}} : {type,
                ...(type === 'code_interpreter' && model.provider === 'openai' ? {container: {type: 'auto'}} : {}),
                ...(type === 'shell' && model.provider === 'openai' ? {environment: {type: 'container_auto'}} : {})});
            el('body').value = JSON.stringify(body, null, 2);
            el('state').textContent = '已写入工具字段；需要文件库、MCP 地址等参数的工具，请在 JSON 中填写。';
        } catch (error) { el('state').textContent = error.message; }
    }
    function timing(data) {
        el('timing').replaceChildren();
        const labels = {prepare_ms: '准备', upstream_wait_ms: '等待上游首字节', first_business_ms: '首业务事件', output_ms: '输出阶段', total_ms: '总耗时'};
        for (const [key, label] of Object.entries(labels)) {
            const item = document.createElement('span');
            item.textContent = `${label}：${data[key] == null ? '无数据' : (data[key] / 1000).toFixed(3) + ' 秒'}`;
            el('timing').append(item);
        }
        if (data.attempts?.length) {
            const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
            summary.textContent = `${data.attempts.length} 次上游尝试`; pre.textContent = JSON.stringify(data.attempts, null, 2); details.append(summary, pre); el('timing').append(details);
        }
    }
    function renderOutput(force = false) {
        const paint = () => {
            renderFrame = 0;
            el('output').textContent = raw.length > 200000 ? '页面展示末尾 200,000 字符，完整内容可下载。\n' + raw.slice(-200000) : raw;
        };
        if (force) { cancelAnimationFrame(renderFrame); paint(); }
        else if (!renderFrame) renderFrame = requestAnimationFrame(paint);
    }
    async function run() {
        if (controller) return;
        let body;
        try { body = JSON.parse(el('body').value); if (!body || Array.isArray(body) || typeof body !== 'object') throw new Error('请求必须为 JSON 对象'); }
        catch (error) { el('state').textContent = error.message; return; }
        const model = selected(); if (!model) return;
        controller = new AbortController(); raw = ''; el('output').textContent = ''; el('timing').replaceChildren();
        el('run').disabled = true; el('model').disabled = true; el('cancel').disabled = false; el('download').disabled = true;
        el('state').textContent = '正在请求上游…';
        let requestId;
        try {
            const response = await fetch('/api/admin/playground/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, signal: controller.signal,
                body: JSON.stringify({model: model.model, endpoint: model.endpoint, request: body, stream: el('stream').checked, session_id: el('session').value.trim()})});
            requestId = response.headers.get('X-Bridge-Request-ID');
            if (response.headers.get('X-Bridge-Session-ID')) el('session').value = response.headers.get('X-Bridge-Session-ID');
            const reader = response.body.getReader(), decoder = new TextDecoder();
            try { while (true) { const {done, value} = await reader.read(); if (done) break; raw += decoder.decode(value, {stream: true}); renderOutput(); } raw += decoder.decode(); }
            finally { reader.releaseLock(); }
            el('state').textContent = response.ok ? '传输结束。请结合终态事件判断成功、内容限制或上游中断。' : `请求失败：HTTP ${response.status}`;
        } catch (error) { el('state').textContent = error.name === 'AbortError' ? '已中断请求，已有输出保留。' : error.message; }
        finally {
            el('cancel').disabled = true; el('download').disabled = !raw; renderOutput(true);
            if (requestId) {
                try {
                    const result = await json('/api/admin/playground/runs/' + encodeURIComponent(requestId));
                    timing(result.timings);
                    const states = {success: '请求成功完成', failed: '上游请求失败或响应中断，错误详情见原始响应', cancelled: '请求已中断，已有输出保留', incomplete: '上游返回未完整完成，具体原因见终态事件'};
                    if (result.outcome?.status) el('state').textContent = states[result.outcome.status] || el('state').textContent;
                    if (result.outcome?.observed_native_tools?.length) el('state').textContent += '；已观察到工具输出：' + result.outcome.observed_native_tools.join(', ');
                } catch (error) { el('state').textContent += '；阶段耗时读取失败：' + error.message; }
            }
            controller = null; el('run').disabled = false; el('model').disabled = false;
        }
    }
    async function analysis(prices) {
        const version = ++analysisVersion;
        try {
            const params = new URLSearchParams();
            if (el('from').value) params.set('start_date', el('from').value);
            if (el('to').value) params.set('end_date', el('to').value);
            const data = await json('/api/admin/' + (prices ? 'current_price_analysis' : 'usage_by_caller') + '?' + params);
            if (version !== analysisVersion) return;
            const columns = prices ? {model: '模型', requests: '请求数', historical_cost: '历史金额', currency: '历史币种', current_estimate: '当前价格估算', current_currency: '当前币种'} :
                {caller_name: '调用方', caller_id: '稳定 ID', requests: '请求数', failures: '失败数', input_tokens: '输入 Token', output_tokens: '输出 Token', total_cost: '历史金额', currency: '币种'};
            const table = document.createElement('table'), head = table.createTHead().insertRow(), tbody = table.createTBody();
            for (const title of Object.values(columns)) { const th = document.createElement('th'); th.textContent = title; head.append(th); }
            for (const item of data.items) { const row = tbody.insertRow(); for (const key of Object.keys(columns)) row.insertCell().textContent = item[key] ?? '无数据'; }
            el('analysis').replaceChildren(data.items.length ? table : document.createTextNode('该范围暂无记录'));
        } catch (error) { if (version === analysisVersion) el('analysis').textContent = error.message; }
    }
    async function load() {
        const version = ++loadVersion, prior = selected();
        try {
            const [catalog, trust] = await Promise.all([json('/api/admin/capabilities'), json('/api/admin/tokenizer_trust')]);
            if (version !== loadVersion || controller) return;
            models = catalog.models; el('model').replaceChildren();
            models.forEach((item, index) => el('model').add(new Option(`${item.model} · 端点 ${item.endpoint + 1} · ${item.protocol}`, String(index))));
            const same = models.findIndex(item => item.model === prior?.model && item.endpoint === prior.endpoint);
            if (same >= 0) el('model').value = String(same);
            showCapabilities(); if (!el('body').value) template();
            if (document.activeElement !== el('trust')) el('trust').value = trust.sources.join('\n');
        } catch (error) { el('diagnosis').textContent = error.message; }
    }
    el('model').addEventListener('change', () => { showCapabilities(); template(); });
    el('refresh').addEventListener('click', load); el('template').addEventListener('click', template);
    el('insert-tools').addEventListener('click', insertTools); el('run').addEventListener('click', run);
    el('cancel').addEventListener('click', () => controller?.abort());
    el('usage').addEventListener('click', () => analysis(false)); el('prices').addEventListener('click', () => analysis(true));
    el('download').addEventListener('click', () => { const url = URL.createObjectURL(new Blob([raw], {type: 'text/plain;charset=utf-8'})), link = document.createElement('a'); link.href = url; link.download = 'gateway-response.txt'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
    el('save-trust').addEventListener('click', async () => {
        try { const result = await json('/api/admin/tokenizer_trust', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({sources: el('trust').value.split('\n').map(value => value.trim()).filter(Boolean)})}); el('trust-state').textContent = result.message; }
        catch (error) { el('trust-state').textContent = error.message; }
    });
    window.gatewayWorkspace = {load};
})();
