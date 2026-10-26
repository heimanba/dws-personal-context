#!/usr/bin/env python3
"""digest 发布流水线：预检 → 固定文件夹 → 发布视图 → 建/更 → 回读验证。

只发布 digest 根目录（默认 ~/.qoder/dws-personal-context/digest）下的文件。`check` 只读，
可在未获发布授权时运行；`create` / `update` 是唯一写操作，只有用户明确要求
写回钉钉文档时才执行。所有结果以 JSON 写到 stdout，退出码 0=成功/完整、
2=已部分写入但未完成回读、1=预检或调用失败。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ensure_publish_folder
import publish_view
from markdown_compare import equivalence
from publication_receipt import Receipt, ReceiptError

DEFAULT_DIGEST_ROOT = Path.home() / '.qoder/dws-personal-context/digest'
DEFAULT_FOLDER_NAME = '日报'
FOOTER_MARK = '由 ctxring 发布'
FOOTER = ('\n\n---\n\n> 由 ctxring 发布 · 来源引用已压缩为发布视图，'
          '完整取证见本地 digest：digest/{name} · {verb}：{stamp}\n')
SNAPSHOT_TOLERANCE = timedelta(seconds=60)

DATA_SOURCE = re.compile(r'^数据源：raw/(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?', re.MULTILINE)
SNAPSHOT_LINE = re.compile(r'^快照：(.*)$', re.MULTILINE)
SNAPSHOT = re.compile(r'(\d{4}-\d{2}-\d{2})@(\d{4}-\d{2}-\d{2} \d{2}:\d{2})')
STEM = re.compile(r'(\d{4}-\d{2}-\d{2})(?:_(\d{4}-\d{2}-\d{2}))?')
H1 = re.compile(r'^#\s+(.+?)\s*$', re.MULTILINE)
LINK = re.compile(r'\]\(([^)]*)\)')
BAD_LINK = re.compile(r'…|\.\.\.|_dt_ac=|_dt_sig=|\[REDACTED_CREDENTIAL\]')
CREDENTIALS = (
    (re.compile(r'sk-[A-Za-z0-9]{8,}'), 'sk_credential'),
    (re.compile(r'Bearer [A-Za-z0-9._-]{8,}'), 'bearer_credential'),
    (re.compile(r'(?i)\b(password|secret|token)='), 'credential_assignment'),
)
NODE_IN_URL = re.compile(r'/nodes/([A-Za-z0-9]+)')


class PublishError(Exception):
    def __init__(self, code, detail='', payload=None):
        super().__init__(code + (': ' + detail if detail else ''))
        self.code = code
        self.detail = detail
        self.payload = payload or {}


def dws(args, stdin=None):
    command = [os.environ.get('DWS_BIN', 'dws'), *args, '--format', 'json']
    profile = os.environ.get('DWS_PROFILE')
    if profile:
        command += ['--profile', profile]
    result = subprocess.run(command, input=stdin, capture_output=True, text=True)
    payload = None
    has_error = False
    for stream in (result.stdout, result.stderr):
        try:
            candidate = json.loads(stream)
        except (ValueError, TypeError):
            continue
        if not isinstance(candidate, dict):
            continue
        error = candidate.get('error')
        has_error = has_error or isinstance(error, dict)
        nested = error.get('details') if isinstance(error, dict) else None
        operation = nested if isinstance(nested, dict) else candidate
        if payload is None or _node_of(_data(operation)):
            payload = operation
        if _node_of(_data(operation)):
            break
    # Business error envelopes are errors regardless of process exit status.
    if result.returncode != 0 or has_error or (isinstance(payload, dict) and payload.get('status') in ('partial_success', 'unknown')):
        detail = (result.stderr.strip() or result.stdout.strip())[:500]
        raise PublishError('dws_failed', detail, payload)
    if payload is None:
        raise PublishError('dws_output_not_object')
    return payload


def _line_of(text, position):
    return text.count('\n', 0, position) + 1


def _days(start, end):
    days, current = [], start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _title(text, stem):
    match = H1.search(text)
    if match:
        return match.group(1).strip()
    return '工作日报 · ' + stem


def _raw_root(args):
    if args.raw_root:
        return Path(args.raw_root).expanduser().resolve()
    return Path(args.digest_root).expanduser().resolve().parent / 'raw'


def preflight(path, digest_root, raw_root, now=None):
    path = Path(path).expanduser().resolve()
    root = Path(digest_root).expanduser().resolve()
    if path.parent != root:
        raise PublishError('outside_digest_root', str(path))
    if not path.is_file():
        raise PublishError('digest_missing', str(path))
    text = path.read_text(encoding='utf-8')
    problems, warnings = [], []
    now = now or datetime.now()

    if not re.search(r'^##\s+覆盖口径\s*$', text, re.MULTILINE):
        problems.append({'code': 'coverage_section_missing'})
    if not re.search(r'^##\s+证据附录\s*$', text, re.MULTILINE):
        warnings.append('evidence_appendix_missing')

    source = DATA_SOURCE.search(text)
    start = end = None
    snapshots = {}
    if not source:
        problems.append({'code': 'data_source_pointer_missing'})
    else:
        start = datetime.strptime(source.group(1), '%Y-%m-%d').date()
        end = datetime.strptime(source.group(2) or source.group(1), '%Y-%m-%d').date()
        if end < start:
            problems.append({'code': 'data_source_range_invalid'})

    line = SNAPSHOT_LINE.search(text)
    if not line:
        problems.append({'code': 'snapshot_pointer_missing'})
    elif '…' in line.group(1) or '...' in line.group(1):
        problems.append({'code': 'snapshot_pointer_truncated'})
    else:
        for day, stamp in SNAPSHOT.findall(line.group(1)):
            try:
                snapshots[day] = datetime.strptime(stamp, '%Y-%m-%d %H:%M')
            except ValueError:
                problems.append({'code': 'snapshot_time_invalid', 'detail': day})

    if start and end:
        stem = STEM.fullmatch(path.stem)
        if not stem:
            warnings.append('digest_name_unparsed')
        else:
            expected_start = date.fromisoformat(stem.group(1))
            expected_end = date.fromisoformat(stem.group(2) or stem.group(1))
            if (start, end) != (expected_start, expected_end):
                problems.append({
                    'code': 'digest_window_mismatch',
                    'detail': 'name %s..%s != pointer %s..%s' % (
                        expected_start, expected_end, start, end)})
        for day in _days(start, end):
            key = day.isoformat()
            raw = Path(raw_root) / key / 'context.json'
            if raw.exists() and key not in snapshots:
                problems.append({'code': 'snapshot_missing', 'detail': key})
            elif not raw.is_file():
                problems.append({'code': 'raw_missing', 'detail': key})
            elif raw.exists() and key in snapshots:
                mtime = datetime.fromtimestamp(raw.stat().st_mtime)
                if mtime > snapshots[key] + SNAPSHOT_TOLERANCE:
                    problems.append({
                        'code': 'stale_snapshot',
                        'detail': '%s raw %s > snapshot %s' % (
                            key, mtime.strftime('%Y-%m-%d %H:%M:%S'),
                            snapshots[key].strftime('%Y-%m-%d %H:%M'))})

    for line_number, content in enumerate(text.splitlines(), 1):
        for target in LINK.findall(content):
            if BAD_LINK.search(target):
                problems.append({'code': 'link_not_publishable', 'line': line_number})
            elif re.search(r'\s', target):
                problems.append({'code': 'link_contains_space', 'line': line_number})
            elif target.endswith('?') or target.endswith('&'):
                problems.append({'code': 'link_incomplete', 'line': line_number})
        for pattern, code in CREDENTIALS:
            if pattern.search(content):
                problems.append({'code': code, 'line': line_number})

    days = publish_view._window_days(path.stem)
    view = publish_view.transform(text, publish_view.load_chat_titles(raw_root, days))
    for line_number, content in enumerate(publish_view.citation_text(view).splitlines(), 1):
        if publish_view.RESIDUAL.search(content):
            problems.append({'code': 'residual_machine_id', 'line': line_number})

    return {
        'ok': not problems,
        'digest': str(path),
        'title': _title(text, path.stem),
        'range': {'start': start.isoformat() if start else None,
                  'end': end.isoformat() if end else None},
        'snapshots': {key: value.strftime('%Y-%m-%d %H:%M')
                      for key, value in snapshots.items()},
        'problems': problems,
        'warnings': warnings,
        'view': view,
        'viewBytes': len(view.encode('utf-8')),
        'checkedAt': now.strftime('%Y-%m-%d %H:%M:%S'),
    }


def _data(payload):
    data = payload.get('data')
    return data if isinstance(data, dict) else {}


def _node_of(data):
    document = data.get('document') if isinstance(data.get('document'), dict) else {}
    node = data.get('node') if isinstance(data.get('node'), dict) else {}
    return data.get('nodeId') or document.get('nodeId') or node.get('nodeId')


def _url_of(data):
    document = data.get('document') if isinstance(data.get('document'), dict) else {}
    return data.get('docUrl') or document.get('docUrl') or data.get('url')


def canonical_node(target):
    match = NODE_IN_URL.search(target)
    return match.group(1) if match else target


def readback(node, marker):
    node = canonical_node(node)
    info = dws(['doc', '+inspect', '--node', node])
    document = _data(info).get('document')
    document = document if isinstance(document, dict) else {}
    inspected = document.get('nodeId') == node
    hit = False
    if inspected:
        try:
            fetched = dws(['doc', '+fetch', '--node', node,
                           '--scope', 'keyword', '--keyword', marker])
            content = fetched.get('content')
            content = content if isinstance(content, dict) else {}
            hit = bool(content.get('count') or content.get('matches'))
        except PublishError:
            hit = False
    return {'inspected': inspected, 'contentHit': hit,
            'url': document.get('docUrl'), 'name': document.get('name')}


def readback_created(node, expected):
    """Verify the entire created document; footer presence alone is insufficient."""
    checked = {'inspected': False, 'contentHit': False, 'url': None,
               'normalization': None}
    try:
        info = dws(['doc', '+inspect', '--node', node])
        document = _data(info).get('document')
        if (info.get('status') != 'success' or info.get('complete') is not True
                or not isinstance(document, dict) or document.get('nodeId') != node):
            return checked
        checked.update(inspected=True, url=document.get('docUrl'), name=document.get('name'))
        fetched = dws(['doc', '+fetch', '--node', node, '--scope', 'full'])
        content = fetched.get('content')
        if (fetched.get('status') != 'success' or fetched.get('complete') is not True
                or not isinstance(content, dict) or content.get('nodeId') != node
                or content.get('success') is False
                or not isinstance(content.get('markdown'), str)):
            return checked
        checked['normalization'] = equivalence(expected, content['markdown'])
        checked['contentHit'] = checked['normalization'] is not None
    except PublishError as error:
        checked['error'] = error.code
    return checked


def find_existing(title):
    existing, warnings = [], []
    try:
        payload = dws(['doc', '+search', '--query', title,
                       '--page-all', '--max-pages', '20'])
    except PublishError as error:
        return [], ['duplicate_check_failed: ' + error.code]
    if (payload.get('complete') is not True
            or payload.get('status') not in (None, 'success')):
        warnings.append('duplicate_check_incomplete')
    documents = payload.get('documents')
    if (not isinstance(documents, list)
            or any(not isinstance(item, dict)
                   or not isinstance(item.get('name'), str) or not item['name']
                   or not isinstance(item.get('docType') or item.get('type'), str)
                   or not (item.get('docType') or item.get('type'))
                   for item in documents)):
        return [], warnings + ['duplicate_check_unavailable']
    matches = [item for item in documents
               if isinstance(item, dict) and item.get('name') == title
               and (item.get('docType') or item.get('type')) == 'adoc'
               and isinstance(item.get('nodeId'), str) and item['nodeId']]
    if any(item.get('name') == title
           and (item.get('docType') or item.get('type')) == 'adoc'
           and (not isinstance(item.get('nodeId'), str) or not item['nodeId'])
           for item in documents):
        warnings.append('duplicate_candidate_missing_id')
    for item in matches:
        try:
            fetched = dws(['doc', '+fetch', '--node', item['nodeId'],
                           '--scope', 'keyword', '--keyword', FOOTER_MARK])
        except PublishError:
            warnings.append('duplicate_candidate_unreadable')
            continue
        content = fetched.get('content')
        content = content if isinstance(content, dict) else {}
        if (fetched.get('status') != 'success'
                or fetched.get('complete') is False
                or not isinstance(content.get('count'), int)
                or isinstance(content.get('count'), bool)
                or content['count'] < 0
                or not isinstance(content.get('matches'), list)):
            warnings.append('duplicate_candidate_unreadable')
            continue
        if content.get('count') or content.get('matches'):
            existing.append({'nodeId': item['nodeId'],
                             'url': item.get('url'), 'name': title})
    return existing, warnings


def _folder(args):
    if args.folder is not None:
        if not args.folder.strip():
            raise PublishError('empty_folder')
        return {'name': None, 'nodeId': args.folder.strip(), 'created': False}
    try:
        return ensure_publish_folder.resolve(args.folder_name, dry_run=args.dry_run)
    except ensure_publish_folder.FolderError as error:
        raise PublishError('folder_resolution_failed', str(error))


def _content(report, verb, stamp):
    return report['view'].rstrip('\n') + FOOTER.format(
        name=Path(report['digest']).name, verb=verb, stamp=stamp)


def _success_exit(status, readback_result):
    if (status == 'success' and readback_result['inspected']
            and readback_result['contentHit']):
        return 0
    return 2


def _create(args, digest_root, raw_root, receipt=None):
    report = preflight(args.digest, digest_root, raw_root)
    if not report['ok']:
        return report, 1
    title = args.name or report['title']
    existing, warnings = ([], ['duplicate_check_skipped'])
    if not args.skip_duplicate_check:
        existing, warnings = find_existing(title)
    if existing:
        return {'ok': False, 'action': 'create', 'title': title,
                'problems': [{'code': 'existing_digest_document',
                              'candidates': existing}],
                'warnings': warnings}, 1

    if warnings and not args.skip_duplicate_check:
        return {'ok': False, 'action': 'create', 'title': title,
                'problems': [{'code': 'duplicate_check_unknown'}],
                'warnings': warnings}, 1
    try:
        folder = _folder(args)
    except PublishError as error:
        return {'ok': False, 'action': 'create', 'problems': [
            {'code': error.code, 'detail': error.detail}]}, 1
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    content = _content(report, '发布', stamp)
    command = ['doc', '+create', '--name', title,
               '--folder', folder['nodeId'], '--content', '-']
    if args.dry_run and folder.get('wouldCreate'):
        return {'ok': True, 'action': 'create', 'title': title, 'folder': folder,
                'executed': False, 'verification': 'dry_run',
                'warnings': warnings, 'plan': ['create_folder', 'create_document']}, 0
    if args.dry_run:
        command.append('--dry-run')
    if receipt is not None:
        receipt.save({'state': 'pending', 'nodeId': None, 'digest': report['digest'],
                      'stamp': stamp, 'doNotRetryCreate': True})
    cli_error = None
    try:
        payload = dws(command, stdin=content)
    except PublishError as error:
        cli_error = error
        payload = error.payload

    data = _data(payload)
    node, url = _node_of(data), _url_of(data)
    result = {'ok': False, 'action': 'create', 'title': title, 'folder': folder,
              'nodeId': node, 'url': url, 'status': payload.get('status'),
              'complete': payload.get('complete'),
              'executed': data.get('executed'), 'warnings': warnings}
    if args.dry_run:
        result['ok'] = (cli_error is None and payload.get('status') == 'success'
                        and data.get('executed') is False)
        result['verification'] = 'dry_run'
        if cli_error:
            result['problems'] = [{'code': cli_error.code, 'detail': cli_error.detail}]
        return result, 0 if result['ok'] else 1

    # Preserve receipts before any readback. No uncertain write is safe to replay.
    result['doNotRetryCreate'] = True
    result['cliStatus'] = payload.get('status')
    result['cliComplete'] = payload.get('complete')
    result['steps'] = payload.get('steps', [])
    if 'compensation' in payload:
        result['compensation'] = payload['compensation']
    if cli_error:
        result['warnings'].append('cli_create_failed')
    if receipt is not None:
        try:
            receipt.save(dict(result, state='pending', digest=report['digest'], stamp=stamp))
        except ReceiptError:
            result['problems'] = [{'code': 'receipt_write_failed'}]
            return result, 2
    if not isinstance(node, str) or not node:
        result['problems'] = [{'code': 'create_state_unknown',
                              'detail': 'Locate by title before any further write.'}]
        return result, 2

    trusted = (cli_error is None and payload.get('status') == 'success'
               and payload.get('complete') is True and data.get('verified') is True)
    if trusted and url:
        result.update(ok=True, verification='verified')
        result['readback'] = {'inspected': True, 'contentHit': True, 'url': url}
        return result, 0

    checked = readback_created(node, content)
    result['readback'] = checked
    result['url'] = url or checked.get('url')
    result['ok'] = checked['inspected'] and checked['contentHit']
    if result['ok']:
        result['status'] = 'success'
        result['complete'] = True
        result['verification'] = ('full_readback_normalized'
                                  if checked['normalization'] == 'normalized'
                                  else 'full_readback')
        return result, 0
    result['problems'] = [{'code': 'readback_incomplete'}]
    return result, 2


def publication_profile():
    payload = dws(['profile', 'list'])
    selector = os.environ.get('DWS_PROFILE') or payload.get('currentProfile')
    profiles = payload.get('profiles')
    if not isinstance(profiles, list):
        raise PublishError('publication_profile_unknown')
    matches = [p for p in profiles if isinstance(p, dict) and
               (p.get('profile') == selector if selector else p.get('isCurrent') is True)]
    if (len(matches) != 1 or not all(isinstance(matches[0].get(k), str) and matches[0][k]
                                   for k in ('profile', 'corpId', 'userId'))):
        raise PublishError('publication_profile_unknown')
    return matches[0]['profile']


def create(args, digest_root, raw_root):
    report = preflight(args.digest, digest_root, raw_root)
    if not report['ok']:
        return report, 1
    profile = publication_profile()
    title = args.name or report['title']
    receipt = Receipt(digest_root, profile, title)
    previous_profile = os.environ.get('DWS_PROFILE')
    os.environ['DWS_PROFILE'] = profile

    def guarded_create():
        saved = receipt.read()
        if saved is not None:
            return {'ok': False, 'action': 'create', 'title': title,
                    'nodeId': saved.get('nodeId'), 'url': saved.get('url'),
                    'doNotRetryCreate': True, 'receipt': str(receipt.path),
                    'problems': [{'code': 'existing_create_receipt',
                                  'detail': 'Inspect the recorded document or pending request; do not create again.'}]}, 2
        result, code = _create(args, digest_root, raw_root, None if args.dry_run else receipt)
        if not args.dry_run and receipt.path.exists():
            try:
                receipt.save(dict(result, state='verified' if result.get('ok') else 'pending'))
            except ReceiptError:
                result.update(ok=False, doNotRetryCreate=True)
                result['problems'] = [{'code': 'receipt_write_failed'}]
                return result, 2
        return result, code

    try:
        if args.dry_run:
            return guarded_create()
        with receipt.locked():
            return guarded_create()
    except (ReceiptError, OSError) as error:
        try:
            saved = receipt.read() or {}
        except ReceiptError:
            saved = {}
        return {'ok': False, 'action': 'create', 'title': title,
                'nodeId': saved.get('nodeId'), 'url': saved.get('url'),
                'doNotRetryCreate': True,
                'problems': [{'code': 'publication_receipt_unavailable', 'detail': str(error)}]}, 2
    finally:
        if previous_profile is None:
            os.environ.pop('DWS_PROFILE', None)
        else:
            os.environ['DWS_PROFILE'] = previous_profile


def update(args, digest_root, raw_root):
    if not args.dry_run and not args.yes:
        return {'ok': False, 'action': 'update',
                'problems': [{'code': 'confirmation_required'}]}, 1
    report = preflight(args.digest, digest_root, raw_root)
    if not report['ok']:
        return report, 1
    node = canonical_node(args.node)
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    marker = '更新：' + stamp
    warnings = []
    if args.mode == 'overwrite':
        try:
            fetched = dws(['doc', '+fetch', '--node', node,
                           '--scope', 'keyword', '--keyword', FOOTER_MARK])
        except PublishError as error:
            return {'ok': False, 'action': 'update', 'nodeId': node,
                    'problems': [{'code': error.code, 'detail': error.detail}]}, 1
        content = fetched.get('content')
        content = content if isinstance(content, dict) else {}
        if not (content.get('count') or content.get('matches')):
            return {'ok': False, 'action': 'update', 'nodeId': node,
                    'problems': [{'code': 'overwrite_requires_ctxring_copy'}]}, 1

    body = _content(report, '更新', stamp)
    command = ['doc', '+checkpoint-update', '--node', node,
               '--mode', args.mode, '--content', '-']
    if args.dry_run:
        command.append('--dry-run')
    else:
        command.append('--yes')
    try:
        payload = dws(command, stdin=body)
    except PublishError as error:
        return {'ok': False, 'action': 'update', 'nodeId': node,
                'warnings': warnings,
                'problems': [{'code': error.code, 'detail': error.detail}]}, 1

    data = _data(payload)
    result = {'ok': False, 'action': 'update', 'nodeId': node,
              'status': payload.get('status'), 'complete': payload.get('complete'),
              'mode': args.mode, 'executed': data.get('executed'),
              'warnings': warnings}
    if args.dry_run:
        result['ok'] = payload.get('status') == 'success' and data.get('executed') is False
        result['verification'] = 'dry_run'
        return result, 0 if result['ok'] else 1

    checked = readback(node, marker)
    result['readback'] = checked
    result['url'] = checked['url']
    result['verification'] = 'readback'
    result['ok'] = (payload.get('status') == 'success'
                    and checked['inspected'] and checked['contentHit'])
    if not result['ok']:
        result['problems'] = [{'code': 'update_not_confirmed'}]
    return result, _success_exit(payload.get('status'), checked)


def main(argv=None):
    parser = argparse.ArgumentParser(description='digest 发布流水线（预检/新建/更新/回读）')
    sub = parser.add_subparsers(dest='command', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('digest', help='digest .md 文件路径')
    common.add_argument('--digest-root', default=str(DEFAULT_DIGEST_ROOT))
    common.add_argument('--raw-root', default=None)

    sub.add_parser('check', parents=[common], help='只读预检')

    create_parser = sub.add_parser('create', parents=[common], help='新建钉钉文档')
    create_parser.add_argument('--name', default=None)
    create_parser.add_argument('--folder', default=None, help='用户给定的文件夹 ID')
    create_parser.add_argument('--folder-name', default=DEFAULT_FOLDER_NAME)
    create_parser.add_argument('--dry-run', action='store_true')
    create_parser.add_argument('--skip-duplicate-check', action='store_true')

    update_parser = sub.add_parser('update', parents=[common], help='更新钉钉文档')
    update_parser.add_argument('--node', required=True)
    update_parser.add_argument('--mode', choices=('append', 'overwrite'), default='append')
    update_parser.add_argument('--yes', action='store_true')
    update_parser.add_argument('--dry-run', action='store_true')

    args = parser.parse_args(argv)
    digest_root = Path(args.digest_root).expanduser().resolve()
    raw_root = _raw_root(args)
    try:
        if args.command == 'check':
            report = preflight(args.digest, digest_root, raw_root)
            report.pop('view', None)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ok'] else 1
        if args.command == 'create':
            result, code = create(args, digest_root, raw_root)
        else:
            result, code = update(args, digest_root, raw_root)
    except PublishError as error:
        result = {'ok': False, 'problems': [
            {'code': error.code, 'detail': error.detail}]}
        code = 1
    result.pop('view', None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
