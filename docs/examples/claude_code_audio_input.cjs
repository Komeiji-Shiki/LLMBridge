'use strict';
// 在 CLI 已有的 Node/Bun 进程中流式上传；插件只传路径，工具结果只保留文件引用。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');

function createAudioInput({ env = process.env, fetch = globalThis.fetch.bind(globalThis) } = {}) {
  function gateway() {
    const base = new URL(env.ANTHROPIC_BASE_URL || '');
    base.pathname = base.pathname.replace(/\/$/, '').replace(/\/v1$/, '') + '/v1/';
    return base;
  }
  function headers() {
    const key = env.ANTHROPIC_AUTH_TOKEN || env.ANTHROPIC_API_KEY;
    return key ? { Authorization: 'Bearer ' + key } : {};
  }
  async function read({ file_path: filePath, mime_type: mimeType }, signal) {
    if (typeof filePath !== 'string' || !path.isAbsolute(filePath) || !/^audio\//.test(mimeType || '')) {
      throw new Error('请提供音频文件的绝对路径和 MIME 类型。');
    }
    const base = gateway();
    const capabilitiesURL = new URL('bridge/audio/capabilities', base);
    capabilitiesURL.searchParams.set('model', env.ANTHROPIC_MODEL || '');
    let capabilities;
    const capabilityResponse = await fetch(capabilitiesURL, { headers: headers(), signal });
    if (!capabilityResponse.ok) throw new Error('当前网关未提供音频文件接口，请先更新并重启桥接服务。');
    capabilities = await capabilityResponse.json();
    if (capabilities.audio_files_version !== 1 || !capabilities.supported) {
      throw new Error('当前模型或网关尚未启用音频文件输入。');
    }
    // 能力协商成功后才接触原文件；旧网关不会收到任何音频字节。
    const stat = await fs.promises.stat(filePath);
    if (!stat.isFile() || !stat.size) throw new Error('音频路径必须是非空的普通文件。');
    if (stat.size > capabilities.max_upload_bytes) throw new Error('音频超过网关配置的上传大小上限。');
    if (!capabilities.files_api && Math.ceil(stat.size / 3) * 4 > capabilities.inline_audio_bytes) {
      throw new Error('音频超过当前上游的内联限制，请选择支持 Files API 的端点，或明确选择压缩、截取或分段。');
    }
    const uploadURL = new URL(capabilities.upload_path, base);
    if (uploadURL.origin !== base.origin) throw new Error('网关返回的音频上传地址不属于当前服务。');
    const stream = fs.createReadStream(filePath);
    try {
      const response = await fetch(uploadURL, {
        method: 'POST', signal, duplex: 'half', body: stream,
        headers: { ...headers(), 'Content-Type': mimeType, 'Content-Length': String(stat.size),
          'X-Audio-Filename': encodeURIComponent(path.basename(filePath)) },
      });
      if (!response.ok) throw new Error('上传音频失败：' + (await response.text()).slice(0, 500));
      const result = await response.json();
      if (!/^[a-f0-9]{32}$/.test(result.file_id || '') ||
          result.reference !== '<graywill_audio_ref id="' + result.file_id + '"/>') {
        throw new Error('网关没有返回有效的音频文件引用。');
      }
      return result;
    } finally { stream.destroy(); }
  }
  return { read };
}

async function start() {
  if (process.env.GRAYWILL_THIRD_PARTY !== '1') return null;
  const identity = Symbol.for('graywill.audio-input');
  if (globalThis[identity]) return globalThis[identity];
  const token = crypto.randomBytes(24).toString('hex');
  const audio = createAudioInput();
  const server = http.createServer(async (request, response) => {
    if (request.method !== 'POST' || request.url !== '/read' || request.headers['x-graywill-token'] !== token) {
      response.writeHead(403).end(); return;
    }
    const abort = new AbortController();
    response.on('close', () => abort.abort());
    try {
      let body = '';
      for await (const chunk of request) {
        body += chunk.toString('utf8');
        if (body.length > 16384) throw new Error('音频读取参数过长。');
      }
      const result = await audio.read(JSON.parse(body), abort.signal);
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result));
    } catch (error) {
      if (!response.destroyed) response.writeHead(400, { 'Content-Type': 'application/json' }).end(JSON.stringify({ error: error.message }));
    }
  });
  const ready = new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      process.env.GRAYWILL_AUDIO_URL = 'http://127.0.0.1:' + server.address().port + '/read';
      process.env.GRAYWILL_AUDIO_TOKEN = token;
      server.unref();
      resolve(server);
    });
  });
  globalThis[identity] = ready;
  return ready;
}

module.exports = { createAudioInput, start };
