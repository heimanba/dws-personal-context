"""Distribution guard and rejection of unsupported input formats."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import chats
from audit_distribution import audit


class DistributionTests(unittest.TestCase):
    def test_distribution_contains_only_reviewed_sources(self):
        self.assertEqual(audit(ROOT), [])

    def test_audit_detects_personal_paths_private_urls_and_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'example.md').write_text(
                '/' + 'Users' + '/testperson/file ' + 'https://' + 'private.invalid/item ' +
                'cid' + 'aB7' * 9 + ' user' + '@' + 'private.invalid')
            (root / 'raw').mkdir()
            (root / 'raw' / 'data.json').write_text('{}')
            codes = {item['code'] for item in audit(root)}
            self.assertTrue({'personal_absolute_path', 'non_public_url',
                             'non_synthetic_reference', 'non_synthetic_email', 'runtime_artifact'} <= codes)

    def test_noncurrent_dialogue_formats_are_rejected(self):
        for data in ({'meta': {}, 'conversations': []},
                     {'meta': {'format': 'unsupported', 'schemaVersion': 2}, 'conversations': []},
                     {'meta': {'format': chats.LEGACY_DIALOGUES_FORMAT, 'schemaVersion': 1},
                      'conversations': []},
                     {'meta': {'format': chats.DIALOGUES_FORMAT, 'schemaVersion': 2},
                      'conversations': []}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                chats.decode_dataset(data)

    def test_legacy_format_still_decodes_for_existing_files(self):
        data = {'meta': {'format': chats.LEGACY_DIALOGUES_FORMAT, 'schemaVersion': 2,
                         'coverage': {}, 'state': {'self': {'senderIds': ['self']},
                         'reads': [], 'emptyConversations': []},
                         'window': {'start': '2026-10-13 00:00:00',
                                    'end': '2026-10-14 00:00:00'}},
                'conversations': [{'cid': 'cidSyntheticExample', 'messages': []}]}
        chats.decode_dataset(data)

    def test_current_header_does_not_admit_noncurrent_conversation_fields(self):
        data = {'meta': {'format': chats.DIALOGUES_FORMAT, 'schemaVersion': 2,
                        'coverage': {}, 'state': {'self': {'senderIds': ['self']},
                        'reads': [], 'emptyConversations': []}},
                'conversations': [{'cid': 'cidSyntheticExample', 'msgs': []}]}
        with self.assertRaises(ValueError):
            chats.decode_dataset(data)

    def test_removed_cli_options_do_not_convert_or_write_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps({'meta': {'self': {'senderIds': ['self']},
                'window': {'start': '2026-10-13 00:00:00', 'end': '2026-10-14 00:00:00'}},
                'conversations': []}))
            out = Path(tmp) / 'out.json'
            for script, flag in (('export-dialogues.sh', '--input'), ('collect-context.sh', '--chat-base')):
                result = subprocess.run(['bash', str(ROOT / 'scripts' / script), flag,
                                         str(source), '--out', str(out)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unrecognized arguments', result.stderr)
                self.assertFalse(out.exists())
