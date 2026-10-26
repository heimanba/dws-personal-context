"""Regression checks for empty directories and nullable CLI identity responses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('chats', ROOT / 'scripts/chats.py')
chats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chats)


def write_dialogue_fixture(path, state):
    meta = dict(state.get('meta', {}))
    meta.setdefault('self', {'senderIds': ['self']})
    meta.setdefault('window', {'start': '2026-10-12 00:00:00', 'end': '2026-10-13 00:00:00'})
    rows = state.get('conversations', [])
    messages = [{'cid': c['cid'], 'title': c.get('title', '测试同事'),
                 'kind': 'private_chat', 'direction': 'self_only',
                 'messages': c.get('msgs', [])} for c in rows if c.get('msgs')]
    empty = [{'cid': c['cid'], 'title': c.get('title', '测试同事'), 'kind': 'private_chat'}
             for c in rows if not c.get('msgs')]
    public = {'meta': {'format': 'dws-personal-context-dialogues', 'schemaVersion': 2,
                      'window': meta['window'], 'coverage': meta,
                      'stats': {'messages': sum(len(c['messages']) for c in messages)},
                      'state': {'self': meta['self'], 'reads': meta.get('reads', []),
                                'emptyConversations': empty}}, 'conversations': messages}
    path.write_text(json.dumps(public))


class CollectionRegressionTests(unittest.TestCase):
    def test_empty_directory_is_not_completed_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / 'dws'
            cli.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                           'assert sys.argv[2] == "+chat-list"\n'
                           'print(json.dumps({"chats":[],"complete":True,"paginationKnown":False,"stopReason":"untrusted_short_page"}))\n')
            cli.chmod(0o700)
            out = root / 'output.json'
            p = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'),
                                '--self-id', 'self', '--start', '2026-10-13 00:00:00',
                                '--end', '2026-10-14 00:00:00', '--no-directory-supplement', '--out', str(out)],
                               env=dict(os.environ, DWS_BIN=str(cli)), capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stderr)
            public = json.loads(out.read_text())
            self.assertEqual(public['meta']['format'], 'dws-personal-context-dialogues')
            data = chats.decode_dataset(public)
            self.assertFalse(data['meta']['knownConversationsComplete'])
            self.assertEqual(data['meta']['stopReason'], 'no_private_conversations_discovered')
            self.assertEqual(public['meta']['coverage']['knownConversations'], 0)
            self.assertEqual(data['meta']['reads'], [])

    def test_only_failed_reads_original_gap_and_keeps_successful_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'base.json'
            write_dialogue_fixture(base, {
                'meta': {'window': {'start': '2026-10-13 00:00:00', 'end': '2026-10-14 00:00:00'},
                         'complete': False, 'reads': [
                             {'cid': 'failed', 'start': '2026-10-13 00:00:00', 'end': '2026-10-14 00:00:00', 'complete': False},
                             {'cid': 'ok', 'start': '2026-10-13 00:00:00', 'end': '2026-10-14 00:00:00', 'complete': True}]},
                'conversations': [{'cid': 'failed', 'msgs': []}, {'cid': 'ok', 'msgs': [
                    {'msgId': 'old', 'createTime': '2026-10-13 01:00:00', 'senderId': 'self', 'text': 'kept'}]}]})
            cli = root / 'dws'
            cli.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                           'a=sys.argv; assert a[2] == "+chat-messages"\n'
                           'assert a[a.index("--open-conversation-id")+1] == "failed"\n'
                           'assert a[a.index("--start")+1] == "2026-10-13 00:00:00"\n'
                           'assert a[a.index("--end")+1] == "2026-10-14 00:00:00"\n'
                           'print(json.dumps({"messages":[{"messageId":"new","conversationId":"failed","createTime":"2026-10-13 02:00:00","senderId":"peer","text":"recovered"}],"complete":True}))\n')
            cli.chmod(0o700)
            out = root / 'out.json'
            proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'), 'update', '--base', str(base),
                                   '--only-failed', '--self-id', 'self', '--end', '2026-10-14 00:00:00',
                                   '--out', str(out)], env=dict(os.environ, DWS_BIN=str(cli)), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            public = json.loads(out.read_text())
            self.assertEqual(public['meta']['format'], 'dws-personal-context-dialogues')
            data = chats.decode_dataset(public)
            self.assertTrue(data['meta']['knownConversationsComplete'])
            self.assertEqual(data['meta']['messages'], 2)
            self.assertEqual(data['meta']['window']['end'], '2026-10-14 00:00:00')
            self.assertTrue(all(r['complete'] for r in data['meta']['reads']))

    def test_explicit_nonretryable_shape_error_keeps_cli_diagnosis(self):
        response = {'messages': [], 'complete': False, 'stopReason': 'response_shape_error',
                    'failures': [{'stage': 'response_shape'}]}
        error = {'category': 'api', 'reason': 'chat_messages_incomplete', 'retryable': False,
                 'origin': 'mcp_gateway', 'operation': 'chat/list_conversation_message_v2'}
        proc = subprocess.CompletedProcess([], 1, json.dumps(response), json.dumps({'error': error}))
        with patch.object(chats.subprocess, 'run', return_value=proc) as run:
            data = chats.call(['+chat-messages', '--open-conversation-id', 'private'], 'messages', 3)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(data['complete'])
        self.assertEqual(data['attempts'][0]['error']['retryable'], False)
        self.assertEqual(data['attempts'][0]['error']['origin'], 'mcp_gateway')

    def test_known_shape_failure_recovers_in_same_call_with_bound(self):
        failed = {'messages': [{'conversationId': 'private', 'messageId': 'm1'}],
                  'complete': False, 'stopReason': 'response_shape_error',
                  'failures': [{'stage': 'response_shape', 'error': '下游缺少明确的集合字段，无法判定结果为空'}]}
        error = {'category': 'api', 'reason': 'chat_messages_incomplete', 'retryable': False,
                 'origin': 'mcp_gateway', 'operation': 'chat/list_conversation_message_v2'}
        bad = subprocess.CompletedProcess([], 1, json.dumps(failed), json.dumps({'error': error}))
        good = subprocess.CompletedProcess([], 0, json.dumps({'messages': [
            {'conversationId': 'private', 'messageId': 'm1'},
            {'conversationId': 'private', 'messageId': 'm2'}], 'complete': True}), '')
        args = ['+chat-messages', '--open-conversation-id', 'private']
        with patch.object(chats.subprocess, 'run', side_effect=[bad, good]) as run, patch.object(chats.time, 'sleep'):
            data = chats.call(args, 'messages', 3, recover_shape=True)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(data['complete'])
        self.assertTrue(data['recoveredWithinCall'])
        self.assertEqual(len(data['messages']), 2)
        self.assertFalse(data['attempts'][0]['error']['retryable'])
        self.assertTrue(data['attempts'][0]['recovery']['retryScheduled'])
        with patch.object(chats.subprocess, 'run', return_value=bad) as run, patch.object(chats.time, 'sleep'):
            data = chats.call(args, 'messages', 2, recover_shape=True)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(data['complete'])
        self.assertFalse(data['recoveredWithinCall'])
        self.assertEqual(len(data['messages']), 1)
        self.assertFalse(data['attempts'][-1]['recovery']['retryScheduled'])
        for changed in ({'category': 'auth'}, {'server_error_code': '403'}, {'reason': 'permission_denied'}):
            denied = subprocess.CompletedProcess([], 1, json.dumps(failed), json.dumps({'error': dict(error, **changed)}))
            with patch.object(chats.subprocess, 'run', return_value=denied) as run:
                data = chats.call(args, 'messages', 3, recover_shape=True)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(data['complete'])

    def test_directory_supplement_unions_private_chats_without_claiming_coverage(self):
        def chat(cid, muted=False):
            return {'openConversationId': cid, 'conversationType': 'direct', 'chatMode': 'p2p',
                    'notificationOff': int(muted)}
        first = {'chats': [chat('muted', True), chat('shared')], 'complete': False,
                 'paginationKnown': False, 'nextCursor': 0, 'pagesFetched': 1, 'stopReason': 'pagination_error'}
        second = dict(first, chats=[chat('shared'), chat('extra'),
                                   {'openConversationId': 'group', 'conversationType': 'group', 'chatMode': 'group'}])
        with patch.object(chats, 'call', side_effect=[first, second, {'categories': []},
                {'conversations': []}]) as call:
            items, discovery = chats.discover_chats(100, 50, 3)
        self.assertEqual({c['openConversationId'] for c in items}, {'muted', 'shared', 'extra'})
        self.assertNotIn('--exclude-muted', call.call_args_list[0].args[0])
        self.assertIn('--exclude-muted', call.call_args_list[1].args[0])
        self.assertFalse(discovery['complete'])
        self.assertFalse(discovery['coverageComplete'])
        self.assertEqual(discovery['rejectedEntries'], 1)
        self.assertEqual(discovery['probes'][1]['addedConversations'], 1)
        complete = dict(first, complete=True, paginationKnown=True, stopReason='source_complete')
        with patch.object(chats, 'call', return_value=complete) as call:
            chats.discover_chats(100, 50, 3, supplement=False)
        self.assertEqual(call.call_count, 1)

    def test_message_search_supplements_verified_private_only(self):
        def fake_call(args, key, retries, **kwargs):
            if args[0] == '+chat-list':
                return {'chats': [{'openConversationId': 'known', 'conversationType': 'direct',
                                   'chatMode': 'p2p'}], 'complete': True, 'paginationKnown': True}
            if args[0] == '+category-list':
                return {'categories': []}
            if args[0] == '+conversation-list-top':
                return {'conversations': []}
            if args[0] == '+search-msg':
                if '--cursor' not in args:
                    return {'messages': [{'conversationId': 'bot'}, {'conversationId': 'group'},
                                         {'conversationId': 'known'}],
                            'complete': False, 'hasMore': True, 'nextCursor': 'next'}
                return {'messages': [{'conversationId': 'bot2'}], 'complete': True, 'hasMore': False}
            raise AssertionError(args)

        def fake_verify(cid, timeout):
            if cid in ('bot', 'bot2'):
                return ({'openConversationId': cid, 'singleChat': True, 'memberCount': 2,
                         'name': cid}, {'complete': True})
            return None, {'complete': False, 'stopReason': 'not_a_private_chat'}

        with patch.object(chats, 'call', side_effect=fake_call) as call, \
                patch.object(chats, 'verify_private_candidate', side_effect=fake_verify):
            items, discovery = chats.discover_chats(
                100, 50, 3, start='2026-10-15 00:00:00', end='2026-10-16 00:00:00')
        self.assertEqual({c['openConversationId'] for c in items}, {'known', 'bot', 'bot2'})
        search = discovery['messageSearch']
        self.assertTrue(search['complete'])
        self.assertEqual(search['invocations'], 2)
        self.assertEqual(search['verifiedPrivate'], 2)
        self.assertEqual(search['alreadyKnown'], 1)
        self.assertEqual(search['rejectedCandidates'], 1)
        self.assertEqual(discovery['returnedPrivateConversations'], 3)
        self.assertFalse(discovery['coverageComplete'])
        search_calls = [c.args[0] for c in call.call_args_list if c.args[0][0] == '+search-msg']
        self.assertIn('direct', search_calls[0])
        self.assertEqual(search_calls[0][search_calls[0].index('--start') + 1],
                         '2026-10-15T00:00:00+08:00')
        self.assertEqual(search_calls[1][search_calls[1].index('--cursor') + 1], 'next')

    def test_search_invocation_timeout_scales_with_the_page_budget(self):
        timeouts = {}

        def fake_call(args, key, retries, **kwargs):
            timeouts.setdefault(args[0], []).append(kwargs.get('timeout'))
            if args[0] == '+chat-list':
                return {'chats': [], 'complete': True, 'paginationKnown': True}
            if args[0] == '+category-list':
                return {'categories': []}
            if args[0] == '+conversation-list-top':
                return {'conversations': [], 'complete': True, 'hasMore': False}
            if args[0] == '+search-msg':
                return {'messages': [], 'complete': True, 'hasMore': False}
            raise AssertionError(args)

        with patch.object(chats, 'call', side_effect=fake_call):
            chats.discover_chats(100, 50, 3, start='2026-10-15 00:00:00',
                                 end='2026-10-16 00:00:00', timeout=60)
            chats.discover_chats(100, 50, 3, start='2026-10-15 00:00:00',
                                 end='2026-10-16 00:00:00', timeout=300)
        # One invocation pages up to 40 times server-side (~1.4s/page observed); a bare
        # 60s bound forfeits the whole paged attempt on normal tail latency.
        self.assertEqual(timeouts['+search-msg'], [110, 300])
        self.assertEqual(timeouts['+chat-list'], [60, 300])

    def test_collection_discovers_category_and_top_dms_without_reading_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / 'dws'
            cli.write_text("""#!/usr/bin/env python3
import json,sys
a=sys.argv; command=a[2]
if command == '+chat-list':
    result={'chats':[{'openConversationId':'primary','conversationType':'direct','chatMode':'p2p'}],
            'complete':False,'paginationKnown':False,'nextCursor':0,'stopReason':'pagination_error'}
elif command == '+category-list':
    result={'categories':[{'categoryId':123}]}
elif command == '+category-list-conversations':
    result={'conversations':[
        {'openConversationId':'category','conversationType':'direct','singleChat':True},
        {'openConversationId':'group','conversationType':'group','singleChat':False},
        {'openConversationId':'unknown','conversationType':'direct'}], 'complete':True}
elif command == '+conversation-list-top':
    cid='top2' if '--cursor' in a else 'top1'
    result={'conversations':[{'openConversationId':cid,'conversationType':'direct','singleChat':True}],
            'complete':True,'hasMore':cid=='top1','nextCursor':7 if cid=='top1' else 0}
elif command == '+chat-messages':
    cid=a[a.index('--open-conversation-id')+1]
    assert cid in ('primary','category','top1','top2'), 'non-private message read'
    result={'messages':[{'conversationId':cid,'messageId':'m-'+cid,
            'createTime':'2026-10-13 01:00:00','senderId':'peer','text':cid}], 'complete':True}
else:
    raise AssertionError(command)
print(json.dumps(result))
""")
            cli.chmod(0o700)
            out = root / 'out.json'
            proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'),
                                   '--self-id', 'self', '--start', '2026-10-13 00:00:00',
                                   '--end', '2026-10-14 00:00:00', '--out', str(out)],
                                  env=dict(os.environ, DWS_BIN=str(cli)), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            public = json.loads(out.read_text())
            self.assertEqual(public['meta']['format'], 'dws-personal-context-dialogues')
            data = chats.decode_dataset(public)
            self.assertEqual({c['cid'] for c in data['conversations']},
                             {'primary', 'category', 'top1', 'top2'})
            self.assertEqual(data['meta']['messages'], 4)
            self.assertTrue(data['meta']['knownConversationsComplete'])
            self.assertFalse(data['meta']['complete'])
            self.assertEqual(public['meta']['coverage']['knownConversations'], 4)

    def test_directory_missing_cursor_is_not_repeated_or_guessed(self):
        data = {'chats': [], 'complete': False, 'paginationKnown': False,
                'nextCursor': 0, 'stopReason': 'pagination_error'}
        error = {'category': 'api', 'retryable': True}
        proc = subprocess.CompletedProcess([], 1, json.dumps(data), json.dumps({'error': error}))
        with patch.object(chats.subprocess, 'run', return_value=proc) as run:
            output = chats.call(['+chat-list', '--types', 'p2p', '--page-all'], 'chats', 3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(output['attempts'][0]['retrySuppressed'], 'missing_directory_continuation')

    def test_collect_keeps_known_cids_but_does_not_import_old_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / 'seed.json'
            write_dialogue_fixture(seed, {'meta': {'self': {'senderIds': ['self']}},
                'conversations': [{'cid': 'known-private', 'singleChat': True, 'title': 'Known',
                    'msgs': [{'msgId': 'old', 'createTime': '2026-10-12 01:00:00',
                              'senderId': 'self', 'text': 'outside new window'}]}]})
            cli = root / 'dws'
            cli.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                'a=sys.argv\n'
                'if a[2]=="+chat-list": print(json.dumps({"chats":[],"complete":True,"paginationKnown":False}))\n'
                'else:\n'
                ' assert a[2]=="+chat-messages" and a[a.index("--open-conversation-id")+1]=="known-private"\n'
                ' print(json.dumps({"messages":[{"messageId":"new","conversationId":"known-private","createTime":"2026-10-13 01:00:00","senderId":"peer","text":"new window"}],"complete":True}))\n')
            cli.chmod(0o700)
            out = root / 'out.json'
            proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'), '--known-chats', str(seed),
                '--self-id', 'self', '--start', '2026-10-13 00:00:00', '--end', '2026-10-14 00:00:00',
                '--out', str(out)], env=dict(os.environ, DWS_BIN=str(cli)), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            public = json.loads(out.read_text())
            self.assertEqual(public['meta']['format'], 'dws-personal-context-dialogues')
            data = chats.decode_dataset(public)
            self.assertEqual([m['msgId'] for m in data['conversations'][0]['msgs']], ['new'])
            self.assertEqual(public['meta']['coverage']['knownConversations'], 1)
            self.assertTrue(data['meta']['knownConversationsComplete'])

    def test_read_errors_override_completion_and_invalid_rows_keep_valid_records(self):
        cases = [
            ({'messages': [{'messageId': 'kept'}], 'complete': True}, 1,
             {'error': {'category': 'permission', 'reason': 'denied', 'retryable': False}}, 'denied'),
            ({'messages': [None, {'messageId': 'kept'}], 'complete': True}, 0, {}, 'invalid_collection'),
            ({'messages': [{'messageId': 'kept'}], 'complete': True, 'success': False}, 0, {}, 'service_failure'),
        ]
        for payload, code, diagnostic, reason in cases:
            with self.subTest(reason=reason), patch.object(chats.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], code, json.dumps(payload), json.dumps(diagnostic))):
                result = chats.call(['+chat-messages'], 'messages', 1)
                self.assertFalse(result['complete'])
                self.assertEqual(result['stopReason'], reason)
                self.assertEqual([m['messageId'] for m in result['messages']], ['kept'])

    def test_request_timeout_is_bounded_and_preserves_earlier_messages(self):
        partial = subprocess.CompletedProcess([], 0, json.dumps({'messages': [{'messageId': 'kept'}],
            'complete': False, 'stopReason': 'read_failure'}), '')
        with patch.object(chats.subprocess, 'run', side_effect=[partial,
                subprocess.TimeoutExpired('dws', 1)]), patch.object(chats.time, 'sleep'):
            result = chats.call(['+chat-messages'], 'messages', 2)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stopReason'], 'timeout')
        self.assertEqual([m['messageId'] for m in result['messages']], ['kept'])

    def test_merge_inherits_timezone_and_rejects_mixed_zones_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {'meta': {'self': {'senderIds': ['self']}, 'window': {
                'start': '2026-10-13 00:00:00', 'end': '2026-10-14 00:00:00',
                'timezone': 'America/New_York'}, 'reads': [], 'complete': False}, 'conversations': []}
            first, second, out = root / 'first.json', root / 'second.json', root / 'out.json'
            write_dialogue_fixture(first, data)
            command = ['bash', str(ROOT / 'scripts/export-dialogues.sh'), 'merge', '--out', str(out)]
            proc = subprocess.run([*command, str(first)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(json.loads(out.read_text())['meta']['window']['timezone'], 'America/New_York')
            before = out.read_bytes()
            data['meta']['window']['timezone'] = 'Asia/Shanghai'
            write_dialogue_fixture(second, data)
            proc = subprocess.run([*command, str(first), str(second)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('timezone', proc.stderr)
            self.assertEqual(out.read_bytes(), before)

    def test_interrupted_collection_saves_completed_and_pending_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, out = root / 'dws', root / 'out.json'
            cli.write_text("""#!/usr/bin/env python3
import json,sys
a=sys.argv
if a[2] == '+chat-list':
    data={'chats':[{'openConversationId':cid,'conversationType':'direct','chatMode':'p2p'}
                   for cid in ('first','second')], 'complete':False,'paginationKnown':False}
else:
    cid=a[a.index('--open-conversation-id')+1]
    data={'messages':[{'messageId':'m','conversationId':cid,'senderId':'peer',
          'createTime':'2026-10-13 01:00:00' if cid=='first' else 'bad-time','text':'kept'}], 'complete':True}
print(json.dumps(data))
""")
            cli.chmod(0o700)
            proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'),
                '--self-id','self','--start','2026-10-13 00:00:00','--end','2026-10-14 00:00:00',
                '--no-directory-supplement','--out',str(out)], env=dict(os.environ,DWS_BIN=str(cli)),
                capture_output=True,text=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            result = json.loads(out.read_text())
            self.assertEqual(result['conversations'][0]['messages'][0]['text'], 'kept')
            reads = {r['cid']:r for r in result['meta']['state']['reads']}
            self.assertTrue(reads['first']['complete'])
            self.assertFalse(reads['second']['complete'])
            self.assertEqual(reads['second']['stopReason'], 'not_attempted')
            self.assertEqual(result['meta']['state']['emptyConversations'][0]['cid'], 'second')
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_null_search_result_reports_identity_failure(self):
        with patch.object(chats, 'command_json', side_effect=[
                {'data': {'userId': 'self', 'name': 'Person'}}, {'result': None}]):
            with self.assertRaisesRegex(ValueError, 'cannot map current account'):
                chats.resolve_self()

    def test_null_self_data_reports_identity_failure(self):
        with patch.object(chats, 'command_json', return_value={'data': None}):
            with self.assertRaisesRegex(ValueError, 'current account'):
                chats.resolve_self()


class RerunAndConcurrencyTests(unittest.TestCase):
    """Auto reruns extend the day file; fresh collect and concurrent runs are refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cli = self.root / 'dws'
        cli.write_text(
            '#!/usr/bin/env python3\nimport json,sys\n'
            'a=sys.argv\n'
            'if a[2]=="+chat-list":\n'
            ' result={"chats":[{"openConversationId":"a","name":"a","conversationType":"direct","chatMode":"p2p"}],"complete":True,"paginationKnown":True}\n'
            'else:\n'
            ' cid=a[a.index("--open-conversation-id")+1]\n'
            ' start=a[a.index("--start")+1]; end=a[a.index("--end")+1]\n'
            ' result={"messages":[{"conversationId":cid,"messageId":"m-"+end,"createTime":start,"senderId":"peer","text":end}],"complete":True}\n'
            'print(json.dumps(result))\n')
        cli.chmod(0o700)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, DWS_BIN=str(cli), HOME=str(self.home))
        # The mode word, when present, must be the first argument: the dispatcher
        # only recognizes it in argv[1].
        self.entry = ['bash', str(ROOT / 'scripts/export-dialogues.sh')]
        self.common = ['--self-id', 'self', '--no-directory-supplement']

    def run_dialogues(self, extra):
        return subprocess.run([*self.entry, *extra, *self.common], env=self.env,
                              capture_output=True, text=True)

    def test_auto_rerun_extends_the_day_file_as_an_update(self):
        first = self.run_dialogues(['--start', '2026-10-13 00:00:00', '--end', '2026-10-13 12:00:00'])
        self.assertEqual(first.returncode, 2, first.stderr)
        day = self.home / '.qoder/dws-personal-context/raw/2026-10-13/dialogues.json'
        first_data = json.loads(day.read_text())
        self.assertEqual(len(first_data['conversations'][0]['messages']), 1)
        self.assertNotIn('priorCoverage', first_data['meta']['coverage'])
        second = self.run_dialogues(['--start', '2026-10-13 00:00:00', '--end', '2026-10-14 00:00:00'])
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn('mode=update', second.stderr)
        second_data = json.loads(day.read_text())
        self.assertTrue(second_data['meta']['coverage']['priorCoverage'])
        self.assertEqual({m['msgId'] for m in second_data['conversations'][0]['messages']},
                         {'m-2026-10-13 12:00:00', 'm-2026-10-14 00:00:00'})

    def test_explicit_collect_refuses_to_overwrite_an_existing_output(self):
        out = self.root / 'day.json'
        args = ['collect', '--start', '2026-10-13 00:00:00', '--end', '2026-10-14 00:00:00', '--out', str(out)]
        first = self.run_dialogues(args)
        self.assertEqual(first.returncode, 2, first.stderr)
        before = out.read_bytes()
        second = self.run_dialogues(args)
        self.assertEqual(second.returncode, 1)
        self.assertIn('already exists', second.stderr)
        self.assertEqual(out.read_bytes(), before)

    def test_force_full_collects_afresh_over_an_existing_output(self):
        out = self.root / 'day.json'
        first = self.run_dialogues(['--start', '2026-10-13 00:00:00',
                                    '--end', '2026-10-13 12:00:00', '--out', str(out)])
        self.assertEqual(first.returncode, 2, first.stderr)
        second = self.run_dialogues(['--force-full', '--start', '2026-10-13 00:00:00',
                                     '--end', '2026-10-14 00:00:00', '--out', str(out)])
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn('mode=collect', second.stderr)
        data = json.loads(out.read_text())
        self.assertEqual([m['msgId'] for m in data['conversations'][0]['messages']],
                         ['m-2026-10-14 00:00:00'])
        self.assertNotIn('priorCoverage', data['meta']['coverage'])

    def test_second_concurrent_collection_is_rejected_by_the_lock(self):
        import fcntl
        out = self.root / 'day.json'
        lock = Path(str(out.resolve()) + '.lock')
        lock.write_text('')
        handle = open(lock, 'w')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            proc = self.run_dialogues(['--start', '2026-10-13 00:00:00',
                                       '--end', '2026-10-14 00:00:00', '--out', str(out)])
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.assertEqual(proc.returncode, 1)
        self.assertIn('another dws-personal-context collection holds', proc.stderr)
        self.assertFalse(out.exists())


class MessageReadOverlapTests(unittest.TestCase):
    """Bounded --message-workers overlaps requests while the artifact stays serial."""

    CONVERSATIONS = ('c0', 'c1', 'c2', 'c3')
    DELAY = 0.4

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'calls.log'
        cli = self.root / 'dws'
        cli.write_text(
            '#!/usr/bin/env python3\nimport json,os,sys,time\n'
            'a=sys.argv\n'
            'if a[2]=="+chat-list":\n'
            ' result={"chats":[{"openConversationId":cid,"name":cid,"conversationType":"direct","chatMode":"p2p"}\n'
            '                   for cid in %s],"complete":True,"paginationKnown":True}\n'
            'else:\n'
            ' cid=a[a.index("--open-conversation-id")+1]\n'
            ' log=os.environ["CALL_LOG"]\n'
            ' open(log,"a").write("%%s start %%f\\n"%%(cid,time.time()))\n'
            ' time.sleep(%s)\n'
            ' open(log,"a").write("%%s end %%f\\n"%%(cid,time.time()))\n'
            ' result={"messages":[{"conversationId":cid,"messageId":"m-"+cid,'
            '"createTime":"2026-10-13 01:00:00","senderId":"peer","text":"hi"}],"complete":True}\n'
            'print(json.dumps(result))\n' % (repr(list(self.CONVERSATIONS)), self.DELAY))
        cli.chmod(0o700)
        self.env = dict(os.environ, DWS_BIN=str(cli), CALL_LOG=str(self.log))

    def collect(self, workers):
        out = self.root / ('out-%s.json' % workers)
        proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'), 'collect',
                               '--start', '2026-10-13 00:00:00', '--end', '2026-10-14 00:00:00',
                               '--out', str(out), '--self-id', 'self', '--no-directory-supplement',
                               '--message-workers', str(workers)],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr.count('read messages=1 complete=True'), len(self.CONVERSATIONS),
                         proc.stderr)
        lines = self.log.read_text().splitlines()
        spans = {}
        for line in lines:
            cid, mark, stamp = line.split()
            spans.setdefault(cid, {})[mark] = float(stamp)
        self.log.write_text('')
        # Exactly one request per conversation: a sliding window must never re-submit a queued CID.
        self.assertEqual(len(lines), 2 * len(self.CONVERSATIONS), lines)
        return json.loads(out.read_text()), [(c, s['start'], s['end']) for c, s in spans.items()]

    @staticmethod
    def overlapping(spans):
        ordered = sorted(spans, key=lambda span: span[1])
        return any(ordered[i + 1][1] < ordered[i][2] for i in range(len(ordered) - 1))

    def test_workers_overlap_requests_but_keep_read_order_and_content(self):
        serial, serial_spans = self.collect(1)
        self.assertEqual(len(serial_spans), len(self.CONVERSATIONS))
        self.assertFalse(self.overlapping(serial_spans), 'workers=1 must stay strictly serial')
        parallel, parallel_spans = self.collect(4)
        self.assertEqual(len(parallel_spans), len(self.CONVERSATIONS))
        self.assertTrue(self.overlapping(parallel_spans), 'workers=4 must overlap requests')
        # The ledger and the delivered rows are unchanged; only the request schedule moved.
        self.assertEqual([r['cid'] for r in parallel['meta']['state']['reads']], list(self.CONVERSATIONS))
        self.assertEqual([r['cid'] for r in serial['meta']['state']['reads']], list(self.CONVERSATIONS))
        self.assertTrue(all(r['complete'] for r in parallel['meta']['state']['reads']))
        self.assertEqual(sorted(c['cid'] for c in parallel['conversations']), list(self.CONVERSATIONS))
        self.assertEqual({m['msgId'] for c in parallel['conversations'] for m in c['messages']},
                         {'m-' + cid for cid in self.CONVERSATIONS})
        self.assertEqual(sum(len(c['messages']) for c in parallel['conversations']),
                         sum(len(c['messages']) for c in serial['conversations']))
        timings = parallel['meta']['coverage']['timings']
        self.assertEqual(sorted(timings), ['checkpointMs', 'conversations', 'discoveryMs',
                                           'fetchMs', 'identityMs', 'networkWaitMs', 'readPhaseMs'])
        self.assertEqual(timings['conversations'], len(self.CONVERSATIONS))
        self.assertTrue(all(v >= 0 for k, v in timings.items() if k != 'conversations'), timings)
        self.assertGreaterEqual(timings['readPhaseMs'], timings['checkpointMs'])
        # fetchMs is the serial-equivalent request cost, so an overlapped run must pay
        # less wall clock than the sum of its own requests; a serial run must not.
        self.assertGreater(timings['fetchMs'], timings['readPhaseMs'])
        serial_timings = serial['meta']['coverage']['timings']
        self.assertAlmostEqual(serial_timings['fetchMs'], serial_timings['readPhaseMs'],
                               delta=max(200, serial_timings['readPhaseMs'] // 5))

    def test_message_workers_outside_one_to_eight_is_rejected(self):
        for workers in ('0', '9'):
            with self.subTest(workers=workers):
                out = self.root / ('bad-%s.json' % workers)
                proc = subprocess.run(['bash', str(ROOT / 'scripts/export-dialogues.sh'), 'collect',
                                       '--start', '2026-10-13 00:00:00', '--end', '2026-10-14 00:00:00',
                                       '--out', str(out), '--self-id', 'self',
                                       '--no-directory-supplement', '--message-workers', workers],
                                      env=self.env, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
