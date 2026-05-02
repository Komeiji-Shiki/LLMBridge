#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键清理敏感数据脚本
清除项目中的 API 密钥、session_id、日志文件等敏感信息
"""

import os
import json
import re
import shutil
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent.resolve()

def clean_config_jsonc():
    """清理 config.jsonc 中的敏感信息"""
    file_path = SCRIPT_DIR / "config.jsonc"
    if not file_path.exists():
        print(f"⚠️ 文件不存在: {file_path}")
        return
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 清理 session_id
    content = re.sub(
        r'("session_id"\s*:\s*")[^"]+(")',
        r'\g<1>00000000-0000-0000-0000-000000000000\2',
        content
    )
    
    # 清理 file_bed_endpoints 中的 api_key
    content = re.sub(
        r'("api_key"\s*:\s*")[^"]+(")',
        r'\g<1>\2',  # 清空为空字符串
        content
    )
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("✅ 已清理 config.jsonc (session_id, api_keys)")

def clean_model_endpoint_map():
    """清理 model_endpoint_map.json"""
    file_path = SCRIPT_DIR / "model_endpoint_map.json"
    if not file_path.exists():
        print(f"⚠️ 文件不存在: {file_path}")
        return
    
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    sensitive_fields = ['api_key', 'session_id', 'message_id']
    placeholders = {
        'api_key': 'YOUR_API_KEY_HERE',
        'session_id': '00000000-0000-0000-0000-000000000000',
        'message_id': '00000000-0000-0000-0000-000000000000'
    }
    
    def clean_dict(d):
        if isinstance(d, dict):
            for key, value in d.items():
                if key in sensitive_fields and isinstance(value, str):
                    d[key] = placeholders.get(key, 'REDACTED')
                elif isinstance(value, (dict, list)):
                    clean_dict(value)
        elif isinstance(d, list):
            for item in d:
                clean_dict(item)
    
    clean_dict(data)
    
    # 保存为示例文件
    example_path = SCRIPT_DIR / "model_endpoint_map.example.json"
    with open(example_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    # 清空原文件
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump({}, f, indent=2)
    
    print("✅ 已清理 model_endpoint_map.json")

def clean_models_json():
    """清理 models.json"""
    file_path = SCRIPT_DIR / "models.json"
    if file_path.exists():
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({}, f, indent=2)
        print("✅ 已清理 models.json")

def clean_logs():
    """清理 logs 目录"""
    logs_dir = SCRIPT_DIR / "logs"
    if not logs_dir.exists():
        return
    
    for item in logs_dir.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
    
    print("✅ 已清理 logs 目录")

def clean_other():
    """清理其他目录"""
    # .idea
    idea_dir = SCRIPT_DIR / ".idea"
    if idea_dir.exists():
        shutil.rmtree(idea_dir)
        print("✅ 已删除 .idea")
    
    # downloaded_images
    images_dir = SCRIPT_DIR / "downloaded_images"
    if images_dir.exists():
        shutil.rmtree(images_dir)
        print("✅ 已删除 downloaded_images")
    
    # __pycache__
    for p in SCRIPT_DIR.rglob("__pycache__"):
        if p.is_dir():
            shutil.rmtree(p)

def main():
    print("=" * 50)
    print("🧹 敏感数据清理工具")
    print("=" * 50)
    
    confirm = input("确定清理敏感数据？(yes): ").strip().lower()
    if confirm != 'yes':
        print("❌ 已取消")
        return
    
    clean_config_jsonc()
    clean_model_endpoint_map()
    clean_models_json()
    clean_logs()
    clean_other()
    
    print("\n✅ 清理完成！")

if __name__ == "__main__":
    main()