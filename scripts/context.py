#!/usr/bin/env python3
"""Collect and refresh personal DingTalk context, with per-source failure ledgers."""
import argparse
import datetime as dt
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import chats
from context_index import index_path, write_index
from sources.common import (CONTEXT_SOURCES, Client, acquire_window_lock, collection, failed)

SOURCES = CONTEXT_SOURCES
DEFAULTS = dict(calendarId='primary', todoRoles='creator,executor,participant', todoStatus='all',
                todoWindow='snapshot', approvalRoles='pending,submitted,executed,cc', minutesScope='all', mailbox=None)
OPTIONS = dict(calendarId='calendar_id', todoRoles='todo_roles', todoStatus='todo_status',
               todoWindow='todo_window', approvalRoles='approval_roles', minutesScope='minutes_scope', mailbox='mailbox')
SOURCE_OPTIONS = dict(calendar=('calendarId',), todo=('todoRoles', 'todoStatus', 'todoWindow'),
                      approval=('approvalRoles',), minutes=('minutesScope',), mail=('mailbox',), documents=())


def timestamp(value, timezone):
    value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone) if value.tzinfo is None else value.astimezone(timezone)


def profile_identity(client, selector):
    data, state = client.read(['profile', 'list'])
    if not state['complete']:
        raise ValueError('cannot establish account identity: ' + state['stopReason'])
    accounts = collection(data, ('profiles',))
    if selector:
        matches = [a for a in accounts if a.get('profile') == selector]
    else:
        current = data.get('currentProfile')
        matches = [a for a in accounts if a.get('profile') == current] if current else [
            a for a in accounts if a.get('isCurrent') is True]
    if len(matches) != 1:
        raise ValueError('specify a unique stable --profile from dws profile list')
    account = matches[0]
    if not all(account.get(k) for k in ('profile', 'corpId', 'userId')):
        raise ValueError('profile has no stable account and organization IDs')
    return {k: str(account[k]) for k in ('profile', 'corpId', 'userId')}


def chat_collect(client, window, config, previous, args, timezone):
    old = previous.get('dataset')
    with tempfile.TemporaryDirectory(prefix='dws-personal-context-chat-') as tmp:
        output = Path(tmp) / 'out.json'
        cmd = [sys.executable, str(Path(__file__).with_name('chats.py')), 'update' if old else 'collect',
               '--end', timestamp(window['end'], timezone).strftime('%Y-%m-%d %H:%M:%S'),
               '--out', str(output), '--retries', str(args.retries), '--page-size', str(args.page_size),
               '--page-limit', str(args.page_limit), '--message-page-limit', str(args.page_limit),
               '--message-workers', str(min(8, args.workers)),
               '--timezone', timezone.key, '--timeout', str(args.timeout)]
        if args.self_id:
            cmd += ['--self-id', args.self_id]
        if old:
            base = Path(tmp) / 'base.json'
            chats.write(base, old)
            cmd += ['--base', str(base)]
            if args.only_failed or timestamp(old['meta']['window']['end'], timezone) == timestamp(window['end'], timezone):
                cmd += ['--only-failed']
        else:
            cmd += ['--start', timestamp(window['start'], timezone).strftime('%Y-%m-%d %H:%M:%S')]
        if args.no_universe and old:
            cmd += ['--no-universe']
        for known in args.known_chats:
            cmd += ['--known-chats', known]
        env = dict(os.environ, DWS_PROFILE=client.profile, TZ=timezone.key)
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                  timeout=args.chat_timeout)
        except subprocess.TimeoutExpired:
            if output.exists():
                data = json.loads(output.read_text())
                return dict(dataset=data, coverage=dict(data['meta']['coverage'], **failed('chat_timeout'),
                            reads=data['meta']['state'].get('reads', []), window=window),
                            checkpoint=previous.get('checkpoint', {}),
                            lastAttemptAt=dt.datetime.now(timezone).isoformat(timespec='seconds'))
            return dict(previous, coverage=dict(failed('chat_timeout'), window=window),
                        lastAttemptAt=dt.datetime.now(timezone).isoformat(timespec='seconds'))
        if proc.returncode in (0, 2) and output.exists():
            with open(output) as stream:
                data = json.load(stream)
            meta = data['meta']
            coverage = dict(meta.get('coverage', meta), window=meta['window'], scope='private_chat')
            if 'state' in meta:
                coverage['reads'] = meta['state'].get('reads', [])
            return dict(dataset=data, coverage=coverage,
                        checkpoint=dict(windowEnd=data['meta']['window']['end']))
        if output.exists():
            data = json.loads(output.read_text())
            return dict(dataset=data, coverage=dict(data['meta']['coverage'],
                        **failed('chat_collection_failed', exitCode=proc.returncode),
                        reads=data['meta']['state'].get('reads', []), window=window),
                        checkpoint=previous.get('checkpoint', {}))
        return dict(previous, coverage=dict(failed('chat_collection_failed', exitCode=proc.returncode), window=window))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('auto', 'collect', 'update'))
    p.add_argument('--base')
    p.add_argument('--force-full', action='store_true', help='auto mode: collect afresh even if output exists')
    p.add_argument('--out', help='default ~/.qoder/dws-personal-context/raw/<window start day>/context.json')
    p.add_argument('--sources', help='comma-separated source names; default all for collect, baseline sources for update')
    p.add_argument('--start', help='ISO8601 or local YYYY-MM-DD HH:MM:SS')
    p.add_argument('--end')
    p.add_argument('--timezone', help='IANA timezone; default Asia/Shanghai')
    p.add_argument('--profile', help='stable corpId:userId selector from dws profile list')
    p.add_argument('--calendar-id')
    p.add_argument('--todo-roles')
    p.add_argument('--todo-status', choices=('all', 'true', 'false'))
    p.add_argument('--todo-window', choices=('snapshot', 'due'))
    p.add_argument('--approval-roles')
    p.add_argument('--minutes-scope', choices=('mine', 'shared', 'all'))
    p.add_argument('--mailbox', help='own mailbox; otherwise automatically resolve a unique mailbox')
    p.add_argument('--page-size', type=int, default=100)
    p.add_argument('--page-limit', type=int, default=50)
    p.add_argument('--max-items', type=int, default=500)
    p.add_argument('--retries', type=int, default=3)
    p.add_argument('--timeout', type=int, default=60, help='per DWS request timeout in seconds')
    p.add_argument('--chat-timeout', type=int, default=1800, help='whole chat collector timeout in seconds')
    p.add_argument('--workers', type=int, default=4, help='bounded parallel detail reads')
    p.add_argument('--only-failed', action='store_true', help='update incomplete sources at their original ranges')
    p.add_argument('--known-chats', action='append', default=[])
    p.add_argument('--self-id', help='verified chat sender ID; same meaning as in chats.py')
    p.add_argument('--no-universe', action='store_true', help='chat update: disable new conversation discovery')
    return p


def main():
    p = parser()
    args = p.parse_args()
    if args.mode == 'auto':
        if args.base:
            p.error('--base requires update; auto mode uses --out as its baseline')
        args.out = args.out or chats.default_out('context.json', args.start, args.timezone or 'Asia/Shanghai')
        if Path(args.out).exists() and not args.force_full:
            args.mode, args.base = 'update', args.out
        else:
            args.mode = 'collect'
        print('[dws-personal-context] mode=%s out=%s' % (args.mode, args.out), file=sys.stderr)
    elif args.force_full and args.mode == 'update':
        p.error('--force-full only applies to a fresh collect')
    if args.mode == 'update' and not args.base:
        p.error('update requires --base')
    if args.only_failed and args.mode != 'update':
        p.error('--only-failed requires update')
    if not (1 <= args.page_size <= 100 and 1 <= args.page_limit <= 500 and
            1 <= args.workers <= 16 and args.retries > 0 and args.timeout > 0 and
            args.chat_timeout > 0 and args.max_items > 0):
        p.error('invalid limits')
    if args.mode == 'update':
        with open(args.base, encoding='utf-8') as stream:
            baseline = json.load(stream)
    else:
        baseline = dict(meta={}, sources={})
    bm = baseline.get('meta', {})
    if args.mode == 'update' and (bm.get('schemaVersion') != 3 or not isinstance(baseline.get('sources'), dict)):
        p.error('update requires a schemaVersion=3 context baseline')
    timezone = ZoneInfo(args.timezone or bm.get('timezone', 'Asia/Shanghai'))
    if bm.get('timezone') and timezone.key != bm['timezone']:
        p.error('timezone differs from baseline; create a new collection')
    now = dt.datetime.now(timezone).replace(microsecond=0)
    start = timestamp(args.start or bm.get('window', {}).get('start', now.replace(hour=0, minute=0, second=0).isoformat()), timezone)
    end = timestamp(args.end or (bm['window']['end'] if args.only_failed else now.isoformat()), timezone)
    if start >= end:
        p.error('start must precede end')
    if args.mode == 'update':
        if start != timestamp(bm['window']['start'], timezone):
            p.error('update preserves baseline start; use a new collect for another window')
        if end < timestamp(bm['window']['end'], timezone):
            p.error('end cannot precede baseline end')
        if args.only_failed and end != timestamp(bm['window']['end'], timezone):
            p.error('--only-failed preserves the baseline end')
    window = dict(start=start.isoformat(timespec='seconds'), end=end.isoformat(timespec='seconds'))
    existing_sources = list(dict.fromkeys(list(baseline['sources']) + bm.get('requestedSources', [])))
    selected = list(dict.fromkeys((args.sources or ','.join(existing_sources) or ','.join(SOURCES)).split(',')))
    if not selected or any(name not in SOURCES for name in selected):
        p.error('unknown or empty --sources; available: ' + ','.join(SOURCES))
    if args.only_failed and any(name not in baseline['sources'] for name in selected):
        p.error('--only-failed cannot add a new source')
    # Completion must keep covering sources retained from earlier updates.
    requested = list(dict.fromkeys(list(bm.get('requestedSources', [])) +
                                   list(baseline['sources']) + selected))
    config = dict(DEFAULTS, **bm.get('config', {}))
    for key, flag in OPTIONS.items():
        value = getattr(args, flag)
        if value is not None:
            for source, keys in SOURCE_OPTIONS.items():
                if source in baseline['sources'] and key in keys and value != config[key] and config[key] is not None:
                    p.error('changing source scope requires a new collect: --' + flag.replace('_', '-'))
            config[key] = value
    if not set(config['approvalRoles'].split(',')) <= {'pending', 'submitted', 'executed', 'cc'}:
        p.error('invalid approval roles')
    if not config['approvalRoles'] or not config['todoRoles'] or not set(config['todoRoles'].split(',')) <= {'creator', 'executor', 'participant'}:
        p.error('invalid source roles')
    args.out = args.out or chats.default_out('context.json', window['start'], timezone.key)
    target = Path(args.out).resolve()
    if args.mode == 'collect' and target.exists() and not args.force_full:
        raise ValueError('output already exists for this window: %s; rerun collect-context.sh to extend it '
                         'as an update, or pass --force-full to collect afresh' % target)
    sidecar = index_path(target)
    if sidecar.resolve() == target or (sidecar.exists() and target.exists() and sidecar.samefile(target)):
        p.error('index output aliases the raw file; choose a different --out')
    lock_handle = acquire_window_lock(target)
    client = Client(retries=args.retries, timeout=args.timeout, workers=args.workers)
    identity = profile_identity(client, args.profile)
    if bm.get('identity') and identity != bm['identity']:
        p.error('baseline belongs to another account or organization')
    args.identity = identity
    client.profile = identity['profile']
    output = dict(baseline)
    output['sources'] = dict(baseline['sources'])
    limits = dict(size=args.page_size, pages=args.page_limit, items=args.max_items)
    attempted = []
    run_started = time.monotonic()

    def covered(names):
        return bool(names) and all(
            output['sources'].get(name, {}).get('coverage', {}).get('complete') is True
            for name in names)

    def save_progress(complete=False, stop_reason='collection_in_progress'):
        output['meta'] = dict(schemaVersion=3, identity=identity, timezone=timezone.key, window=window,
                              config=config, generatedAt=now.isoformat(timespec='seconds'),
                              requestedSources=requested, refreshedSources=list(attempted),
                              selectedSources=list(selected), complete=complete,
                              selectedSourcesComplete=covered(selected), stopReason=stop_reason,
                              elapsedMs=int((time.monotonic() - run_started) * 1000))
        chats.write(args.out, output)
        write_index(args.out)
    for name in selected:
        previous = output['sources'].get(name, {})
        if args.only_failed and previous.get('coverage', {}).get('complete') is True:
            continue
        source_window = window
        if args.only_failed:
            source_window = previous.get('coverage', {}).get('window', window)
        started = time.monotonic()
        try:
            if name == 'chat':
                result = chat_collect(client, source_window, config, previous, args, timezone)
            else:
                adapter = importlib.import_module('sources.' + name)
                result = adapter.collect(client, source_window, config, previous, limits)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = dict(previous, coverage=dict(failed('source_collection_failed',
                         exceptionType=type(exc).__name__), window=source_window))
        # Per-source wall time is the only in-artifact answer to "which source was slow".
        result = dict(result, coverage=dict(result.get('coverage') or {},
                                            elapsedMs=int((time.monotonic() - started) * 1000)))
        history = list(previous.get('failureHistory', []))
        prior = previous.get('coverage', {})
        if prior.get('failures') or prior.get('failedReads'):
            summary = {k: prior[k] for k in ('window', 'scope', 'failures', 'failedReads', 'fetchedAt') if k in prior}
            if summary not in history:
                history.append(summary)
        if history:
            result['failureHistory'] = history
        output['sources'][name] = result
        attempted.append(name)
        coverage = result['coverage']
        chat_meta = result.get('dataset', {}).get('meta', {})
        count = len(result.get('items', [])) if name != 'chat' else chat_meta.get('stats', {}).get('messages', chat_meta.get('messages', 0))
        print('[dws-personal-context] source=%s items=%d complete=%s elapsed_ms=%d' %
              (name, count, coverage.get('complete') is True, coverage.get('elapsedMs', 0)), file=sys.stderr)
        # Checkpoint each completed source atomically, including partial results.
        save_progress()
    selected_complete = covered(selected)
    # Retained sources may refer to older windows; file-level completion requires all requested sources.
    file_complete = covered(requested)
    stop_reason = ('ok' if file_complete else
                   'retained_sources_incomplete' if selected_complete else 'partial_sources')
    save_progress(file_complete, stop_reason)
    print('[dws-personal-context] selected_complete=%s complete=%s raw=%s index=%s' %
          (selected_complete, file_complete, args.out, index_path(Path(args.out).resolve())))
    return 0 if selected_complete else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print('[dws-personal-context] failed: %s' % exc, file=sys.stderr)
        sys.exit(1)
