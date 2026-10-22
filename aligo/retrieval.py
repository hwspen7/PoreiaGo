"""English-first multilingual retrieval with persisted embeddings and hybrid ranking.

The SQLite index avoids re-embedding document chunks on each query. Index
backends are replaceable; legacy in-memory stores retain the v3.1 behavior.
Only documents for the requesting user are retrieved.
"""
from __future__ import annotations

from collections import Counter
import math
import re

from .indexing import SQLiteRetrievalIndex, tokenize
from .models import ModelUnavailable


class Retrieval:
    def __init__(self, store, models, settings, index_backend=None):
        self.store = store
        self.models = models
        self.settings = settings
        self._embedder = None
        self._reranker = None
        self._embedder_id = None
        self._reranker_id = None
        self.index = index_backend if index_backend is not None else (
            SQLiteRetrievalIndex(store) if hasattr(store, '_connection') else None)

    @staticmethod
    def _segments(docs, tokenizer=None, budget=448, overlap=48):
        chunks = []
        for document in docs:
            content = document['content']
            if tokenizer is None or not hasattr(tokenizer, 'encode') or not hasattr(tokenizer, 'decode'):
                parts = [(start, content[start:start + 1100]) for start in range(0, len(content), 900)]
            else:
                ids = tokenizer.encode(content, add_special_tokens=False)
                width = max(32, int(budget))
                step = max(1, width - max(0, min(int(overlap), width - 1)))
                parts = [(start, tokenizer.decode(ids[start:start+width], skip_special_tokens=True))
                         for start in range(0, len(ids), step)]
            for start, text in parts:
                if text.strip():
                    chunks.append({'title':document['title'], 'document_id':document['id'],
                                   'offset':start, 'text':text})
        return chunks

    @staticmethod
    def _tokens(text):
        return re.findall(r'[^\W_]{2,}', text.lower(), flags=re.UNICODE)

    def _lexical(self, query, chunks):
        terms = Counter(self._tokens(query))
        return [sum(count * chunk['text'].lower().count(term) for term,count in terms.items()) for chunk in chunks]

    def _get_embedder(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ModelUnavailable('Install requirements-ml.txt to enable semantic retrieval.') from exc
        name = self.models.embedding_model_id()
        if self._embedder is None or self._embedder_id != name:
            self._embedder = SentenceTransformer(name)
            self._embedder_id = name
        return self._embedder

    def _dense(self, query, chunks):
        # Legacy API maintained for custom non-persistent stores and tests.
        try:
            from sentence_transformers.util import cos_sim
        except ImportError as exc:
            raise ModelUnavailable('Install requirements-ml.txt to enable semantic retrieval.') from exc
        model = self._get_embedder()
        inputs = [self.models.embedding_query(query)] + [chunk['text'] for chunk in chunks]
        vectors = model.encode(inputs, convert_to_tensor=True, normalize_embeddings=True)
        return cos_sim(vectors[0], vectors[1:])[0].tolist()

    def _rerank(self, query, chunks, selected):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ModelUnavailable('Install requirements-ml.txt to enable reranking.') from exc
        name = self.models.reranker_model_id()
        if self._reranker is None or self._reranker_id != name:
            self._reranker = CrossEncoder(name)
            self._reranker_id = name
        predicted = self._reranker.predict([(query,chunks[i]['text']) for i in selected])
        scored = {i:float(score) for i,score in zip(selected,predicted)}
        return sorted(selected,key=lambda i:scored[i],reverse=True)

    def index_document(self, user_id, document_id, *, semantic=None):
        """Index a single document; keep lexical chunks if ML models unavailable."""
        if self.index is None:
            raise RuntimeError('The current store does not support persistent indexing.')
        document = self.store.get_document(user_id, document_id)
        if document is None:
            raise KeyError('Document is not available for this user.')
        semantic = (self.settings.retrieval_mode in {'dense','hybrid'}) if semantic is None else bool(semantic)
        encoder = None
        warnings = []
        if semantic:
            try:
                encoder = self._get_embedder()
            except (ImportError, ModelUnavailable, OSError, RuntimeError, ValueError) as exc:
                warnings.append(f'Embedding unavailable ({type(exc).__name__}); lexical chunks retained.')
        profile = self.models.local_model_profile('embedding')
        chunks = self._segments([document],getattr(encoder,'tokenizer',None) if encoder else None,
                                profile.get('document_token_budget',448),profile.get('document_token_overlap',48))
        embeddings = None
        if encoder is not None and chunks:
            try:
                # Single batched encode for changed document, never once per query.
                encoded = encoder.encode([chunk['text'] for chunk in chunks], batch_size=16, normalize_embeddings=True)
                embeddings = [v.tolist() if hasattr(v,'tolist') else list(v) for v in encoded]
            except (ModelUnavailable, OSError, RuntimeError, ValueError) as exc:
                warnings.append(f'Embedding failed ({type(exc).__name__}); lexical chunks retained.')
        report = self.index.replace_document(user_id,document_id,chunks,
                                              model_id=self.models.embedding_model_id() if embeddings is not None else None,
                                              vectors=embeddings)
        return {**report,'document_id':document_id,'limitations':warnings}

    def reindex(self, user_id, *, semantic=None):
        if self.index is None:raise RuntimeError('A persistent index backend is required.')
        outcome = []
        for document in self.store.documents(user_id):
            outcome.append(self.index_document(user_id,document['id'],semantic=semantic))
        return {'documents':len(outcome),'indexed':outcome,
                'status':self.index.stats(user_id,self.models.embedding_model_id())}

    def delete_indexed_document(self, user_id,document_id):
        if self.index is not None:
            self.index.delete_document(user_id,document_id)

    @staticmethod
    def _bm25(query, records):
        terms = Counter(tokenize(query))
        if not records or not terms:return [0.0]*len(records)
        doc_count=len(records)
        lengths=[sum(item['terms'].values()) for item in records]
        avg_len=max(sum(lengths)/doc_count,1.0)
        dfs={term:sum(1 for row in records if term in row['terms']) for term in terms}
        result=[]
        for row,length in zip(records,lengths):
            score=0.0
            for term,qfreq in terms.items():
                freq=row['terms'].get(term,0)
                if not freq:continue
                idf=math.log1p((doc_count-dfs[term]+0.5)/(dfs[term]+0.5))
                score+=idf*(freq*2.2)/(freq+1.2*(0.25+0.75*length/avg_len))*min(qfreq,2)
            result.append(score)
        return result

    @staticmethod
    def _similarities(query_vector,records):
        """Cosine similarity on normalized stored vectors, no third-party ANN requirement."""
        q = [float(x) for x in query_vector]
        qnorm=math.sqrt(sum(x*x for x in q)) or 1.0
        scores=[]
        for row in records:
            v=row['vector']
            if v is None or len(v)!=len(q):
                scores.append(None)
                continue
            denom=(math.sqrt(sum(x*x for x in v)) or 1.0)*qnorm
            scores.append(sum(a*b for a,b in zip(q,v))/denom)
        return scores

    @staticmethod
    def _rrf(base_order,semantic_order,rrf_k=60):
        result={}
        for order in (base_order,semantic_order):
            for rank,index in enumerate(order,1):
                result[index]=result.get(index,0)+1/(rrf_k+rank)
        return result

    def _indexed_search(self,user_id,query,k):
        model_id=self.models.embedding_model_id()
        existing=self.index.indexed_document_ids(user_id)
        limitations=[]
        # One-time lexical migration for documents uploaded before the index existed.
        for doc_id in self.store.document_ids(user_id):
            if doc_id not in existing:
                self.index_document(user_id,doc_id,semantic=False)
                limitations.append('Legacy document indexed lexically. Reindex to generate embeddings.')
        rows=self.index.records(user_id,model_id)
        if not rows:
            return {'hits':[],'mode':'empty','limitations':['No indexed documents for this user.']}
        lexical=self._bm25(query,rows)
        lexical_order=sorted((i for i,s in enumerate(lexical) if s>0),key=lambda i:(-lexical[i],i))
        mode='lexical'
        scores={i:lexical[i] for i in lexical_order}
        desired=self.settings.retrieval_mode
        if desired in ('dense','hybrid'):
            if not any(row['vector'] is not None for row in rows):
                limitations.append('No vectors for the active embedding model. Reindex the knowledge base.')
            else:
                try:
                    encoder=self._get_embedder()
                    qvector=encoder.encode([self.models.embedding_query(query)],normalize_embeddings=True)[0]
                    if hasattr(qvector,'tolist'):qvector=qvector.tolist()
                    dense=self._similarities(qvector,rows)
                    missing=sum(value is None for value in dense)
                    if missing:
                        limitations.append(f'{missing} chunks lack vectors for the active model. Reindex recommended.')
                    dense_order=sorted((i for i,s in enumerate(dense) if s is not None),
                                       key=lambda i:(-dense[i],i))
                    if dense_order:
                        mode=desired
                        if desired=='dense':scores={i:float(dense[i]) for i in dense_order}
                        else:scores=self._rrf(lexical_order,dense_order)
                except (ModelUnavailable,OSError,RuntimeError,ValueError,ImportError) as exc:
                    limitations.append(f'Semantic search unavailable ({type(exc).__name__}); lexical fallback used.')
        ordered=sorted(scores,key=lambda i:(-scores[i],i))
        # Rerank an expanded candidate pool, rather than only the final k results.
        pool=ordered[:min(max(k*4,20),50)]
        rerank_scores={}
        if desired=='hybrid' and mode=='hybrid' and pool:
            try:
                from sentence_transformers import CrossEncoder
                name=self.models.reranker_model_id()
                if self._reranker is None or self._reranker_id!=name:
                    self._reranker=CrossEncoder(name)
                    self._reranker_id=name
                ranked=self._reranker.predict([(query,rows[i]['text']) for i in pool])
                rerank_scores={i:float(score) for i,score in zip(pool,ranked)}
                pool=sorted(pool,key=lambda i:(-rerank_scores[i],i))
            except (ModelUnavailable,ImportError,OSError,RuntimeError,ValueError) as exc:
                limitations.append(f'Rerank unavailable ({type(exc).__name__}); fusion order retained.')
        result=[]
        for i in pool[:k]:
            row=rows[i]
            hit={'title':row['title'],'document_id':row['document_id'],
                 'offset':row['offset'],'text':row['text'],
                 'score':round(float(scores[i]),6),'chunk_number':row['chunk_number']}
            if i in rerank_scores:hit['rerank_score']=round(rerank_scores[i],6)
            result.append(hit)
        return {'hits':result,'mode':mode,'limitations':limitations}

    def _legacy_search(self,user_id,query,k):
        """Compatibility for injected minimal document stores used by plugins/tests."""
        documents=self.store.documents(user_id)
        if not documents:return {'hits':[],'mode':'empty','limitations':['No indexed documents for this user.']}
        desired=self.settings.retrieval_mode
        encoder=None
        limitations=[]
        if desired in ('hybrid','dense'):
            try:encoder=self._get_embedder()
            except (ModelUnavailable,OSError,RuntimeError,ValueError):
                limitations.append('Semantic retrieval unavailable; lexical fallback used.')
        profile=self.models.local_model_profile('embedding')
        chunks=self._segments(documents,getattr(encoder,'tokenizer',None) if encoder else None,
                              profile.get('document_token_budget',448),profile.get('document_token_overlap',48))
        lexical=self._lexical(query,chunks)
        scores=lexical
        mode='lexical'
        if encoder is not None:
            try:
                dense=self._dense(query,chunks)
                mode=desired
                if mode=='dense':scores=dense
                else:
                    lo=sorted(range(len(chunks)),key=lambda i:lexical[i],reverse=True)
                    do=sorted(range(len(chunks)),key=lambda i:dense[i],reverse=True)
                    ranks=self._rrf(lo,do)
                    scores=[ranks[i] for i in range(len(chunks))]
            except (ModelUnavailable,OSError,RuntimeError,ValueError):
                limitations.append('Semantic retrieval unavailable; lexical fallback used.')
        order=sorted(range(len(chunks)),key=lambda i:scores[i],reverse=True)
        selected=[i for i in order[:min(k,10)] if scores[i]>0]
        if desired=='hybrid' and mode=='hybrid' and selected:
            try:selected=self._rerank(query,chunks,selected)
            except (ModelUnavailable,OSError,RuntimeError,ValueError):
                limitations.append('Optional reranker unavailable; fusion ranking retained.')
        return {'hits':[{**chunks[i],'score':round(float(scores[i]),5)} for i in selected],
                'mode':mode,'limitations':limitations}

    def search(self,user_id,query,k=5):
        k=min(20,max(1,int(k)))
        if not query or not query.strip():return {'hits':[],'mode':'empty','limitations':['A search query is required.']}
        if self.index is not None:return self._indexed_search(user_id,query,k)
        return self._legacy_search(user_id,query,k)
