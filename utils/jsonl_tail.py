"""从文件末尾按块读取 JSONL，内存只保存当前块与尚未完整的一行。"""


def reverse_lines(path, block_size=64 * 1024):
    with path.open('rb') as stream:
        stream.seek(0, 2)
        position = stream.tell()
        fragments = []
        while position:
            size = min(position, block_size)
            position -= size
            stream.seek(position)
            lines = stream.read(size).split(b'\n')
            if len(lines) == 1:
                fragments.append(lines[0])
                continue
            tail = lines.pop() + b''.join(reversed(fragments))
            fragments = [lines.pop(0)]
            for line in (tail, *reversed(lines)):
                if line.strip():
                    yield line.removesuffix(b'\r')
        head = b''.join(reversed(fragments))
        if head.strip():
            yield head.removesuffix(b'\r')
