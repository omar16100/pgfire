#!/usr/bin/env python3
"""Download pinned local acceptance dependencies (no cloud resources)."""
import hashlib
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / '.cache'
CACHE.mkdir(exist_ok=True)
EMULATOR = 'cloud-firestore-emulator-v1.22.0.jar'
SHA256 = '9b6498b7f62714d67f48f59b3818883cd682dbcd46b9f59511de81c97bb5166c'
file = CACHE / EMULATOR
if not file.exists():
    urllib.request.urlretrieve('https://storage.googleapis.com/firebase-preview-drop/emulator/' + EMULATOR, file)
if hashlib.sha256(file.read_bytes()).hexdigest() != SHA256:
    raise RuntimeError('Firestore emulator checksum mismatch')
source = CACHE / 'penpot'
if not source.exists():
    subprocess.run(['git', 'clone', '--depth', '1', '--branch', '2.18.0',
                    'https://github.com/penpot/penpot.git', str(source)], check=True)
commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
if commit != '5baffdc213f0deaaeb318e97a41d611ac0656a94':
    raise RuntimeError('Expected unmodified Penpot 2.18.0 source commit')
mockery = CACHE / 'mockery-0.1.4.jar'
if not mockery.exists():
    urllib.request.urlretrieve('https://repo.clojars.org/mockery/mockery/0.1.4/mockery-0.1.4.jar', mockery)
print('Penpot 2.18.0 and the verified Firestore emulator are ready.')
