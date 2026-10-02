#!/usr/bin/env python3
"""Database behavior and fail-closed acknowledgement checks against pgfire."""
import concurrent.futures
import hashlib
import os
import subprocess
import time
import unittest
import psycopg

DSN = os.environ.get('PGFIRE_TEST_DSN',
    'host=127.0.0.1 port=55434 user=penpot password=pgfire-local-test dbname=penpot connect_timeout=5')
ENGINE = os.environ.get('PGFIRE_TEST_CONTAINER', 'pgfire-engine')


def connection():
    return psycopg.connect(DSN, autocommit=True)


def receiver_signal(name):
    code = """
import os, signal
from pathlib import Path
matches=[]
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args=path.read_bytes().split(b'\\0')
        if args[:2] == [b'pgfire', b'stream']:
            matches.append(int(path.parent.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
assert len(matches) == 1, matches
os.kill(matches[0], getattr(signal, 'SIG' + os.environ['TEST_SIGNAL']))
"""
    subprocess.run(['container', 'exec', '-e', f'TEST_SIGNAL={name}', ENGINE,
                    'python3', '-c', code], check=True, capture_output=True, text=True, timeout=60)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connection() as c:
            c.execute('CREATE SCHEMA IF NOT EXISTS pgfire_acceptance')
            c.execute('CREATE TABLE IF NOT EXISTS pgfire_acceptance.probe (id integer PRIMARY KEY, value bytea)')
            c.execute('CREATE TABLE IF NOT EXISTS pgfire_acceptance.queue (id integer PRIMARY KEY, n integer NOT NULL DEFAULT 0)')
            c.execute('INSERT INTO pgfire_acceptance.queue(id) VALUES (1),(2) ON CONFLICT DO NOTHING')

    def test_large_transaction(self):
        payload = os.urandom(3 * 1024 * 1024 + 37)
        with connection() as c:
            c.execute('INSERT INTO pgfire_acceptance.probe VALUES (1,%s) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (payload,))
            value = c.execute('SELECT value FROM pgfire_acceptance.probe WHERE id=1').fetchone()[0]
            self.assertEqual(hashlib.sha256(value).hexdigest(), hashlib.sha256(payload).hexdigest())

    def test_skip_locked(self):
        with connection() as a, connection() as b:
            a.execute('BEGIN')
            a.execute('SELECT * FROM pgfire_acceptance.queue WHERE id=1 FOR UPDATE')
            rows = b.execute('SELECT id FROM pgfire_acceptance.queue ORDER BY id FOR UPDATE SKIP LOCKED').fetchall()
            self.assertEqual(rows, [(2,)])
            a.execute('ROLLBACK')

    def test_concurrent_upsert(self):
        with connection() as c:
            c.execute('UPDATE pgfire_acceptance.queue SET n=0 WHERE id=1')

        def increment(_):
            with connection() as c:
                c.execute('INSERT INTO pgfire_acceptance.queue VALUES(1,1) ON CONFLICT(id) DO UPDATE SET n=pgfire_acceptance.queue.n+1')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(16)))
        with connection() as c:
            self.assertEqual(c.execute('SELECT n FROM pgfire_acceptance.queue WHERE id=1').fetchone()[0], 16)

    def test_durability_cannot_be_disabled(self):
        with connection() as c:
            for query in ['SET synchronous_commit=off',
                          "SELECT set_config('synchronous_commit','off',false)",
                          'CREATE UNLOGGED TABLE pgfire_acceptance.unsafe(id integer)']:
                with self.subTest(query=query):
                    with self.assertRaises(psycopg.errors.FeatureNotSupported):
                        c.execute(query)
            self.assertEqual(c.execute('SHOW synchronous_commit').fetchone()[0], 'on')

    def test_cancel_does_not_acknowledge_unflushed_commit(self):
        with connection() as c:
            receiver_signal('STOP')
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(c.execute,
                        "INSERT INTO pgfire_acceptance.probe VALUES (2,'cancel-probe') ON CONFLICT(id) DO UPDATE SET value=excluded.value")
                    time.sleep(1)
                    self.assertFalse(result.done(), 'commit returned before Firestore received WAL')
                    c.cancel()
                    with self.assertRaises(psycopg.OperationalError):
                        result.result(timeout=12)
            finally:
                receiver_signal('CONT')
        # Cancellation yields an unknown outcome; the row may commit after resumption.
        with connection() as c:
            self.assertEqual(c.execute('SELECT 1').fetchone(), (1,))


if __name__ == '__main__':
    unittest.main(verbosity=2)
