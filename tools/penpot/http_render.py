#!/usr/bin/env python3
"""Exercise Penpot's normal PNG export service through its HTTP API."""
import hashlib
import http.cookiejar
import json
from pathlib import Path
import struct
import urllib.request

root=Path(__file__).resolve().parents[2]
base='http://localhost:59001'
report=json.loads((root/'artifacts/http-smoke.json').read_text())
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
login=urllib.request.Request(base+'/api/main/methods/login-with-password',
    data=json.dumps({'email':'pgfire.acceptance@example.com','password':'Pgfire-local-only-2026!'}).encode(),
    headers={'Content-Type':'application/json','Accept':'application/json'})
with client.open(login,timeout=30) as response:
    response.read()
params={'~:cmd':'~:export-shapes','~:wait':True,'~:profile-id':'~u'+report['profile_id'],
        '~:exports':[{'~:file-id':'~u'+report['file_id'],'~:page-id':'~u'+report['page_id'],
                     '~:object-id':'~u'+report['shape_id'],'~:type':'~:png','~:suffix':'',
                     '~:scale':1,'~:name':'pgfire-rectangle'}]}
request=urllib.request.Request(base+'/api/export',data=json.dumps(params).encode(),
                               headers={'Content-Type':'application/transit+json'})
try:
    with client.open(request,timeout=120) as response:
        resource=json.load(response)
except urllib.error.HTTPError as error:
    raise RuntimeError(f'PNG export HTTP {error.code}: {error.read().decode()[:2000]}') from error
(root/'artifacts/http-render-resource.json').write_text(json.dumps(resource,indent=2)+'\n')
uri=resource['~:uri']
if isinstance(uri,dict): uri=uri['~#uri']
# Exporter uses its internal frontend origin; retrieve that same asset through the host frontend.
uri=base+urllib.parse.urlsplit(uri).path
with client.open(uri,timeout=30) as response:
    image=response.read()
assert image.startswith(b'\x89PNG\r\n\x1a\n')
width,height=struct.unpack('>II',image[16:24])
assert (width,height)==(320,180),(width,height)
path=root/'artifacts/pgfire-rectangle.png'
path.write_bytes(image)
result={'passed':True,'width':width,'height':height,'bytes':len(image),'sha256':hashlib.sha256(image).hexdigest()}
(root/'artifacts/http-render.json').write_text(json.dumps(result,indent=2)+'\n')
print(f'\033]8;;{path.resolve().as_uri()}\033\\PNG export\033]8;;\033\\: {width} × {height}')
