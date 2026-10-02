#!/usr/bin/env python3
"""Exercise Penpot's HTTP API through the frontend against pgfire."""
import argparse
import copy
import hashlib
import io
import zipfile
import http.cookiejar
import json
from pathlib import Path
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
(ROOT / 'artifacts').mkdir(exist_ok=True)
BASE = 'http://localhost:59001'
jar = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
checks = []

def rpc(method, params, get=False, raw=False):
    url = BASE + '/api/main/methods/' + method
    if get:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=None if get else json.dumps(params).encode(),
        headers={'Content-Type':'application/json','Accept':'application/json'})
    try:
        with client.open(request,timeout=60) as response:
            data=response.read()
            checks.append({'method':method,'status':response.status})
            return data if raw else json.loads(data or b'null')
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'{method}: HTTP {error.code}: {error.read().decode()[:2000]}') from error

email='pgfire.acceptance@example.com'
password='Pgfire-local-only-2026!'
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--resume',action='store_true')
args=parser.parse_args()
if args.resume:
    report=json.loads((ROOT/'artifacts/http-smoke.json').read_text())
    checks.extend(report['checks'])
    profile=rpc('login-with-password',{'email':email,'password':password})
    file=rpc('get-file',{'id':report['file_id']})
else:
    prepared=rpc('prepare-register-profile',{'fullname':'pgfire Acceptance','email':email,'password':password,'accept-newsletter-updates':False})
    rpc('register-profile',{'token':prepared['token'],'accept-newsletter-updates':False})
    profile=rpc('login-with-password',{'email':email,'password':password})
    assert profile['email']==email
    team=rpc('create-team',{'name':'pgfire Acceptance'})
    project=rpc('create-project',{'team-id':team['id'],'name':'Firestore recovery demo'})
    file=rpc('create-file',{'project-id':project['id'],'name':'pgfire Acceptance'})
    report={'profile_id':profile['id'],'team_id':team['id'],'project_id':project['id'],'file_id':file['id']}
page_id=file['data']['pages'][0]
zero='00000000-0000-0000-0000-000000000000'
shape_id=str(uuid.uuid4())
shape=copy.deepcopy(file['data']['pagesIndex'][page_id]['objects'][zero])
shape.update(id=shape_id,name='Firestore verified rectangle',type='rect',x=80,y=80,width=320,height=180,
             proportion=320/180,shapes=[],fills=[{'fillColor':'#355CFF','fillOpacity':1}],
             selrect={'x':80,'y':80,'width':320,'height':180,'x1':80,'y1':80,'x2':400,'y2':260},
             points=[{'x':80,'y':80},{'x':400,'y':80},{'x':400,'y':260},{'x':80,'y':260}])
rpc('update-file',{'id':file['id'],'session-id':str(uuid.uuid4()),'revn':file['revn'],'vern':file['vern'],
                  'changes':[{'type':'mod-page','id':page_id,'name':'Firestore verified'},
                             {'type':'add-obj','id':shape_id,'page-id':page_id,'frame-id':zero,'parent-id':zero,'obj':shape}]})
loaded=rpc('get-file',{'id':file['id']})
assert loaded['data']['pagesIndex'][page_id]['name']=='Firestore verified'
assert loaded['data']['pagesIndex'][page_id]['objects'][shape_id]['name']=='Firestore verified rectangle'
comment=rpc('create-comment-thread',{'file-id':file['id'],'page-id':page_id,'frame-id':zero,
             'position':{'x':120,'y':120},'content':'Verified with Firestore WAL durability.'})
comments=rpc('get-comments',{'thread-id':comment['id']})
assert any(c['content']=='Verified with Firestore WAL durability.' for c in comments)
report.update(page_id=page_id,shape_id=shape_id,comment_thread_id=comment['id'],checks=checks)
(ROOT/'artifacts/http-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
events=rpc('export-binfile',{'file-id':file['id'],'include-libraries':False,'embed-assets':True},raw=True)
(ROOT/'artifacts/http-export-events.txt').write_bytes(events)
print('Created profile, logged in, created team/project/file, edited a rectangle, and read the saved file and comment.')
end=None
for block in events.decode().split('\n\n'):
    if block.startswith('event: end\n'):
        end=json.loads(block.split('data: ',1)[1])['~#uri']
assert end and end.startswith(BASE+'/assets/by-id/'), 'export did not finish successfully'
with client.open(end,timeout=60) as response:
    exported=response.read()
archive=zipfile.ZipFile(io.BytesIO(exported))
assert archive.testzip() is None
assert any(b'Firestore verified rectangle' in archive.read(name) for name in archive.namelist() if not name.endswith('/'))
path=ROOT/'artifacts/pgfire-acceptance.penpot'
path.write_bytes(exported)
report.update(checks=checks,passed=True,export_bytes=len(exported),export_sha256=hashlib.sha256(exported).hexdigest(),
              export_members=archive.namelist(),file_url=BASE+f"/#/workspace?team-id={report['team_id']}&file-id={file['id']}&page-id={page_id}")
(ROOT/'artifacts/http-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
(ROOT/'artifacts/registration-prepared.json').unlink(missing_ok=True)
print(f'\033]8;;{path.resolve().as_uri()}\033\\Exported Penpot file\033]8;;\033\\ ({len(exported)} bytes; ZIP content verified)')
