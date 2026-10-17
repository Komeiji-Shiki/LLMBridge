#!/usr/bin/env python3
"""会话缓存库维护工具。

用法：
    python scripts/compact_conversations.py --stats
    python scripts/compact_conversations.py --copy data/conversations.compacted.db
    python scripts/compact_conversations.py --in-place

--copy 使用 VACUUM INTO 在线生成压缩副本，不阻塞现有写入；替换正式库前
需要先停止服务。--in-place 会短暂独占数据库写入，适合在服务停止时执行。
两种方式都会把副本/正式库切换到 incremental auto-vacuum，之后后台清理
任务可以持续回收空洞。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.conversation_store import ConversationStore  # noqa: E402


def print_stats(label: str, store: ConversationStore) -> None:
    stats = store.storage_stats()
    auto_vacuum = {0: 'NONE', 1: 'FULL', 2: 'INCREMENTAL'}.get(
        stats['auto_vacuum'], str(stats['auto_vacuum'])
    )
    print(
        f"{label}: file={stats['file_bytes'] / 1048576:.1f}MB "
        f"used={stats['used_bytes'] / 1048576:.1f}MB "
        f"free={stats['free_bytes'] / 1048576:.1f}MB "
        f"auto_vacuum={auto_vacuum}"
    )


def verify_database(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=30)
    try:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        conversations = connection.execute(
            'SELECT COUNT(*) FROM conversations'
        ).fetchone()[0]
        artifacts = connection.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
        auto_vacuum = connection.execute('PRAGMA auto_vacuum').fetchone()[0]
        free_pages = connection.execute('PRAGMA freelist_count').fetchone()[0]
        print(
            f"verify: integrity={integrity} conversations={conversations} "
            f"artifacts={artifacts} auto_vacuum={auto_vacuum} "
            f"free_pages={free_pages}"
        )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/conversations.db')
    parser.add_argument('--stats', action='store_true', help='只看数据库占用与空洞')
    parser.add_argument('--copy', metavar='TARGET', help='在线生成压缩副本')
    parser.add_argument('--in-place', action='store_true', help='原地 VACUUM（独占写入）')
    parser.add_argument('--verify', metavar='TARGET', help='校验指定数据库')
    args = parser.parse_args()

    if args.verify:
        verify_database(Path(args.verify))
        return 0

    store = ConversationStore(args.db)
    print_stats('before', store)

    if args.copy:
        target = store.compact_copy(args.copy)
        print_stats('copy', ConversationStore(target))
        verify_database(target)
    elif args.in_place:
        store.compact()
        print_stats('after', store)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
