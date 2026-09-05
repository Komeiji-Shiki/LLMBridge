"""Explicit administrator trust for custom tokenizer code sources."""
from core.config_loader import get_setting


def remote_code_allowed(source: str) -> bool:
    trusted = get_setting('tokenizer_trusted_sources', [])
    return isinstance(trusted, list) and source in trusted
