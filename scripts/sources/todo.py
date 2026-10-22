from .common import OPERATOR_NOT_IN_TASK, checked_detail, identifier, page_read, reconcile


DETAIL_ROLES = {'creator', 'executor'}
ID_KEYS = ('taskId', 'todoTaskId')


def operator_denied(state):
    return any(OPERATOR_NOT_IN_TASK in str(failure.get('message', ''))
               for failure in state.get('failures', ()))


def detail(client, uid, row):
    # The API only allows the creator or executor to read full task details.
    # Participant-only rows are still discoverable, but stay metadata-only.
    if not DETAIL_ROLES.intersection(row.get('roles', ())):
        return dict(metadataOnly=True, detailSupported=False), dict(
            complete=True, stopReason='detail_not_supported_for_participant',
            metadataOnly=True, detailSupported=False)
    content, state = checked_detail(client, ['todo', 'task', 'get', '--task-id', uid],
                                    ('result.todoDetailModel',), uid, ('taskId', 'todoTaskId'))
    # Role listings can briefly disagree with the detail endpoint. The server
    # explicitly denies the relation, so this is an unavailable detail rather
    # than a retryable read gap.
    if not state.get('complete') and operator_denied(state):
        return dict(metadataOnly=True, detailSupported=False,
                    detailUnavailableReason='operator_not_in_task'), dict(
            complete=True, stopReason='detail_not_supported_operator_not_in_task',
            metadataOnly=True, detailSupported=False,
            unavailableReason='task not exist or operator not in task')
    return content, state


def collect(client, window, config, previous, limits):
    roles = config['todoRoles'].split(',')
    rows, reads = {}, []
    complete = True
    for role in roles:
        args = ['todo', 'task', 'list', '--size', str(min(limits['size'], 20)),
                '--role-types', role]
        if config['todoStatus'] != 'all':
            args += ['--status', config['todoStatus']]
        # Default is a current snapshot, including tasks without a due date.
        if config['todoWindow'] == 'due':
            args += ['--plan-finish-date-start', window['start'],
                     '--plan-finish-date-end', window['end']]
        values, ledger, done = page_read(client, args, ('todoCards',), ID_KEYS,
                                         limits['pages'], mode='page', target=role)
        reads.extend(ledger)
        complete = complete and done
        for value in values:
            uid = identifier(value, ID_KEYS)
            prior = rows.get(uid)
            if prior:
                if role not in prior['roles']:
                    prior['roles'].append(role)
            else:
                rows[uid] = dict(value, roles=[role])
    scope = dict(roles=config['todoRoles'], status=config['todoStatus'],
                 timeBasis='due_time' if config['todoWindow'] == 'due' else 'current_snapshot',
                 detailRoles=sorted(DETAIL_ROLES),
                 participantDetail='list_metadata_only')
    result = reconcile(client, 'todo', previous, list(rows.values()), ID_KEYS, reads, complete,
                       window, scope, detail=detail, snapshot=True, refresh=True)
    count = sum(1 for item in result['items']
                if item.get('read', {}).get('stopReason') == 'detail_not_supported_operator_not_in_task')
    if count:
        result['coverage']['scope']['detailFallbacks'] = count
    return result
