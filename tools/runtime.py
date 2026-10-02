#!/usr/bin/env python3
"""Single-owner pgfire supervisor. All PostgreSQL files are disposable cache.

Successful SQL COMMIT requires the C receiver's Firestore flush acknowledgement.
Any receiver failure stops PostgreSQL. Startup always reconstructs from Firestore.
"""
import json
import os
from pathlib import Path
import re
import signal
import select
import socket
import subprocess
import sys
import tarfile
import threading
import time

stopping = False


def stop(_signal, _frame):
    global stopping
    stopping = True


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def inspect():
    fields = json.loads(run('pgfire', 'inspect', capture_output=True).stdout)
    return {k: v.get('stringValue', v.get('integerValue')) for k, v in fields.items()}


def acquire():
    deadline = time.monotonic() + 120
    while not stopping and time.monotonic() < deadline:
        state = inspect()
        remaining = int(state.get('expires') or 0) - time.time()
        if not state.get('owner') or remaining <= 0:
            result = subprocess.run(['pgfire', 'acquire'], capture_output=True, text=True)
            if result.returncode == 0:
                token = result.stdout.strip()
                os.environ['PGFIRE_OWNER_TOKEN'] = token
                return token
        time.sleep(min(5, max(0.2, remaining + 0.1)))
    raise RuntimeError('could not acquire database ownership within 120 seconds')


def sql(query):
    return run('psql', '-h', '/tmp', '-p', '5433', '-U', 'postgres', '-d', 'postgres',
               '-v', 'ON_ERROR_STOP=1', '-At', '-c', query, capture_output=True).stdout.strip()


def configure(data):
    with (data / 'postgresql.conf').open('a') as out:
        out.write("""
listen_addresses = '127.0.0.1'
port = 5433
unix_socket_directories = '/tmp'
max_connections = 150
wal_level = replica
max_wal_senders = 5
max_replication_slots = 5
wal_keep_size = '1GB'
fsync = on
full_page_writes = on
synchronous_commit = on
synchronous_standby_names = 'FIRST 1 (pgfire)'
shared_preload_libraries = 'pgfire_guard'
log_min_messages = warning
""")


def offline_checkpoint(data, archive, expected_end):
    """Only called with ownership held, PostgreSQL stopped and no public listener."""
    control = run('pg_controldata', str(data), capture_output=True).stdout
    if not re.search(r'Database cluster state:\s+shut down\s*$', control, re.MULTILINE):
        raise RuntimeError('checkpoint requires a cleanly stopped PostgreSQL cluster')
    system_id = re.search(r'Database system identifier:\s+(\d+)', control).group(1)
    redo = re.search(r"Latest checkpoint's REDO location:\s+([0-9A-F]+/[0-9A-F]+)", control).group(1)
    timeline = re.search(r"Latest checkpoint's TimeLineID:\s+(\d+)", control).group(1)
    wal_file = re.search(r"Latest checkpoint's REDO WAL file:\s+([0-9A-F]+)", control).group(1)

    def needed(member):
        # A clean checkpoint needs only its current WAL segment, plus history.
        path = Path(member.name)
        if path.parent == Path('data/pg_wal') and re.fullmatch(r'[0-9A-F]{24}', path.name) and path.name != wal_file:
            return None
        return member

    with tarfile.open(archive, 'w:gz') as out:
        out.add(data, arcname='data', filter=needed)
    run('pgfire', 'checkpoint', str(archive), redo, system_id, timeline, expected_end)
    archive.unlink()
    print('{"event":"offline_checkpoint_published"}', flush=True)


def relay(client):
    upstream = None
    try:
        upstream = socket.create_connection(('127.0.0.1', 5433), timeout=5)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.settimeout(5)
        while not stopping:
            readable, _, _ = select.select([client, upstream], [], [], 0.5)
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                (upstream if source is client else client).sendall(payload)
    except (OSError, ValueError):
        pass
    finally:
        client.close()
        if upstream:
            upstream.close()


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    data = Path(os.environ.get('PGDATA', '/tmp/pgfire/data'))
    if data.exists():
        raise RuntimeError('PGDATA already exists; use a fresh empty runtime directory')
    data.parent.mkdir(parents=True, exist_ok=True)
    archive = data.parent / 'base.tar.gz'
    state = inspect()
    owner = None
    if not state.get('base'):
        run('initdb', '-D', str(data), '--auth-local=trust', '--auth-host=scram-sha-256', '--no-instructions')
        configure(data)
        run('pg_ctl', '-D', str(data), '-l', str(data.parent / 'bootstrap.log'),
            '-o', "-c listen_addresses='' -c synchronous_standby_names='' -c shared_preload_libraries=''",
            '-w', 'start')
        try:
            password = os.environ.get('PGFIRE_PASSWORD')
            if not password:
                raise RuntimeError('PGFIRE_PASSWORD is required for initial bootstrap')
            # SQL string escaping, never shell interpolation.
            quoted = password.replace("'", "''")
            sql("CREATE ROLE penpot LOGIN PASSWORD '" + quoted + "'")
            sql('CREATE DATABASE penpot OWNER penpot')
            sql('CREATE DATABASE penpot_test OWNER penpot')
            sql("SELECT pg_create_physical_replication_slot('pgfire', true)")
        finally:
            run('pg_ctl', '-D', str(data), '-m', 'fast', '-w', 'stop')
        control = run('pg_controldata', str(data), capture_output=True).stdout
        system_id = re.search(r'Database system identifier:\s+(\d+)', control).group(1)
        redo = re.search(r"Latest checkpoint's REDO location:\s+([0-9A-F]+/[0-9A-F]+)", control).group(1)
        timeline = re.search(r"Latest checkpoint's TimeLineID:\s+(\d+)", control).group(1)
        hi, lo = (int(x, 16) for x in redo.split('/'))
        start_lsn = ((hi << 32) | lo) & ~(16 * 1024 * 1024 - 1)
        lsn = f'{start_lsn >> 32:X}/{start_lsn & 0xffffffff:X}'
        with tarfile.open(archive, 'w:gz') as out:
            out.add(data, arcname='data')
        run('pgfire', 'bootstrap', str(archive), lsn, system_id, timeline)
    else:
        owner = acquire()
        state = inspect()
        run('pgfire', 'download-base', str(archive))
        with tarfile.open(archive, 'r:gz') as source:
            # Only normal files/directories from the engine's own snapshot are allowed.
            for member in source.getmembers():
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or path.parts[0] != 'data' or not (member.isfile() or member.isdir()):
                    raise RuntimeError('unsafe base snapshot member')
            # Debian's bundled Python predates tarfile's filter argument.
            # The complete member list above rejects links, devices and escapes.
            source.extractall(data.parent)
        run('pgfire', 'restore-wal', str(data / 'pg_wal'))
        if state.get('head'):
            (data / 'recovery.signal').touch()
            with (data / 'postgresql.conf').open('a') as out:
                out.write("\nrestore_command = 'false'\nrecovery_target_timeline = 'latest'\n")
        # A recovery creates a new timeline; the receiver verifies its ancestry.
    archive.unlink()
    # Acquire before starting PostgreSQL; the receiver adopts this exact token.
    if owner is None:
        owner = acquire()
    pg = subprocess.Popen(['postgres', '-D', str(data)])
    receiver = None
    listener = None
    try:
        for _ in range(240):
            if stopping or pg.poll() is not None:
                raise RuntimeError('PostgreSQL stopped during startup')
            ready = subprocess.run(['pg_isready', '-h', '/tmp', '-p', '5433', '-U', 'postgres'], capture_output=True)
            if ready.returncode == 0 and sql('SELECT pg_is_in_recovery()') == 'f':
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('PostgreSQL startup timed out')
        incomplete_tail = False
        if state.get('head'):
            timeline = int(sql('SELECT timeline_id FROM pg_control_checkpoint()'))
            history = data / 'pg_wal' / f'{timeline:08X}.history'
            fork = None
            for line in history.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == state['timeline']:
                    fork = parts[1]
            if fork is None:
                raise RuntimeError('recovery history does not include the durable timeline')
            def position(value):
                high, low = value.split('/')
                return (int(high, 16) << 32) | int(low, 16)
            incomplete_tail = position(fork) < position(state['end_lsn'])
            if incomplete_tail:
                # A physical replication message can end inside a WAL record.
                # PostgreSQL discards that incomplete tail on recovery. Publish
                # its clean recovered database before streaming on the new fork.
                os.environ['PGFIRE_RECOVERY_FORK_LSN'] = fork
                print(json.dumps({'event': 'incomplete_wal_tail', 'published_end': state['end_lsn'],
                                  'recovery_fork': fork}), flush=True)
        if state.get('base') and (incomplete_tail or os.environ.get('PGFIRE_CHECKPOINT_ON_RECOVERY') == '1'):
            run('pg_ctl', '-D', str(data), '-m', 'fast', '-w', 'stop')
            pg.wait(timeout=10)
            offline_checkpoint(data, archive, state['end_lsn'])
            pg = subprocess.Popen(['postgres', '-D', str(data)])
            for _ in range(240):
                if pg.poll() is not None or stopping:
                    raise RuntimeError('PostgreSQL stopped after checkpoint')
                ready = subprocess.run(['pg_isready', '-h', '/tmp', '-p', '5433', '-U', 'postgres'], capture_output=True)
                if ready.returncode == 0 and sql('SELECT pg_is_in_recovery()') == 'f':
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError('PostgreSQL checkpoint restart timed out')
        env = dict(os.environ, PGFIRE_CONNINFO='host=/tmp port=5433 user=postgres dbname=postgres',
                   PGFIRE_OWNER_TOKEN=owner, PGFIRE_LEASE_FILE=str(data.parent / 'lease.deadline'))
        receiver = subprocess.Popen(['pgfire', 'stream'], env=env)
        for _ in range(240):
            if receiver.poll() is not None or pg.poll() is not None or stopping:
                raise RuntimeError('engine stopped before durable streaming became ready')
            if sql("SELECT count(*) FROM pg_stat_replication WHERE application_name='pgfire' AND sync_state='sync' AND flush_lsn IS NOT NULL") == '1':
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('durable streaming readiness timed out')
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('0.0.0.0', 5432))
        listener.listen(150)
        listener.settimeout(0.1)
        print('{"event":"pgfire_ready","port":5432}', flush=True)
        while not stopping:
            if pg.poll() is not None:
                raise RuntimeError('PostgreSQL exited')
            if receiver.poll() is not None:
                raise RuntimeError(f'Firestore receiver exited ({receiver.returncode}); stopping PostgreSQL')
            deadline = float((data.parent / 'lease.deadline').read_text())
            if time.monotonic() >= deadline:
                raise RuntimeError('Firestore lease deadline expired; stopping PostgreSQL')
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=relay, args=(client,), daemon=True).start()
    finally:
        if listener:
            listener.close()
        if pg.poll() is None:
            pg.send_signal(signal.SIGQUIT)
            try:
                pg.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pg.kill()
                pg.wait()
        if receiver is not None and receiver.poll() is None:
            receiver.terminate()
            try:
                receiver.wait(timeout=5)
            except subprocess.TimeoutExpired:
                receiver.kill()
                receiver.wait()
        if stopping:
            # The receiver can observe PostgreSQL's EOF before its SIGTERM.
            # Re-read the fenced state before releasing, only after both child
            # processes and the SQL listener have stopped.
            try:
                subprocess.run(['pgfire', 'release'], env=dict(os.environ, PGFIRE_OWNER_TOKEN=owner),
                               timeout=8, check=False, capture_output=True)
            except subprocess.TimeoutExpired:
                pass  # Keep the lease until expiry if release is uncertain.


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'pgfire-runtime: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
