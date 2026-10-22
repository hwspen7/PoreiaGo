"""Optional real-model smoke test. Downloads model weights on first run.

Run only after installing requirements-ml.txt. No network calls are made by
regular pytest unit tests; this script intentionally loads the real models.
"""
from __future__ import annotations

from aligo.config import settings
from aligo.models import ModelGateway


def main():
    try:
        from sentence_transformers import CrossEncoder, SentenceTransformer
        from sentence_transformers.util import cos_sim
    except ImportError as exc:
        raise SystemExit('Missing ML dependencies. Run: python -m pip install -r requirements-ml.txt') from exc

    gateway = ModelGateway(settings)
    embedding_model = gateway.embedding_model_id()
    reranker_model = gateway.reranker_model_id()
    print('Embedding model:', embedding_model)
    print('Reranking model:', reranker_model)
    print('Loading model weights. This may require a substantial download on first use...')
    embedder = SentenceTransformer(embedding_model)
    question = 'Find quiet seaside walking trails near public transportation.'
    documents = [
        'A coastal walking path with views of the sea is accessible by local transit.',
        'A technical guide to configuring server-side database replication.',
    ]
    vectors = embedder.encode([gateway.embedding_query(question), *documents], convert_to_tensor=True, normalize_embeddings=True)
    similarities = cos_sim(vectors[0], vectors[1:])[0].tolist()
    print('Embeddings shape:', list(vectors.shape))
    print('Similarity scores:', [round(float(value), 4) for value in similarities])

    ranker = CrossEncoder(reranker_model)
    scores = ranker.predict([(question, document) for document in documents])
    print('Reranking scores:', [round(float(value), 4) for value in scores])
    print('Model invocation succeeded. Evaluate quality separately on a held-out multilingual corpus.')


if __name__ == '__main__':
    main()
