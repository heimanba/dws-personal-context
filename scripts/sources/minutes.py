from .common import error_info, failed, page_read, reconcile, identifier


def detail(client, uid, row):
    data, state = client.read(['minutes', '+detail', '--id', uid, '--artifacts', 'basic,summary,keywords'])
    if state['complete']:
        try:
            if not isinstance(data, dict) or data.get('complete') is not True:
                raise ValueError('partial_artifacts')
            if identifier(data, ('taskUuid',)) != uid:
                raise ValueError('detail_identity_mismatch')
            basic = data.get('basic', {}).get('result')
            if not isinstance(basic, dict) or identifier(basic, ('taskUuid',)) != uid:
                raise ValueError('detail_identity_mismatch')
            if not all(isinstance(data.get(k), dict) and isinstance(data[k].get('result'), dict)
                       for k in ('basic', 'summary', 'keywords')):
                raise ValueError('missing_artifact')
            if any(error_info(data[k]) for k in ('basic', 'summary', 'keywords')):
                raise ValueError('artifact_failed')
            return {k: data[k]['result'] for k in ('basic', 'summary', 'keywords')}, state
        except ValueError as exc:
            state.update(failed(str(exc)))
    return None, state


def collect(client, window, config, previous, limits):
    rows, reads, complete = page_read(client, [
        'minutes', '+search', '--scope', config['minutesScope'], '--start', window['start'],
        '--end', window['end'], '--page-all', '--limit', str(min(limits['size'], 50)),
        '--page-limit', str(limits['pages'])], ('minutes',), ('taskUuid',), 1, managed=True)
    return reconcile(client, 'minutes', previous, rows, ('taskUuid',), reads, complete,
                     window, dict(scope=config['minutesScope'], timeBasis='recording_range'), detail=detail)
