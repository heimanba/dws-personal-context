"""publish_digest contracts: preflight gates, folder/footer, duplicate stop, readback."""
import contextlib
import io
import json
import os
import stat
import subprocess
import time
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish_digest.py'
sys.path.insert(0, str(SCRIPT.parent))
import publish_digest as pd

SNAPSHOT = datetime(2026, 10, 21, 20, 39)

FAKE = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({'args': args, 'stdin': stdin}, ensure_ascii=False) + '\\n')
def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
if args[:2] == ['profile', 'list']:
    emit({'success':True,'currentProfile':'CORP:USER','profiles':[{'profile':'CORP:USER','corpId':'CORP','userId':'USER','isCurrent':True}]})
elif args[:3] == ['wiki', 'space', 'list']:
    emit({'wikiSpaces': [{'workspaceId': 'WS1'}]})
elif args[:3] == ['wiki', 'node', 'list']:
    emit({'nodes': [], 'hasMore': False})
elif args[:3] == ['wiki', 'node', 'create']:
    emit({'success': True, 'nodeType': 'folder', 'nodeId': 'FOLDER1'})
elif '+search' in args:
    if os.environ.get('FAKE_SEARCH_FAIL'):
        sys.exit(3)
    emit(json.loads(os.environ.get('FAKE_SEARCH', '{"complete": true, "documents": []}')))
elif '+fetch' in args:
    if os.environ.get('FAKE_FETCH_FAIL'):
        sys.exit(3)
    if 'OLD10' in args:
        emit({'status':'success','content':{'count':1,'matches':[{'text':'footer'}]}})
        sys.exit(0)
    emit(json.loads(os.environ.get('FAKE_FETCH', '{"status":"success","content":{"count":0,"matches":[]}}')))
elif '+inspect' in args:
    if os.environ.get('FAKE_INSPECT_GATE'):
        import time
        open(os.environ['FAKE_INSPECT_GATE'], 'w').close()
        while os.path.exists(os.environ['FAKE_INSPECT_GATE']):
            time.sleep(0.05)
    if os.environ.get('FAKE_INSPECT_FAIL'):
        sys.exit(3)
    emit(json.loads(os.environ.get('FAKE_INSPECT', '{}')))
elif '+create' in args:
    if os.environ.get('FAKE_CREATE_ERROR'):
        stream = sys.stderr if os.environ.get('FAKE_ERROR_STDERR') else sys.stdout
        print(os.environ['FAKE_CREATE_ERROR'], file=stream)
        sys.exit(1)
    emit(json.loads(os.environ.get('FAKE_CREATE', '{}')))
elif '+checkpoint-update' in args:
    emit(json.loads(os.environ.get('FAKE_UPDATE', '{}')))
else:
    sys.exit(3)
'''

BODY = '''# 工作日报 · 2026-10-15

## 覆盖口径

数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）
快照：2026-10-15@2026-10-21 20:39

## 时间线

- 10:52 已沟通。（来源：2026-10-15 10:52:55 | peer 同事甲 | chat:cidABCDEFG= msg:msgXYZ==）
'''


ROUNDTRIP_BODY = """# 工作日报 · 2026-10-15

## 覆盖口径

数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）
快照：2026-10-15@2026-10-21 20:39

| 日期 | 数量 |
|---|---|
| 10-15 | 16* |

## 结论

agent_infra 已完成 12 项。[详情](https://example.com/a_b?x=1)

- 已沟通。
  来源：私聊·同事甲

代码：`a  b`。
"""

ROUNDTRIP_REMOTE = r"""# 工作日报 · 2026-10-15

## 覆盖口径

数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）
快照：2026-10-15@2026-10-21 20:39

| 日期 | 数量 |
|------|------|
| 10-15 | 16\* |

## 结论

agent\_infra 已完成 12 项。[详情](https://example.com/a_b?x=1)
- 已沟通。
来源：私聊·同事甲

代码：`a  b`。

---
> 由 ctxring 发布 · 来源引用已压缩为发布视图，完整取证见本地 digest：digest/2026-10-15.md · 发布：2026-10-21 20:39   
"""


class PublishDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.digest_root = self.root / 'digest'
        self.raw_root = self.root / 'raw'
        self.digest_root.mkdir()
        self.raw_day = self.raw_root / '2026-10-15'
        self.raw_day.mkdir(parents=True)
        raw = self.raw_day / 'context.json'
        raw.write_text('{}', encoding='utf-8')
        stamp = SNAPSHOT.timestamp()
        os.utime(raw, (stamp, stamp))
        self.digest = self.digest_root / '2026-10-15.md'
        self.digest.write_text(BODY, encoding='utf-8')
        self.log = self.root / 'dws.log'
        self.log.write_text('', encoding='utf-8')
        fake = self.root / 'dws'
        fake.write_text(FAKE, encoding='utf-8')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env = mock.patch.dict(os.environ, {'DWS_BIN': str(fake),
                                                'FAKE_LOG': str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('DWS_PROFILE', None)
        self.addCleanup(self.log.write_text, '', encoding='utf-8')

    def invoke(self, *args, extra=None):
        env = dict(extra or {})
        with mock.patch.dict(os.environ, env, clear=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pd.main([*args, '--digest-root', str(self.digest_root),
                                '--raw-root', str(self.raw_root)])
        return code, json.loads(out.getvalue())

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def test_check_accepts_fresh_digest_and_compresses_view(self):
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['title'], '工作日报 · 2026-10-15')
        self.assertEqual(result['snapshots'], {'2026-10-15': '2026-10-21 20:39'})
        self.assertEqual(result['problems'], [])
        self.assertEqual(self.calls(), [])

    def test_default_home_root_reads_existing_receipt_and_blocks_duplicate_create(self):
        from publication_receipt import Receipt
        home = self.root / 'home'
        data_root = home / '.qoder' / 'dws-personal-context'
        data_root.parent.mkdir(parents=True)
        data_root.mkdir(exist_ok=True)
        self.digest_root.rename(data_root / 'digest')
        self.raw_root.rename(data_root / 'raw')
        digest = data_root / 'digest' / self.digest.name
        receipt = Receipt(digest.parent, 'CORP:USER', '工作日报 · 2026-10-15')
        receipt.save({'state': 'pending', 'nodeId': 'SYNTHETIC_NODE'})
        original = receipt.path.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), 'create', str(digest)],
                                env=dict(os.environ, HOME=str(home)),
                                capture_output=True, text=True, timeout=30)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2, report)
        self.assertEqual(report['problems'][0]['code'], 'existing_create_receipt')
        self.assertEqual(report['nodeId'], 'SYNTHETIC_NODE')
        self.assertTrue(report['doNotRetryCreate'])
        self.assertEqual(receipt.path.read_bytes(), original)
        self.assertFalse(any('+create' in call['args'] for call in self.calls()))
        self.assertEqual({p.name for p in (home / '.qoder').iterdir()}, {'dws-personal-context'})

    def test_check_warns_when_evidence_appendix_missing(self):
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 0, result)
        self.assertIn('evidence_appendix_missing', result['warnings'])

    def test_check_rejects_stale_raw(self):
        raw = self.raw_day / 'context.json'
        later = (SNAPSHOT.timestamp() + 120)
        os.utime(raw, (later, later))
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 1)
        self.assertEqual(result['problems'][0]['code'], 'stale_snapshot')

    def test_check_rejects_missing_coverage(self):
        self.digest.write_text('# 工作日报\n\n正文\n', encoding='utf-8')
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 1)
        codes = {item['code'] for item in result['problems']}
        self.assertIn('coverage_section_missing', codes)
        self.assertIn('data_source_pointer_missing', codes)
        self.assertIn('snapshot_pointer_missing', codes)

    def test_check_rejects_window_mismatch(self):
        other = self.digest_root / '2026-10-15_2026-10-16.md'
        other.write_text(BODY, encoding='utf-8')
        code, result = self.invoke('check', str(other))
        self.assertEqual(code, 1)
        codes = {item['code'] for item in result['problems']}
        self.assertIn('digest_window_mismatch', codes)

    def test_check_rejects_bad_links_and_credentials(self):
        self.digest.write_text(
            BODY + '\n- [x](https://a.example/b?x=1&_dt_ac=secret)\n'
            '- token=abcdefgh\n- sk-abcdefghijk\n', encoding='utf-8')
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 1)
        codes = {item['code'] for item in result['problems']}
        self.assertIn('link_not_publishable', codes)
        self.assertIn('credential_assignment', codes)
        self.assertIn('sk_credential', codes)

    def test_create_passes_folder_and_footer(self):
        created = {'status': 'success', 'complete': True,
                   'data': {'nodeId': 'NEW1', 'verified': True,
                            'docUrl': 'https://alidocs.example/nodes/NEW1'}}
        code, result = self.invoke(
            'create', str(self.digest), '--folder', 'FOLDER1',
            extra={'FAKE_CREATE': json.dumps(created)})
        self.assertEqual(code, 0, result)
        self.assertEqual(result['nodeId'], 'NEW1')
        self.assertEqual(result['verification'], 'verified')
        create = [call for call in self.calls() if '+create' in call['args']][0]
        self.assertIn('--folder', create['args'])
        self.assertIn('FOLDER1', create['args'])
        self.assertIn('> 由 ctxring 发布', create['stdin'])
        self.assertNotIn('chat:', create['stdin'])

    def test_create_readback_failure_is_partial(self):
        created = {'status': 'success', 'complete': True, 'data': {'nodeId': 'NEW1'}}
        inspected = {'status': 'success', 'complete': True,
                     'data': {'document': {'nodeId': 'NEW1'}}}
        fetched = {'status': 'success', 'content': {'count': 0, 'matches': []}}
        code, result = self.invoke(
            'create', str(self.digest), '--folder', 'FOLDER1',
            extra={'FAKE_CREATE': json.dumps(created),
                   'FAKE_INSPECT': json.dumps(inspected),
                   'FAKE_FETCH': json.dumps(fetched)})
        self.assertEqual(code, 2, result)
        self.assertFalse(result['ok'])
        self.assertEqual(result['nodeId'], 'NEW1')
        self.assertEqual(result['problems'][0]['code'], 'readback_incomplete')

    def test_create_blocks_existing_ctxring_document(self):
        search = {'complete': True, 'documents': [
            {'docType': 'adoc', 'name': '工作日报 · 2026-10-15',
             'nodeId': 'OLD1', 'url': 'https://alidocs.example/nodes/OLD1'}]}
        fetch = {'status': 'success', 'content': {'count': 1, 'matches': [{'text': 'x'}]}}
        code, result = self.invoke(
            'create', str(self.digest), '--folder', 'FOLDER1',
            extra={'FAKE_SEARCH': json.dumps(search), 'FAKE_FETCH': json.dumps(fetch)})
        self.assertEqual(code, 1)
        self.assertEqual(result['problems'][0]['code'], 'existing_digest_document')
        self.assertEqual(result['problems'][0]['candidates'][0]['nodeId'], 'OLD1')
        self.assertFalse([call for call in self.calls() if '+create' in call['args']])

    def test_dry_run_missing_folder_never_writes(self):
        code, result = self.invoke('create', str(self.digest), '--dry-run',
                                  extra={'FAKE_CREATE': '{"status":"success","data":{"executed":false}}'})
        self.assertEqual(code, 0, result)
        self.assertFalse(result['executed'])
        self.assertFalse(result['folder']['created'])
        self.assertTrue(result['folder']['wouldCreate'])
        self.assertFalse([c for c in self.calls()
                          if c['args'][:3] == ['wiki', 'node', 'create']
                          or '+create' in c['args']])

    def test_create_stops_when_duplicate_check_unknown(self):
        candidate = {'docType': 'adoc', 'name': '工作日报 · 2026-10-15', 'nodeId': 'OLD1'}
        cases = [
            {'FAKE_SEARCH_FAIL': '1'},
            {'FAKE_SEARCH': '{"complete":false,"documents":[]}'},
            {'FAKE_SEARCH': '{"documents":[]}'},
            {'FAKE_SEARCH': '{"complete":true}'},
            {'FAKE_SEARCH': '{"complete":true,"documents":[{"nodeId":"OLD1"}]}'},
            {'FAKE_SEARCH': json.dumps({'complete': True, 'documents': [
                {'name': candidate['name'], 'nodeId': 'OLD1'}]})},
            {'FAKE_SEARCH': json.dumps({'complete': True, 'documents': [candidate]}),
             'FAKE_FETCH_FAIL': '1'},
            {'FAKE_SEARCH': json.dumps({'complete': True, 'documents': [candidate]}),
             'FAKE_FETCH': '{}'},
            {'FAKE_SEARCH': json.dumps({'complete': True, 'documents': [candidate]}),
             'FAKE_FETCH': '{"status":"error","content":{"count":0,"matches":[]}}'},
            {'FAKE_SEARCH': json.dumps({'complete': True, 'documents': [None]}),
             'FAKE_FETCH': '{"status":"success","content":{"count":0,"matches":[]}}'},
        ]
        for env in cases:
            with self.subTest(env=env):
                self.log.write_text('', encoding='utf-8')
                code, result = self.invoke('create', str(self.digest), extra=env)
                self.assertEqual(code, 1, result)
                self.assertEqual(result['problems'][0]['code'], 'duplicate_check_unknown')
                self.assertFalse([c for c in self.calls()
                                  if c['args'][:3] == ['wiki', 'node', 'create']
                                  or '+create' in c['args']])

    def test_check_rejects_absent_raw_and_empty_snapshot(self):
        (self.raw_day / 'context.json').unlink()
        self.digest.write_text(BODY.replace('2026-10-15@2026-10-21 20:39', '—'), encoding='utf-8')
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 1, result)
        self.assertIn('raw_missing', {p['code'] for p in result['problems']})
        self.assertEqual(self.calls(), [])

    def test_create_checks_candidate_beyond_ten(self):
        search = {'complete': True, 'documents': [
            {'docType': 'adoc', 'name': '工作日报 · 2026-10-15', 'nodeId': 'OLD%d' % i}
            for i in range(11)]}
        code, result = self.invoke('create', str(self.digest), '--folder', 'FOLDER1',
                                  extra={'FAKE_SEARCH': json.dumps(search)})
        self.assertEqual(code, 1, result)
        self.assertEqual(result['problems'][0]['code'], 'existing_digest_document')
        self.assertEqual(result['problems'][0]['candidates'][0]['nodeId'], 'OLD10')
        self.assertFalse([c for c in self.calls() if '+create' in c['args']])

    def test_publish_accepts_protected_url_and_code(self):
        self.digest.write_text(BODY + '\n[源](https://example.com/mail:abc)\n'
                               '`mail:abc`\n', encoding='utf-8')
        code, result = self.invoke('check', str(self.digest))
        self.assertEqual(code, 0, result)

    def create_with_readback(self, remote=ROUNDTRIP_REMOTE, channel='stdout', extra=None):
        self.digest.write_text(ROUNDTRIP_BODY, encoding='utf-8')
        self.publication_number = getattr(self, 'publication_number', 0) + 1
        receipt = {'status': 'partial_success', 'complete': False,
                   'data': {'nodeId': 'NEW1'},
                   'steps': [{'name': 'create_document', 'status': 'success'}]}
        envelope = {'error': {'cause': '回读结果与完整初始内容不一致',
                              'diagnostic': 'x' * 700,
                              'details': receipt}}
        env = {'FAKE_CREATE_ERROR': json.dumps(envelope),
               'FAKE_INSPECT': json.dumps({'status': 'success', 'complete': True,
                   'data': {'document': {'nodeId': 'NEW1',
                           'docUrl': 'https://alidocs.example/nodes/NEW1'}}}),
               'FAKE_FETCH': json.dumps({'status': 'success', 'complete': True,
                   'content': {'nodeId': 'NEW1', 'markdown': remote}})}
        if channel == 'stderr':
            env['FAKE_ERROR_STDERR'] = '1'
        env.update(extra or {})
        with mock.patch.object(pd, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = SNAPSHOT
            return self.invoke('create', str(self.digest), '--folder', 'FOLDER1',
                               '--name', 'roundtrip-%d' % self.publication_number, extra=env)

    def test_create_recovers_normalized_markdown_from_nonzero_cli(self):
        for channel in ('stdout', 'stderr'):
            with self.subTest(channel=channel):
                self.log.write_text('', encoding='utf-8')
                code, result = self.create_with_readback(channel=channel)
                self.assertEqual(code, 0, result)
                self.assertTrue(result['ok'])
                self.assertEqual(result['nodeId'], 'NEW1')
                self.assertEqual(result['verification'], 'full_readback_normalized')
                self.assertEqual(result['cliStatus'], 'partial_success')
                self.assertEqual(len([c for c in self.calls() if '+create' in c['args']]), 1)

    def test_create_recovers_partial_success_on_zero_exit(self):
        receipt = {'status': 'partial_success', 'complete': False,
                   'data': {'nodeId': 'NEW1'}}
        code, result = self.create_with_readback(extra={
            'FAKE_CREATE_ERROR': '', 'FAKE_CREATE': json.dumps(receipt)})
        self.assertEqual(code, 0, result)
        self.assertEqual(result['verification'], 'full_readback_normalized')
        self.assertEqual(result['nodeId'], 'NEW1')

    def test_create_fails_closed_without_markdown_parser(self):
        with mock.patch.dict(sys.modules, {'markdown_it': None}):
            code, result = self.create_with_readback()
        self.assertEqual(code, 2, result)
        self.assertEqual(result['nodeId'], 'NEW1')
        self.assertTrue(result['doNotRetryCreate'])

    def test_create_preserves_partial_node_on_real_content_change(self):
        variants = [
            ROUNDTRIP_REMOTE.replace('12 项', '13 项'),
            ROUNDTRIP_REMOTE.replace('https://example.com/a_b?x=1', 'https://example.com/other'),
            ROUNDTRIP_REMOTE.replace('`a  b`', '`a b`'),
            ROUNDTRIP_REMOTE.replace('agent\\_infra 已完成', 'agent\\_infra已完成'),
            ROUNDTRIP_REMOTE.replace('## 结论', '结论'),
            ROUNDTRIP_REMOTE.replace('| 10-15 | 16\\* |', '| 10-15 16\\* |'),
            ROUNDTRIP_REMOTE.split('## 结论')[0],
        ]
        for remote in variants:
            with self.subTest(remote=remote):
                self.log.write_text('', encoding='utf-8')
                code, result = self.create_with_readback(remote)
                self.assertEqual(code, 2, result)
                self.assertFalse(result['ok'])
                self.assertEqual(result['nodeId'], 'NEW1')
                self.assertTrue(result['doNotRetryCreate'])
                self.assertEqual(len([c for c in self.calls() if '+create' in c['args']]), 1)

    def test_create_keeps_node_when_readback_fails(self):
        for extra in ({'FAKE_FETCH_FAIL': '1'}, {'FAKE_INSPECT_FAIL': '1'},
                      {'FAKE_FETCH': '{"status":"success","complete":false,"content":{"markdown":"x"}}'}):
            with self.subTest(extra=extra):
                code, result = self.create_with_readback(extra=extra)
                self.assertEqual(code, 2, result)
                self.assertEqual(result['nodeId'], 'NEW1')
                self.assertTrue(result['doNotRetryCreate'])

    def test_create_unknown_without_node_never_invites_retry(self):
        code, result = self.invoke('create', str(self.digest), '--folder', 'FOLDER1',
                                  extra={'FAKE_CREATE_ERROR': '{"error":{"details":{"status":"unknown"}}}'})
        self.assertEqual(code, 2, result)
        self.assertTrue(result['doNotRetryCreate'])

    def test_create_rejects_content_discarded_by_markdown_parser(self):
        from markdown_compare import equivalence
        cases = [
            ('| A | B |\n|---|---|\n| 1 | 2 | 验收不通过 |\n',
             '| A | B |\n|---|---|\n| 1 | 2 |\n'),
            ('正文。\n\n[证据]: https://example.com/old\n',
             '正文。\n\n[证据]: https://example.com/new\n'),
            ('正文。\n\n[证据]: https://example.com/source\n', '正文。\n'),
        ]
        for expected, actual in cases:
            with self.subTest(expected=expected):
                self.assertIsNone(equivalence(expected, actual))

    def test_partial_without_footer_blocks_later_create(self):
        search = {'complete': True, 'documents': [
            {'name': '工作日报 · 2026-10-15', 'docType': 'adoc', 'nodeId': 'NEW1'}]}
        env = {'FAKE_SEARCH': json.dumps(search),
               'FAKE_CREATE_ERROR': json.dumps({'error': {'details': {
                   'status': 'partial_success', 'data': {'nodeId': 'NEW1'}}}})}
        for _ in range(2):
            code, result = self.invoke('create', str(self.digest), '--folder', 'FOLDER1', extra=env)
            self.assertEqual(code, 2, result)
            self.assertEqual(result['nodeId'], 'NEW1')
        self.assertEqual(len([c for c in self.calls() if '+create' in c['args']]), 1)

    def test_pending_receipt_survives_interrupt_and_blocks_concurrent_create(self):
        gate = self.root / 'inspect-started'
        env = dict(os.environ, FAKE_INSPECT_GATE=str(gate),
                   FAKE_CREATE_ERROR=json.dumps({'error': {'details': {
                       'status': 'partial_success', 'data': {'nodeId': 'NEW1'}}}}))
        command = [sys.executable, str(SCRIPT), 'create', str(self.digest),
                   '--folder', 'FOLDER1', '--digest-root', str(self.digest_root),
                   '--raw-root', str(self.raw_root)]
        first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while not gate.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(gate.exists(), 'first publication did not reach readback')
            second = subprocess.run(command, env=dict(os.environ), capture_output=True, text=True)
            self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
            self.assertEqual(json.loads(second.stdout)['nodeId'], 'NEW1')
            self.assertEqual(json.loads(second.stdout)['problems'][0]['code'],
                             'publication_receipt_unavailable')
        finally:
            first.kill()
            gate.unlink(missing_ok=True)
            first.communicate(timeout=3)
        code, result = self.invoke('create', str(self.digest), '--folder', 'FOLDER1')
        self.assertEqual(code, 2, result)
        self.assertEqual(result['nodeId'], 'NEW1')
        self.assertEqual(result['problems'][0]['code'], 'existing_create_receipt')
        self.assertEqual(len([c for c in self.calls() if '+create' in c['args']]), 1)

    def test_zero_exit_error_envelope_preserves_node(self):
        code, result = self.invoke('create', str(self.digest), '--folder', 'FOLDER1', extra={
            'FAKE_CREATE': json.dumps({'error': {'details': {
                'status': 'partial_success', 'data': {'nodeId': 'NEW2'}}}})})
        self.assertEqual(code, 2, result)
        self.assertEqual(result['nodeId'], 'NEW2')

    def test_update_requires_confirmation(self):
        code, result = self.invoke('update', str(self.digest), '--node', 'DOC1')
        self.assertEqual(code, 1)
        self.assertEqual(result['problems'][0]['code'], 'confirmation_required')
        self.assertEqual(self.calls(), [])

    def test_overwrite_requires_ctxring_copy(self):
        code, result = self.invoke(
            'update', str(self.digest), '--node', 'DOC1',
            '--mode', 'overwrite', '--yes')
        self.assertEqual(code, 1)
        self.assertEqual(result['problems'][0]['code'], 'overwrite_requires_ctxring_copy')
        self.assertFalse([call for call in self.calls() if '+checkpoint-update' in call['args']])

    def test_update_verifies_by_readback(self):
        updated = {'status': 'success', 'complete': True, 'data': {'nodeId': 'DOC1'}}
        inspected = {'status': 'success', 'complete': True,
                     'data': {'document': {'nodeId': 'DOC1',
                                           'docUrl': 'https://alidocs.example/nodes/DOC1'}}}
        fetched = {'status': 'success', 'content': {'count': 1, 'matches': [{'text': 'x'}]}}
        code, result = self.invoke(
            'update', str(self.digest), '--node', 'DOC1', '--yes',
            extra={'FAKE_UPDATE': json.dumps(updated),
                   'FAKE_INSPECT': json.dumps(inspected),
                   'FAKE_FETCH': json.dumps(fetched)})
        self.assertEqual(code, 0, result)
        self.assertEqual(result['verification'], 'readback')
        self.assertEqual(result['url'], 'https://alidocs.example/nodes/DOC1')
        call = [item for item in self.calls() if '+checkpoint-update' in item['args']][0]
        self.assertIn('--yes', call['args'])
        self.assertIn('更新：', call['stdin'])


if __name__ == '__main__':
    unittest.main()
