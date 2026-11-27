"""Exercise snapshot/replay protocol with a tiny deterministic index adapter."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h import semantic_index as module


class Index:
    def __init__(self,d):
        self.d=d;self.ids=[];self.values=[];self.hnsw=SimpleNamespace()
    @property
    def ntotal(self):return len(self.ids)
    @property
    def id_map(self):return np.asarray(self.ids,dtype='int64')
    def add_with_ids(self,values,ids):self.values.extend(values.tolist());self.ids.extend(ids.tolist())
    def search(self,query,k):
        scores=query@np.asarray(self.values).T
        order=np.argsort(-scores,axis=1)[:,:k]
        return np.take_along_axis(scores,order,axis=1),np.asarray(self.ids)[order]


class FakeFaiss:
    METRIC_INNER_PRODUCT=0
    def __init__(self):self.writes=0;self.reads=0
    def IndexHNSWFlat(self,d,*args):return Index(d)
    def IndexIDMap2(self,index):return index
    def vector_to_array(self,ids):return ids
    def write_index(self,index,path):
        self.writes+=1;Path(path).write_text(json.dumps([index.d,index.ids,index.values]))
    def read_index(self,path):
        self.reads+=1;d,ids,values=json.loads(Path(path).read_text());index=Index(d)
        index.ids=ids;index.values=values;return index


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE embedding_config(singleton,model,dimensions);INSERT INTO embedding_config VALUES(1,"fake",2);CREATE TABLE embeddings(number INTEGER PRIMARY KEY,text_id,topic,vector);CREATE TABLE prompts(id PRIMARY KEY,item);')
        module._CACHE.clear();self.fake=FakeFaiss()
        self.patches=[patch.object(module,'faiss_module',return_value=self.fake),patch.object(module,'_SNAPSHOT_BATCH',2)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in self.patches:p.stop()
        module._CACHE.clear();self.db.close();self.tmp.cleanup()
    def add(self,n,vector):
        value=module.normalized_vector(vector,2)
        self.db.execute('INSERT INTO prompts VALUES(?,?)',(str(n),json.dumps({'id':str(n),'text':'motion '+str(n)})))
        self.db.execute('INSERT INTO embeddings VALUES(?,?,?,?)',(n,str(n),'walking',value.tobytes()));self.db.commit()
    def search(self):return module.nearest(self.db,self.root,'walking',[[0,1]],top_k=5)

    def test_batch_snapshot_and_crash_replay(self):
        self.add(1,[1,0]);self.search();self.assertEqual(self.fake.writes,0)
        self.add(2,[1,1]);self.search();self.assertEqual(self.fake.writes,1)
        self.add(3,[0,1]);self.assertEqual(self.search()[0][0]['text_id'],'3')
        self.assertEqual(self.fake.writes,1)
        module._CACHE.clear() # Process crash before dirty in-memory vector snapshot.
        self.assertEqual(self.search()[0][0]['text_id'],'3')
        self.assertEqual(self.fake.writes,1);self.assertEqual(self.fake.reads,1)

    def test_other_process_snapshot_invalidates_resident_state(self):
        self.add(1,[1,0]);self.add(2,[1,1]);self.search()
        old=dict(module._CACHE);module._CACHE.clear()
        self.add(3,[.1,1]);self.add(4,[0,1]);self.search()
        module._CACHE.clear();module._CACHE.update(old)
        self.assertEqual(self.search()[0][0]['text_id'],'4')

    def test_active_index_stays_cached_above_soft_budget(self):
        self.add(1,[1,0]);self.add(2,[0,1]);self.search()
        with patch.object(module,'_CACHE_BYTES',1):
            self.search();self.search()
        self.assertEqual(len(module._CACHE),1);self.assertEqual(self.fake.reads,0)


if __name__=='__main__':unittest.main()
