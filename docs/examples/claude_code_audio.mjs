// Claude Code Read 的音频适配。与 graywill-profile 的 hooks/profile.mjs 一起加载。
// Read 的结果结构没有音频类型，专用文本封装由 LLMBridge 入口还原为原始音频。
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
      '\n\n音频读取：也可以读取本地 WAV、MP3、AIFF、AAC、OGG、FLAC、M4A、Opus、WebM 音频，供支持音频的上游模型直接分析。使用完整文件路径，不传 offset、limit 或 pages。插件文件读取接口限制单个文件为 4 MiB。' };
  });

  on('tool.call', { tool: 'Read' }, async ($, event, next) => {
    const extension = /\.([^.\\/]+)$/.exec(event.file_path || '')?.[1]?.toLowerCase();
    const mimeType = audioMimeTypes[extension];
    if (!mimeType || await $.env.get('GRAYWILL_THIRD_PARTY') !== '1') return next(event);
    // 复用引擎的 Read 权限判断；需要询问或被拒绝时交回原生流程。
    const permission = await $.tool.check({ tool: 'Read', input: { file_path: event.file_path } });
    if (permission.decision !== 'allow') return next(event);
    if (event.offset !== undefined || event.limit !== undefined || event.pages !== undefined) {
      return { deny: '音频需要整文件读取，请去掉 offset、limit 和 pages。' };
    }
    try {
      const { base64 } = await $.fs.read(event.file_path, { as: 'bytes' });
      return { result: { type: 'text', file: {
        filePath: event.file_path,
        content: '<graywill_audio mime_type="' + mimeType + '">' + base64 + '</graywill_audio>',
        numLines: 1, startLine: 1, totalLines: 1,
      } } };
    } catch (error) {
      return { deny: '无法读取音频文件：' + error.message };
    }
  });
}
