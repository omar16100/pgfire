#!/usr/bin/env python3
"""A stalled receiver must not leave a readable SQL server beyond its lease."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import psycopg

root = Path(__file__).resolve().parents[2]
name = 'pgfire-watchdog-test'
namespace = 'watchdog-' + uuid.uuid4().hex[:12]
emulator = os.environ.get('PGFIRE_TEST_EMULATOR', '192.168.64.10:8080')
dsn = 'host=127.0.0.1 port=55435 user=penpot password=pgfire-local-test dbname=penpot connect_timeout=2'


def run(*args, **kw):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kw)


run('container', 'run', '-d', '--name', name, '-m', '1G', '-p', '127.0.0.1:55435:5432',
    '-e', 'FIRESTORE_EMULATOR_HOST=' + emulator, '-e', 'PGFIRE_NAMESPACE=' + namespace,
    '-e', 'PGFIRE_PASSWORD=pgfire-local-test', 'pgfire:local')
try:
    for _ in range(120):
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute('CREATE TABLE acknowledged(id int PRIMARY KEY)')
                c.execute('INSERT INTO acknowledged VALUES(1)')
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        raise RuntimeError('watchdog test engine did not start')
    code = '''
import os, signal, time
from pathlib import Path
for p in Path('/proc').glob('[0-9]*/cmdline'):
    if p.read_bytes().split(b'\\0')[:2] == [b'pgfire',b'stream']:
        os.kill(int(p.parent.name),signal.SIGSTOP)
        print(float(Path('/tmp/pgfire/lease.deadline').read_text())-time.monotonic())
        break
else:
    raise RuntimeError('receiver missing')
'''
    remaining = float(run('container', 'exec', name, 'python3', '-c', code).stdout)
    began = time.monotonic()
    while time.monotonic() - began < remaining + 8:
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                assert c.execute('SELECT count(*) FROM acknowledged').fetchone()[0] == 1
        except psycopg.OperationalError:
            break
        time.sleep(1)
    else:
        raise AssertionError('SQL still served reads beyond the lease deadline')
    elapsed = time.monotonic() - began
    assert elapsed >= remaining - 3, (elapsed, remaining)
    log = run('container', 'logs', name)
    (root / 'artifacts/watchdog-engine.log').write_text(log.stdout + log.stderr)
    # The supervisor may still be reaping the SIGSTOP'ed receiver; logs flush on exit.
    report = {'namespace': namespace, 'receiver_signal': 'SIGSTOP', 'lease_remaining_seconds': remaining,
              'sql_stopped_after_seconds': elapsed, 'passed': True}
    path = root / 'artifacts/watchdog-test.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)
finally:
    subprocess.run(['container', 'kill', name], capture_output=True)
    run('container', 'delete', name)
