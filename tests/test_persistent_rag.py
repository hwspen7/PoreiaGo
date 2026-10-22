"""Persistent RAG regression suite; never loads or downloads model weights."""
from __future__ import annotations

from dataclasses import replace
import json
import sys
import types

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.config import settings
from aligo.indexing import SQLiteRetrievalIndex
from aligo.memory import MemoryStore
from aligo.models import ModelGateway
from aligo.retrieval import Retrieval
from aligo.retrieval_eval import evaluate_search


class MockEncoder:
    calls=[]
    tokenizer=None

    def encode(self,texts,**kwargs):
        self.calls.append(list(texts))
        def embed(text):
            text=text.casefold()
            return [1.0,0.0] if any(w in text for w in ('coast','sea','beach','shore')) else [0.0,1.0]
        return [embed(t) for t in texts]


class MockReranker:
    def __init__(self,model):
        self.model=model

    def predict(self,pairs):
        return [float('coast' in passage.lower()) for _,passage in pairs]


def make_retrieval(tmp_path,mode='hybrid'):
    store=MemoryStore(tmp_path/'rag.sqlite3')
    models=ModelGateway(settings)
    backend=Retrieval(store,models,replace(settings,retrieval_mode=mode))
    encoder=MockEncoder()
    backend._get_embedder=lambda:encoder
    return store,backend,encoder


def test_persisted_vectors_not_reencoded_per_query(tmp_path):
    store,engine,encoder=make_retrieval(tmp_path,mode='dense')
    doc_id=store.add_document('alice','Coastal guide','A quiet coastal trail with sea views.')
    result=engine.index_document('alice',doc_id)
    assert result['embedded']>0
    recorded=len(encoder.calls)
    engine.search('alice','sea scenery')
    engine.search('alice','beach')
    assert len(encoder.calls)==recorded+2
    # Reinitialize with same SQLite database to establish actual persistence.
    store2=MemoryStore(tmp_path/'rag.sqlite3')
    engine2=Retrieval(store2,ModelGateway(settings),replace(settings,retrieval_mode='lexical'))
    assert engine2.index.stats('alice',engine2.models.embedding_model_id())['embedded_chunks']>0
    assert engine2.search('alice','coastal')['hits'][0]['document_id']==doc_id


def test_incremental_edit_and_delete_invalidate_vectors(tmp_path):
    store,engine,_=make_retrieval(tmp_path,mode='dense')
    doc_a=store.add_document('one','Coast','Beaches by the sea')
    doc_b=store.add_document('one','Trains','Airport express information')
    engine.index_document('one',doc_a)
    engine.index_document('one',doc_b)
    assert len(engine.index.records('one',engine.models.embedding_model_id()))==2
    store.update_document('one',doc_a,'Mountain trails','Mountain hiking trails.')
    assert all(row['document_id']!=doc_a for row in engine.index.records('one'))
    engine.index_document('one',doc_a)
    assert not engine.search('one','Beaches')['hits'] or all(
        'Beaches' not in result['text'] for result in engine.search('one','Beaches')['hits'])
    engine.delete_indexed_document('one',doc_b)
    store.delete_document('one',doc_b)
    assert all(result['document_id']!=doc_b for result in engine.search('one','mountain')['hits'])
    assert engine.index.stats('one',engine.models.embedding_model_id())['documents']==1


def test_multi_user_index_isolation_and_deletion(tmp_path):
    store,engine,_=make_retrieval(tmp_path,mode='lexical')
    alpha=store.add_document('alice','Private policy','hotel reimbursement limit 100')
    beta=store.add_document('bob','Private policy','hotel reimbursement limit 200')
    engine.index_document('alice',alpha)
    engine.index_document('bob',beta)
    assert engine.index.stats('alice',engine.models.embedding_model_id())['chunks']==1
    assert engine.index.stats('bob',engine.models.embedding_model_id())['chunks']==1
    assert [r['document_id'] for r in engine.search('alice','hotel reimbursement')['hits']]==[alpha]
    store.delete_user('alice')
    assert engine.index.records('alice')==[]
    assert len(engine.index.records('bob'))==1


def test_model_switch_requires_reindex(tmp_path,monkeypatch):
    store,engine,_=make_retrieval(tmp_path,mode='dense')
    doc=store.add_document('alice','Coast','A coastal walk by the sea.')
    engine.index_document('alice',doc)
    assert engine.index.stats('alice',engine.models.embedding_model_id())['stale_chunks']==0
    monkeypatch.setenv('ALIGO_EMBEDDING_MODEL','example-org/alternative-embedding')
    result=engine.search('alice','coastal')
    assert result['mode']=='lexical'
    assert any('Reindex' in message for message in result['limitations'])
    assert engine.index.stats('alice','example-org/alternative-embedding')['stale_chunks']>0
    engine.reindex('alice',semantic=True)
    assert engine.index.stats('alice','example-org/alternative-embedding')['stale_chunks']==0


def test_injected_vector_backend_supported(tmp_path):
    store,engine,_=make_retrieval(tmp_path)
    alternate=SQLiteRetrievalIndex(store)
    other=Retrieval(store,engine.models,engine.settings,index_backend=alternate)
    assert other.index is alternate


def test_dense_uses_only_indexed_vectors_for_active_model(tmp_path):
    store,engine,encoder=make_retrieval(tmp_path,mode='dense')
    doc=store.add_document('alice','Coast','A coastal trail by the sea.')
    engine.index_document('alice',doc)
    result=engine.search('alice','Where is the beach?')
    assert result['mode']=='dense'
    assert result['hits'][0]['document_id']==doc
    assert encoder.calls[-1][0].startswith('Instruct: ')
    assert len(encoder.calls[-1])==1


def test_hybrid_reranking_uses_expanded_pool(tmp_path,monkeypatch):
    store,engine,_=make_retrieval(tmp_path)
    doc1=store.add_document('alice','Coast','A coastal trail by the sea.')
    doc2=store.add_document('alice','Airport','Airport shuttle downtown')
    engine.index_document('alice',doc1)
    engine.index_document('alice',doc2)
    module=types.ModuleType('sentence_transformers')
    module.CrossEncoder=MockReranker
    monkeypatch.setitem(sys.modules,'sentence_transformers',module)
    result=engine.search('alice','coastal',k=1)
    assert result['mode']=='hybrid'
    assert result['hits'][0]['document_id']==doc1
    assert 'rerank_score' in result['hits'][0]


def test_metrics_exact_values_and_empty_prohibition():
    cases=[{'id':'first','query':'x','relevant_document_ids':[3]},
           {'id':'second','query':'y','relevant_document_ids':[1]}]
    hits={'x':[{'document_id':3},{'document_id':2}], 'y':[{'document_id':2},{'document_id':1}]}
    report=evaluate_search(lambda query,k:hits[query][:k],cases,cutoffs=(1,2))
    assert report['metrics']['recall@1']==0.5
    assert report['metrics']['mrr@2']==0.75
    assert report['metrics']['recall@2']==1.0
    with pytest.raises(ValueError):evaluate_search(lambda q,k:[],[])


@pytest.mark.asyncio
async def test_document_api_crud_and_scope(tmp_path):
    app=create_app(database_path=tmp_path/'web.sqlite3')
    app.state.retrieval.settings=replace(settings,retrieval_mode='lexical')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        created=await client.post('/api/knowledge/documents',json={
            'user_id':'one','title':'Railway','content':'The city railway station is centrally located.'})
        assert created.status_code==200
        doc_id=created.json()['document_id']
        status=(await client.get('/api/knowledge/index/status?user_id=one')).json()
        assert status['chunks']==1 and status['documents']==1
        updated=await client.put(f'/api/knowledge/documents/{doc_id}',json={
            'user_id':'one','title':'River','content':'A riverside cycling track is available.'})
        assert updated.status_code==200
        assert (await client.put(f'/api/knowledge/documents/{doc_id}',json={
            'user_id':'two','title':'Stolen','content':'Attempted overwrite'})).status_code==404
        search=(await client.get('/api/knowledge/search?user_id=one&query=riverside')).json()
        assert search['hits'] and search['hits'][0]['title']=='River'
        assert not (await client.get('/api/knowledge/search?user_id=two&query=riverside')).json()['hits']
        assert (await client.delete(f'/api/knowledge/documents/{doc_id}?user_id=two')).status_code==404
        assert (await client.delete(f'/api/knowledge/documents/{doc_id}?user_id=one')).json()['deleted']
        assert (await client.get('/api/knowledge/index/status?user_id=one')).json()['chunks']==0


@pytest.mark.asyncio
async def test_reindex_api_supports_lexical_only(tmp_path):
    app=create_app(database_path=tmp_path/'web.sqlite3')
    app.state.retrieval.settings=replace(settings,retrieval_mode='lexical')
    app.state.store.add_document('one','Rail','Trains leave every hour.')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        answer=await client.post('/api/knowledge/reindex',json={'user_id':'one','semantic':False})
        assert answer.status_code==200
        assert answer.json()['documents']==1
        assert answer.json()['status']['chunks']==1
