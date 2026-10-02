#!/usr/bin/env python3
"""Graceful shutdown releases ownership only after SQL service has stopped."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid
import psycopg

root = Path(__file__).resolve().parents[2]
name = 'pgfire-shutdown-test'
namespace = 'shutdown-' + uuid.uuid4().hex[:12]
emulator = os.environ.get('PGFIRE_TEST_EMULATOR','192.168.64.10:8080')
def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True)
run('container','run','-d','--name',name,'-m','1G','-p','127.0.0.1:55435:5432',
    '-e','FIRESTORE_EMULATOR_HOST='+emulator,'-e','PGFIRE_NAMESPACE='+namespace,
    '-e','PGFIRE_PASSWORD=pgfire-local-test','pgfire:local')
try:
    for _ in range(120):
        try:
            with psycopg.connect('host=127.0.0.1 port=55435 user=penpot password=pgfire-local-test dbname=penpot connect_timeout=2',autocommit=True) as c:
                c.execute('CREATE TABLE shutdown_probe(id int)')
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else: raise RuntimeError('test engine did not become ready')
    began=time.monotonic()
    run('container','stop',name)
    with urllib.request.urlopen(f'http://127.0.0.1:58080/v1/projects/pgfire-local/databases/(default)/documents/pgfire/{namespace}/meta/state') as response:
        fields=json.load(response)['fields']
    assert fields['owner']['stringValue']=='', 'lease was not released'
    assert int(fields['expires']['integerValue'])==0
    report={'passed':True,'namespace':namespace,'owner_released':True,'shutdown_seconds':time.monotonic()-began}
    (root/'artifacts/shutdown-test.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
finally:
    subprocess.run(['container','kill',name],capture_output=True)
    run('container','delete',name)
