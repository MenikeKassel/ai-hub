"""Conservative open vocabulary discovery; confirmation creates research topics only."""
from __future__ import annotations

import hashlib
import json
import re
import logging
from functools import lru_cache
from theme_leads import ThemeCatalog, theme_evidence

# Discover explicit topic phrases without maintaining another sector whitelist.
_BOUNDARY = r'(?:^|[。！？?!\n，,；;：:\s“”()（）/、])'
_CUE = r'(?:(?:重点|继续|持续|正在|主要|近期|目前|未来|接下来|积极|相关)?(?:关注|研究|看好|推荐|布局|跟踪|梳理|聚焦|针对)\s*)?'
_TERM = r'([A-Z][A-Za-z0-9+/-]{1,11}|[\u4e00-\u9fffA-Za-z0-9]{2,8})'
_TOPIC = re.compile(_BOUNDARY + _CUE + _TERM + r'\s*(?:板块|赛道|产业链|行业|概念)')
_EXPLICIT = re.compile(_BOUNDARY + r'(?:关注|研究|看好|聚焦|跟踪)[：:\s]*' + _TERM + r'(?=[，,。；;\s])')
_RESEARCH_CONTEXT = re.compile(r'产能|供需|估值|业绩|产业|板块|赛道|需求|营收|技术|订单|公司|市场')
_PREFIX = re.compile(r'^(?:(?:重点|继续|持续|正在|主要|近期|目前|当下|未来|接下来|积极|相关)?(?:关注|研究|看好|推荐|布局|跟踪|梳理|聚焦|针对)|的|这个|整个|国内|关于|随着|对于|市场针对|市场关注)+')
_NOISE = re.compile(r'广告|优惠|返利|公众号|扫码|领取|加群|课程|私信|免费领取|欢迎交流|第一目标.*市值')
_STOP = {'这个','整个','整体','相关','所有','各个','传统','新兴','核心','优质','热门','重点','某个','股票','投资','市场','机会','很多','科技','证券','金融','机构','盈利','赚钱','其他','多个','行业','概念','题材','业绩','景气','办法','问问','核心业务','历史单','结构','内容','来源','资料'}
_SENTENCE = re.compile(r'正在|带来|引爆|引发|观察|增长|专家|梳理|整条|关注|研究|推荐|看好|目前|爆发|因为|由于|所以|为了|仍然|已经|这个|那个|多家|当前|强势|证实|景气度|同时|还有|缺乏|统一|验证|高于|市场|此前|可能|空前|要求|提供|作者|事件|我们|相声|证明|结果|各种|解释|进行|选择权|院长|理论|问题|机会|策略|观点|未来|趋势|是否|如何|什么|谁|哪|若|对|的|为|是|了|也|或|但|没|在|有|将|等')


@lru_cache(maxsize=4096)
def _noun_phrase(term):
    if re.fullmatch(r'[A-Z][A-Za-z0-9+/-]{1,11}',term):
        return True
    try:
        import jieba
        import jieba.posseg as posseg
    except ImportError:
        # Missing optional proposal tooling never blocks source indexing.
        return False
    jieba.setLogLevel(logging.ERROR)
    words = list(posseg.cut(term,HMM=False))
    technical_nouns = {'计算','制造','设计','封装','存储','运输','服务','显示','通信','发电','储能'}
    noun = lambda word: word.flag in {'n','nz','ng','eng','j'} or word.word in technical_nouns
    return bool(words and not any(word.word.isdigit() for word in words) and noun(words[-1]) and any(noun(word) for word in words)
        and all(noun(word) or word.flag in {'b','a'} for word in words))


def discover_topics(post, catalog):
    if post.get('is_aggregation') or post.get('post_type') in {'aggregation','retweet'}:
        return []
    found = {}
    fields = ('text','article_text') + (() if post.get('post_type') == 'answer' else ('article_title',))
    for field in fields:
        text = str(post.get(field) or '')
        if _NOISE.search(text) or (re.search(r'复盘|回顾',text[:150]) and len(re.findall(r'[/、]',text)) >= 5):
            continue
        matches = list(_TOPIC.finditer(text))
        if _RESEARCH_CONTEXT.search(text):
            matches.extend(_EXPLICIT.finditer(text))
        for match in matches:
            term = _PREFIX.sub('', match.group(1)).strip()
            term = re.sub(r'^(?:比如|前者|后者|代表|连带|季度|明确指出|子公司)+','',term)
            # Coordinated nouns and possessives are boundaries, not topic names.
            term = re.split(r'以及|和|与|的',term)[-1]
            if len(term) < 2 or term in _STOP or term.endswith('的') or term.endswith(('高景气','影子','题材')):
                continue
            if _SENTENCE.search(term):
                continue
            if not _noun_phrase(term):
                continue
            if any(rule['term'].casefold() == term.casefold() for theme in catalog.themes for rule in theme['terms']):
                continue
            local = text[max(0,match.start()-40):match.end()+80]
            if _NOISE.search(local):
                continue
            synthetic = ThemeCatalog(catalog.version, ({'id':'candidate','name':term,'terms':[{'term':term,'require_any':[],'context_chars':60}], 'mapped_symbols':[]},))
            leads = theme_evidence(post, synthetic)
            if not leads:
                continue
            lead = leads[0]
            if lead['source_role'] == 'quoted' or lead['kind'] == 'product':
                continue
            # Recaps remain discoverable with their correct role, never current calls.
            found[term] = {'term': term, **lead}
    return list(found.values())[:12]


def candidates(store, limit=50):
    with store.connect() as db:
        rows = db.execute("SELECT c.*,(SELECT COUNT(*) FROM theme_candidate_evidence e WHERE e.candidate_id=c.id) evidence_count "
            "FROM theme_candidates c WHERE c.status='pending' AND EXISTS(SELECT 1 FROM theme_candidate_evidence e WHERE e.candidate_id=c.id) ORDER BY evidence_count DESC,c.id DESC LIMIT ?", (limit,)).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            evidence = db.execute('SELECT e.kind,e.source_role,e.evidence_json,e.observation_id,p.post_id,p.author_name,p.url,p.posted_at '
                'FROM theme_candidate_evidence e JOIN posts p ON p.post_id=e.post_id WHERE candidate_id=? ORDER BY p.posted_at DESC LIMIT 3', (row['id'],)).fetchall()
            value['evidence'] = [{**{k:r[k] for k in r.keys() if k != 'evidence_json'}, 'spans':json.loads(r['evidence_json'])} for r in evidence]
            values.append(value)
    return values


def decide_candidate(store, candidate_id, action):
    if action not in {'accept','reject'}:
        raise ValueError('invalid candidate action')
    from kol_tracker import now_iso
    with store.connect() as db:
        row = db.execute('SELECT * FROM theme_candidates WHERE id=?', (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        if row['status'] != 'pending':
            return dict(row)
        theme_id = 'topic_' + hashlib.sha256(row['term'].casefold().encode()).hexdigest()[:16]
        if action == 'accept':
            db.execute('INSERT OR IGNORE INTO research_themes VALUES(?,?,?,?)',
                (theme_id, row['term'], json.dumps([row['term']], ensure_ascii=False), now_iso()))
        db.execute('UPDATE theme_candidates SET status=?,theme_id=? WHERE id=?',
            ('accepted' if action == 'accept' else 'rejected', theme_id if action == 'accept' else '', candidate_id))
    return {'id':candidate_id,'status':'accepted' if action == 'accept' else 'rejected','theme_id':theme_id if action == 'accept' else ''}


def register_topic(store, term):
    term = str(term).strip()
    if not re.fullmatch(r'[A-Za-z0-9\u4e00-\u9fff+/-]{2,24}',term):
        raise ValueError('研究主题须为 2–24 个中英文或数字字符')
    from kol_tracker import now_iso
    theme_id = 'topic_' + hashlib.sha256(term.casefold().encode()).hexdigest()[:16]
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO research_themes VALUES(?,?,?,?)', (theme_id,term,json.dumps([term],ensure_ascii=False),now_iso()))
    return {'theme_id':theme_id,'name':term,'association':'research_only'}
