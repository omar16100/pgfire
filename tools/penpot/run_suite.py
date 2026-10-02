#!/usr/bin/env python3
"""Run unmodified upstream Penpot tests using its release JVM/dependencies."""
import argparse
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database-uri', required=True)
parser.add_argument('--redis-uri', default='redis://192.168.64.3/1')
parser.add_argument('--password', default='pgfire-local-test')
parser.add_argument('--name', required=True)
parser.add_argument('--pattern')
args = parser.parse_args()
directory = root / 'artifacts' / args.name
directory.mkdir(parents=True, exist_ok=True)
command = ['container', 'run', '--rm', '--name', args.name, '-m', '4G',
           '-v', f'{root}:/workspace', '--entrypoint', '/opt/jre/bin/java',
           '-w', '/opt/penpot/backend',
           '-e', f'PENPOT_TEST_DATABASE_URI={args.database_uri}',
           '-e', 'PENPOT_TEST_DATABASE_USERNAME=penpot',
           '-e', f'PENPOT_TEST_DATABASE_PASSWORD={args.password}',
           '-e', f'PENPOT_TEST_REDIS_URI={args.redis_uri}',
           '-e', 'PENPOT_TELEMETRY_ENABLED=false',
           '-e', f'PGFIRE_TEST_REPORT=/workspace/artifacts/{args.name}/result.edn']
if args.pattern:
    command += ['-e', f'PGFIRE_TEST_PATTERN={args.pattern}']
command += ['docker.io/penpotapp/backend:2.18.0',
            '-Xmx3g', '--enable-preview', '--enable-native-access=ALL-UNNAMED',
            '--sun-misc-unsafe-memory-access=allow',
            '-Dlog4j2.configurationFile=/opt/penpot/backend/log4j2.xml',
            '-cp', '/workspace/.cache/penpot/backend/test:/workspace/.cache/mockery-0.1.4.jar:/opt/penpot/backend/penpot.jar',
            'clojure.main', '/workspace/tools/penpot/runner.clj']
with (directory / 'suite.log').open('w') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
link = (directory / 'suite.log').resolve().as_uri()
print(f'\033]8;;{link}\033\\Suite log\033]8;;\033\\ (exit {result.returncode})')
sys.exit(result.returncode)
