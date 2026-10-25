"""Deterministic information-retrieval metrics with explicit relevance labels."""
from __future__ import annotations

import math


def evaluate_search(search_fn, cases, cutoffs=(1, 3, 5)):
    if not cases:
        raise ValueError('A nonempty evaluation set is required.')
    cutoffs=tuple(sorted(set(int(k) for k in cutoffs if k>0)))
    if not cutoffs:
        raise ValueError('At least one positive cutoff is required.')
    values={f'{metric}@{k}':[] for k in cutoffs for metric in ('recall','mrr','ndcg')}
    per_case=[]
    for case in cases:
        relevant=set(case['relevant_document_ids'])
        if not relevant:
            raise ValueError(f"Query {case['id']} has no relevance labels.")
        hits=search_fn(case['query'],max(cutoffs))
        ranked=[]
        for hit in hits:
            doc=int(hit['document_id'])
            if doc not in ranked:ranked.append(doc)
        row={'id':case['id'],'language':case.get('language','unknown'),'retrieved':ranked}
        for k in cutoffs:
            subset=ranked[:k]
            first=next((position for position,doc in enumerate(subset,1) if doc in relevant),None)
            recall=len(relevant.intersection(subset))/len(relevant)
            rr=1/first if first else 0
            dcg=sum(1/math.log2(position+1) for position,doc in enumerate(subset,1) if doc in relevant)
            ideal=sum(1/math.log2(position+1) for position in range(1,min(len(relevant),k)+1))
            ndcg=dcg/ideal if ideal else 0
            for metric,value in (('recall',recall),('mrr',rr),('ndcg',ndcg)):
                key=f'{metric}@{k}'
                values[key].append(value)
                row[key]=round(value,5)
        per_case.append(row)
    return {'queries':len(cases),'metrics':{k:round(sum(v)/len(v),5) for k,v in values.items()},'cases':per_case}
