#!/usr/bin/env python3
"""Audit the distributable skill tree without reading user runtime data."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

PUBLIC_HOSTS = {'open.dingtalk.com', 'opensource.dingtalk.com', 'www.dingtalk.com'}
RESERVED_HOSTS = {'example.com', 'example.org', 'example.net'}
URL = re.compile(r'https?://[^\s<>\x60\'"\u3000-\u303f\uff00-\uffef]+')
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
REFERENCE = re.compile(r'\b(?:cid|msg)[A-Za-z0-9+/=]{12,}')
OPAQUE = re.compile(r'(?<![\w])(?=[A-Za-z0-9+/=]{24,}(?![\w]))[A-Za-z0-9+/=]{24,}')
PRIVATE_PATH = re.compile(r'/(?:Users|home)/[^/\s<>]+/')
FORBIDDEN = {'raw', 'digest', '.publish-receipts', '__pycache__', '.git', '.DS_Store'}
TEXT_SUFFIXES = {'.md', '.py', '.sh', '.toml', '.yaml', '.yml', '.txt'}


def allowed_host(host):
    return host in PUBLIC_HOSTS or host in RESERVED_HOSTS or host.endswith(('.test', '.example'))


def audit(root):
    problems = []
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root)
        if path.is_symlink():
            problems.append({'path': str(relative), 'code': 'symlink_not_distributable'})
            continue
        if any(part in FORBIDDEN for part in relative.parts) or path.suffix in ('.pyc', '.lock'):
            problems.append({'path': str(relative), 'code': 'runtime_artifact'})
            continue
        if not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES:
            problems.append({'path': str(relative), 'code': 'unreviewed_file_type'})
            continue
        text = path.read_text(encoding='utf-8')
        for line_number, line in enumerate(text.splitlines(), 1):
            codes = set()
            if PRIVATE_PATH.search(line):
                codes.add('personal_absolute_path')
            for target in URL.findall(line):
                try:
                    host = urlsplit(target.rstrip(').,;')).hostname or ''
                except ValueError:
                    host = ''
                if not allowed_host(host):
                    codes.add('non_public_url')
            for host in EMAIL.findall(line):
                if not (host in RESERVED_HOSTS or host.endswith(('.test', '.example'))):
                    codes.add('non_synthetic_email')
            for value in REFERENCE.findall(line):
                if (any(c.isdigit() for c in value) or value.endswith('=')) and not value.startswith(('cidSynthetic', 'msgSynthetic')):
                    codes.add('non_synthetic_reference')
            for value in OPAQUE.findall(line):
                if (any(c.isupper() for c in value) and any(c.islower() for c in value)
                        and any(c.isdigit() for c in value)
                        and '=' not in value.rstrip('=')
                        and not value.startswith(('cidSynthetic', 'msgSynthetic'))):
                    codes.add('opaque_identifier')
            problems.extend({'path': str(relative), 'line': line_number, 'code': code}
                            for code in sorted(codes))
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    problems = audit(args.root)
    print(json.dumps({'ok': not problems, 'problems': problems}, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
