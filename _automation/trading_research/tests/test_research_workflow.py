from dataclasses import replace
from datetime import datetime
from pathlib import Path
import json
import sys
from unittest.mock import patch

import pytest
from filelock import FileLock
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kol_posts import KolPostStore, normalise_twitter_post
from kol_sources.core import ProviderFetchResult
from kol_sources.coverage import record_surfaces, surface_coverage
from kol_sources.observations import list_observations
from kol_tracker import SHANGHAI
from research_topics import candidates, decide_candidate
from research_workflow import ResearchWorkflow
from theme_leads import query_theme_leads, ThemeCatalog, theme_evidence
from kol_api import ApiSettings, create_app
from runtime_jobs import initialize_schema

NOW = datetime(2026,10,3,8,tzinfo=SHANGHAI)


def inventory(root):
    store = KolPostStore(root/'posts.db',root/'media')
    kol_id,_ = store.add_kol('原作者','author')
    return store,store.get_kol(kol_id)


def post(store,kol,text,post_id='90001',posted='2026-10-02T12:00:00+08:00',updated='',fetched='2026-10-02T13:00:00+08:00'):
    record = normalise_twitter_post({'id':post_id,'text':text,'createdAtISO':posted,
        'updatedAtISO':updated,'author':{'screenName':kol['handle'],'name':kol['display_name']}},kol)
    record = replace(record,fetched_at=fetched)
    store.upsert_post(record)
    return record


def worker(store):
    return ResearchWorkflow(store,now_provider=lambda:NOW)


def test_edited_post_preserves_approval_evidence_and_alerts_withdrawal(tmp_path):
    store,kol = inventory(tmp_path)
    original = post(store,kol,'看好黄酒板块机会。',updated='2026-10-02T12:00:00+08:00')
    workflow = worker(store)
    assert workflow.drain()['failed'] == 0
    before = workflow.digest('2026-10-03')['total']
    edited = post(store,kol,'黄酒板块不再推荐，市场情况改变。',updated='2026-10-03T06:00:00+08:00',fetched='2026-10-03T07:00:00+08:00')
    assert store.get_post(original.post_id)['text'] == original.text
    assert workflow.drain()['processed'] == 1
    assert '不再推荐' in query_theme_leads(store)['items'][0]['text']
    assert query_theme_leads(store,query='市场情况改变')['total'] == 1
    item = workflow.digest('2026-10-03')['items'][0]
    assert item['kind'] == 'withdrawal'
    assert item['payload']['edited'] is True
    assert item['payload']['source_updated_at'] == '2026-10-03T06:00:00+08:00'
    assert len(list_observations(store,original.post_id)) == 2
    post(store,kol,original.text,updated='2026-10-02T12:00:00+08:00',fetched='2026-10-03T07:30:00+08:00')
    assert workflow.drain()['processed'] == 0
    assert workflow.digest('2026-10-03')['total'] == before+1
    assert store.get_post(edited.post_id)['content_hash'] == original.content_hash


def test_unknown_edit_time_cannot_revert_to_already_seen_body(tmp_path):
    store,kol = inventory(tmp_path)
    original = post(store,kol,'黄酒板块值得研究。')
    post(store,kol,'黄酒板块研究更新。',fetched='2026-10-03T07:00:00+08:00')
    post(store,kol,original.text,fetched='2026-10-03T07:30:00+08:00')
    worker(store).drain()
    assert query_theme_leads(store)['items'][0]['text'] == '黄酒板块研究更新。'


def test_later_edit_does_not_backdate_earliest_theme_source(tmp_path):
    store,kol = inventory(tmp_path)
    post(store,kol,'今天研究其他主题。',posted='2026-09-01T12:00:00+08:00')
    post(store,kol,'黄酒板块值得研究。','90002',posted='2026-09-02T12:00:00+08:00')
    post(store,kol,'关注黄酒板块。',posted='2026-09-01T12:00:00+08:00',updated='2026-10-03T06:00:00+08:00',fetched='2026-10-03T07:00:00+08:00')
    worker(store).drain()
    summary = query_theme_leads(store)['summary'][0]
    assert summary['first_post_id'] == '90002'
    assert summary['first_evidence_at'] == '2026-09-02T12:00:00+08:00'


def test_parser_hash_change_preserves_original_body_and_ocr_version(tmp_path):
    store,kol = inventory(tmp_path)
    original = post(store,kol,'黄酒板块值得研究。')
    store.upsert_post(replace(original,content_hash='changed-parser-hash',fetched_at='2026-10-03T07:00:00+08:00'))
    worker(store).drain()
    current = next(v for v in list_observations(store,original.post_id) if v['is_current'])
    assert current['content_hash'] == original.content_hash


def test_schema_failure_rolls_back_alter_and_version_marker(tmp_path):
    store,_ = inventory(tmp_path)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        initialize_schema(store.path,lambda:None,migrations=((99,'ALTER TABLE posts ADD COLUMN partial_column TEXT; SELECT * FROM missing_table;'),))
    with store.connect() as db:
        assert 'partial_column' not in {r['name'] for r in db.execute('PRAGMA table_info(posts)')}
        assert db.execute('SELECT 1 FROM workbench_schema_migrations WHERE version=99').fetchone() is None


def test_research_summary_and_media_attribution_survive_clause_boundaries():
    catalog = ThemeCatalog('test',({'id':'storage','name':'储能','terms':[{'term':'储能','require_any':[],'context_chars':60}],'mapped_symbols':[]},))
    for text in ('【某机构电子】中报点评：季度情况改善。\n未来储能需求有望增长。',
                 '有媒体报道，储能行业增长。\n作者另谈其他行业。'):
        lead = theme_evidence({'text':text,'posted_at':NOW.isoformat()},catalog)[0]
        assert lead['source_role'] == 'secondhand'
        assert lead['kind'] == 'secondhand'
    lead = theme_evidence({'text':'有媒体报道其他行业。\n我看好储能。','posted_at':NOW.isoformat()},catalog)[0]
    assert lead['source_role'] == 'original'


def test_news_recap_does_not_become_a_fresh_industry_call():
    catalog = ThemeCatalog('test',({'id':'storage','name':'储能','terms':[{'term':'储能','require_any':[],'context_chars':60}],'mapped_symbols':[]},))
    text = '• A股收盘：指数上行\n• 板块：消费上涨\n• 新闻：供需改善\n未来储能产业链可能有机会。'
    assert theme_evidence({'text':text,'posted_at':NOW.isoformat()},catalog)[0]['kind'] == 'retrospective'


def test_technical_acronym_does_not_match_inside_an_english_word():
    catalog = ThemeCatalog('test',({'id':'sic','name':'SiC','terms':[{'term':'SiC','require_any':[],'context_chars':60}],'mapped_symbols':[]},))
    assert theme_evidence({'text':'Basic music research.'},catalog) == []
    assert theme_evidence({'text':'关注SiC产业链。'},catalog)[0]['matched_terms'] == ['SiC']


def test_earliest_original_discussion_excludes_recaps_and_withdrawals(tmp_path):
    store,kol = inventory(tmp_path)
    post(store,kol,'直播复盘总结：新消费黄酒加个会稽山。',posted='2026-09-01T12:00:00+08:00')
    post(store,kol,'黄酒不再推荐。','90002',posted='2026-09-02T12:00:00+08:00')
    workflow = worker(store);workflow.drain()
    summary = query_theme_leads(store,theme_id='huangjiu')['summary'][0]
    assert summary['first_original_post_id'] == '90001'
    assert summary['first_research_post_id'] is None
    post(store,kol,'黄酒消费局限于江南，值得研究。','90003',posted='2026-09-03T12:00:00+08:00')
    workflow.drain()
    summary = query_theme_leads(store,theme_id='huangjiu')['summary'][0]
    assert summary['first_research_post_id'] == '90003'
    assert summary['research_count'] == 1


def test_open_topic_confirmation_only_changes_research_registry(tmp_path):
    store,kol = inventory(tmp_path)
    post(store,kol,'重点关注光子计算产业链，后续研究技术进展。')
    workflow = worker(store)
    workflow.drain()
    candidate = next(c for c in candidates(store) if c['term']=='光子计算')
    assert query_theme_leads(store)['total'] == 0
    accepted = decide_candidate(store,candidate['id'],'accept')
    workflow.drain()
    assert query_theme_leads(store,theme_id=accepted['theme_id'])['total'] == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM stock_leads').fetchone()[0] == 0
    item = workflow.digest('2026-10-03')['items'][0]
    workflow.acknowledge(item['id'])
    assert workflow.digest('2026-10-03')['unread'] < workflow.digest('2026-10-03')['total']


def test_history_replay_and_question_ads_do_not_create_current_digest(tmp_path):
    store,kol = inventory(tmp_path)
    post(store,kol,'黄酒板块值得研究。','90001',posted='2026-09-02T09:00:00+08:00')
    post(store,kol,'扫码领取课程，关注光子计算板块。','90002')
    workflow = worker(store)
    workflow.drain()
    assert workflow.digest('2026-10-03')['total'] == 0
    assert candidates(store) == []
    with pytest.raises(ValueError):
        workflow.digest('2026-09-03')


def test_digest_pagination_keeps_older_unread_items_reachable(tmp_path):
    store,_ = inventory(tmp_path)
    workflow = worker(store)
    with store.connect() as db:
        db.executemany('INSERT INTO research_items(dedupe_key,kind,payload_json,detected_at,delivery_date) VALUES(?,?,?,?,?)',
            [(str(i),'coverage_gap','{}',NOW.isoformat(),'2026-10-03') for i in range(205)])
    first = workflow.digest('2026-10-03')
    # Acknowledgement during reading must not change page membership.
    workflow.acknowledge(first['items'][0]['id'])
    second = workflow.digest('2026-10-03',before_id=first['next_cursor'])
    third = workflow.digest('2026-10-03',before_id=second['next_cursor'])
    ids = [item['id'] for page in (first,second,third) for item in page['items']]
    assert len(ids) == len(set(ids)) == 205
    assert third['next_cursor'] == 0
    assert third['total'] == 205 and third['unread'] == 204


def test_failure_rolls_back_marker_delivery_and_retries_durably(tmp_path):
    store,kol = inventory(tmp_path)
    post(store,kol,'黄酒板块值得研究。')
    workflow = worker(store)
    with patch('research_workflow.discover_topics',side_effect=RuntimeError('interrupted')):
        assert workflow.drain()['failed'] == 1
    assert workflow.status()['failed'] == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM theme_extractions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM research_items').fetchone()[0] == 0
        db.execute("UPDATE research_jobs SET next_retry_at=''")
    assert worker(store).drain()['processed'] == 1
    assert worker(store).status()['pending'] == 0


def test_per_surface_empty_partial_failure_and_dry_range(tmp_path):
    store,kol = inventory(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE kols SET platform='Zhihu' WHERE id=?",(kol['id'],))
    kol = store.get_kol(kol['id'])
    result = ProviderFetchResult('zhihu-local',[],[],[],surfaces={
        'answers':{'status':'empty','exhausted':True},'articles':{'status':'partial','received_count':2},'ideas':{'status':'failed'}})
    record_surfaces(store,'empty-test',kol,result=result,surfaces=('answers','articles','ideas'),recorded_at=NOW.isoformat())
    coverage = surface_coverage(store,now=NOW)
    assert coverage['gap_count'] == 2
    assert {i['surface']:i['status'] for i in coverage['items']} == {'answers':'range_complete','articles':'partial','ideas':'failed'}
    assert not coverage['historical_complete']


def test_console_starts_with_locked_market_and_provider_offline(tmp_path):
    runtime = tmp_path/'runtime'
    market = runtime/'market'
    market.mkdir(parents=True)
    with FileLock(str(market/'.market.lock')):
        app = create_app(ApiSettings(runtime_root=runtime,frontend_dist=tmp_path/'dist',codex_schema=Path(__file__).resolve().parents[1]/'kol_classifier_schema.json'))
        with TestClient(app) as client:
            assert client.get('/api/ready').json()['ok'] is True
            health = client.get('/api/system/health').json()
            assert health['process_ready'] is True
            assert health['business_ok'] is False
            assert client.get('/api/theme-leads').status_code == 200
            response = client.get('/api/instruments')
            assert response.status_code == 503
    with TestClient(app) as client:
        assert client.get('/api/instruments').status_code == 200
