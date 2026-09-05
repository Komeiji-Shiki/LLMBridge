"""Render user-controlled labels as spreadsheet text, never formulas."""


def csv_safe_text(value) -> str:
    text = str(value or '')
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text
