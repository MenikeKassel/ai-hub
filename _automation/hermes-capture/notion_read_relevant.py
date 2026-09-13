# -*- coding: utf-8 -*-
import sys, os, json, urllib.request

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass
KEY = os.environ.get('NOTION_API_KEY', '')
def api(method, url, body=None, v='2022-06-28'):
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', 'Bearer ' + KEY)
    req.add_header('Notion-Version', v)
    req.add_header('Content-Type', 'application/json')
    data = json.dumps(body).encode('utf-8') if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {'error': e.code, 'body': e.read().decode('utf-8','replace')}

# 1. Obsidian制造业知识库建设方案 page
pid='3c752633-5cd6-81c6-b24e-c4b5949ae6b1'
print('========== PAGE: Obsidian制造业知识库建设方案 ==========')
md = api('GET', f'https://api.notion.com/v1/pages/{pid}/markdown', v='2025-09-03')
if 'markdown' in md:
    print(md['markdown'][:2000])
else:
    print(md)
    print(api('GET', f'https://api.notion.com/v1/pages/{pid}'))

# 2. 法士特 page
pid2='2a752633-5cd6-802f-a93a-e243598906e4'
print('\n========== PAGE: 法士特 ==========')
md2 = api('GET', f'https://api.notion.com/v1/pages/{pid2}/markdown', v='2025-09-03')
if 'markdown' in md2:
    print(md2['markdown'][:1200])
else:
    print(md2)

# 3. 法士特问问题 DB
db='28d52633-5cd6-804d-b6a5-dc2242d7b46c'
print('\n========== DB: 法士特问问题 (query rows) ==========')
q = api('POST', f'https://api.notion.com/v1/databases/{db}/query', {'page_size': 20})
if 'error' in q:
    print(q)
else:
    print('rows:', len(q.get('results',[])), 'has_more:', q.get('has_more'))
    for r in q.get('results',[]):
        props=r.get('properties',{})
        vals=[]
        for k,v in props.items():
            t=v.get('type')
            if t=='title':
                s=''.join(x.get('plain_text','') for x in v.get('title',[]))
            elif t=='rich_text':
                s=''.join(x.get('plain_text','') for x in v.get('rich_text',[]))
            elif t=='select':
                s=(v.get('select') or {}).get('name','')
            elif t=='date':
                s=str((v.get('date') or {}).get('start',''))
            else:
                s=''
            if s: vals.append(f'{k}={s}')
        print('  -', ' | '.join(vals)[:300])