"""Bounded DWS reads, conservative pagination and incremental reconciliation."""
import datetime as dt
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from redact import redact_obj

TERMINAL_ERROR_CATEGORIES = ('authentication', 'auth', 'permission', 'validation', 'confirmation')

CONTEXT_SOURCES = ('chat', 'calendar', 'todo', 'approval', 'minutes', 'mail', 'documents')

# CLI error strings pinned to observed dws 1.0.63 behavior (verified 2026-10-14).
# They match CLI output verbatim; when the CLI wording changes they must be updated
# together or the recovery/degradation paths silently stop firing.
MISSING_COLLECTION_MESSAGE = '下游缺少明确的集合字段，无法判定结果为空'
OPERATOR_NOT_IN_TASK = 'operator not in task'
SPREADSHEET_MARKERS = ('axls', '钉钉表格', 'notable')
AI_TABLE_MARKERS = ('ai表格', 'ai 表格')


def dws_command(args, profile=None):
    command = [os.environ.get('DWS_BIN', 'dws'), *args, '--format', 'json']
    profile = profile or os.environ.get('DWS_PROFILE')
    if profile:
        command += ['--profile', profile]
    return command


def acquire_window_lock(target):
    """Serialize collections writing the same output; the kernel releases the lock if a run is killed."""
    lock_path = Path(str(target) + '.lock')
    handle = open(lock_path, 'w')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError('another dws-personal-context collection holds ' + str(lock_path) +
                         '; wait for it to finish instead of running a second collection')
    return handle


def containers(data):
    if not isinstance(data, dict):
        return []
    return [data] + [data[k] for k in ('data', 'result') if isinstance(data.get(k), dict)]


def field(data, key, default=None):
    for obj in containers(data):
        if key in obj:
            return obj[key]
    return default


def collection(data, keys):
    for obj in containers(data):
        for key in keys:
            if key in obj:
                value = obj[key]
                if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                    raise ValueError('invalid_collection')
                return value
    raise ValueError('missing_collection')


def object_at(data, paths):
    for path in paths:
        value = data
        for key in path.split('.'):
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, dict):
            return value
    raise ValueError('missing_detail')


def identifier(item, keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    raise ValueError('missing_stable_id')


def failed(reason, **extra):
    return dict(complete=False, stopReason=reason, failures=[dict(reason=reason, **extra)])


def parse_time(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:
            number /= 1000
        try:
            return dt.datetime.fromtimestamp(number, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_time(int(text))
        try:
            return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def _aware(value):
    parsed = parse_time(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def in_presence_window(item, presence_window):
    if not presence_window:
        return True
    value = (item.get('metadata') or {}).get(presence_window.get('field', 'receivedDateTime'))
    when = _aware(value)
    start = _aware(presence_window.get('start'))
    end = _aware(presence_window.get('end'))
    if when is None or start is None or end is None:
        # An undated row cannot be excluded from the listing scope safely.
        return True
    return start <= when < end


def error_info(data):
    for obj in containers(data):
        error = obj.get('error')
        if isinstance(error, dict) and error:
            info = {k: error[k] for k in ('category', 'reason', 'retryable', 'origin',
                                          'operation', 'code', 'trace_id', 'retry_after_seconds') if k in error}
            if error.get('message') is not None:
                info['message'] = str(error['message'])[:500]
            return info
        for key in ('ok', 'success'):
            if obj.get(key) is False or obj.get(key) == 'false':
                return dict(reason='service_failure', retryable=False)
        for key in ('errcode', 'errorCode', 'dingOpenErrcode'):
            if obj.get(key) not in (None, '', 0, '0'):
                return dict(reason='service_error', code=obj[key], retryable=False)
        if obj.get('outcome') in ('failure', 'failed', 'partial_success'):
            return dict(reason=obj['outcome'], retryable=False)
    return {}


def partial_collections(payloads):
    """Keep rows from unsuccessful retries without inheriting completion claims."""
    latest = copy.deepcopy(payloads[-1]) if payloads and isinstance(payloads[-1], dict) else {}
    # Registration point: a source adapter's list key must appear here or failed
    # retries silently drop their partial rows.
    keys = ('events', 'todoCards', 'values', 'processInstanceList', 'minutes',
            'messages', 'items', 'emailAccounts', 'profiles')
    for wrapper in (None, 'data', 'result'):
        for key in keys:
            retained = []
            for payload in payloads:
                obj = payload if wrapper is None else payload.get(wrapper) if isinstance(payload, dict) else None
                if isinstance(obj, dict) and isinstance(obj.get(key), list):
                    retained.extend(obj[key])
            if retained:
                unique = {}
                for row in retained:
                    # Full-row equality removes retry duplicates without collapsing recurring events.
                    unique[json.dumps(row, sort_keys=True)] = row
                retained = list(unique.values())
                if wrapper is None:
                    latest[key] = retained
                else:
                    if not isinstance(latest.get(wrapper), dict):
                        latest[wrapper] = {}
                    latest[wrapper][key] = retained
    return latest


class Client:
    def __init__(self, profile=None, retries=3, timeout=60, workers=4):
        self.profile, self.retries, self.timeout, self.workers = profile, retries, timeout, workers

    def read(self, args):
        # Adapters supply fixed read commands; no persisted command text is executed.
        cmd = dws_command(args, self.profile)
        attempts, payloads, payload = [], [], None
        for number in range(1, self.retries + 1):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
                try:
                    payload = json.loads(proc.stdout)
                except ValueError:
                    payload = None
                payloads.append(payload)
                try:
                    diagnostic = json.loads(proc.stderr)
                except ValueError:
                    diagnostic = None
                error = error_info(payload) or error_info(diagnostic)
                ok = proc.returncode == 0 and isinstance(payload, dict) and not error
                if not ok and not error:
                    error = dict(reason='invalid_json' if payload is None else 'command_failed', retryable=False)
                state = dict(attempt=number, exitCode=proc.returncode, complete=ok)
                if error:
                    state['error'] = error
            except subprocess.TimeoutExpired:
                ok, error = False, dict(reason='timeout', retryable=True)
                state = dict(attempt=number, complete=False, error=error)
            except OSError:
                ok, error = False, dict(reason='command_unavailable', retryable=False)
                state = dict(attempt=number, complete=False, error=error)
            attempts.append(state)
            if ok:
                retained = payload if len(payloads) == 1 else partial_collections(payloads)
                return retained, dict(complete=True, attempts=attempts)
            terminal = error.get('category') in TERMINAL_ERROR_CATEGORIES
            if terminal or error.get('retryable') is not True or number == self.retries:
                break
            delay = error.get('retry_after_seconds', 0.5)
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                delay = 0.5
            if not 0 <= delay <= 60:
                break
            time.sleep(delay)
        return partial_collections(payloads), dict(failed(error.get('reason', 'read_failure'), **{
            k: v for k, v in error.items() if k != 'reason'}), attempts=attempts)


def page_read(client, args, keys, id_keys, page_limit, mode='cursor', managed=False,
              terminal_tokens=('', None), target=None, key_fn=None, empty_is_complete=False):
    items, reads, seen_tokens, seen_pages = {}, [], set(), set()
    next_value = None
    for page in range(1, page_limit + 1):
        command = list(args)
        if mode == 'page':
            command += ['--page', str(page)]
        elif next_value is not None:
            command += ['--cursor', str(next_value)]
        data, state = client.read(command)
        state.update(stage='list', page=page)
        if target is not None:
            state['target'] = target
        if next_value is not None:
            state['cursor'] = next_value
        # Preserve valid partial rows even when the command returns an error.
        collection_found = False
        try:
            rows = collection(data, keys)
            collection_found = True
            for row in rows:
                items[key_fn(row) if key_fn else identifier(row, id_keys)] = row
        except ValueError as exc:
            if state['complete']:
                state.update(failed(str(exc)))
            else:
                state['responseIssue'] = str(exc)
            rows = []
        for key in ('complete', 'hasMore', 'nextCursor', 'nextToken', 'stopReason', 'failures', 'scopeLedger'):
            value = field(data, key)
            if value is not None:
                state.setdefault('response', {})[key] = value
        reads.append(state)
        if not state['complete']:
            return list(items.values()), reads, False
        pagination = data.get('meta', {}).get('pagination', {}) if isinstance(data, dict) else {}
        has_more = field(data, 'hasMore')
        if empty_is_complete and collection_found and not rows and state['complete'] and has_more is not True:
            return list(items.values()), reads, True
        explicit = field(data, 'complete')
        cursor = field(data, 'nextCursor', field(data, 'nextToken', field(data, 'next_token')))
        if cursor is None:
            cursor = pagination.get('next_token')
        if field(data, 'failures') or explicit is False:
            # Managed shortcuts already paginate; do not infer completion from their outer envelope.
            if managed or field(data, 'failures'):
                state.update(failed(field(data, 'stopReason', 'partial_response')))
                return list(items.values()), reads, False
        exhausted = (has_more is False or explicit is True or
                     pagination.get('endpoint_exhausted') is True)
        cursor_present = any('nextCursor' in obj or 'nextToken' in obj or 'next_token' in obj
                             for obj in containers(data)) or 'next_token' in pagination
        if mode == 'cursor' and cursor_present and cursor in terminal_tokens and has_more is not True:
            exhausted = True
        if has_more is True or (cursor_present and cursor not in terminal_tokens):
            exhausted = False
        if exhausted:
            if explicit is False:
                state.update(failed('partial_response'))
                return list(items.values()), reads, False
            return list(items.values()), reads, True
        if managed:
            state.update(failed('pagination_incomplete'))
            return list(items.values()), reads, False
        if mode == 'page':
            if has_more is not True:
                state.update(failed('pagination_unknown'))
                return list(items.values()), reads, False
            fingerprint = tuple(sorted(identifier(r, id_keys) for r in rows))
            if not rows or fingerprint in seen_pages:
                state.update(failed('pagination_stalled'))
                return list(items.values()), reads, False
            seen_pages.add(fingerprint)
        else:
            if cursor in terminal_tokens or cursor is None:
                state.update(failed('missing_cursor'))
                return list(items.values()), reads, False
            token = str(cursor)
            if token in seen_tokens:
                state.update(failed('cursor_cycle'))
                return list(items.values()), reads, False
            seen_tokens.add(token)
            next_value = cursor
    reads[-1].update(failed('page_limit'))
    return list(items.values()), reads, False


def revision(item):
    # Access time/status are not revision evidence. Missing revisions require a re-read.
    return next((item[k] for k in ('revision', 'version', 'modifiedTime', 'updateTime', 'updatedAt')
                 if item.get(k) is not None), None)


def reconcile(client, source, previous, rows, id_keys, reads, discovery_complete,
              window, scope, detail=None, snapshot=False, refresh=False, checkpoint=None,
              presence_window=None):
    old = {item['id']: item for item in previous.get('items', [])}
    listed, failures = {}, []
    for row in rows:
        try:
            listed[identifier(row, id_keys)] = row
        except ValueError as exc:
            failures.append(dict(stage='normalize', reason=str(exc)))
    if failures:
        discovery_complete = False
    targets = dict(listed)
    # Retry pending detail IDs even if discovery no longer returns them.
    for uid, item in old.items():
        if item.get('read', {}).get('complete') is not True and detail:
            targets.setdefault(uid, item['metadata'])
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

    def fetch(pair):
        uid, row = pair
        prior = old.get(uid, {})
        item = dict(prior, id=uid, source=source, metadata=row)
        if uid in listed:
            item.update(lastSeenAt=fetched_at, presentInLastListing=True)
        elif discovery_complete:
            if in_presence_window(item, presence_window):
                item['presentInLastListing'] = False
        else:
            item['presentInLastListing'] = None
        if not detail:
            item.update(read=dict(complete=True), fetchedAt=fetched_at)
            return item, None
        # Metadata-only reads are cheap to re-evaluate and may gain a supported
        # detail reader when a row's type or role changes.
        unchanged = (uid in old and prior.get('read', {}).get('complete') is True and
                     prior.get('read', {}).get('detailSupported') is not False and
                     revision(row) is not None and revision(row) == prior.get('revision'))
        if not refresh and unchanged:
            return item, None
        try:
            content, state = detail(client, uid, row)
        except (ValueError, KeyError, TypeError):
            content, state = None, failed('invalid_detail_response')
        state.update(stage='detail', target=uid)
        item['read'] = state
        if state['complete']:
            item.update(content=redact_obj(content) if content is not None else content,
                        fetchedAt=fetched_at, revision=revision(row))
            item.pop('contentStale', None)
            item.pop('partialContent', None)
        elif 'content' in prior:
            item['contentStale'] = True
        if not state['complete'] and content is not None:
            item['partialContent'] = redact_obj(content)
        return item, state

    with ThreadPoolExecutor(max_workers=client.workers) as pool:
        results = list(pool.map(fetch, targets.items()))
    merged = dict(old)
    for item, state in results:
        merged[item['id']] = item
        if state:
            reads.append(state)
    # Absence is only known after a complete listing that actually covered the item.
    for uid in old.keys() - listed.keys():
        if not in_presence_window(merged[uid], presence_window):
            continue
        merged[uid] = dict(merged[uid], presentInLastListing=False if discovery_complete else None)
    retained_count = 0
    if snapshot and discovery_complete:
        items = [merged[uid] for uid in listed]
        retained = [merged[uid] for uid in merged.keys() - listed.keys()]
        retained_count = len(retained)
        # Preserve historical records explicitly outside the current result set.
        items += retained
    else:
        items = list(merged.values())
    outstanding = [dict(r, window=r.get('window', window)) for r in reads if r.get('complete') is not True]
    outstanding.extend(failures)
    complete = discovery_complete and not outstanding
    previous_checkpoint = previous.get('checkpoint', {})
    new_checkpoint = dict(previous_checkpoint)
    if discovery_complete:
        new_checkpoint.update(checkpoint or {}, windowEnd=window['end'])
    coverage = dict(window=window, scope=scope, complete=complete,
                    discoveryComplete=discovery_complete,
                    stopReason='ok' if complete else 'partial_reads',
                    reads=reads, failures=outstanding, fetchedAt=fetched_at)
    if retained_count:
        coverage['snapshotRetained'] = retained_count
    return dict(items=sorted(items, key=lambda i: i['id']), coverage=coverage,
                checkpoint=new_checkpoint)


def checked_detail(client, args, paths, uid=None, id_keys=()):
    data, state = client.read(args)
    if state['complete']:
        try:
            content = object_at(data, paths)
            if uid is not None and identifier(content, id_keys) != uid:
                raise ValueError('detail_identity_mismatch')
            if field(data, 'complete') is False or field(data, 'failures'):
                raise ValueError('partial_detail')
            if content.get('bodyHasMore') is True:
                state.update(failed('body_truncated'))
                return content, state
            return content, state
        except ValueError as exc:
            state.update(failed(str(exc)))
    return None, state
