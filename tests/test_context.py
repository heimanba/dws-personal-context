"""CLI contract tests: pagination, mutable snapshots, gaps and isolated failures.

Fixtures use the independently observed DWS response envelopes. Tests exercise
the shell entry points and persisted output, rather than adapter implementation.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
START = '2026-10-14T00:00:00+08:00'
END = '2026-10-15T00:00:00+08:00'
LATER = '2026-10-16T00:00:00+08:00'
PROFILE = 'corp:self'

FAKE = '''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['FIXTURE_LOG'],'a') as f:
    f.write(json.dumps(args)+'\\n')
rules=json.loads(Path(os.environ['FIXTURE_FILE']).read_text())
route=[]
for arg in args:
    if arg.startswith('--'): break
    route.append(arg)
seen=0
for line in Path(os.environ['FIXTURE_LOG']).read_text().splitlines():
    prior=json.loads(line)
    if prior[:len(route)] == route: seen+=1
for rule in rules:
    if route != rule['route']: continue
    if rule.get('nth') is not None and rule['nth'] != seen: continue
    if any(flag not in args or args[args.index(flag)+1]!=value for flag,value in rule.get('flags',{}).items()): continue
    if any(flag in args for flag in rule.get('absent',[])): continue
    time.sleep(rule.get('delay',0))
    print(json.dumps(rule['data']))
    if rule.get('stderr'): print(json.dumps(rule['stderr']),file=sys.stderr)
    sys.exit(rule.get('exit',0))
print(json.dumps({'error':{'category':'validation','reason':'unmatched_fixture','retryable':False}}))
sys.exit(3)
'''


def rule(route, data, flags=None, **extra):
    return dict(route=route.split(), data=data, flags=flags or {}, **extra)


def profile_rule(user='self'):
    return rule('profile list', dict(success=True, currentProfile='corp:' + user, profiles=[
        dict(profile='corp:' + user, corpId='corp', userId=user, isCurrent=True)]))


def todo_detail(uid, done=False, subject='task'):
    return rule('todo task get', dict(success=True, result=dict(todoDetailModel=dict(
        taskId=uid, subject=subject, isDone=done, modifiedTime=100))), {'--task-id': uid})


def minutes_detail(summary):
    return rule('minutes +detail', dict(complete=True, taskUuid='m1',
        basic=dict(success=True, result=dict(taskUuid='m1', title='meeting')),
        summary=dict(success='true', result=dict(fullSummary=summary)),
        keywords=dict(success=True, result=dict(keywords=['project']))), {'--id': 'm1'})


def mail_detail(uid, body, **extra):
    return rule('mail message get', dict(success='true', message=dict(
        id=uid, markdownBody=body, **extra)), {'--id': uid})


MAIL_RULES = lambda: [
    rule('mail mailbox list', dict(success=True, emailAccounts=[
        dict(type='ORG', email='me@example.test')])),
    rule('mail message search', dict(success=True, messages=[dict(id='m1')], nextCursor='$')),
    mail_detail('m1', 'body')]


class ContextContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'dws'
        self.cli.write_text(FAKE)
        self.cli.chmod(0o700)
        self.fixture = self.root / 'fixture.json'
        self.log = self.root / 'calls.jsonl'
        self.env = dict(os.environ, DWS_BIN=str(self.cli), FIXTURE_FILE=str(self.fixture),
                        FIXTURE_LOG=str(self.log))
        self.env.pop('DWS_PROFILE', None)

    def run_cli(self, rules, mode='collect', sources=None, base=None, end=END, extra=(), expected=0):
        self.fixture.write_text(json.dumps([profile_rule(), *rules]))
        self.log.write_text('')
        out = self.root / ('output-%d.json' % len(list(self.root.glob('output-*.json'))))
        args = ['bash', str(ROOT / 'scripts' / (mode + '-context.sh')), '--end', end,
                '--out', str(out), '--workers', '2', '--retries', '2', '--page-limit', '3']
        if mode == 'collect':
            args += ['--start', START]
        if sources:
            args += ['--sources', sources]
        if base:
            args += ['--base', str(base)]
        proc = subprocess.run([*args, *extra], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, expected, proc.stderr + proc.stdout)
        self.calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return out, json.loads(out.read_text()) if out.exists() else None

    def test_collect_auto_reuses_output_and_force_full_discards_baseline(self):
        out = self.root / 'auto.json'
        command = ['bash', str(ROOT / 'scripts/collect-context.sh'), '--sources', 'mail',
                   '--start', START, '--out', str(out)]
        def run(rules, end, extra=()):
            self.fixture.write_text(json.dumps([profile_rule(), *rules]))
            self.log.write_text('')
            proc = subprocess.run([*command,'--end',end,*extra],env=self.env,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            index = json.loads(out.with_suffix('.index.json').read_text())
            self.assertEqual([e['id'] for e in index['sources']['mail']['entries']],
                             [e['id'] for e in json.loads(out.read_text())['sources']['mail']['items']])
            self.assertEqual(out.with_suffix('.index.json').stat().st_mode & 0o777, 0o600)
            calls = [json.loads(line) for line in self.log.read_text().splitlines()]
            query = next(c for c in calls if c[:3]==['mail','message','search'])
            return json.loads(out.read_text()), query[query.index('--query')+1], proc.stderr
        mailbox = rule('mail mailbox list',dict(success=True,emailAccounts=[
            dict(type='ORG',email='me@example.test')]))
        def search(uid):
            return rule('mail message search',dict(success=True,messages=[dict(id=uid)],nextCursor='$'))
        first, query, log = run([mailbox,search('old'),mail_detail('old','old body')],END)
        self.assertEqual(query,'date>=%s AND date<%s' % (START,END))
        self.assertIn('mode=collect',log)
        updated, query, log = run([search('new'),mail_detail('new','new body')],LATER)
        self.assertEqual(query,'date>=%s AND date<%s' % (END,LATER))
        self.assertIn('mode=update',log)
        self.assertEqual({i['id'] for i in updated['sources']['mail']['items']},{'old','new'})
        fresh, query, log = run([mailbox,search('fresh'),mail_detail('fresh','fresh body')],
                               LATER,['--force-full'])
        self.assertEqual(query,'date>=%s AND date<%s' % (START,LATER))
        self.assertIn('mode=collect',log)
        self.assertEqual([i['id'] for i in fresh['sources']['mail']['items']],['fresh'])
        self.assertEqual(out.stat().st_mode & 0o777,0o600)

    def test_default_output_uses_the_canonical_home_root_and_reuses_it_within_the_window(self):
        home = self.root / 'home'
        home.mkdir()
        env = dict(self.env, HOME=str(home))
        self.fixture.write_text(json.dumps([profile_rule(),
            rule('mail mailbox list', dict(success=True, emailAccounts=[
                dict(type='ORG', email='me@example.test')])),
            rule('mail message search', dict(success=True, messages=[dict(id='m1')], nextCursor='$')),
            mail_detail('m1', 'body')]))
        self.log.write_text('')
        command = ['bash', str(ROOT / 'scripts/collect-context.sh'), '--sources', 'mail', '--start', START]
        out = home / '.qoder/dws-personal-context/raw/2026-10-14/context.json'
        first = subprocess.run([*command, '--end', END], env=env, cwd=self.root,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('mode=collect', first.stderr)
        self.assertTrue(out.exists())
        self.assertTrue(out.with_suffix('.index.json').exists())
        self.assertEqual({p.name for p in (home / '.qoder').iterdir()}, {'dws-personal-context'})
        self.assertEqual({p.name for p in self.root.glob('*.json')}, {'fixture.json'})
        second = subprocess.run([*command, '--end', LATER], env=env, cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('mode=update', second.stderr)

    def test_fresh_collect_refuses_existing_output_and_force_full_overrides(self):
        self.fixture.write_text(json.dumps([profile_rule(), *MAIL_RULES()]))
        self.log.write_text('')
        out = self.root / 'guard.json'
        command = ['bash', str(ROOT / 'scripts/collect-context.sh'), '--sources', 'mail',
                   '--start', START, '--end', END, '--out', str(out),
                   '--workers', '2', '--retries', '2', '--page-limit', '3']
        first = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        # A direct fresh collect over the existing file is refused instead of clobbering it.
        direct = ['python3', str(ROOT / 'scripts/context.py'), 'collect', '--sources', 'mail',
                  '--start', START, '--end', END, '--out', str(out),
                  '--workers', '2', '--retries', '2', '--page-limit', '3']
        refused = subprocess.run(direct, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('already exists', refused.stderr)
        forced = subprocess.run([*command, '--force-full'], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_concurrent_collection_on_same_output_is_refused_by_lock(self):
        import fcntl
        self.fixture.write_text(json.dumps([profile_rule(), *MAIL_RULES()]))
        self.log.write_text('')
        out = self.root / 'locked.json'
        command = ['bash', str(ROOT / 'scripts/collect-context.sh'), '--sources', 'mail',
                   '--start', START, '--end', END, '--out', str(out),
                   '--workers', '2', '--retries', '2', '--page-limit', '3']
        holder = open(str(out) + '.lock', 'w')
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            blocked = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(blocked.returncode, 1, blocked.stderr)
            self.assertIn('holds', blocked.stderr)
            self.assertFalse(out.exists())
        finally:
            holder.close()
        allowed = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue(out.exists())

    def test_update_without_out_writes_canonical_path_and_keeps_base(self):
        home = self.root / 'home-update'
        home.mkdir()
        env = dict(self.env, HOME=str(home))
        self.fixture.write_text(json.dumps([profile_rule(), *MAIL_RULES()]))
        self.log.write_text('')
        base = self.root / 'copied-base.json'
        first = subprocess.run(['bash', str(ROOT / 'scripts/collect-context.sh'), '--sources', 'mail',
                                '--start', START, '--end', END, '--out', str(base),
                                '--workers', '2', '--retries', '2', '--page-limit', '3'],
                               env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        baseline = base.read_bytes()
        canonical = home / '.qoder/dws-personal-context/raw/2026-10-14/context.json'
        self.assertFalse(canonical.exists())
        self.fixture.write_text(json.dumps([profile_rule(),
            rule('mail mailbox list', dict(success=True, emailAccounts=[
                dict(type='ORG', email='me@example.test')])),
            rule('mail message search', dict(success=True,
                 messages=[dict(id='m1'), dict(id='m2')], nextCursor='$')),
            mail_detail('m1', 'body'), mail_detail('m2', 'second body')]))
        second = subprocess.run(['bash', str(ROOT / 'scripts/update-context.sh'), '--base', str(base),
                                 '--end', LATER, '--workers', '2', '--retries', '2', '--page-limit', '3'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(canonical.exists())
        self.assertEqual(base.read_bytes(), baseline)
        updated = json.loads(canonical.read_text())
        self.assertEqual(updated['meta']['window']['end'], LATER)
        self.assertNotIn('-updated', second.stdout + second.stderr)

    def test_collect_auto_rejects_invalid_baseline_without_overwrite_or_online_reads(self):
        out = self.root / 'invalid.json'
        out.write_text('invalid JSON')
        self.log.write_text('')
        proc = subprocess.run(['bash',str(ROOT / 'scripts/collect-context.sh'),'--out',str(out)],
                              env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(proc.returncode,1)
        self.assertEqual(out.read_text(),'invalid JSON')
        self.assertEqual(self.log.read_text(),'')

    def test_all_sources_paginate_read_content_and_pin_identity(self):
        rules = [
            rule('calendar event list', dict(success=True, result=dict(events=[
                dict(id='recurring', start=dict(dateTime=START), summary='first')],
                hasMore=True, nextCursor='opaque:next')), absent=['--cursor']),
            rule('calendar event list', dict(success=True, result=dict(events=[
                dict(id='recurring', start=dict(dateTime='2026-10-14T10:00:00+08:00'), summary='second')],
                hasMore=False, nextCursor='')), {'--cursor': 'opaque:next'}),
            rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t1')], hasMore=True)), {'--page': '1'}),
            rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t2')], hasMore=False)), {'--page': '2'}),
            todo_detail('t1'), todo_detail('t2'),
            rule('minutes +search', dict(ok=True, data=dict(minutes=[dict(taskUuid='m1')], complete=True))),
            minutes_detail('minutes body'),
            rule('mail mailbox list', dict(success='true', emailAccounts=[dict(type='ORG', email='me@example.test')])),
            rule('mail message search', dict(success='true', messages=[dict(id='e1')], nextCursor='$')),
            mail_detail('e1', 'mail body'),
            rule('drive +recent', dict(ok=True, data=dict(items=[
                dict(nodeId='d1', contentType='ALIDOC'), dict(nodeId='s1', contentType='AXLS')], hasMore=False),
                meta=dict(pagination=dict(endpoint_exhausted=True)))),
            rule('doc +fetch', dict(complete=True, content=dict(nodeId='d1', markdown='document body'))),
            rule('oa approval detail', dict(success=True, result=dict(processInstanceId='a1', status='RUNNING'))),
        ]
        for role in ('pending', 'submitted', 'executed', 'cc'):
            rules.append(rule('oa approval list-' + role, dict(success=True, result=dict(
                values=[dict(processInstanceId='a1', title='approval')], hasMore=False))))
        out, data = self.run_cli(rules, sources='calendar,todo,approval,minutes,mail,documents')
        self.assertTrue(data['meta']['complete'])
        self.assertEqual(data['meta']['identity'], dict(profile=PROFILE, corpId='corp', userId='self'))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        sources = data['sources']
        self.assertEqual(len(sources['calendar']['items']), 2)
        self.assertEqual(len(sources['todo']['items']), 2)
        self.assertEqual(sources['approval']['items'][0]['metadata']['roles'], ['pending', 'submitted', 'executed', 'cc'])
        self.assertEqual(sources['minutes']['items'][0]['content']['summary']['fullSummary'], 'minutes body')
        self.assertEqual(sources['mail']['items'][0]['content']['markdownBody'], 'mail body')
        self.assertEqual(sources['documents']['items'][0]['content']['markdown'], 'document body')
        self.assertTrue(sources['documents']['items'][1]['content']['metadataOnly'])
        for call in self.calls:
            if call[:2] != ['profile', 'list']:
                self.assertEqual(call[call.index('--profile') + 1], PROFILE)
        self.assertFalse(any(call[:2] in (['chat', 'message'], ['mail', 'message', 'send']) for call in self.calls))

    def test_updates_mutable_sources_and_refreshes_unversioned_content(self):
        def rules(done, summary, doc, empty_calendar=False):
            return [
                rule('calendar event list', dict(success=True, result=dict(events=[] if empty_calendar else [
                    dict(id='c1', start=dict(dateTime=START), summary='meeting')], hasMore=False))),
                rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t1')], hasMore=False))),
                todo_detail('t1', done),
                rule('oa approval list-submitted', dict(success=True, result=dict(values=[
                    dict(processInstanceId='a1', status='COMPLETED' if done else 'RUNNING')], hasMore=False))),
                rule('oa approval detail', dict(success=True, result=dict(processInstanceId='a1', status='COMPLETED' if done else 'RUNNING'))),
                rule('minutes +search', dict(ok=True, data=dict(minutes=[dict(taskUuid='m1')], complete=True))),
                minutes_detail(summary),
                rule('drive +recent', dict(ok=True, data=dict(items=[dict(nodeId='d1', contentType='ALIDOC')], hasMore=False))),
                rule('doc +fetch', dict(complete=True, content=dict(nodeId='d1', markdown=doc))),
            ]
        base, _ = self.run_cli(rules(False, 'old minutes', 'old doc'),
            sources='calendar,todo,approval,minutes,documents', extra=['--approval-roles', 'submitted'])
        _, updated = self.run_cli(rules(True, 'new minutes', 'new doc', True), mode='update', base=base, end=LATER)
        s = updated['sources']
        self.assertTrue(s['todo']['items'][0]['content']['isDone'])
        self.assertEqual(s['approval']['items'][0]['content']['status'], 'COMPLETED')
        self.assertFalse(s['calendar']['items'][0]['presentInLastListing'])
        self.assertEqual(s['minutes']['items'][0]['content']['summary']['fullSummary'], 'new minutes')
        self.assertEqual(s['documents']['items'][0]['content']['markdown'], 'new doc')
        for call in self.calls:
            if call[:3] == ['calendar', 'event', 'list']:
                self.assertEqual(call[call.index('--start') + 1], START)

    def test_mail_replays_failed_window_then_retries_body_after_checkpoint(self):
        mailbox = rule('mail mailbox list', dict(success='true', emailAccounts=[dict(type='ORG', email='me@example.test')]))
        auth = dict(error=dict(category='permission', reason='denied', retryable=False))
        base, first = self.run_cli([
            mailbox,
            rule('mail message search', dict(success='true', messages=[dict(id='e1')], nextCursor='real-cursor'), absent=['--cursor']),
            rule('mail message search', auth, {'--cursor': 'real-cursor'}, exit=1),
            mail_detail('e1', 'preserved'),
        ], sources='mail', expected=2)
        self.assertNotIn('windowEnd', first['sources']['mail']['checkpoint'])
        self.assertEqual(first['sources']['mail']['items'][0]['content']['markdownBody'], 'preserved')
        self.assertEqual(first['sources']['mail']['coverage']['failures'][0]['stopReason'], 'denied')
        second_path, second = self.run_cli([
            rule('mail message search', dict(success='true', messages=[dict(id='e1'), dict(id='e2')], nextCursor='$')),
            mail_detail('e1', 'preserved'), mail_detail('e2', 'partial', bodyHasMore=True),
        ], mode='update', base=base, end=LATER, expected=2)
        searches = [call for call in self.calls if call[:3] == ['mail', 'message', 'search']]
        self.assertEqual(searches[0][searches[0].index('--query') + 1], 'date>=%s AND date<%s' % (START, LATER))
        self.assertEqual(second['sources']['mail']['checkpoint']['windowEnd'], LATER)
        self.assertEqual(second['sources']['mail']['items'][1]['read']['stopReason'], 'body_truncated')
        self.assertEqual(second['sources']['mail']['items'][1]['partialContent']['markdownBody'], 'partial')
        _, recovered = self.run_cli([mail_detail('e2', 'full')], mode='update', base=second_path,
            end=LATER, extra=['--only-failed'])
        self.assertTrue(recovered['sources']['mail']['coverage']['complete'])
        self.assertEqual(len(recovered['sources']['mail']['failureHistory']), 2)
        self.assertEqual(recovered['sources']['mail']['items'][1]['content']['markdownBody'], 'full')
        self.assertFalse(any(call[:3] == ['mail', 'message', 'search'] for call in self.calls))

    def test_detail_failure_keeps_old_body_and_does_not_block_other_sources(self):
        listing = rule('drive +recent', dict(ok=True, data=dict(items=[dict(nodeId='d1', contentType='ALIDOC')], hasMore=False)))
        base, _ = self.run_cli([listing, rule('doc +fetch', dict(complete=True, content=dict(nodeId='d1', markdown='old')))], sources='documents')
        updated_path, updated = self.run_cli([listing,
            rule('doc +fetch', dict(error=dict(category='authentication', reason='expired', retryable=False)), exit=1),
            rule('calendar event list', dict(success=True, result=dict(events=[], hasMore=False)))],
            mode='update', base=base, sources='documents,calendar', end=LATER, expected=2)
        self.assertTrue(updated['sources']['calendar']['coverage']['complete'])
        document = updated['sources']['documents']['items'][0]
        self.assertEqual(document['content']['markdown'], 'old')
        self.assertTrue(document['contentStale'])
        self.assertFalse(document['read']['complete'])
        self.assertEqual(len([c for c in self.calls if c[:2] == ['doc', '+fetch']]), 1)
        _, recovered = self.run_cli([listing,
            rule('doc +fetch', dict(complete=True, content=dict(nodeId='d1', markdown='fresh')))],
            mode='update', base=updated_path, extra=['--only-failed'], end=LATER)
        self.assertEqual(recovered['sources']['documents']['items'][0]['content']['markdown'], 'fresh')
        self.assertNotIn('contentStale', recovered['sources']['documents']['items'][0])
        self.assertFalse(any(call[:3] == ['calendar', 'event', 'list'] for call in self.calls))

    def test_uncertain_or_stalled_pagination_never_becomes_complete(self):
        cases = [
            ('calendar', [rule('calendar event list', dict(success=True, result=dict(events=[dict(id='c', start={})])))], 'missing_cursor'),
            ('todo', [rule('todo task list', dict(success=True, result=dict(todoCards=[])))], 'pagination_unknown'),
            ('approval', [rule('oa approval list-pending', dict(success=True, result=dict(values=[])))], 'pagination_unknown'),
            ('minutes', [rule('minutes +search', dict(ok=True, data=dict(minutes=[], complete=False)))], 'partial_response'),
            ('documents', [rule('drive +recent', dict(ok=True, data=dict(items=[], hasMore=True, nextCursor='next')))], 'pagination_incomplete'),
            ('calendar', [rule('calendar event list', dict(success=True, result=dict(events=[], hasMore=True, nextCursor='same')))], 'cursor_cycle'),
            ('calendar', [rule('calendar event list', dict(success=True, result=None))], 'missing_collection'),
        ]
        for source, rules, reason in cases:
            with self.subTest(source=source, reason=reason):
                _, data = self.run_cli(rules, sources=source, extra=['--approval-roles', 'pending'], expected=2)
                coverage = data['sources'][source]['coverage']
                self.assertFalse(coverage['complete'])
                self.assertEqual(coverage['failures'][-1]['stopReason'], reason)

    def test_cross_account_update_leaves_existing_output_untouched(self):
        base, _ = self.run_cli([rule('calendar event list', dict(success=True, result=dict(events=[], hasMore=False)))], sources='calendar')
        self.fixture.write_text(json.dumps([profile_rule('other')]))
        out = self.root / 'protected.json'
        out.write_text('keep')
        proc = subprocess.run(['bash', str(ROOT / 'scripts/update-context.sh'), '--base', str(base),
            '--end', LATER, '--out', str(out)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('another account', proc.stderr)
        self.assertEqual(out.read_text(), 'keep')

    def test_retry_exhaustion_retains_partial_rows_and_attempt_diagnostics(self):
        _, data = self.run_cli([
            rule('calendar event list', dict(error=dict(reason='temporary', retryable=True,
                retry_after_seconds=0), result=dict(events=[dict(id='c1', start={})])), exit=1, nth=1),
            rule('calendar event list', dict(error=dict(category='permission', reason='denied', retryable=False)),
                 exit=1, nth=2),
        ], sources='calendar', expected=2)
        calendar = data['sources']['calendar']
        self.assertEqual(len(calendar['items']), 1)
        state = calendar['coverage']['failures'][0]
        self.assertEqual(state['stopReason'], 'denied')
        self.assertEqual([a['error']['reason'] for a in state['attempts']], ['temporary', 'denied'])
        self.assertEqual(len([c for c in self.calls if c[:3] == ['calendar', 'event', 'list']]), 2)

    def test_approval_uses_pending_range_and_filters_other_roles_locally(self):
        _, data = self.run_cli([
            rule('oa approval list-pending', dict(success=True, result=dict(values=[], hasMore=False))),
            rule('oa approval list-submitted', dict(success=True, result=dict(values=[
                dict(processInstanceId='in', processCreateTime='2026-10-14T10:00:00+08:00'),
                dict(processInstanceId='out', processCreateTime='2026-10-13T10:00:00+08:00'),
                dict(processInstanceId='undated')], hasMore=False))),
            rule('oa approval detail', dict(success=True, result=dict(processInstanceId='in', status='RUNNING')),
                 {'--instance-id': 'in'}),
            rule('oa approval detail', dict(success=True, result=dict(processInstanceId='undated', status='RUNNING')),
                 {'--instance-id': 'undated'}),
        ], sources='approval', extra=['--approval-roles', 'pending,submitted'])
        approval = data['sources']['approval']
        self.assertEqual({i['id'] for i in approval['items']}, {'in', 'undated'})
        pending = next(c for c in self.calls if c[:3] == ['oa', 'approval', 'list-pending'])
        self.assertEqual(pending[pending.index('--start') + 1], START)
        self.assertEqual(pending[pending.index('--end') + 1], '2026-10-14T23:59:59+08:00')
        submitted = next(c for c in self.calls if c[:3] == ['oa', 'approval', 'list-submitted'])
        self.assertNotIn('--create-time-from', submitted)
        self.assertNotIn('--start', submitted)
        self.assertEqual(pending[pending.index('--limit') + 1], '20')
        self.assertEqual(submitted[submitted.index('--limit') + 1], '20')
        self.assertEqual(approval['coverage']['scope']['localTimeFilter']['undatedRows'], 1)
        self.assertTrue(approval['coverage']['complete'])

    def test_todo_merges_roles_and_keeps_participant_detail_metadata_only(self):
        _, data = self.run_cli([
            rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t1')], hasMore=False)),
                 {'--role-types': 'creator'}),
            rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t1')], hasMore=False)),
                 {'--role-types': 'executor'}),
            rule('todo task list', dict(success=True, result=dict(todoCards=[dict(taskId='t2')], hasMore=False)),
                 {'--role-types': 'participant'}),
            todo_detail('t1'),
        ], sources='todo')
        todo = data['sources']['todo']
        items = {i['id']: i for i in todo['items']}
        self.assertEqual(sorted(items['t1']['metadata']['roles']), ['creator', 'executor'])
        self.assertEqual(items['t1']['content']['subject'], 'task')
        self.assertEqual(items['t2']['read']['stopReason'], 'detail_not_supported_for_participant')
        self.assertTrue(items['t2']['content']['metadataOnly'])
        self.assertFalse(items['t2']['content']['detailSupported'])
        self.assertEqual(todo['coverage']['scope']['participantDetail'], 'list_metadata_only')
        self.assertEqual(len([c for c in self.calls if c[:3] == ['todo', 'task', 'get']]), 1)
        self.assertTrue(todo['coverage']['complete'])

    def test_todo_denied_detail_becomes_metadata_only(self):
        _, data = self.run_cli([
            rule('todo task list', dict(success=True, result=dict(todoCards=[
                dict(taskId='t1', subject='task')], hasMore=False)), {'--role-types': 'executor'}),
            rule('todo task get', dict(error=dict(category='api', reason='business_error',
                 retryable=False, message='task not exist or operator not in task')),
                 {'--task-id': 't1'}, exit=1),
        ], sources='todo', extra=['--todo-roles', 'executor'])
        todo = data['sources']['todo']
        item = todo['items'][0]
        self.assertEqual(item['read']['stopReason'], 'detail_not_supported_operator_not_in_task')
        self.assertTrue(item['content']['metadataOnly'])
        self.assertFalse(item['content']['detailSupported'])
        self.assertEqual(todo['coverage']['scope']['detailFallbacks'], 1)
        self.assertTrue(todo['coverage']['complete'])

    def test_documents_infer_types_and_fall_back_after_unsupported_fetch(self):
        _, data = self.run_cli([
            rule('drive +recent', dict(ok=True, data=dict(items=[
                dict(nodeId='doc', contentType='ALIDOC', docUrl='https://docs.example.test/note/edit?docId=1'),
                dict(nodeId='sheet', contentType='ALIDOC', docUrl='https://docs.example.test/spreadsheetv2/abc'),
                dict(nodeId='masked', contentType='ALIDOC', docUrl='https://docs.example.test/note/edit?docId=2')],
                hasMore=False), meta=dict(pagination=dict(endpoint_exhausted=True)))),
            rule('doc +fetch', dict(complete=True, content=dict(nodeId='doc', markdown='body')),
                 {'--node': 'doc'}),
            rule('doc +fetch', dict(error=dict(category='api', reason='unsupported',
                 retryable=False, message='钉钉表格暂不支持读取')), {'--node': 'masked'}, exit=1),
        ], sources='documents')
        documents = data['sources']['documents']
        items = {i['id']: i for i in documents['items']}
        self.assertEqual(items['doc']['content']['markdown'], 'body')
        self.assertEqual(items['sheet']['content']['contentType'], 'axls')
        self.assertTrue(items['sheet']['content']['metadataOnly'])
        self.assertEqual(items['masked']['content']['contentType'], 'axls')
        self.assertTrue(items['masked']['content']['metadataOnly'])
        self.assertEqual(len([c for c in self.calls if c[:2] == ['doc', '+fetch']]), 2)
        self.assertTrue(documents['coverage']['complete'])

    def test_update_completion_covers_retained_requested_sources(self):
        base, _ = self.run_cli([
            rule('calendar event list', dict(success=True, result=dict(events=[], hasMore=False))),
            rule('mail mailbox list', dict(success='true', emailAccounts=[
                dict(type='ORG', email='me@example.test')])),
            rule('mail message search', dict(error=dict(category='authentication', reason='expired',
                 retryable=False)), exit=1),
        ], sources='calendar,mail', expected=2)
        _, updated = self.run_cli([
            rule('calendar event list', dict(success=True, result=dict(events=[], hasMore=False))),
        ], mode='update', base=base, sources='calendar', end=LATER)
        meta = updated['meta']
        self.assertTrue(meta['selectedSourcesComplete'])
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['stopReason'], 'retained_sources_incomplete')
        self.assertEqual(meta['refreshedSources'], ['calendar'])
        self.assertEqual(set(meta['requestedSources']), {'calendar', 'mail'})

    def test_chat_total_timeout_preserves_saved_messages_and_pending_ledger(self):
        listing = rule('chat +chat-list', dict(chats=[dict(openConversationId=cid,
            conversationType='direct',chatMode='p2p') for cid in ('a','b')],
            complete=False,paginationKnown=False))
        def message(cid, delay=0):
            return rule('chat +chat-messages', dict(messages=[dict(messageId=cid,
                conversationId=cid,senderId='self',createTime='2026-10-14 01:00:00',text=cid)],
                complete=True), {'--open-conversation-id':cid}, delay=delay)
        _, data = self.run_cli([listing,
            rule('chat +category-list',dict(categories=[])),
            rule('chat +conversation-list-top',dict(conversations=[])),
            message('a'),message('b',5)], sources='chat',
            extra=['--self-id','self','--chat-timeout','3'], expected=2)
        chat = data['sources']['chat']
        self.assertEqual(chat['coverage']['stopReason'],'chat_timeout')
        self.assertEqual(chat['dataset']['conversations'][0]['messages'][0]['msgId'],'a')
        self.assertEqual({r['cid']:r['complete'] for r in chat['coverage']['reads']},
                         {'a':True,'b':False})

    def test_successful_retry_retains_partial_rows_and_latest_duplicate_revision(self):
        _, data = self.run_cli([
            rule('calendar event list', dict(error=dict(reason='temporary', retryable=True,
                retry_after_seconds=0), result=dict(events=[dict(id='a',start={},summary='old'),
                    dict(id='b',start={},summary='retained')])), exit=1, nth=1),
            rule('calendar event list', dict(success=True, result=dict(events=[
                dict(id='a',start={},summary='new')],hasMore=False)), nth=2),
        ], sources='calendar')
        items = data['sources']['calendar']['items']
        self.assertEqual({i['metadata']['id']: i['metadata']['summary'] for i in items},
                         {'a':'new','b':'retained'})
        self.assertTrue(data['sources']['calendar']['coverage']['complete'])

    def test_chat_adapter_preserves_current_dataset_and_incremental_roles(self):
        listing = rule('chat +chat-list', dict(chats=[dict(openConversationId='cid',
            name='peer', conversationType='direct', chatMode='p2p')], complete=False, paginationKnown=False))
        def message(uid, sender, when):
            return rule('chat +chat-messages', dict(messages=[dict(messageId=uid,
                conversationId='cid', senderId=sender, createTime=when, text=uid)], complete=True))
        base, data = self.run_cli([listing, message('m1', 'self', '2026-10-14 01:00:00')],
            sources='chat', extra=['--self-id', 'self'], expected=2)
        dataset = data['sources']['chat']['dataset']
        self.assertEqual(dataset['meta']['schemaVersion'], 2)
        self.assertEqual(dataset['conversations'][0]['messages'][0]['role'], 'self')
        _, updated = self.run_cli([message('m2', 'peer', '2026-10-15 01:00:00')],
            mode='update', base=base, end=LATER, extra=['--self-id', 'self', '--no-universe'], expected=2)
        messages = updated['sources']['chat']['dataset']['conversations'][0]['messages']
        self.assertEqual([(m['msgId'], m['role']) for m in messages], [('m1', 'self'), ('m2', 'peer')])
        reads = [c for c in self.calls if c[:2] == ['chat', '+chat-messages']]
        self.assertEqual(reads[0][reads[0].index('--start') + 1], '2026-10-15 00:00:00')

    def test_per_source_elapsed_time_lands_in_coverage_meta_and_index(self):
        out, data = self.run_cli([
            rule('calendar event list', dict(success=True, result=dict(events=[
                dict(id='c1', start=dict(dateTime=START), summary='meeting')], hasMore=False)), delay=0.3),
        ], sources='calendar')
        coverage = data['sources']['calendar']['coverage']
        self.assertGreaterEqual(coverage['elapsedMs'], 300)
        self.assertGreaterEqual(data['meta']['elapsedMs'], coverage['elapsedMs'])
        manifest = json.loads(out.with_suffix('.index.json').read_text())
        self.assertEqual(manifest['sources']['calendar']['coverage']['elapsedMs'], coverage['elapsedMs'])
        self.assertGreaterEqual(manifest['meta']['elapsedMs'], coverage['elapsedMs'])

    def test_failed_source_still_reports_elapsed_time(self):
        _, data = self.run_cli([
            rule('calendar event list', dict(success=True, result=dict(events=[
                dict(id='c1', start=dict(dateTime=START), summary='meeting')], hasMore=True)), delay=0.2),
        ], sources='calendar', expected=2)
        coverage = data['sources']['calendar']['coverage']
        self.assertFalse(coverage['complete'])
        self.assertGreaterEqual(coverage['elapsedMs'], 200)


    def test_worker_count_above_the_chat_cap_is_clamped_not_rejected(self):
        listing = rule('chat +chat-list', dict(chats=[dict(openConversationId='cid',
            name='peer', conversationType='direct', chatMode='p2p')], complete=True, paginationKnown=True))
        messages = rule('chat +chat-messages', dict(messages=[dict(messageId='m1',
            conversationId='cid', senderId='peer', createTime='2026-10-14 01:00:00', text='m1')],
            complete=True))
        _, data = self.run_cli([listing, messages], sources='chat', expected=2,
                               extra=['--self-id', 'self', '--workers', '16'])
        conversations = data['sources']['chat']['dataset']['conversations']
        self.assertEqual([m['msgId'] for m in conversations[0]['messages']], ['m1'])


if __name__ == '__main__':
    unittest.main()
