#!/usr/bin/env python3
"""Crash on a published WAL page boundary during an uncommitted large insert."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid
import psycopg

ROOT = Path(__file__).resolve().parents[2]
NAME = 'pgfire-tail-test'
DSN = 'host=127.0.0.1 port=55435 user=penpot password=pgfire-local-test dbname=penpot connect_timeout=2'
REPORT = ROOT / 'artifacts/partial-wal.json'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--recover-only', action='store_true')
parser.add_argument('--emulator', default='192.168.64.10:8080')
args = parser.parse_args()
namespace = json.loads(REPORT.read_text())['namespace'] if args.recover_only else 'tail-' + uuid.uuid4().hex[:12]
url = f'http://127.0.0.1:58080/v1/projects/pgfire-local/databases/(default)/documents/pgfire/{namespace}/meta/state'


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def start():
    run('container', 'run', '-d', '--name', NAME, '-m', '1G', '-p', '127.0.0.1:55435:5432',
        '-e', 'FIRESTORE_EMULATOR_HOST=' + args.emulator, '-e', 'PGFIRE_NAMESPACE=' + namespace,
        '-e', 'PGFIRE_PASSWORD=pgfire-local-test', 'pgfire:local')


def ready():
    began = time.monotonic()
    while time.monotonic() - began < 240:
        try:
            with psycopg.connect(DSN, autocommit=True) as c:
                c.execute('SELECT 1')
            return time.monotonic() - began
        except psycopg.OperationalError:
            status = json.loads(run('container', 'inspect', NAME).stdout)[0]['status']['state']
            if status == 'stopped':
                logs = run('container', 'logs', NAME)
                (ROOT / 'artifacts/partial-wal-engine.log').write_text(logs.stdout + logs.stderr)
                raise RuntimeError('engine stopped during partial WAL recovery; inspect the saved log')
            time.sleep(1)
    raise RuntimeError('partial WAL recovery timed out')


def state():
    with urllib.request.urlopen(url, timeout=5) as response:
        return {k: v.get('stringValue', v.get('integerValue')) for k,v in json.load(response)['fields'].items()}


def lsn(s):
    high, low = s.split('/')
    return (int(high,16) << 32) | int(low,16)


if not args.recover_only:
    start()
    ready()
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute('CREATE TABLE tail_probe (id int PRIMARY KEY, payload bytea)')
        c.execute("INSERT INTO tail_probe VALUES(1,'acknowledged-before-crash')")
    before = state()
    base = lsn(before['end_lsn'])
    payload = os.urandom(64 * 1024 * 1024)
    def insert():
        try:
            with psycopg.connect(DSN, autocommit=True) as c:
                c.execute('BEGIN')
                c.execute('INSERT INTO tail_probe VALUES(2,%s)', (payload,))
                # Never COMMIT, even if INSERT finishes before the kill.
                time.sleep(3)
                c.execute('ROLLBACK')
        except psycopg.OperationalError:
            pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(insert)
        began = time.monotonic()
        while time.monotonic() - began < 90:
            observed = state()
            end = lsn(observed['end_lsn'])
            if end > base + 256 * 1024 and end % 8192 == 0:
                break
            if future.done():
                raise AssertionError('large insert finished before an intermediate WAL page was observed')
            time.sleep(0.005)
        else:
            raise AssertionError('no intermediate WAL page boundary observed')
        run('container', 'kill', '--signal', 'KILL', NAME)
        stopped = state()
        REPORT.write_text(json.dumps({'namespace': namespace, 'observed_end_before_kill': observed['end_lsn'],
                                     'published_end_after_kill': stopped['end_lsn'],
                                     'wal_bytes_after_acknowledged_row': lsn(stopped['end_lsn']) - base,
                                     'passed': False}, indent=2) + '\n')
else:
    subprocess.run(['container','kill',NAME], capture_output=True)
run('container', 'delete', NAME)
start()
seconds = ready()
with psycopg.connect(DSN, autocommit=True) as c:
    assert c.execute('SELECT id,payload FROM tail_probe ORDER BY id').fetchall() == [(1,b'acknowledged-before-crash')]
    c.execute("INSERT INTO tail_probe VALUES(3,'after-recovery')")
report = json.loads(REPORT.read_text())
report.update(recovery_seconds=seconds, passed=True, confirmed_row_survived=True, uncommitted_row_absent=True)
REPORT.write_text(json.dumps(report, indent=2) + '\n')
logs = run('container','logs',NAME)
(ROOT / 'artifacts/partial-wal-engine.log').write_text(logs.stdout + logs.stderr)
run('container','stop',NAME)
run('container','delete',NAME)
print(json.dumps(report))
