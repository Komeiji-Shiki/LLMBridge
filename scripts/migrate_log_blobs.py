"""给去重存储上线前写下的旧日志补做块拆分，可重复运行。网关运行时也可以执行。

    python scripts/migrate_log_blobs.py
    python scripts/migrate_log_blobs.py --root logs
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.log_blob_migration import migrate
from core.log_pack import PackBusy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='logs')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    try:
        stats = migrate(args.root)
    except PackBusy as exc:
        print(f'{exc}，稍后再运行')
        return 1
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
