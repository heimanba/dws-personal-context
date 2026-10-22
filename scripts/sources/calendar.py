import json
from .common import identifier, page_read, reconcile


def occurrence(row):
    return identifier(row, ('eventId', 'id')) + ':' + json.dumps(row.get('start'), sort_keys=True)


def collect(client, window, config, previous, limits):
    calendar_id = config['calendarId']
    rows, reads, complete = page_read(client, [
        'calendar', 'event', 'list', '--calendar-id', calendar_id,
        '--start', window['start'], '--end', window['end'], '--limit', str(limits['size'])],
        ('events',), ('eventId', 'id'), limits['pages'], key_fn=occurrence)
    # Recurring events may repeat an event ID across occurrences: keep occurrence identity.
    for row in rows:
        row['_occurrenceId'] = occurrence(row)
    return reconcile(client, 'calendar', previous, rows, ('_occurrenceId',), reads, complete,
                     window, dict(calendarId=calendar_id, timeBasis='event_range'), snapshot=True)
