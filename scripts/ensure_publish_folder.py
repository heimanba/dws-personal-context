#!/usr/bin/env python3
"""解析钉钉固定发布文件夹：在「我的文档」根目录存在同名文件夹则复用，否则创建。

只用于用户明确要求写回 digest 的新建路径。目录无法完整读取时不创建，
避免在“是否存在”未证明时产生重复文件夹；多个同名文件夹交回调用方消歧。
不使用 drive +create-folder：它建在钉盘个人空间，会在「我的文档」里表现为
“我的文件（来自钉盘）”下面的子目录，而不是「我的文档」根目录。
"""
import argparse
import json
import os
import subprocess
import sys

DEFAULT_NAME = '日报'
MAX_PAGES = 20


class FolderError(RuntimeError):
    pass


def dws(args):
    command = [os.environ.get('DWS_BIN', 'dws'), *args, '--format', 'json']
    profile = os.environ.get('DWS_PROFILE')
    if profile:
        command += ['--profile', profile]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise FolderError('dws_failed: ' + (result.stderr.strip() or result.stdout.strip()))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FolderError('dws_output_not_json: ' + str(error))


def my_workspace(run=dws):
    payload = run(['wiki', 'space', 'list', '--type', 'myWikiSpace'])
    spaces = payload.get('wikiSpaces') if isinstance(payload, dict) else None
    if not isinstance(spaces, list) or len(spaces) != 1 or not isinstance(spaces[0], dict):
        raise FolderError('my_wiki_space_not_unique')
    workspace = spaces[0].get('workspaceId')
    if not isinstance(workspace, str) or not workspace:
        raise FolderError('missing_workspace_id')
    return workspace


def list_root_nodes(workspace, run=dws):
    nodes = []
    cursor = None
    for _ in range(MAX_PAGES):
        args = ['wiki', 'node', 'list', '--workspace', workspace, '--limit', '50']
        if cursor:
            args += ['--cursor', cursor]
        payload = run(args)
        batch = payload.get('nodes') if isinstance(payload, dict) else None
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise FolderError('missing_collection')
        nodes.extend(batch)
        has_more = payload.get('hasMore')
        if has_more is False:
            return nodes
        if has_more is not True:
            raise FolderError('root_list_incomplete')
        cursor = payload.get('nextPageToken')
        if not isinstance(cursor, str) or not cursor:
            raise FolderError('root_list_incomplete')
    raise FolderError('root_list_page_cap')


def select_folder(nodes, name):
    matches = [item for item in nodes
               if (item.get('nodeType') or '').lower() == 'folder'
               and item.get('name') == name
               and isinstance(item.get('nodeId'), str) and item['nodeId']]
    if len(matches) > 1:
        raise FolderError('ambiguous_folder: ' + ', '.join(item['nodeId'] for item in matches))
    return matches[0]['nodeId'] if matches else None


def create_folder(workspace, name, run=dws):
    payload = run(['wiki', 'node', 'create', '--workspace', workspace,
                   '--type', 'folder', '--name', name])
    if not isinstance(payload, dict):
        raise FolderError('create_unverified')
    node = payload.get('nodeId')
    if (payload.get('success') is not True
            or (payload.get('nodeType') or '').lower() != 'folder'
            or not isinstance(node, str) or not node):
        raise FolderError('create_unverified')
    return node


def resolve(name=DEFAULT_NAME, run=dws, *, dry_run=False):
    if not name or len(name) > 50:
        raise FolderError('invalid_folder_name')
    workspace = my_workspace(run)
    node = select_folder(list_root_nodes(workspace, run), name)
    if node:
        return {'name': name, 'nodeId': node, 'created': False}
    if dry_run:
        return {'name': name, 'nodeId': None, 'created': False,
                'wouldCreate': True, 'workspaceId': workspace}
    return {'name': name, 'nodeId': create_folder(workspace, name, run), 'created': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='解析或创建钉钉「我的文档」根目录下的固定发布文件夹')
    parser.add_argument('--name', default=DEFAULT_NAME)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(resolve(args.name), ensure_ascii=False))
    except FolderError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
