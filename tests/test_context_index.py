"""Offline CLI contracts for lightweight routing and lossless evidence retrieval."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/context_index.py'


class ContextIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / 'context.json'
        self.index = self.raw.with_suffix('.index.json')
        self.document = dict(id='same', metadata=dict(name='Design'),
                             content=dict(markdown='a' * 500 + 'needle in full body'),
                             contentStale=True, partialContent=dict(markdown='partial'),
                             read=dict(complete=False, stopReason='denied'))
        self.message = dict(msgId='m/1', text='exact original', role='peer', createTime='2026-10-14 10:00:00')
        self.data = dict(meta=dict(schemaVersion=3, complete=False), sources=dict(
            documents=dict(items=[self.document], coverage=dict(complete=False, failures=[dict(reason='denied')])),
            mail=dict(items=[dict(id='same', metadata=dict(subject='Other'))], coverage=dict(complete=True)),
            chat=dict(dataset=dict(meta={}, conversations=[dict(cid='c/1', title='Peer', messages=[self.message])]),
                      coverage=dict(complete=False, stopReason='directory_coverage_unproven'))))
        self.raw.write_text(json.dumps(self.data, ensure_ascii=False))

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_build_preserves_raw_and_reads_exact_source_scoped_evidence(self):
        original = self.raw.read_bytes()
        self.cli('build', '--input', str(self.raw))
        self.assertEqual(self.raw.read_bytes(), original)
        manifest = json.loads(self.index.read_text())
        self.assertEqual(manifest['meta']['rawSha256'], hashlib.sha256(original).hexdigest())
        self.assertFalse(manifest['sources']['documents']['coverage']['complete'])
        self.assertEqual(manifest['sources']['documents']['coverage']['failureCount'], 1)
        entry = manifest['sources']['documents']['entries'][0]
        self.assertTrue(entry['contentStale'])
        self.assertTrue(entry['hasPartialContent'])
        self.assertNotIn('needle', entry['preview'])
        result = self.cli('read', '--index', str(self.index), '--source', 'documents', '--id', 'same')
        self.assertEqual(json.loads(result.stdout)['record'], self.document)
        result = self.cli('read', '--index', str(self.index), '--source', 'chat', '--id', 'c/1', '--message-id', 'm/1')
        self.assertEqual(json.loads(result.stdout)['record'], dict(cid='c/1', message=self.message))
        self.cli('read', '--index', str(self.index), '--source', 'documents', '--id', 'missing', expected=1)
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)

    def test_search_reaches_omitted_content_and_reports_result_limit(self):
        self.cli('build', '--input', str(self.raw))
        result = self.cli('search', '--index', str(self.index), '--query', 'needle')
        self.assertEqual([(h['source'], h['id']) for h in json.loads(result.stdout)['matches']], [('documents', 'same')])
        result = self.cli('search', '--index', str(self.index), '--query', 'same', '--limit', '1')
        self.assertTrue(json.loads(result.stdout)['truncated'])
        self.assertEqual(json.loads(result.stdout)['totalMatches'], 2)

    def test_search_cites_message_level_evidence_in_chat(self):
        self.cli('build', '--input', str(self.raw))
        result = self.cli('search', '--index', str(self.index), '--query', 'EXACT original')
        hit = json.loads(result.stdout)['matches'][0]
        self.assertEqual((hit['source'], hit['id'], hit['field']), ('chat', 'c/1', 'text'))
        self.assertEqual((hit['msgId'], hit['createTime'], hit['role']),
                         ('m/1', '2026-10-14 10:00:00', 'peer'))
        self.assertIn('exact original', hit['snippet'])
        result = self.cli('search', '--index', str(self.index), '--query', 'peer')
        hit = json.loads(result.stdout)['matches'][0]
        self.assertEqual((hit['source'], hit['id'], hit['field'], hit['msgId']),
                         ('chat', 'c/1', 'conversation', None))
        result = self.cli('search', '--index', str(self.index), '--query', 'design')
        hit = json.loads(result.stdout)['matches'][0]
        self.assertEqual((hit['source'], hit['id'], hit['field']), ('documents', 'same', 'title'))

    def test_cite_returns_canonical_provenance_and_untruncated_text(self):
        long_text = 'x' * 400 + ' 完整结尾'
        self.data['sources']['chat']['dataset']['conversations'][0]['messages'].append(
            dict(msgId='m/long', text=long_text, role='self', createTime='2026-10-14 11:00:00'))
        self.raw.write_text(json.dumps(self.data, ensure_ascii=False))
        self.cli('build', '--input', str(self.raw))
        result = self.cli('cite', '--index', str(self.index), '--source', 'chat', '--id', 'c/1', '--message-id', 'm/long')
        out = json.loads(result.stdout)
        self.assertEqual(out['text'], long_text)
        self.assertNotIn('…', out['text'])
        self.assertEqual((out['msgId'], out['role']), ('m/long', 'self'))
        self.assertEqual(out['createTime'], '2026-10-14 11:00:00')
        self.assertIn('2026-10-14 11:00:00', out['citation'])
        self.assertIn('msg:m/long', out['citation'])
        self.assertIn('c/1', out['citation'])
        result = self.cli('cite', '--index', str(self.index), '--source', 'chat', '--id', 'c/1')
        out = json.loads(result.stdout)
        self.assertEqual((out['title'], out['messageCount'], out['firstTime']), ('Peer', 2, '2026-10-14 10:00:00'))
        result = self.cli('cite', '--index', str(self.index), '--source', 'documents', '--id', 'same')
        out = json.loads(result.stdout)
        self.assertEqual(out['title'], 'Design')
        self.assertIn('documents:same', out['citation'])
        self.cli('cite', '--index', str(self.index), '--source', 'chat', '--id', 'c/1', '--message-id', 'missing', expected=1)

    def test_week_rollup_merges_per_day_coverage_and_conversation_matrix(self):
        root = Path(self.tmp.name) / 'raw'
        for day in ('2026-10-12', '2026-10-13'):
            day_dir = root / day
            day_dir.mkdir(parents=True)
            (day_dir / 'context.json').write_text(json.dumps(self.data, ensure_ascii=False))
        self.data['sources']['chat']['dataset']['conversations'][0]['title'] = 'Renamed'
        (root / '2026-10-13' / 'context.json').write_text(json.dumps(self.data, ensure_ascii=False))
        result = self.cli('week', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14')
        rollup = json.loads(result.stdout)
        self.assertEqual([d['status'] for d in rollup['days']], ['ok', 'ok', 'missing'])
        self.assertEqual(rollup['days'][0]['sources']['chat']['messageCount'], 1)
        self.assertFalse(rollup['days'][0]['sources']['chat']['complete'])
        conv = rollup['conversations'][0]
        self.assertEqual((conv['id'], conv['totalMessages']), ('c/1', 2))
        self.assertEqual(conv['perDay'], {'2026-10-12': 1, '2026-10-13': 1})
        self.assertEqual(conv['title'], 'Peer')
        self.assertTrue((root / '2026-10-12' / 'context.index.json').exists())
        result = self.cli('week', '--root', str(root), '--start', '2026-10-14', '--end', '2026-10-12', expected=1)

    def test_read_merges_conversation_across_days(self):
        root = Path(self.tmp.name) / 'merge'
        day_one = dict(cid='c/1', title='Peer', messages=[
            dict(msgId='m/1', text='first day', role='peer', createTime='2026-10-12 10:00:00'),
            dict(msgId='m/2', text='overlap', role='self', createTime='2026-10-12 11:00:00')])
        day_two = dict(cid='c/1', title='Renamed', messages=[
            dict(msgId='m/2', text='overlap', role='self', createTime='2026-10-12 11:00:00'),
            dict(msgId='m/3', text='next day', role='peer', createTime='2026-10-13 09:00:00')])
        for day, conv in (('2026-10-12', day_one), ('2026-10-13', day_two)):
            day_dir = root / day
            day_dir.mkdir(parents=True)
            data = json.loads(json.dumps(self.data, ensure_ascii=False))
            data['sources']['chat']['dataset']['conversations'] = [conv]
            (day_dir / 'context.json').write_text(json.dumps(data, ensure_ascii=False))
        result = self.cli('read', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                          '--source', 'chat', '--id', 'c/1')
        out = json.loads(result.stdout)
        self.assertEqual(out['coverage']['days'], ['2026-10-12', '2026-10-13'])
        self.assertEqual(out['coverage']['missing'], ['2026-10-14'])
        self.assertEqual(out['coverage']['perDay'], {'2026-10-12': 2, '2026-10-13': 2})
        record = out['record']
        self.assertEqual(record['title'], 'Renamed')
        self.assertEqual(record['direction'], 'bidirectional')
        self.assertEqual([m['msgId'] for m in record['messages']], ['m/1', 'm/2', 'm/3'])
        self.assertEqual(out['coverage']['mergedMessageCount'], 3)
        result = self.cli('read', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                          '--source', 'chat', '--id', 'c/1', '--message-id', 'm/3')
        self.assertEqual(json.loads(result.stdout)['record'],
                         dict(cid='c/1', message=day_two['messages'][1]))
        self.cli('read', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                 '--source', 'chat', '--id', 'c/9', expected=1)
        self.cli('read', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                 '--index', str(self.index), '--source', 'chat', '--id', 'c/1', expected=1)
        self.cli('read', '--root', str(root), '--start', '2026-10-12',
                 '--source', 'chat', '--id', 'c/1', expected=1)
        self.cli('read', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                 '--source', 'documents', '--id', 'same', expected=1)

    def test_week_markdown_emits_embeddable_coverage_table(self):
        root = Path(self.tmp.name) / 'raw'
        for day in ('2026-10-12', '2026-10-13'):
            day_dir = root / day
            day_dir.mkdir(parents=True)
            (day_dir / 'context.json').write_text(json.dumps(self.data, ensure_ascii=False))
        result = self.cli('week', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-14',
                          '--markdown')
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], '数据源：raw/2026-10-12..2026-10-14（3 天，缺失 1，不可读 0）')
        self.assertTrue(lines[1].startswith('快照：2026-10-12@'), lines[1])
        self.assertIn('2026-10-13@', lines[1])
        self.assertEqual(lines[3], '| 日期 | chat消息 | chat会话 | mail | documents |')
        self.assertEqual(lines[4], '|---|---|---|---|---|')
        self.assertEqual(lines[5], '| 2026-10-12 | 1* | 1 | 1 | 1* |')
        self.assertEqual(lines[7], '| 2026-10-14 (missing) | — | — | — | — |')
        self.assertIn('complete=false', lines[-3])
        self.assertIn('快照 = 各日', lines[-1])
        result = self.cli('week', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-12',
                          '--markdown')
        self.assertEqual(result.stdout.splitlines()[0], '数据源：raw/2026-10-12（1 天，缺失 0，不可读 0）')

    def test_week_flags_window_overflow_past_day_boundary(self):
        root = Path(self.tmp.name) / 'overflow'
        windows = {'2026-10-12': dict(start='2026-10-12T00:00:00+08:00', end='2026-10-13T05:00:00+08:00'),
                   '2026-10-13': dict(start='2026-10-13T00:00:00+08:00', end='2026-10-14T00:00:00+08:00')}
        for day, window in windows.items():
            day_dir = root / day
            day_dir.mkdir(parents=True)
            data = json.loads(json.dumps(self.data, ensure_ascii=False))
            data['meta']['window'] = window
            (day_dir / 'context.json').write_text(json.dumps(data, ensure_ascii=False))
        result = self.cli('week', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-13')
        rollup = json.loads(result.stdout)
        self.assertEqual(rollup['days'][0]['windowOverflow'], '2026-10-13T05:00:00+08:00')
        self.assertNotIn('windowOverflow', rollup['days'][1])
        result = self.cli('week', '--root', str(root), '--start', '2026-10-12', '--end', '2026-10-13',
                          '--markdown')
        lines = result.stdout.splitlines()
        self.assertIn('⚠窗口→2026-10-13 05:00', lines[5])
        self.assertTrue(lines[6].startswith('| 2026-10-13 '), lines[6])
        self.assertIn('不是单日口径', lines[-2])

    def test_empty_private_conversations_are_indexed_readable_and_searchable(self):
        self.data['sources']['chat']['dataset']['meta'] = {'state': {'emptyConversations': [
            dict(cid='c/empty', title='Silent peer', kind='private_chat')]}}
        self.raw.write_text(json.dumps(self.data, ensure_ascii=False))
        self.cli('build', '--input', str(self.raw))
        manifest = json.loads(self.index.read_text())
        entries = {entry['id']: entry for entry in manifest['sources']['chat']['entries']}
        self.assertTrue(entries['c/empty']['empty'])
        self.assertEqual(entries['c/empty']['messageCount'], 0)
        result = self.cli('read', '--index', str(self.index), '--source', 'chat', '--id', 'c/empty')
        self.assertEqual(json.loads(result.stdout)['record']['cid'], 'c/empty')
        result = self.cli('search', '--index', str(self.index), '--query', 'Silent')
        self.assertIn(('chat', 'c/empty'),
                      [(hit['source'], hit['id']) for hit in json.loads(result.stdout)['matches']])

    def test_stale_index_is_rejected_and_can_be_rebuilt_without_raw_changes(self):
        self.cli('build', '--input', str(self.raw))
        self.data['sources']['documents']['items'][0]['content']['markdown'] = 'new revision'
        self.raw.write_text(json.dumps(self.data))
        changed = self.raw.read_bytes()
        result = self.cli('read', '--index', str(self.index), '--source', 'documents', '--id', 'same', expected=1)
        self.assertIn('stale', result.stderr)
        self.cli('build', '--input', str(self.raw))
        result = self.cli('read', '--index', str(self.index), '--source', 'documents', '--id', 'same')
        self.assertEqual(json.loads(result.stdout)['record']['content']['markdown'], 'new revision')
        self.assertEqual(self.raw.read_bytes(), changed)

    def test_build_refuses_output_alias_of_raw(self):
        self.index.symlink_to(self.raw)
        original = self.raw.read_bytes()
        self.cli('build', '--input', str(self.raw), expected=1)
        self.assertEqual(self.raw.read_bytes(), original)

    def test_batch_cite_and_read_match_single_record_calls(self):
        self.data['sources']['chat']['dataset']['conversations'][0]['messages'].append(
            dict(msgId='m/2', text='second', role='self', createTime='2026-10-14 11:00:00'))
        self.data['sources']['chat']['dataset']['conversations'].append(
            dict(cid='c/2', title='Other', messages=[
                dict(msgId='m/9', text='other thread', role='peer', createTime='2026-10-14 12:00:00')]))
        self.raw.write_text(json.dumps(self.data, ensure_ascii=False))
        self.cli('build', '--input', str(self.raw))
        singles = [json.loads(self.cli('cite', '--index', str(self.index), '--source', 'chat',
                                       '--id', 'c/1', '--message-id', mid).stdout)
                   for mid in ('m/1', 'm/2')]
        batch = json.loads(self.cli('cite', '--index', str(self.index), '--source', 'chat',
                                    '--id', 'c/1', '--all-messages').stdout)
        self.assertEqual((batch['requested'], batch['resolved']), (2, 2))
        self.assertEqual(batch['results'], singles)
        # A single request keeps the unwrapped record shape.
        single = json.loads(self.cli('cite', '--index', str(self.index), '--source', 'chat',
                                     '--id', 'c/1', '--message-id', 'm/1').stdout)
        self.assertNotIn('results', single)
        self.assertEqual(single, singles[0])
        whole_source = json.loads(self.cli('cite', '--index', str(self.index),
                                           '--source', 'mail', '--all').stdout)
        self.assertEqual([item['id'] for item in whole_source['results']], ['same'])
        reads = json.loads(self.cli('read', '--index', str(self.index), '--source', 'chat',
                                    '--id', 'c/1', '--id', 'c/2').stdout)
        self.assertEqual([item['record']['cid'] for item in reads['results']], ['c/1', 'c/2'])

    def test_batch_isolates_unresolvable_ids_and_rejects_ambiguous_selections(self):
        self.cli('build', '--input', str(self.raw))
        batch = json.loads(self.cli('cite', '--index', str(self.index), '--source', 'documents',
                                    '--id', 'same', '--id', 'missing', expected=1).stdout)
        self.assertEqual((batch['requested'], batch['resolved']), (2, 1))
        self.assertEqual(batch['results'][0]['citation'], 'documents:same | Design')
        self.assertEqual(batch['results'][1], dict(source='documents', id='missing',
                                                   error='record ID not found or not unique in source'))
        index = str(self.index)
        for args in (
                ('cite', '--index', index, '--source', 'documents', '--all', '--id', 'same'),
                ('cite', '--index', index, '--source', 'documents', '--id', 'same', '--all-messages'),
                ('cite', '--index', index, '--source', 'chat', '--id', 'c/1', '--id', 'c/1',
                 '--message-id', 'm/1'),
                ('cite', '--index', index, '--source', 'documents'),
                ('read', '--root', self.tmp.name, '--start', '2026-10-14', '--end', '2026-10-14',
                 '--source', 'chat', '--id', 'c/1', '--id', 'c/2'),
        ):
            self.cli(*args, expected=1)

    def test_week_rollup_exposes_elapsed_time_without_changing_the_table(self):
        self.data['sources']['documents']['coverage']['elapsedMs'] = 4321
        self.data['meta']['elapsedMs'] = 9999
        self.data['sources']['chat']['coverage']['timings'] = dict(
            identityMs=5, discoveryMs=111, readPhaseMs=222, networkWaitMs=180,
            checkpointMs=33, conversations=4)
        root = Path(self.tmp.name) / 'timed'
        (root / '2026-10-14').mkdir(parents=True)
        (root / '2026-10-14' / 'context.json').write_text(json.dumps(self.data, ensure_ascii=False))
        rollup = json.loads(self.cli('week', '--root', str(root), '--start', '2026-10-14',
                                     '--end', '2026-10-14').stdout)
        day = rollup['days'][0]
        self.assertEqual(day['collectMs'], 9999)
        self.assertEqual(day['sources']['documents']['elapsedMs'], 4321)
        self.assertIsNone(day['sources']['mail']['elapsedMs'])
        self.assertEqual(day['sources']['chat']['timings'], dict(
            identityMs=5, discoveryMs=111, readPhaseMs=222, networkWaitMs=180,
            checkpointMs=33, conversations=4))
        self.assertIsNone(day['sources']['mail']['timings'])
        lines = self.cli('week', '--root', str(root), '--start', '2026-10-14', '--end', '2026-10-14',
                         '--markdown').stdout.splitlines()
        self.assertEqual(lines[3], '| 日期 | chat消息 | chat会话 | mail | documents |')
        self.assertNotIn('4321', '\n'.join(lines))
        self.assertNotIn('discoveryMs', '\n'.join(lines))
