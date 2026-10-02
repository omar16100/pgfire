#!/usr/bin/env python3
"""Exercise real Firestore REST calls against the official emulator."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.namespace = 'test-' + uuid.uuid4().hex
        self.env = dict(os.environ, FIRESTORE_EMULATOR_HOST=os.environ.get('FIRESTORE_EMULATOR_HOST', '127.0.0.1:58080'),
                        PGFIRE_PROJECT='pgfire-local', PGFIRE_NAMESPACE=self.namespace)
        self.root = f"http://{self.env['FIRESTORE_EMULATOR_HOST']}/v1/projects/pgfire-local/databases/(default)/documents/pgfire/{self.namespace}"

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args, success=True):
        result = subprocess.run([str(ROOT / 'build/pgfire'), *map(str, args)], env=self.env,
                                capture_output=True, text=True, timeout=120)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_objects_over_request_limit_and_empty(self):
        for size in [0, 1, 512 * 1024 + 1, 11 * 1024 * 1024 + 7]:
            with self.subTest(size=size):
                source = self.directory / f'input-{size}'
                source.write_bytes(os.urandom(size))
                object_id = self.command('put-object', source).stdout.strip()
                output = self.directory / f'output-{size}'
                self.command('get-object', object_id, output)
                self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_checksum_corruption_is_rejected_and_partial_output_removed(self):
        source = self.directory / 'input'
        source.write_bytes(b'abc')
        object_id = self.command('put-object', source).stdout.strip()
        request = urllib.request.Request(f'{self.root}/objects/{object_id}/chunks/00000000',
            data=json.dumps({'fields': {'data': {'bytesValue': base64.b64encode(b'xyz').decode()}}}).encode(),
            method='PATCH', headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        output = self.directory / 'corrupt'
        result = self.command('get-object', object_id, output, success=False)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertFalse(output.exists())

    def test_second_owner_is_rejected_and_token_can_be_adopted(self):
        token = self.command('acquire').stdout.strip()
        self.assertEqual(len(token), 32)
        result = self.command('acquire', success=False)
        self.assertIn('unexpired owner', result.stderr)
        self.env['PGFIRE_OWNER_TOKEN'] = token
        self.assertEqual(self.command('acquire').stdout.strip(), token)

    def test_existing_output_is_not_overwritten(self):
        source = self.directory / 'input'
        source.write_bytes(b'new')
        object_id = self.command('put-object', source).stdout.strip()
        output = self.directory / 'existing'
        output.write_bytes(b'keep')
        self.command('get-object', object_id, output, success=False)
        self.assertEqual(output.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main(verbosity=2)
