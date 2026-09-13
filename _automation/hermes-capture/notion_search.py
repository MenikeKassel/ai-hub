# -*- coding: utf-8 -*-
import sys, os, json, urllib.request

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

KEY = os.environ.get('NOTION_API_KEY', '')
if not KEY:
    print('NO NOTION_API_KEY'); sys.exit(1)

def api(method, url, body=None):
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', 'Bearer ' + KEY)
    req.add_header('Notion-Version', '2022-06-28')
    req.add_header('Content-Type', 'application/json')
    data = json.dumps(body).encode('utf-8') if body is not None else None
    with urllib.request.urlopen(req, data=data) as resp:
        return json.loads(resp.read().decode('utf-8'))

def page_title(r):
    props = r.get('properties', {})
    for k, v in props.items():
        if v.get('type') == 'title':
            return ''.join(t.get('plain_text', '') for t in v.get('title', []))
    return ''

queries = sys.argv[1:] if len(sys.argv) > 1 else ['制造']
for q in queries:
    try:
        d = api('POST', 'https://api.notion.com/v1/search', {'query': q, 'page_size': 25})
    except Exception as e:
        print(f'[{q}] error: {e}'); continue
    res = d.get('results', [])
    print(f'===== 查询: {q} （{len(res)} 结果，has_more={d.get("has_more")}） =====')
    for r in res:
        pid = r.get('id', '')
        if r.get('object') == 'page':
            print(f'  PAGE | {page_title(r)} | parent={r.get("parent",{}).get("type")} | {pid}')
        else:
            tt = ''
            t = r.get('title')
            if isinstance(t, list):
                tt = ''.join(x.get('plain_text','') for x in t)
            print(f'  DB   | {tt} | {pid}')