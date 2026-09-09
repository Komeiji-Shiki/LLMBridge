"""按已核对目录更新模型价格；默认只报告，--apply 才写入，端点和密钥不进入报告。"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def normalized(value):
    value = re.sub(r'[-_](?:20\d{2}-?\d{2}-?\d{2}|2603)(?=$|[-_])', '', str(value).lower())
    return re.sub(r'[-_.\s]+', '', value)


def identify_model(name, config, catalog, aliases):
    for candidate in (name, config.get('model_id', '')):
        if candidate in aliases:
            return aliases[candidate]
    # 本地量化、蒸馏或社区微调模型不能套用名字中出现的旗舰模型价格。
    identifier = str(config.get('model_id') or name).lower()
    if any(marker in identifier for marker in ('exl3', 'gguf', 'heretic', 'qwopus', 'ornith', 'agents-a1')):
        return None
    patterns = [(normalized(key), key) for key in catalog]
    patterns.extend((normalized(alias), key) for key, value in catalog.items() for alias in value.get('aliases', []))
    patterns.sort(key=lambda pair: len(pair[0]), reverse=True)
    # 上游ID优先；自定义展示名只有在上游无法识别时才参与匹配。
    for candidate in (config.get('model_id'), name):
        if not candidate:
            continue
        candidate = normalized(candidate)
        for pattern, key in patterns:
            start = candidate.find(pattern)
            if start >= 0:
                suffix = candidate[start + len(pattern):]
                # 不把未发布的小版本自动匹配为旧版本，例如 gpt-5.9 -> gpt-5。
                if not suffix or not suffix[0].isdigit():
                    return key
    return None


def update_prices(configs, catalog, aliases=None):
    result = deepcopy(configs)
    report = {'updated': [], 'unchanged': [], 'unresolved': []}
    for name, raw in result.items():
        endpoints = raw if isinstance(raw, list) else [raw]
        for index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, dict):
                continue
            canonical = identify_model(name, endpoint, catalog, aliases or {})
            row = {'name': name, 'endpoint_index': index, 'model': canonical}
            if canonical is None:
                report['unresolved'].append({**row, 'pricing': endpoint.get('pricing')})
                continue
            entry = catalog[canonical]
            old = endpoint.get('pricing')
            price = deepcopy(entry['pricing'])
            row.update(before=old, after=price, sources=entry['sources'], note=entry.get('note', ''))
            if price != old:
                endpoint['pricing'] = price
                report['updated'].append(row)
            else:
                report['unchanged'].append(row)
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'model_endpoint_map.json')
    parser.add_argument('--catalog', type=Path, default=ROOT / 'docs/model_launch_prices.json')
    parser.add_argument('--aliases', type=Path, help='用户确认的别名 -> 目录模型名 JSON 文件')
    parser.add_argument('--report', type=Path, default=ROOT / 'logs/launch-price-update-report.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    original = args.config.read_bytes()
    configs = json.loads(original.decode('utf-8-sig'))
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    aliases = json.loads(args.aliases.read_text(encoding='utf-8')) if args.aliases else {}
    unknown_targets = set(aliases.values()) - set(catalog['models'])
    if unknown_targets:
        raise ValueError(f'别名指向未定价模型：{sorted(unknown_targets)}')
    result, report = update_prices(configs, catalog['models'], aliases)
    report.update(verified_at=catalog['verified_at'], applied=args.apply)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.apply and report['updated']:
        if args.config.read_bytes() != original:
            raise RuntimeError('配置在检查期间发生变化，请重新执行以保留并发修改。')
        backup = args.config.parent / 'logs' / ('model-pricing-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        temporary = backup.with_suffix('.new.json')
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(args.config)
        report['backup'] = str(backup)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: len(report[key]) for key in ('updated', 'unchanged', 'unresolved')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
