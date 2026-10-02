#!/usr/bin/env python3
"""Start the local acceptance stack with Apple's container CLI on macOS."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import psycopg

ROOT = Path(__file__).resolve().parents[2]
FLAGS = 'disable-email-verification disable-secure-session-cookies enable-login-with-password enable-registration disable-telemetry'
PASSWORD = 'pgfire-local-test'
SECRET = 'pgfire-local-acceptance-only-key'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def info(name):
    result = subprocess.run(['container', 'inspect', name], capture_output=True, text=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def ip(name):
    return info(name)['status']['networks'][0]['ipv4Address'].split('/')[0]


def ensure(name, image, memory, env=None, options=None, arguments=None):
    existing = info(name)
    if existing:
        if existing['status']['state'] != 'running':
            raise RuntimeError(f'{name} exists but is stopped; remove this disposable container before recreating it')
        return
    args = ['container', 'run', '-d', '--name', name, '-m', memory]
    for key, value in (env or {}).items():
        args += ['-e', f'{key}={value}']
    run(*args, *(options or []), image, *(arguments or []), capture_output=True)


def remove(name):
    existing = info(name)
    if existing:
        if existing['status']['state'] == 'running':
            run('container', 'stop', name, capture_output=True)
        run('container', 'delete', name, capture_output=True)


def wait_for_engine():
    began = time.monotonic()
    while time.monotonic() - began < 600:
        try:
            with psycopg.connect(host='127.0.0.1', port=55434, user='penpot',
                                password=PASSWORD, dbname='penpot', connect_timeout=2) as connection:
                connection.execute('SELECT 1')
                return
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError('pgfire did not become ready within 600 seconds')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['up', 'refresh-app', 'status'])
    parser.add_argument('--namespace', default='penpot-v1')
    args = parser.parse_args()
    if args.action == 'up':
        ensure('pgfire-firestore', 'eclipse-temurin:21-jre', '2G',
               options=['-v', f'{ROOT / ".cache"}:/emulators:ro', '-p', '127.0.0.1:58080:8080'],
               arguments=['java', '-Xmx1g', '-jar', '/emulators/cloud-firestore-emulator-v1.22.0.jar',
                          '--host=0.0.0.0', '--port=8080', '--project_id=pgfire-local'])
        for _ in range(120):
            try:
                urllib.request.urlopen('http://127.0.0.1:58080', timeout=1)
                break
            except urllib.error.HTTPError:
                break  # A Firestore 404 still establishes readiness.
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('Firestore emulator startup timed out')
        ensure('pgfire-valkey', 'valkey/valkey:8.1', '256M',
               arguments=['valkey-server', '--save', '', '--appendonly', 'no'])
        ensure('pgfire-engine', 'pgfire:local', '2G',
               {'FIRESTORE_EMULATOR_HOST': ip('pgfire-firestore') + ':8080', 'PGFIRE_PROJECT': 'pgfire-local',
                'PGFIRE_NAMESPACE': args.namespace, 'PGFIRE_PASSWORD': PASSWORD, 'PGFIRE_CHECKPOINT_ON_RECOVERY': '1'},
               options=['-p', '127.0.0.1:55434:5432'])
    if args.action in ('up', 'refresh-app'):
        wait_for_engine()
        if args.action == 'refresh-app':
            # Repoint the app after Apple's container runtime assigns a new engine IP.
            for name in ['pgfire-penpot-frontend', 'pgfire-penpot-backend', 'pgfire-penpot-exporter']:
                remove(name)
        assets = ROOT / '.cache' / 'assets'
        assets.mkdir(exist_ok=True)
        assets.chmod(0o777)  # Disposable local test assets; container uid is 1001.
        ensure('pgfire-penpot-backend', 'penpotapp/backend:2.18.0', '3G',
               {'PENPOT_DATABASE_URI': f'postgresql://{ip("pgfire-engine")}:5432/penpot',
                'PENPOT_DATABASE_USERNAME': 'penpot', 'PENPOT_DATABASE_PASSWORD': PASSWORD,
                'PENPOT_REDIS_URI': f'redis://{ip("pgfire-valkey")}/3',
                'PENPOT_PUBLIC_URI': 'http://localhost:59001', 'PENPOT_SECRET_KEY': SECRET,
                'PENPOT_FLAGS': FLAGS, 'PENPOT_TELEMETRY_ENABLED': 'false',
                'PENPOT_OBJECTS_STORAGE_BACKEND': 'fs', 'PENPOT_OBJECTS_STORAGE_FS_DIRECTORY': '/opt/data/assets'},
               options=['-v', f'{assets}:/opt/data/assets'])
        # Apple containers reach published host ports through the gateway.
        ensure('pgfire-penpot-exporter', 'penpotapp/exporter:2.18.0', '1G',
               {'PENPOT_PUBLIC_URI': 'http://192.168.64.1:59002', 'PENPOT_INTERNAL_URI': 'http://192.168.64.1:59002',
                'PENPOT_REDIS_URI': f'redis://{ip("pgfire-valkey")}/3', 'PENPOT_SECRET_KEY': SECRET})
        ensure('pgfire-penpot-frontend', 'penpotapp/frontend:2.18.0', '512M',
               {'PENPOT_BACKEND_URI': f'http://{ip("pgfire-penpot-backend")}:6060',
                'PENPOT_EXPORTER_URI': f'http://{ip("pgfire-penpot-exporter")}:6061',
                'PENPOT_FLAGS': FLAGS},
               options=['-p', '127.0.0.1:59001:8080', '-p', '192.168.64.1:59002:8080',
                        '-v', f'{assets}:/opt/data/assets:ro'])
    for name in ['pgfire-engine', 'pgfire-firestore', 'pgfire-valkey']:
        print(f'{name}: {ip(name)}')
    print('\033]8;;http://localhost:59001\033\\Penpot\033]8;;\033\\ (backend startup may take several minutes)')


if __name__ == '__main__':
    main()
