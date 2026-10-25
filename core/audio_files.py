"""音频文件的持久引用；对话只保存文件 ID，文件读取按调用方隔离。"""
import asyncio
import json
import os
from pathlib import Path
import re
import uuid

from fastapi import HTTPException


class AudioFiles:
    def __init__(self, root="data/audio_files"):
        self.root = Path(root)

    def paths(self, file_id):
        if not re.fullmatch(r"[a-f0-9]{32}", file_id):
            raise HTTPException(404, "音频文件不存在")
        return self.root / f"{file_id}.bin", self.root / f"{file_id}.json"

    def metadata(self, file_id, owner_id):
        data_path, metadata_path = self.paths(file_id)
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise HTTPException(404, "音频文件不存在，请重新读取原文件") from None
        if value["owner_id"] != owner_id or not data_path.is_file():
            raise HTTPException(404, "音频文件不存在，请重新读取原文件")
        return value

    def save_metadata(self, file_id, metadata):
        _, target = self.paths(file_id)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    async def upload(self, chunks, owner_id, mime_type, filename, max_bytes):
        file_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        target, _ = self.paths(file_id)
        temporary = target.with_suffix(".partial")
        size = 0
        try:
            with temporary.open("wb") as stream:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "音频超过网关配置的上传大小上限")
                    await asyncio.to_thread(stream.write, chunk)
            if not size:
                raise HTTPException(400, "音频文件为空")
            os.replace(temporary, target)
            self.save_metadata(file_id, {
                "owner_id": owner_id, "mime_type": mime_type,
                "filename": filename, "size": size,
            })
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return {
            "file_id": file_id, "mime_type": mime_type, "filename": filename, "size": size,
            "reference": f'<graywill_audio_ref id="{file_id}"/>',
        }
