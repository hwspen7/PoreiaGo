"""Held-out multilingual retrieval evaluation; downloads local ML weights in hybrid mode.

Example:
    python -m evaluation.evaluate_retrieval --mode lexical
    python -m evaluation.evaluate_retrieval --mode hybrid
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from statistics import median
from time import perf_counter
from tempfile import TemporaryDirectory

from core.config import ROOT, settings
from storage.memory import MemoryStore
from intelligence.models import ModelGateway
from storage.retrieval import Retrieval
from storage.retrieval_eval import evaluate_search


def main():
    parser=argparse.ArgumentParser(description='Evaluate PoreiaGo retrieval with explicit relevance labels.')
    parser.add_argument('--mode',choices=['lexical','dense','hybrid'],default='lexical')
    parser.add_argument('--dataset',type=Path,default=ROOT/'benchmarks'/'retrieval_multilingual.json')
    parser.add_argument('--output',type=Path,default=None)
    args=parser.parse_args()
    corpus=json.loads(args.dataset.read_text(encoding='utf-8'))
    with TemporaryDirectory() as tmp:
        with MemoryStore(Path(tmp)/'eval.sqlite3') as store:
            gateway=ModelGateway(settings)
            search=Retrieval(store,gateway,replace(settings,retrieval_mode=args.mode))
            ids={}
            user='eval-run'
            build_start=perf_counter()
            for document in corpus['documents']:
                id_=store.add_document(user,document['title'],document['content'])
                ids[document['id']]=id_
                search.index_document(user,id_,semantic=args.mode!='lexical')
            build_elapsed_ms=round((perf_counter()-build_start)*1000,2)
            queries=[{**item,'relevant_document_ids':[ids[key] for key in item['relevant_document_ids']]}
                     for item in corpus['queries']]
            latencies=[]
            def timed_search(query,k):
                started=perf_counter()
                hits=search.search(user,query,k)['hits']
                latencies.append((perf_counter()-started)*1000)
                return hits
            report=evaluate_search(timed_search,queries)
            measured=sorted(latencies)
            p95=measured[min(len(measured)-1, max(0,int(0.95*len(measured)+0.999)-1))]
            report['latency_ms']={
                'index_build':build_elapsed_ms,
                'query_p50':round(median(measured),2),
                'query_p95':round(p95,2),
            }
            report['model']=gateway.embedding_model_id() if args.mode!='lexical' else None
            report['reranker']=gateway.reranker_model_id() if args.mode=='hybrid' else None
            report['mode']=args.mode
            report['dataset']=corpus['name']
            report['limitations']=['Small curated smoke benchmark, not a statistically representative model-quality claim.']
    printed=json.dumps(report,indent=2,ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(printed+'\n',encoding='utf-8')
    print(printed)

if __name__=='__main__':
    main()
