#!/usr/bin/env python3
"""Collect, update and merge Agent-facing private dialogues."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from sources.common import (MISSING_COLLECTION_MESSAGE, TERMINAL_ERROR_CATEGORIES,
                            acquire_window_lock, dws_command, error_info)

os.umask(0o077)

DATA_ROOT = Path.home() / '.qoder' / 'dws-personal-context'

def now(timezone='Asia/Shanghai'):
    return dt.datetime.now(ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M:%S')

def ms_since(start):
    return int((time.monotonic() - start) * 1000)

DAY_KEY = re.compile(r'\d{4}-\d{2}-\d{2}')

def default_out(name, day=None, timezone='Asia/Shanghai'):
    """~/.qoder/dws-personal-context/raw/<day>/<name>; ignores QODER_CONFIG_DIR so a dev override cannot split reads and writes."""
    key = (day or '')[:10]
    if not DAY_KEY.fullmatch(key):
        key = now(timezone)[:10]
    root = DATA_ROOT / 'raw' / key
    root.mkdir(parents=True, exist_ok=True)
    return str(root / name)

def load(path):
    with open(path) as f:
        return decode_dataset(json.load(f))

def decode_dataset(data):
    """Load the public Agent format into the collector's in-memory representation."""
    meta = data.get('meta', {})
    if meta.get('format') != 'ctxring-dialogues' or meta.get('schemaVersion') != 2:
        raise ValueError('unsupported dialogue format; requires ctxring-dialogues schemaVersion=2')
    state = meta.get('state')
    if (not isinstance(state, dict) or not isinstance(state.get('self'), dict) or not state['self']
            or not isinstance(state.get('reads'), list)
            or not isinstance(state.get('emptyConversations'), list)
            or not isinstance(meta.get('coverage'), dict)
            or not isinstance(data.get('conversations'), list)):
        raise ValueError('dialogue file requires complete continuation state')
    if any(not isinstance(c, dict) or not isinstance(c.get('cid'), str) or not c['cid']
           or not isinstance(c.get('messages'), list) or 'msgs' in c
           for c in data['conversations']):
        raise ValueError('dialogue conversations require cid and messages')
    coverage = meta.get('coverage', {})
    internal_meta = dict(coverage, schemaVersion=2,
                         conversations=coverage.get('knownConversations', 0),
                         messages=meta.get('stats', {}).get('messages', 0),
                         self=state['self'], window=meta['window'],
                         generatedAt=meta.get('generatedAt'), reads=state.get('reads', []))
    conversations = [dict(cid=c['cid'], title=c.get('title') or '?', singleChat=True,
                          kind=c.get('kind', 'private_chat'), msgs=c.get('messages', []))
                     for c in data['conversations']]
    conversations += [dict(c, singleChat=True, msgs=[]) for c in state.get('emptyConversations', [])]
    if len({c['cid'] for c in conversations}) != len(conversations):
        raise ValueError('duplicate conversation in dialogue continuation state')
    return dict(meta=internal_meta, conversations=conversations)


def write(path, data):
    target = Path(path).resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.dws-personal-context-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def recoverable_message_shape(args, key, data, error):
    """Narrow read-only recovery for the observed DWS v1.0.63 missing-list bug."""
    failures = data.get('failures') if isinstance(data, dict) else None
    return (key == 'messages' and args[0] == '+chat-messages'
            and '--open-conversation-id' in args
            and data.get('complete') is False
            and data.get('stopReason') == 'response_shape_error'
            and error.get('category') == 'api'
            and error.get('reason') == 'chat_messages_incomplete'
            and error.get('origin') == 'mcp_gateway'
            and error.get('operation') == 'chat/list_conversation_message_v2'
            and not error.get('server_error_code') and not error.get('rpc_code')
            and isinstance(failures, list) and bool(failures)
            and all(isinstance(f, dict) and f.get('stage') == 'response_shape'
                    and f.get('error') == MISSING_COLLECTION_MESSAGE
                    for f in failures))


def call(args, key, retries, recover_shape=False, timeout=60):
    items, history = {}, []
    latest = None
    for attempt in range(retries):
        try:
            proc = subprocess.run(dws_command(['chat', *args]), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc = subprocess.CompletedProcess([], 1, '', json.dumps({
                'error': {'reason': 'timeout', 'retryable': True}}))
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = None
        try:
            diagnostic = json.loads(proc.stderr)
        except ValueError:
            diagnostic = {}
        if not isinstance(diagnostic, dict):
            diagnostic = {}
        error = (data.get('error') if isinstance(data, dict) else None) or diagnostic.get('error', {})
        error = error or error_info(data) or error_info(diagnostic)
        if not isinstance(error, dict):
            error = {}
        usable = isinstance(data, dict) and isinstance(data.get(key), list)
        if usable:
            latest = dict(data)
            invalid_rows = any(not isinstance(item, dict) for item in data[key])
            if invalid_rows:
                latest.update(complete=False, stopReason='invalid_collection',
                              failures=[{'reason': 'invalid_collection'}])
            for item in data[key]:
                if not isinstance(item, dict):
                    continue
                # Retain messages from earlier partial attempts, even if a later attempt fails.
                identity = (item.get('messageId') or item.get('openMessageId')) if key == 'messages' else item.get('openConversationId')
                identity = (item.get('conversationId', ''), identity) if identity else json.dumps(item, sort_keys=True)
                items[identity] = item
        else:
            latest = {key: [], 'complete': False,
                      'stopReason': 'timeout' if error.get('reason') == 'timeout' else 'read_failure',
                      'failures': [{'reason': error.get('reason', 'no_usable_response'),
                                    'category': error.get('category'), 'exitCode': proc.returncode}]}
        if proc.returncode != 0 or error:
            latest['complete'] = False
            if not latest.get('stopReason'):
                latest['stopReason'] = error.get('reason', 'command_failed')
            if not latest.get('failures'):
                latest['failures'] = [{'reason': error.get('reason', 'command_failed')}]
        stop = latest.get('stopReason')
        history.append(dict(attempt=attempt + 1, exitCode=proc.returncode,
                            complete=latest.get('complete') is True,
                            stopReason=stop, failures=latest.get('failures', [])))
        if error:
            # Keep diagnostic provenance without copying potentially sensitive CLI payloads.
            history[-1]['error'] = {k: error[k] for k in
                                   ('category', 'reason', 'retryable', 'origin', 'operation',
                                    'stage', 'execution_started', 'trace_id') if k in error}
        terminal = error.get('category') in TERMINAL_ERROR_CATEGORIES
        retryable = (error.get('retryable') is True or
                     (error.get('retryable') is not False and
                      (stop in ('response_shape_error', 'read_failure') or
                       (not usable and proc.returncode == 0))))
        shape_recovery = (recover_shape and usable and
                          recoverable_message_shape(args, key, data, error))
        if shape_recovery and not terminal:
            retryable = True
            history[-1]['recovery'] = {'policy': 'ctxring_missing_collection_read_retry',
                                       'retryScheduled': attempt + 1 < retries}
        # Repeating a terminal directory page cannot invent a missing continuation.
        stalled_directory = (key == 'chats' and usable and stop == 'pagination_error'
                             and latest.get('paginationKnown') is False
                             and latest.get('nextCursor') in (None, 0, '0', ''))
        if stalled_directory:
            history[-1]['retrySuppressed'] = 'missing_directory_continuation'
        if latest.get('complete') is True or terminal or not retryable or stalled_directory or attempt + 1 >= retries:
            break
        print('[ctxring] retry attempt=%d stop=%s' % (attempt + 1, stop), file=sys.stderr)
        delay = error.get('retry_after_seconds', 1)
        try:
            delay = float(delay)
        except (TypeError, ValueError):
            delay = 1.0
        if not 0 <= delay <= 60:
            delay = 1.0
        time.sleep(delay)
    latest[key] = list(items.values())
    if 'count' in latest:
        latest['count'] = len(latest[key])
    latest['attempts'] = history
    latest['attemptsUsed'] = len(history)
    if any('recovery' in a for a in history):
        latest['recoveredWithinCall'] = latest.get('complete') is True
    return latest

def ledger(data):
    fields = ('complete', 'hasMore', 'paginationKnown', 'nextPage', 'nextPageToken',
              'nextCursor', 'pagesFetched', 'stopReason', 'failures', 'truncated',
              'truncatedByPageLimit', 'truncatedByResultLimit', 'recoveredWithinCall')
    result = {k: data[k] for k in fields if k in data}
    attempts = data.get('attempts', [])
    if len(attempts) > 1 or any(a.get('complete') is not True for a in attempts):
        result['attempts'] = attempts
    return result


def rfc3339(value, timezone):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.isoformat(timespec='seconds')


def search_message_candidates(start, end, timezone, page_limit, retries, timeout,
                              max_invocations=6):
    """Page message search to discover direct conversations the directory omits."""
    pages = min(max(page_limit, 1), 40)
    # One invocation pages up to `pages` times server-side (~1.4s/page observed, 55-70s
    # for a full 40-page run), so a bare `timeout` (default 60s) fires on normal tail
    # latency and forfeits the whole paged attempt; scale the bound to the page budget.
    invocation_timeout = max(timeout, int(pages * 2.5) + 10)
    args = ['+search-msg', '--conversation-type', 'direct',
            '--start', rfc3339(start, timezone), '--end', rfc3339(end, timezone),
            '--order', 'asc', '--page-all', '--page-limit', str(pages),
            '--limit', '100', '--no-reactions']
    candidates, probes, seen_cursors = {}, [], set()
    cursor, complete = None, False
    for invocation in range(1, max_invocations + 1):
        command = args + (['--cursor', cursor] if cursor else [])
        response = call(command, 'messages', retries, timeout=invocation_timeout)
        for message in response['messages']:
            cid = message.get('conversationId')
            if cid:
                candidates.setdefault(cid, dict(conversationId=cid))
        probes.append(dict(ledger(response), invocation=invocation, cursorUsed=cursor is not None))
        if response.get('complete') is True and response.get('hasMore') is not True:
            complete = True
            break
        next_cursor = response.get('nextCursor')
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    result = dict(complete=complete, invocations=len(probes),
                  candidates=len(candidates), probes=probes,
                  stopReason=None if complete else 'message_search_incomplete')
    return candidates, result


def verify_private_candidate(cid, timeout):
    try:
        proc = subprocess.run(dws_command(['chat', 'conversation-info',
                                           '--conversation-id', cid]),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, dict(complete=False, stopReason='timeout')
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    try:
        diagnostic = json.loads(proc.stderr)
    except ValueError:
        diagnostic = {}
    error = error_info(data) or error_info(diagnostic)
    if proc.returncode != 0 or not isinstance(data, dict) or error:
        return None, dict(complete=False, stopReason=(error.get('reason') if error else 'read_failure'))
    result = data.get('result')
    info = result.get('conversationInfo') if isinstance(result, dict) else None
    if not isinstance(info, dict):
        return None, dict(complete=False, stopReason='missing_conversation_info')
    if info.get('openConversationId') != cid:
        return None, dict(complete=False, stopReason='conversation_identity_mismatch')
    if info.get('singleChat') is not True:
        return None, dict(complete=False, stopReason='not_a_private_chat')
    if info.get('memberCount') not in (None, 2):
        return None, dict(complete=False, stopReason='unexpected_member_count')
    return dict(openConversationId=cid, conversationType='direct', chatMode='p2p',
                name=info.get('title') or '', singleChat=True,
                memberCount=info.get('memberCount')), dict(complete=True)


def enrich_with_message_search(chats, start, end, timezone, page_limit, retries, timeout):
    candidates, summary = search_message_candidates(
        start, end, timezone, page_limit, retries, timeout)
    missing = [cid for cid in candidates if cid not in chats]
    verified, reasons = 0, {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(missing)))) as pool:
        results = list(pool.map(lambda cid: (cid, *verify_private_candidate(cid, timeout)), missing))
    for cid, chat, state in results:
        if chat:
            chats[cid] = chat
            verified += 1
        else:
            reason = state.get('stopReason', 'verification_failed')
            reasons[reason] = reasons.get(reason, 0) + 1
    summary.update(alreadyKnown=len(candidates) - len(missing), verifiedPrivate=verified,
                   rejectedCandidates=sum(reasons.values()), rejectionReasons=reasons)
    return summary


def discover_chats(page_size, page_limit, retries, supplement=True, timeout=60,
                   start=None, end=None, timezone='Asia/Shanghai'):
    from functools import partial
    read = partial(call, timeout=timeout)
    args = ['+chat-list', '--types', 'p2p', '--page-size', str(page_size),
            '--page-all', '--page-limit', str(page_limit)]
    primary = read(args, 'chats', retries)
    results = [('unfiltered', primary)]
    # Keep the unfiltered result: excluding muted conversations is only a supplement.
    if supplement and (primary.get('complete') is not True or primary.get('paginationKnown') is not True):
        results.append(('exclude_muted_supplement', read(args + ['--exclude-muted'], 'chats', retries)))
    if supplement:
        categories = read(['+category-list'], 'categories', retries)
        results.append(('categories', categories))
        seen_categories = set()
        for category in categories['categories']:
            category_id = category.get('categoryId') if isinstance(category, dict) else None
            if (not isinstance(category_id, int) or isinstance(category_id, bool)
                    or category_id <= 0 or category_id in seen_categories):
                continue
            seen_categories.add(category_id)
            results.append(('category:%s' % category_id, read(
                ['+category-list-conversations', '--category-id', str(category_id)],
                'conversations', retries)))
        cursor, seen_cursors = None, set()
        for page in range(page_limit):
            top_args = ['+conversation-list-top', '--type', 'direct', '--limit', str(page_size)]
            if cursor is not None:
                top_args += ['--cursor', str(cursor)]
            top = read(top_args, 'conversations', retries)
            results.append(('top:%d' % (page + 1), top))
            if top.get('hasMore') is not True:
                break
            next_cursor = top.get('nextCursor')
            if (not isinstance(next_cursor, int) or isinstance(next_cursor, bool)
                    or next_cursor <= 0 or next_cursor in seen_cursors):
                top.update(complete=False, stopReason='missing_directory_continuation')
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            if top.get('hasMore') is True:
                top.update(complete=False, stopReason='page_limit', truncatedByPageLimit=True)
    chats, probes, rejected = {}, [], 0
    for source, response in results:
        valid, added = 0, 0
        entries = response.get('chats', response.get('conversations', []))
        for c in entries:
            is_primary = source in ('unfiltered', 'exclude_muted_supplement')
            direct = (isinstance(c, dict) and c.get('conversationType') == 'direct'
                      and (c.get('chatMode') == 'p2p' if is_primary
                           else c.get('singleChat') is True))
            if not direct or not c.get('openConversationId'):
                rejected += 1
                continue
            c = dict(c, chatMode='p2p', name=c.get('name') or c.get('conversationName') or '')
            valid += 1
            cid = c['openConversationId']
            if cid not in chats:
                chats[cid] = c
                added += 1
        probes.append(dict(ledger(response), source=source, returnedPrivateConversations=valid,
                           addedConversations=added))
    search = None
    if supplement and start and end:
        search = enrich_with_message_search(chats, start, end, timezone,
                                            page_limit, retries, timeout)
    directory_complete = primary.get('complete') is True and primary.get('paginationKnown') is True
    discovery = dict(ledger(primary), scope='combined_private_directories', coverageComplete=False,
                     probes=probes, returnedPrivateConversations=len(chats), rejectedEntries=rejected,
                     directoryComplete=directory_complete, messageSearch=search,
                     note='Directory and message-search supplements cannot prove full private-chat coverage.')
    discovery['complete'] = directory_complete
    discovery.pop('attempts', None)
    discovery['pagesFetched'] = sum(r.get('pagesFetched', 0) or 0 for _, r in results)
    return list(chats.values()), discovery


def coverage_summary(meta):
    result = {k: meta[k] for k in ('window', 'complete', 'stopReason', 'generatedAt',
                                  'knownConversationsComplete') if k in meta}
    failed = list(meta.get('failedReads', [])) + [{k: r[k] for k in ('cid', 'start', 'end', 'complete', 'stopReason', 'failures')
               if k in r} for r in meta.get('reads', []) if r.get('complete') is not True]
    if failed:
        result['failedReads'] = failed
    return result


def history_summaries(meta):
    history = []
    for key in ('priorCoverage', 'sourceCoverage'):
        for prior in meta.get(key, []):
            history.extend(history_summaries(prior))
    history.append(coverage_summary(meta))
    return list({json.dumps(x, sort_keys=True): x for x in history}.values())


def command_json(args, timeout=60):
    try:
        p = subprocess.run(dws_command(args),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('self identity lookup timed out') from exc
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = None
    if p.returncode != 0 or not isinstance(data, dict):
        raise ValueError('self identity lookup failed')
    return data


def resolve_self(explicit=None, timeout=60):
    if explicit:
        return {'senderIds': [explicit], 'source': 'explicit --self-id'}
    me = command_json(['contact', '+me'], timeout).get('data') or {}
    if not isinstance(me, dict) or not me.get('userId') or not me.get('name'):
        raise ValueError('current account has no stable identity')
    search = command_json(['aisearch', 'person', '--query', me['name'], '--dimension', 'name'], timeout)
    candidates = search.get('result') or []
    if not isinstance(candidates, list):
        raise ValueError('cannot map current account: invalid search result')
    matches = [c for c in candidates if isinstance(c, dict) and c.get('userId') == me['userId']]
    if len(matches) != 1 or not matches[0].get('openDingTalkId'):
        raise ValueError('cannot map current account to a unique message sender ID')
    return dict(userId=me['userId'], openDingTalkId=matches[0]['openDingTalkId'],
                name=me['name'], org=me.get('org'), source='contact +me / aisearch userId match')


def self_ids(identity):
    return set(identity.get('senderIds', [])) | {identity[k] for k in ('userId', 'openDingTalkId') if identity.get(k)}


def validate_identity(identity, datasets):
    for data in datasets:
        prior = data.get('meta', {}).get('self')
        if prior and (any(prior.get(k) and identity.get(k) and prior[k] != identity[k]
                          for k in ('userId', 'openDingTalkId')) or
                      not self_ids(prior).intersection(self_ids(identity)) or
                      (prior.get('org') and identity.get('org') and prior['org'] != identity['org'])):
            raise ValueError('datasets belong to different self identities or organizations')


def annotate_roles(data, identity):
    ids = self_ids(identity)
    if not ids:
        raise ValueError('missing self sender ID')
    for c in data['conversations']:
        for msg in c['msgs']:
            if not msg.get('senderId'):
                raise ValueError('message missing senderId; cannot safely assign self/peer')
            msg['role'] = 'self' if msg['senderId'] in ids else 'peer'
    data['meta']['self'] = identity
    data['meta']['schemaVersion'] = 2

def canonical(m):
    result = dict(msgId=m.get('msgId') or m.get('messageId') or m.get('openMessageId') or '',
                  createTime=m.get('createTime'), sender=m.get('sender'),
                  senderId=m.get('senderId') or m.get('senderOpenDingTalkId') or '',
                  text=m.get('text') or m.get('content') or '')
    if m.get('contentFlags'):
        result['contentFlags'] = list(m['contentFlags'])
    if m.get('sendType') is not None:
        result['sendType'] = m['sendType']
    return result

def merge_convs(datasets):
    convs = {}
    for data in datasets:
        for c in data.get('conversations', []):
            if c.get('singleChat') is False:
                raise ValueError('dataset contains an explicitly non-private conversation')
            cid = c.get('cid')
            if not cid:
                raise ValueError('conversation is missing cid')
            dest = convs.setdefault(cid, dict(cid=cid, title=c.get('title') or '?',
                                            singleChat=True, kind=c.get('kind', 'private_chat'), msgs=[]))
            if c.get('kind') == 'self_notes':
                dest['kind'] = 'self_notes'
            dest['msgs'].extend(canonical(m) for m in c.get('msgs', []))
    for c in convs.values():
        unique = {}
        for m in c['msgs']:
            key = m['msgId'] or (m['createTime'], m['senderId'] or m['sender'], m['text'])
            unique[key] = m
        c['msgs'] = sorted(unique.values(), key=lambda m: m['createTime'] or '')
    return sorted(convs.values(), key=lambda c: -len(c['msgs']))

def dataset_timezone(explicit, datasets):
    zones = {d.get('meta', {}).get('window', {}).get('timezone') for d in datasets}
    zones.discard(None)
    if len(zones) > 1 or (explicit and zones and explicit not in zones):
        raise ValueError('timezone differs between datasets; collect a new window instead')
    timezone = explicit or next(iter(zones), 'Asia/Shanghai')
    ZoneInfo(timezone)
    return timezone


def collection_dataset(base, fetched, reads, discovery, window_start, end, args):
    bm = base.get('meta', {})
    reads_complete = bool(reads) and all(r.get('complete') is True for r in reads)
    meta = dict(window=dict(start=window_start, end=end), generatedAt=now(args.timezone),
                sources=sorted(set(bm.get('sources', []) + ['private directory + per-conversation messages'])),
                complete=False, stopReason=('no_private_conversations_discovered' if not reads else
                            'directory_coverage_unproven' if reads_complete else 'partial_read_failures'),
                discovery=discovery, reads=reads, knownConversationsComplete=reads_complete,
                priorCoverage=history_summaries(bm) if args.mode == 'update' else [])
    # Directory discovery does not prove global coverage.
    data = dict(meta=meta, conversations=merge_convs([base, dict(conversations=fetched)]))
    if args.mode == 'update':
        meta['base'] = args.base
        meta['addedByThisUpdate'] = sum(len(c['msgs']) for c in data['conversations']) - sum(
            len(c.get('msgs', [])) for c in base['conversations'])
    return data


def save_dialogues(data, identity, args):
    data['meta']['conversations'] = len(data['conversations'])
    data['meta']['messages'] = sum(len(c['msgs']) for c in data['conversations'])
    annotate_roles(data, identity)
    from export_dialogues import export
    notes = set(args.self_notes_cid) | {c['cid'] for c in data['conversations'] if c.get('kind') == 'self_notes'}
    write(args.out, export(data, args.timezone, notes))


def merge_reads(inputs):
    by_cid = {}
    for source in inputs:
        for r in source.get('meta', {}).get('reads', []):
            by_cid.setdefault(r['cid'], []).append(r)
    reads = []
    for cid, states in by_cid.items():
        incomplete = [r for r in states if r.get('complete') is not True]
        state = dict(ledger(max(states, key=lambda r: r.get('end', ''))),
                     cid=cid, start=min(r['start'] for r in states), end=max(r['end'] for r in states))
        if incomplete:
            state.update(complete=False, start=min(r['start'] for r in incomplete),
                         stopReason='partial_source_reads')
        reads.append(state)
    return reads


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['collect', 'update', 'merge', 'auto'])
    p.add_argument('files', nargs='*')
    p.add_argument('--base')
    p.add_argument('--self-id', help='explicitly verified current-user message sender ID')
    p.add_argument('--start')
    p.add_argument('--end')
    p.add_argument('--out', help='default ~/.qoder/dws-personal-context/raw/<window start day>/dialogues.json; merge uses merged.json')
    p.add_argument('--timezone', help='IANA timezone; default baseline timezone or Asia/Shanghai')
    p.add_argument('--timeout', type=int, default=60, help='per DWS request timeout in seconds')
    p.add_argument('--self-notes-cid', action='append', default=[])
    p.add_argument('--limit', type=int, default=100, help='message page size')
    p.add_argument('--page-size', type=int, default=100, help='directory page size (1-100)')
    p.add_argument('--page-limit', type=int, default=50, help='directory page cap')
    p.add_argument('--message-page-limit', type=int, default=50)
    p.add_argument('--message-workers', type=int, default=4,
                   help='bounded parallel +chat-messages reads (1-8); requests overlap, checkpoints stay serial')
    p.add_argument('--retries', type=int, default=3)
    p.add_argument('--force-full', action='store_true', help='auto/collect: collect afresh even if the output exists')
    p.add_argument('--no-universe', action='store_true')
    p.add_argument('--no-directory-supplement', action='store_true', help='disable supplementary discovery (exclude-muted, categories, top, message search)')
    p.add_argument('--known-chats', action='append', default=[], help='same-account dataset to seed known private CIDs; never imports its messages')
    p.add_argument('--only-failed', action='store_true', help='update only failed baseline ranges; skip directory discovery')
    a = p.parse_intermixed_args()
    if a.only_failed:
        if a.mode != 'update':
            p.error('--only-failed only applies to update')
        a.no_universe = True
    if a.force_full and a.mode not in ('collect', 'auto'):
        p.error('--force-full only applies to a fresh collect')
    if not (1 <= a.page_size <= 100 and 1 <= a.page_limit <= 500
            and 1 <= a.message_page_limit <= 500 and a.limit > 0 and a.retries > 0 and a.timeout > 0
            and 1 <= a.message_workers <= 8):
        p.error('invalid page size, page limit or retry count')
    if a.mode == 'auto':
        # Same-day rerun without a mode word extends the existing day file as an update.
        if a.base:
            p.error('--base requires update; auto mode uses --out as its baseline')
        a.out = a.out or default_out('dialogues.json', a.start, a.timezone or 'Asia/Shanghai')
        if Path(a.out).exists() and not a.force_full:
            a.mode, a.base = 'update', a.out
        else:
            a.mode = 'collect'
        print('[ctxring] mode=%s out=%s' % (a.mode, a.out), file=sys.stderr)
    if a.mode == 'merge':
        if not a.files:
            p.error('merge requires input files')
        inputs = [load(f) for f in a.files]
        a.timezone = dataset_timezone(a.timezone, inputs)
        identities = [d.get('meta', {}).get('self') for d in inputs if d.get('meta', {}).get('self')]
        identity = resolve_self(a.self_id, a.timeout) if a.self_id else identities[0]
        validate_identity(identity, inputs)
        if a.self_id and identities:
            identity = max([identity, *identities], key=lambda x: len(self_ids(x)))
        windows = [d.get('meta', {}).get('window', {}) for d in inputs]
        # Multiple complete inputs do not prove that the interval between them is covered.
        intervals = sorted((w['start'], w['end']) for w in windows if w.get('start') and w.get('end'))
        gap = len(intervals) != len(inputs)
        edge = intervals[0][1] if intervals else ''
        for start, end in intervals[1:]:
            gap = gap or start > edge
            edge = max(edge, end)
        complete = not gap and all(d.get('meta', {}).get('complete') is True for d in inputs)
        meta = dict(sources=a.files, generatedAt=now(a.timezone), complete=complete,
                    stopReason='ok' if complete else 'partial_sources_or_window_gap',
                    sourceCoverage=list({json.dumps(x, sort_keys=True): x for d in inputs
                                         for x in history_summaries(d.get('meta', {}))}.values()))
        if intervals:
            meta['window'] = dict(start=intervals[0][0], end=max(e for _, e in intervals))
        meta['reads'] = merge_reads(inputs)
        a.out = a.out or default_out('merged.json', meta.get('window', {}).get('start'), a.timezone)
        lock_handle = acquire_window_lock(Path(a.out).resolve())
        data = dict(meta=meta, conversations=merge_convs(inputs))
    else:
        if a.mode == 'update' and not a.base:
            p.error('update requires --base')
        base = load(a.base) if a.mode == 'update' else dict(meta={}, conversations=[])
        bm = base.get('meta', {})
        a.timezone = dataset_timezone(a.timezone, [base])
        phase = time.monotonic()
        identity = resolve_self(a.self_id, a.timeout)
        timings = dict(identityMs=ms_since(phase))
        validate_identity(identity, [base])
        if a.self_id and bm.get('self') and len(self_ids(bm['self'])) > len(self_ids(identity)):
            identity = bm['self']
        start = a.start or (bm.get('window', {}).get('end') if a.mode == 'update'
                            else now(a.timezone)[:10] + ' 00:00:00')
        if not start:
            p.error('base has no window.end; specify --start')
        end = a.end or now(a.timezone)
        def parse_time(value):
            return dt.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        if parse_time(start) > parse_time(end) or (parse_time(start) == parse_time(end) and not a.only_failed):
            p.error('start must precede end')
        window_start = bm.get('window', {}).get('start') or start
        a.out = a.out or default_out('dialogues.json', window_start, a.timezone)
        if a.mode == 'collect' and Path(a.out).exists() and not a.force_full:
            raise ValueError('output already exists for this window: %s; rerun export-dialogues.sh to extend it '
                             'as an update, or pass --force-full to collect afresh' % a.out)
        lock_handle = acquire_window_lock(Path(a.out).resolve())
        targets = {c['cid']: dict(cid=c['cid'], title=c.get('title') or '?',
                                 singleChat=True, msgs=[]) for c in merge_convs([base])}
        if a.only_failed:
            discovery = bm.get('discovery') or dict(complete=False, stopReason='not_probed', coverageComplete=False)
        elif a.no_universe:
            if a.mode != 'update':
                p.error('--no-universe only applies to update')
            discovery = dict(complete=False, stopReason='disabled', scope='known_conversations')
        else:
            phase = time.monotonic()
            directory, discovery = discover_chats(a.page_size, a.page_limit, a.retries,
                                                   not a.no_directory_supplement, a.timeout,
                                                   window_start, end, a.timezone)
            timings['discoveryMs'] = ms_since(phase)
            for c in directory:
                cid = c['openConversationId']
                targets.setdefault(cid, dict(cid=cid, title=c.get('name') or '?', singleChat=True, msgs=[]))
        known_ids = {c['cid'] for c in base['conversations']}
        for path in a.known_chats:
            seed = load(path)
            if not seed.get('meta', {}).get('self'):
                raise ValueError('known-chats dataset must contain a verified meta.self identity')
            validate_identity(identity, [seed])
            for c in merge_convs([seed]):
                known_ids.add(c['cid'])
                targets.setdefault(c['cid'], dict(cid=c['cid'], title=c['title'], singleChat=True, msgs=[]))
        if not a.only_failed:
            discovered_ids = {c['openConversationId'] for c in directory} if not a.no_universe else set()
            discovery['knownConversationsRetained'] = len(known_ids - discovered_ids)
            discovery['totalTargets'] = len(targets)
        previous = {r['cid']: r for r in bm.get('reads', [])}
        base_ids = {b['cid'] for b in base['conversations']}
        gap = bool(a.mode == 'update' and bm.get('window', {}).get('end')
                   and start > bm['window']['end'])
        if a.only_failed:
            if not isinstance(bm.get('reads'), list):
                raise ValueError('base has no read ledger; use a regular update before --only-failed')
            targets = {cid: c for cid, c in targets.items()
                       if cid in previous and previous[cid].get('complete') is not True}
            reads = [r for r in bm.get('reads', []) if r['cid'] not in targets]
        else:
            reads = []
        fetched = []
        output_end = bm.get('window', {}).get('end', end) if a.only_failed else end
        pending = {cid: dict(cid=cid, start=previous.get(cid, {}).get('start') or window_start,
                             end=previous.get(cid, {}).get('end') if a.only_failed else end,
                             complete=False, stopReason='not_attempted') for cid in targets}
        # Resolve every range before requesting anything, so a bad range aborts without partial writes.
        ranges = {}
        for cid in targets:
            prior = previous.get(cid, {})
            read_start = start if cid in base_ids else window_start
            if gap or (a.mode == 'update' and bm.get('complete') is not True and not prior):
                read_start = window_start
            if prior and prior.get('complete') is not True:
                read_start = min(read_start, prior.get('start') or window_start)
            read_end = prior.get('end') if a.only_failed else end
            if not read_end or parse_time(read_start) >= parse_time(read_end):
                raise ValueError('invalid failed range; refusing to skip or silently truncate it')
            ranges[cid] = (read_start, read_end)

        def fetch(cid):
            """Read one conversation; returns rows instead of mutating shared state."""
            read_start, read_end = ranges[cid]
            started = time.monotonic()
            response = call(['+chat-messages', '--open-conversation-id', cid, '--start', read_start,
                             '--end', read_end, '--order', 'asc', '--limit', str(a.limit), '--page-all',
                             '--page-limit', str(a.message_page_limit), '--no-reactions'], 'messages',
                            a.retries, recover_shape=True, timeout=a.timeout)
            duration = ms_since(started)
            mismatch = any(m.get('conversationId') not in (None, '', cid) for m in response['messages'])
            state = dict(ledger(response), cid=cid, start=read_start, end=read_end)
            if mismatch:
                state.update(complete=False, stopReason='conversation_mismatch')
                return state, None, duration
            return state, [canonical(m) for m in response['messages']], duration

        order = list(targets)
        phase = time.monotonic()
        waited = checkpointed = fetched_ms = 0
        with ThreadPoolExecutor(max_workers=min(a.message_workers, max(1, len(order)))) as pool:
            futures = {}
            for position, cid in enumerate(order):
                # Requests run ahead of the checkpoint so ledger order and per-conversation
                # durability stay exactly what a serial run would have written.
                for ahead in order[position:position + a.message_workers]:
                    if ahead not in futures:
                        futures[ahead] = pool.submit(fetch, ahead)
                started = time.monotonic()
                state, messages, one_ms = futures.pop(cid).result()
                waited += ms_since(started)
                fetched_ms += one_ms
                c = targets[cid]
                if messages is not None:
                    c['msgs'] = messages
                reads.append(state)
                fetched.append(c)
                pending.pop(cid)
                started = time.monotonic()
                checkpoint = collection_dataset(base, fetched, [*reads, *pending.values()],
                                                discovery, window_start, output_end, a)
                # Retain unattempted private CIDs so an interrupted run can resume them.
                checkpoint['conversations'] = merge_convs([checkpoint, dict(
                    conversations=[targets[key] for key in pending])])
                save_dialogues(checkpoint, identity, a)
                checkpointed += ms_since(started)
                print('[ctxring] read messages=%d complete=%s' % (len(c['msgs']), state.get('complete') is True), file=sys.stderr)
        # fetchMs is the serial-equivalent request cost; readPhaseMs is the wall clock paid,
        # so fetchMs/readPhaseMs measures the overlap actually achieved.
        timings.update(readPhaseMs=ms_since(phase), networkWaitMs=waited, checkpointMs=checkpointed,
                       fetchMs=fetched_ms, conversations=len(order))
        data = collection_dataset(base, fetched, reads, discovery, window_start, output_end, a)
        data['meta']['timings'] = timings
    save_dialogues(data, identity, a)
    print('[ctxring] complete=%s conversations=%d messages=%d out=%s' %
          (data['meta']['complete'], data['meta']['conversations'], data['meta']['messages'], a.out))
    return 0 if data['meta']['complete'] else 2

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print('[ctxring] failed: %s' % exc, file=sys.stderr)
        sys.exit(1)
