import json, sys
from pathlib import Path
q = json.load(open('/data/queue/candidates.json', encoding='utf-8'))
# 今天开的 PR 对应哪些候选
targets = ['deer-flow#7822','qdrant#11361','deer-flow#7812','milvus#54967','agentgateway#4257']
found = []
for k, v in q.items():
    for t in targets:
        r, n = t.split('#')
        if k.endswith(f'/{r.split("-")[0]}#{n}') or (r.split('-')[0] in k and f'#{n}' in k):
            found.append((k, v.get('status'), v.get('repo')))
print('matched candidates:', len(found))
for f in found[:10]:
    print(' ', f)
# 当前 new/queued 候选数
import collections
c = collections.Counter(v.get('status') for v in q.values())
print('status dist:', dict(c.most_common(8)))
