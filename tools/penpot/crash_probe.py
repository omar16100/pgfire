#!/usr/bin/env python3
"""Destroy the test engine's complete root filesystem and recover from Firestore.

Run only after the Penpot suite: this deliberately interrupts the test database.
The supplied container must be the disposable pgfire-engine created by this project.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import psycopg

ROOT = Path(__file__).resolve().parents[2]
DSN = 'host=127.0.0.1 port=55434 user=penpot password=pgfire-local-test dbname=penpot connect_timeout=2'


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def wait_ready(timeout=600):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        try:
            with psycopg.connect(DSN, autocommit=True) as connection:
                connection.execute('SELECT 1')
                return time.monotonic() - began
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError('engine did not recover')


def inspect_state():
    response = command('container', 'exec', 'pgfire-engine', 'pgfire', 'inspect', capture_output=True)
    return {k: v.get('stringValue', v.get('integerValue')) for k, v in json.loads(response.stdout).items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emulator', required=True, help='emulator host:port reachable inside the container')
    parser.add_argument('--namespace', default='penpot-v1')
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--checkpoint', action='store_true', help='publish a new offline snapshot after each recovery')
    args = parser.parse_args()
    reports = []
    output = ROOT / 'artifacts' / ('checkpoint-recovery.json' if args.checkpoint else 'crash-recovery.json')
    for cycle in range(args.rounds):
        payload = os.urandom(3 * 1024 * 1024 + 137)
        digest = hashlib.sha256(payload).hexdigest()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute('CREATE SCHEMA IF NOT EXISTS pgfire_acceptance')
            c.execute('CREATE TABLE IF NOT EXISTS pgfire_acceptance.queue (id int PRIMARY KEY, n int NOT NULL DEFAULT 0)')
            c.execute('INSERT INTO pgfire_acceptance.queue(id) VALUES(2) ON CONFLICT DO NOTHING')
            c.execute('CREATE TABLE IF NOT EXISTS pgfire_acceptance.crash_probe (id int PRIMARY KEY, payload bytea)')
            c.execute('INSERT INTO pgfire_acceptance.crash_probe VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (cycle, payload))
            # Only record acknowledged data; the expected bytes never enter the replacement container.
            expected = c.execute('SELECT id, encode(sha256(payload),\'hex\') FROM pgfire_acceptance.crash_probe ORDER BY id').fetchall()
            penpot = {}
            for table in ['migrations', 'profile', 'team', 'project', 'file', 'comment']:
                if c.execute('SELECT to_regclass(%s)', ('public.' + table,)).fetchone()[0]:
                    penpot[table] = c.execute(f'SELECT count(*) FROM public.{table}').fetchone()[0]
        before = inspect_state()
        command('container', 'kill', '--signal', 'KILL', 'pgfire-engine', capture_output=True)
        log = command('container', 'logs', 'pgfire-engine', capture_output=True)
        (ROOT / 'artifacts' / f'engine-before-{output.stem}-{cycle}.log').write_text(log.stdout + log.stderr)
        # Deleting the container deletes ALL local PostgreSQL state. No volumes are mounted.
        command('container', 'delete', 'pgfire-engine', capture_output=True)
        args_run = ['container', 'run', '-d', '--name', 'pgfire-engine', '-m', '2G',
                    '-p', '127.0.0.1:55434:5432', '-e', f'FIRESTORE_EMULATOR_HOST={args.emulator}',
                    '-e', 'PGFIRE_PROJECT=pgfire-local', '-e', f'PGFIRE_NAMESPACE={args.namespace}',
                    '-e', 'PGFIRE_PASSWORD=pgfire-local-test']
        if args.checkpoint:
            args_run += ['-e', 'PGFIRE_CHECKPOINT_ON_RECOVERY=1']
        command(*args_run, 'pgfire:local', capture_output=True)
        seconds = wait_ready()
        with psycopg.connect(DSN, autocommit=True) as c:
            actual = c.execute('SELECT id, encode(sha256(payload),\'hex\') FROM pgfire_acceptance.crash_probe ORDER BY id').fetchall()
            assert actual == expected, (expected, actual)
            assert dict(actual)[cycle] == digest
            for table, count in penpot.items():
                assert c.execute(f'SELECT count(*) FROM public.{table}').fetchone()[0] == count, table
            # Confirm new writes work on the new timeline.
            c.execute('UPDATE pgfire_acceptance.queue SET n=n+1 WHERE id=2')
        after = inspect_state()
        assert int(after['timeline']) > int(before['timeline']), (before['timeline'], after['timeline'])
        if args.checkpoint:
            assert after['base'] != before['base'], 'checkpoint did not replace snapshot'
        reports.append({'cycle': cycle + 1, 'rootfs_deleted': True, 'recovery_seconds': seconds,
                        'before_timeline': before['timeline'], 'after_timeline': after['timeline'],
                        'payload_bytes': len(payload), 'sha256': digest,
                        'verified_rows': len(expected), 'penpot_row_counts': penpot,
                        'checkpoint': args.checkpoint, 'passed': True})
        output.write_text(json.dumps(reports, indent=2) + '\n')
        print(f'Cycle {cycle + 1} passed: {len(expected)} blobs and Penpot tables recovered; {seconds:.1f}s', flush=True)
    print(f'\033]8;;{output.resolve().as_uri()}\033\\Recovery report\033]8;;\033\\')


if __name__ == '__main__':
    main()
