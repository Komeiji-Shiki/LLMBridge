"""把旧日志压进可随机读取的分块归档。网关每天自动调用，也可手动运行。

    python scripts/pack_old_logs.py --older-than 14 --keep-source
    python scripts/pack_old_logs.py --older-than 14
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.log_pack import DEFAULT_COMPRESS_AFTER_DAYS, PackBusy, pack_old_logs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='logs')
    parser.add_argument('--older-than', type=int, default=DEFAULT_COMPRESS_AFTER_DAYS)
    parser.add_argument('--keep-source', action='store_true', help='校验通过后仍保留原文件')
    args = parser.parse_args()
    try:
        stats = pack_old_logs(args.root, args.older_than, keep_source=args.keep_source)
    except PackBusy as exc:
        print(exc)
        return 0
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if stats['conflicts'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
