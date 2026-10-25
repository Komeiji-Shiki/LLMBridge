// 音频通过 CLI 的本地传输模块上传，Read 结果只保存短文件引用。
const audioMimeTypes = {
  wav: 'audio/wav', mp3: 'audio/mpeg', mpeg: 'audio/mpeg',
  aif: 'audio/aiff', aiff: 'audio/aiff', aac: 'audio/aac',
  ogg: 'audio/ogg', oga: 'audio/ogg', flac: 'audio/flac',
  m4a: 'audio/mp4', opus: 'audio/opus', webm: 'audio/webm',
};

export function register(on) {
  on('tool.describe', { tool: 'Read' }, async ($, event, next) => {
    const result = await next(event);
    if (await $.env.get('GRAYWILL_THIRD_PARTY') !== '1') return result;
    return { ...result, description: result.description +
      '\n\n音频读取：可通过音频文件的绝对路径读取 WAV、MP3、AIFF、AAC、OGG、FLAC、M4A、Opus、WebM。文件会先经过网关能力检查，再通过二进制上传供模型分析，工具历史保留文件引用。音频按完整文件读取，不传 offset、limit 或 pages。大小限制以当前网关与上游能力为准。' };
  });

  on('tool.call', { tool: 'Read' }, async ($, event, next) => {
    const extension = /\.([^.\\/]+)$/.exec(event.file_path || '')?.[1]?.toLowerCase();
    const mimeType = audioMimeTypes[extension];
    if (!mimeType || await $.env.get('GRAYWILL_THIRD_PARTY') !== '1') return next(event);
    const permission = await $.tool.check({ tool: 'Read', input: { file_path: event.file_path } });
    if (permission.decision !== 'allow') return { deny: permission.reason || '请先授权这个文件的 Read 权限，再读取音频。' };
    if (event.offset !== undefined || event.limit !== undefined || event.pages !== undefined) {
      return { deny: '音频需要整文件读取，请去掉 offset、limit 和 pages。' };
    }
    const url = await $.env.get('GRAYWILL_AUDIO_URL');
    const token = await $.env.get('GRAYWILL_AUDIO_TOKEN');
    if (!url || !token) return { deny: '音频传输模块尚未加载，请新建或恢复 Claude Code 会话后再试。' };
    try {
      const response = await $.http.fetch(url, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Graywill-Token': token },
        body: JSON.stringify({ file_path: event.file_path, mime_type: mimeType }),
      });
      const result = JSON.parse(response.text);
      if (!response.ok) return { deny: result.error || '音频上传失败。' };
      return { result: { type: 'text', file: {
        filePath: event.file_path,
        content: '音频文件：' + result.filename + '\n大小：' + result.size + ' 字节\n' + result.reference,
        numLines: 3, startLine: 1, totalLines: 3,
      } } };
    } catch (error) {
      return { deny: '无法读取音频文件：' + error.message };
    }
  });
}
