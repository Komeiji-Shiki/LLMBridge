"""删除日志里指定任务带的图片：消息里含有给定文本的请求，图片换成占位说明，然后压缩块库。

    python scripts/drop_log_images.py --contains "批量识图任务标记" --dry-run
    python scripts/drop_log_images.py --contains "批量识图任务标记"

同一张图还被别的请求引用时保留。需要先用 migrate_log_blobs.py 把旧日志转成块存储。
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.log_blob_maintenance import drop_images


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='logs')
    parser.add_argument('--contains', required=True, help='消息里含有这段文本的请求')
    parser.add_argument('--dry-run', action='store_true', help='只统计，不修改')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    print(json.dumps(drop_images(args.root, args.contains, args.dry_run), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
