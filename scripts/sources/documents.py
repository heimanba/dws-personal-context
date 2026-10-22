from .common import AI_TABLE_MARKERS, SPREADSHEET_MARKERS, checked_detail, page_read, reconcile
from redact import redact_obj


def type_hint(row):
    # contentType is unreliable for cloud spreadsheets and AI tables: they can
    # still be labelled ALIDOC while containing a URL for another renderer.
    url = row.get('docUrl') or ''
    if 'spreadsheetv2' in url:
        return 'axls'
    if 'iframe/notable' in url:
        return 'able'
    if 'note/edit' in url or 'docType=doc' in url:
        return 'ALIDOC'
    return row.get('contentType')


def unsupported_content_type(state):
    texts = []
    for failure in state.get('failures', []):
        if isinstance(failure, dict):
            texts.append(str(failure.get('message') or ''))
    for attempt in state.get('attempts', []):
        error = attempt.get('error') if isinstance(attempt, dict) else None
        if isinstance(error, dict):
            texts.append(str(error.get('message') or ''))
    text = ' '.join(texts).lower()
    if any(marker in text for marker in SPREADSHEET_MARKERS):
        return 'axls'
    if any(marker in text for marker in AI_TABLE_MARKERS):
        return 'able'
    return None


def metadata_only(content_type, **extra):
    return dict(metadataOnly=True, detailSupported=False, contentType=content_type, **extra)


def read_state(reason, content_type, **extra):
    return dict(complete=True, stopReason=reason, metadataOnly=True,
                detailSupported=False, inferredContentType=content_type, **extra)


def detail(client, uid, row):
    hint = type_hint(row)
    # Only ALIDOC has a body reader; other Drive node types are metadata only.
    if hint != 'ALIDOC':
        return metadata_only(hint), read_state('metadata_only_unsupported_content_type', hint)
    content, state = checked_detail(client, ['doc', '+fetch', '--node', uid, '--scope', 'full'],
                                    ('content',), uid, ('nodeId',))
    inferred = unsupported_content_type(state)
    if not state['complete'] and inferred:
        return metadata_only(inferred, readError=state.get('stopReason')), read_state(
            'metadata_only_unsupported_content_type', inferred, readError=state.get('stopReason'))
    return (redact_obj(content) if content is not None else content), state


def collect(client, window, config, previous, limits):
    rows, reads, complete = page_read(client, [
        'drive', '+recent', '--operate-type', '1', '--page-all', '--limit', str(min(limits['size'], 20)),
        '--max-pages', str(limits['pages']), '--max-items', str(limits['items'])],
        ('items',), ('nodeId',), 1, managed=True)
    return reconcile(client, 'documents', previous, rows, ('nodeId',), reads, complete,
                     window, dict(timeBasis='recent_edit_snapshot', windowFilterApplied=False,
                                  contentScope='ALIDOC body; other types metadata only',
                                  typeDetection='docUrl then CLI content-type error'), detail=detail,
                     snapshot=True)
