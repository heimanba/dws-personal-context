from .common import collection, checked_detail, failed, page_read, reconcile
from redact import redact_obj


def collect(client, window, config, previous, limits):
    email = config.get('mailbox')
    reads = []
    if not email:
        data, state = client.read(['mail', 'mailbox', 'list'])
        state.update(stage='mailbox')
        reads.append(state)
        if state['complete']:
            try:
                accounts = collection(data, ('emailAccounts',))
                org = [a for a in accounts if a.get('type') == 'ORG' and a.get('email')]
                candidates = org or [a for a in accounts if a.get('email')]
                if len(candidates) != 1:
                    raise ValueError('mailbox_ambiguous_or_missing')
                email = candidates[0]['email']
                config['mailbox'] = email
            except ValueError as exc:
                state.update(failed(str(exc)))
        if not email:
            return reconcile(client, 'mail', previous, [], ('id', 'messageId'), reads, False,
                             window, dict(timeBasis='mail_date', mailbox=None))
    # Keep discovery checkpoint separate from outstanding body reads.
    start = previous.get('checkpoint', {}).get('windowEnd', window['start'])
    if start < window['start'] or start > window['end']:
        start = window['start']
    if start == window['end']:
        rows, ledger, complete = [], [], True
    else:
        query = 'date>=%s AND date<%s' % (start, window['end'])
        rows, ledger, complete = page_read(client, [
            'mail', 'message', 'search', '--email', email, '--query', query,
            '--limit', str(limits['size'])], ('messages',), ('id', 'messageId'),
            limits['pages'], terminal_tokens=('$',))
        for read in ledger:
            read['window'] = dict(start=start, end=window['end'])
    reads.extend(ledger)

    def detail(client, uid, row):
        content, state = checked_detail(
            client, ['mail', 'message', 'get', '--email', email, '--id', uid],
            ('message',), uid, ('id', 'messageId'))
        return (redact_obj(content) if content is not None else content), state

    # Presence is relative to this incremental search window, not the
    # cumulative file window; otherwise unchanged mail would be marked absent.
    return reconcile(client, 'mail', previous, rows, ('id', 'messageId'), reads, complete,
                     window, dict(mailbox=email, timeBasis='mail_date', readWindow=dict(start=start, end=window['end'])),
                     detail=detail, checkpoint=dict(mailbox=email),
                     presence_window=dict(start=start, end=window['end'], field='receivedDateTime'))
