"""Shared credential redaction for collected raw content."""
import re


REDACTIONS = [
    (re.compile(r'\bsk-[A-Za-z0-9_.-]{16,}'), '[REDACTED_CREDENTIAL]'),
    (re.compile(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]{12,}=*'), r'\1[REDACTED_CREDENTIAL]'),
    (re.compile(r'''(?ix)([?&](?:access_token|refresh_token|api_key|apikey|token|secret|signature|_dt_ac)=)(?!\[REDACTED_CREDENTIAL\])[^&\s<>"')\]]+'''), r'\1[REDACTED_CREDENTIAL]'),
    (re.compile(r'''(?ix)(["']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|app[_-]?secret|password)\b["']?\s*[:=]\s*["']?)(?!\[REDACTED_CREDENTIAL\])[^\s,"';}\]]{8,}'''), r'\1[REDACTED_CREDENTIAL]'),
    (re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', re.S), '[REDACTED_PRIVATE_KEY]'),
]


def redact_text(text):
    flags = []
    for pattern, replacement in REDACTIONS:
        text, count = pattern.subn(replacement, text)
        if count and 'redacted_credential' not in flags:
            flags.append('redacted_credential')
    return text, flags


def redact_obj(value):
    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    return value
