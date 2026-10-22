import datetime as dt
from .common import checked_detail, page_read, parse_time, reconcile


ROUTES = dict(pending='list-pending', submitted='list-submitted',
              executed='list-executed', cc='list-cc')
TIME_FIELDS = ('processCreateTime', 'createTime', 'gmtCreate')


def detail(client, uid, row):
    return checked_detail(client, ['oa', 'approval', 'detail', '--instance-id', uid],
                          ('result',), uid, ('processInstanceId',))


def creation_time(row, timezone):
    for key in TIME_FIELDS:
        value = parse_time(row.get(key))
        if value is not None:
            return value if value.tzinfo else value.replace(tzinfo=timezone)
    return None


def collect(client, window, config, previous, limits):
    start = dt.datetime.fromisoformat(window['start'])
    end = dt.datetime.fromisoformat(window['end'])
    roles = config['approvalRoles'].split(',')
    rows, reads = {}, []
    complete = True
    undated = 0
    filtered = {}
    for role in roles:
        # Only list-pending accepts a creation-time range. The other personal
        # relations expose an unwindowed listing, so filter locally.
        args = ['oa', 'approval', ROUTES[role]]
        if role == 'pending':
            args += ['--start', start.isoformat(timespec='seconds'),
                     '--end', (end - dt.timedelta(seconds=1)).isoformat(timespec='seconds')]
        # Every personal approval route caps a page at 20; larger values fail upstream.
        args += ['--limit', str(min(limits['size'], 20))]
        values, ledger, done = page_read(
            client, args, ('values', 'processInstanceList'), ('processInstanceId',),
            limits['pages'], mode='page', target=role,
            empty_is_complete=role != 'pending')
        reads.extend(ledger)
        complete = complete and done
        kept = 0
        for value in values:
            if role != 'pending':
                when = creation_time(value, start.tzinfo)
                if when is None:
                    undated += 1
                elif not start <= when < end:
                    continue
            kept += 1
            prior = rows.get(value['processInstanceId'])
            if prior:
                prior['roles'].append(role)
            else:
                rows[value['processInstanceId']] = dict(value, roles=[role])
        filtered[role] = dict(returned=len(values), kept=kept)
    scope = dict(roles=config['approvalRoles'], timeBasis='creation_date',
                 windowFilterApplied=True, localTimeFilter=dict(
                     fields=list(TIME_FIELDS), window=[window['start'], window['end']],
                     undatedRows=undated, perRole=filtered),
                 queryTimeBasis={role: ('server' if role == 'pending' else 'local') for role in roles})
    return reconcile(client, 'approval', previous, list(rows.values()), ('processInstanceId',), reads, complete,
                     window, scope, detail=detail, snapshot=True, refresh=True)
