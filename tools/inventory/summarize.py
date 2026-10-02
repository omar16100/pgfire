#!/usr/bin/env python3
"""Summarize observed PostgreSQL messages; exact SQL counts are not fingerprints."""
import argparse
import collections
import json
from pathlib import Path
from pglast import fingerprint


def summarize(path):
    kinds = collections.Counter()
    statements = collections.Counter()
    errors = collections.Counter()
    connections = set()
    named = set()
    formats = set()
    limits = set()
    capture_errors = []
    fingerprints = {}
    for line in path.read_text().splitlines():
        event = json.loads(line)
        connections.add(event['connection'])
        kinds[f"{event.get('direction', 'proxy')}:{event['kind']}"] += 1
        if 'sql' in event:
            statements[event['sql']] += 1
            try:
                key = fingerprint(event['sql'])
                fingerprints.setdefault(key, event['sql'])
            except Exception:
                pass
        if event.get('statement'):
            named.add((event['connection'], event['statement']))
        formats.update(event.get('parameter_formats', []))
        formats.update(event.get('result_formats', []))
        if 'max_rows' in event:
            limits.add(event['max_rows'])
        if 'sqlstate' in event:
            errors[event['sqlstate']] += 1
        if event['kind'] == 'capture_error':
            capture_errors.append(event)
    return {'connections': len(connections), 'message_counts': dict(kinds),
            'distinct_exact_sql': len(statements), 'named_statements': len(named),
            'formats_observed': sorted(formats), 'execute_row_limits': sorted(limits),
            'sqlstates': dict(errors), 'capture_errors': capture_errors,
            'distinct_fingerprints': len(fingerprints),
            'fingerprints': fingerprints,
            'statements': [{'count': count, 'sql': sql} for sql, count in statements.most_common()]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summarize(args.capture), indent=2) + '\n')
