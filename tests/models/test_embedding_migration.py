"""Offline regression tests for the English-first multilingual retrieval migration."""
from __future__ import annotations

from dataclasses import replace
import sys
import types

import numpy as np
import pytest

from core.config import settings
from intelligence.models import ModelGateway
from storage.retrieval import Retrieval


class DocumentStore:
    def documents(self, user_id):
        return [{'id': 1, 'title': 'Travel guide', 'content': 'A multilingual travel document about museums, food, and local transport. ' * 20}]


class FakeTokenizer:
    def encode(self, content, add_special_tokens=False):
        assert not add_special_tokens
        return list(range(len(content.split())))

    def decode(self, values, skip_special_tokens=True):
        assert skip_special_tokens
        return ' '.join('token' + str(value) for value in values)


class FakeSentenceTransformer:
    created = []
    encoded = []

    def __init__(self, model_id):
        self.created.append(model_id)
        self.tokenizer = FakeTokenizer()

    def encode(self, texts, **kwargs):
        self.encoded.append(list(texts))
        assert kwargs['normalize_embeddings'] is True
        return np.array([[1.0, 0.0]] + [[0.9, 0.1] for _ in texts[1:]])


class FakeCrossEncoder:
    created = []
    predicted = []

    def __init__(self, model_id):
        self.created.append(model_id)

    def predict(self, pairs):
        self.predicted.append(list(pairs))
        return np.array([float(i) for i in range(len(pairs))])


def fake_cos_sim(query, docs):
    return np.array([[0.8 for _ in docs]])


def install_fake_ml(monkeypatch):
    FakeSentenceTransformer.created.clear()
    FakeSentenceTransformer.encoded.clear()
    FakeCrossEncoder.created.clear()
    FakeCrossEncoder.predicted.clear()
    pkg = types.ModuleType('sentence_transformers')
    pkg.__path__ = []
    pkg.SentenceTransformer = FakeSentenceTransformer
    pkg.CrossEncoder = FakeCrossEncoder
    util = types.ModuleType('sentence_transformers.util')
    util.cos_sim = fake_cos_sim
    monkeypatch.setitem(sys.modules, 'sentence_transformers', pkg)
    monkeypatch.setitem(sys.modules, 'sentence_transformers.util', util)


def test_default_models_are_english_first_and_multilingual(monkeypatch):
    monkeypatch.delenv('POREIAGO_EMBEDDING_MODEL', raising=False)
    monkeypatch.delenv('POREIAGO_RERANKER_MODEL', raising=False)
    gateway = ModelGateway(settings)
    assert gateway.embedding_model_id() == 'intfloat/multilingual-e5-large-instruct'
    assert gateway.reranker_model_id() == 'mixedbread-ai/mxbai-rerank-base-v2'
    assert gateway.embedding_query('Find scenic cycling trips').startswith('Instruct: ')
    assert '\nQuery: Find scenic cycling trips' in gateway.embedding_query('Find scenic cycling trips')


def test_override_does_not_apply_e5_instructions_to_other_models(monkeypatch):
    monkeypatch.setenv('POREIAGO_EMBEDDING_MODEL', 'example-org/another-embedding-model')
    gateway = ModelGateway(settings)
    assert gateway.embedding_model_id() == 'example-org/another-embedding-model'
    assert gateway.embedding_query('Find a museum') == 'Find a museum'


def test_dense_uses_instruction_only_for_query(monkeypatch):
    install_fake_ml(monkeypatch)
    monkeypatch.delenv('POREIAGO_EMBEDDING_MODEL', raising=False)
    backend = Retrieval(DocumentStore(), ModelGateway(settings), replace(settings, retrieval_mode='dense'))
    result = backend.search('user', 'quiet museums', 2)
    assert result['mode'] == 'dense'
    assert FakeSentenceTransformer.created == ['intfloat/multilingual-e5-large-instruct']
    assert FakeSentenceTransformer.encoded[0][0].startswith('Instruct: ')
    assert '\nQuery: quiet museums' in FakeSentenceTransformer.encoded[0][0]
    assert all(not text.startswith('Instruct:') for text in FakeSentenceTransformer.encoded[0][1:])
    assert result['hits']


def test_hybrid_uses_configured_multilingual_reranker(monkeypatch):
    install_fake_ml(monkeypatch)
    monkeypatch.delenv('POREIAGO_EMBEDDING_MODEL', raising=False)
    monkeypatch.delenv('POREIAGO_RERANKER_MODEL', raising=False)
    backend = Retrieval(DocumentStore(), ModelGateway(settings), replace(settings, retrieval_mode='hybrid'))
    result = backend.search('user', 'food transport', 2)
    assert result['mode'] == 'hybrid'
    assert FakeCrossEncoder.created == ['mixedbread-ai/mxbai-rerank-base-v2']
    assert all(pair[0] == 'food transport' for pair in FakeCrossEncoder.predicted[0])


def test_token_based_chunking_is_bounded_for_multilingual_documents():
    chunks = Retrieval._segments([{'id': 2, 'title': 'Example', 'content': ' '.join(str(i) for i in range(1300))}], FakeTokenizer(), budget=448, overlap=48)
    assert len(chunks) >= 3
    assert chunks[0]['offset'] == 0
    assert chunks[1]['offset'] == 400
    assert all(len(chunk['text'].split()) <= 448 for chunk in chunks)
