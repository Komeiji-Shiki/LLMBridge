"""巡检上游官方 schema 里的类型枚举，列出代码里从未提到的新类型。

“哪些新功能还没兼容”不用再靠人工翻文档。用法：

    python scripts/check_upstream_schema.py

只读，不改任何文件；有未覆盖的类型时退出码为 1，可以放进定时任务。
"""
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ('converters', 'services', 'routes', 'utils')

# (名称, schema 地址, 提取类型值的正则)
SOURCES = (
    ('Gemini Interactions', 'https://ai.google.dev/api/interactions-api.md.txt', r'Value: `([a-z][a-z0-9_.]+)`'),
    ('Claude Messages', 'https://platform.claude.com/docs/en/api/messages/create.md', r'- `type: "([a-z][a-z0-9_]+)"`'),
    ('OpenAI Responses', 'https://developers.openai.com/api/reference/resources/responses/methods/create.md',
     r'- `type: "([a-z][a-z0-9_.]+)"`'),
)


def extract_types(text: str, pattern: str) -> set:
    return set(re.findall(pattern, text))


def known_source() -> str:
    parts = []
    for directory in SOURCE_DIRS:
        for path in (ROOT / directory).rglob('*.py'):
            parts.append(path.read_text(encoding='utf-8', errors='replace'))
    return '\n'.join(parts)


def uncovered(types: set, source: str) -> list:
    """代码里没有以字符串形式出现过的类型。子串也算出现：带日期后缀的工具按前缀匹配。"""
    return sorted(kind for kind in types if kind not in source)


def main() -> int:
    source = known_source()
    missing_total = 0
    for name, url, pattern in SOURCES:
        try:
            # 部分文档站拒绝 urllib 的默认 User-Agent
            request = urllib.request.Request(url, headers={'User-Agent': 'curl/8.7.1'})
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode('utf-8', errors='replace')
        except OSError as error:
            print(f'[{name}] 读取失败：{error}')
            missing_total += 1
            continue
        types = extract_types(text, pattern)
        missing = uncovered(types, source)
        missing_total += len(missing)
        print(f'[{name}] 共 {len(types)} 个类型，未覆盖 {len(missing)} 个')
        for kind in missing:
            print(f'  - {kind}')
    return 1 if missing_total else 0


if __name__ == '__main__':
    sys.exit(main())
