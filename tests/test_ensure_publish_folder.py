"""ensure_publish_folder contracts: root-only reuse, ambiguity stop, create fallback."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ensure_publish_folder.py'
sys.path.insert(0, str(SCRIPT.parent))
import ensure_publish_folder as epf


def spaces(workspace='WS'):
    return {'hasMore': False, 'success': True,
            'wikiSpaces': [{'workspaceId': workspace, 'name': '我的文档'}]}


def page(*nodes, has_more=False, token=None):
    payload = {'success': True, 'nodes': list(nodes), 'hasMore': has_more}
    if token:
        payload['nextPageToken'] = token
    return payload


def folder(name, node):
    return {'nodeType': 'folder', 'name': name, 'nodeId': node}


def dispatch(list_handler, create_handler):
    def run(args):
        if args[:3] == ['wiki', 'space', 'list']:
            return spaces()
        if args[:3] == ['wiki', 'node', 'list']:
            return list_handler(args)
        if args[:3] == ['wiki', 'node', 'create']:
            return create_handler(args)
        raise AssertionError('unexpected call: %r' % (args,))
    return run


class EnsurePublishFolderTests(unittest.TestCase):
    def test_reuses_exact_folder_at_root(self):
        calls = []

        def run(args):
            calls.append(args)
            if args[:3] == ['wiki', 'space', 'list']:
                return spaces()
            if args[:3] == ['wiki', 'node', 'list']:
                return page(folder('日报', 'F1'), folder('工作日报', 'F2'),
                            {'nodeType': 'file', 'name': '日报', 'nodeId': 'D1'})
            raise AssertionError('create must not be called')

        self.assertEqual(epf.resolve('日报', run),
                         {'name': '日报', 'nodeId': 'F1', 'created': False})
        self.assertEqual(calls[0][:3], ['wiki', 'space', 'list'])
        self.assertEqual(calls[1][:3], ['wiki', 'node', 'list'])

    def test_paginates_until_complete(self):
        def list_handler(args):
            if '--cursor' in args:
                return page(folder('日报', 'F1'))
            return page(folder('资料', 'F0'), has_more=True, token='T1')

        run = dispatch(list_handler, lambda args: self.fail('create must not be called'))
        self.assertEqual(epf.resolve('日报', run),
                         {'name': '日报', 'nodeId': 'F1', 'created': False})

    def test_creates_when_missing(self):
        calls = []

        def create_handler(args):
            calls.append(args)
            return {'success': True, 'nodeType': 'folder',
                    'nodeId': 'NEW', 'folderId': 'ROOT'}

        run = dispatch(lambda args: page(folder('资料', 'F1')), create_handler)
        self.assertEqual(epf.resolve('日报', run),
                         {'name': '日报', 'nodeId': 'NEW', 'created': True})
        self.assertEqual(calls[0][:3], ['wiki', 'node', 'create'])
        self.assertIn('--workspace', calls[0])
        self.assertIn('WS', calls[0])

    def test_ambiguous_stops_without_creating(self):
        run = dispatch(lambda args: page(folder('日报', 'F1'), folder('日报', 'F2')),
                       lambda args: self.fail('create must not be called'))
        with self.assertRaises(epf.FolderError) as ctx:
            epf.resolve('日报', run)
        self.assertIn('ambiguous_folder', str(ctx.exception))

    def test_incomplete_listing_does_not_create(self):
        run = dispatch(lambda args: {'success': True, 'nodes': []},
                       lambda args: self.fail('create must not be called'))
        with self.assertRaises(epf.FolderError) as ctx:
            epf.resolve('日报', run)
        self.assertIn('root_list_incomplete', str(ctx.exception))

    def test_missing_cursor_does_not_create(self):
        run = dispatch(lambda args: page(folder('资料', 'F1'), has_more=True),
                       lambda args: self.fail('create must not be called'))
        with self.assertRaises(epf.FolderError) as ctx:
            epf.resolve('日报', run)
        self.assertIn('root_list_incomplete', str(ctx.exception))

    def test_non_success_create_is_error(self):
        run = dispatch(lambda args: page(),
                       lambda args: {'success': True, 'nodeType': 'folder', 'nodeId': ''})
        with self.assertRaises(epf.FolderError) as ctx:
            epf.resolve('日报', run)
        self.assertIn('create_unverified', str(ctx.exception))

    def test_cli_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'dws'
            fake.write_text(
                '#!/bin/sh\n'
                'case "$*" in\n'
                '  *"wiki space list"*) '
                "printf '%s' '{\"success\":true,\"wikiSpaces\":[{\"workspaceId\":\"WS1\"}]}' ;;\n"
                '  *"wiki node list"*) '
                "printf '%s' '{\"success\":true,\"nodes\":[{\"nodeType\":\"folder\","
                "\"name\":\"日报\",\"nodeId\":\"F1\"}],\"hasMore\":false}' ;;\n"
                '  *) exit 1 ;;\n'
                'esac\n',
                encoding='utf-8')
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = dict(os.environ, DWS_BIN=str(fake))
            env.pop('DWS_PROFILE', None)
            result = subprocess.run([sys.executable, str(SCRIPT), '--name', '日报'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'name': '日报', 'nodeId': 'F1', 'created': False})


if __name__ == '__main__':
    unittest.main()
