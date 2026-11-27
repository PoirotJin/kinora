"""ID integrity, migration and indexed history; no API or GPU calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import MotionFarm as farm
from g2h import farm_index as ids, semantic_index as vectors
from g2h.collection_store import CollectionStore, connect


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = farm.catalog(self.root)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def insert(self, topic='walking', text='A person walks forward.', family='forward'):
        item = dict(id=farm.task_id(dict(topic=topic,text=farm.normalized(text))),topic_id=topic,
                    text=text,attributes=dict(direction=family,speed='slow',style='ordinary'),
                    source={'semantic_review':{'family':family}},duration_s=8)
        with self.db:
            self.db.execute('INSERT INTO prompts VALUES(?,?,?,?)',
                (item['id'],topic,farm.normalized(text),json.dumps(item)))
        return item

    def test_short_ids_global_and_idempotent(self):
        a,b = self.insert(),self.insert('running')
        first = ids.register_motion(self.db,a['id'],'walking','task1',42)
        self.assertEqual(first,'m00000001')
        self.assertEqual(ids.register_motion(self.db,a['id'],'walking','task1',42),first)
        second = ids.register_motion(self.db,b['id'],'running','task2',42)
        self.assertNotEqual(first,second)
        with self.assertRaises(ValueError):
            ids.register_motion(self.db,b['id'],'running','task1',42)
        with self.assertRaises(ValueError):
            ids.register_motion(self.db,a['id'],'running','task3',42)

    def test_one_text_multiple_motions_and_direct_read(self):
        item = self.insert()
        mids = [ids.register_motion(self.db,item['id'],'walking','task'+str(seed),seed) for seed in (42,43)]
        queue = self.root/'topics/walking'
        with CollectionStore(queue) as store:
            store.append({'id':'task42','motion':[1,2,3]})
        self.assertEqual(ids.read_motion(self.db,self.root,mids[0])['text_id'],item['id'])
        self.assertEqual(ids.lookup_motion(self.db,self.root,mids[1])['state'],'not_enqueued')
        location = ids.lookup_motion(self.db,self.root,mids[0])['location']
        with Path(location['file']).open('r+b') as stream:
            stream.write(b'corrupt')
        with self.assertRaises(ValueError):
            ids.read_motion(self.db,self.root,mids[0])

    def test_migration_is_incremental_and_preserves_shard(self):
        item = self.insert()
        queue = self.root/'topics/walking'
        db = connect(queue)
        with db:
            for seed in (42,43):
                db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('task'+str(seed),
                    json.dumps(dict(seed=seed,farm={'prompt_id':item['id']}))))
        db.close()
        topics = [{'id':'walking'}]
        self.assertEqual(ids.sync_legacy(self.db,self.root,topics,1),1)
        self.assertEqual(ids.sync_legacy(self.db,self.root,topics,1),1)
        self.assertEqual(ids.sync_legacy(self.db,self.root,topics,1),0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM motion_links').fetchone()[0],2)

    def test_summary_is_incremental_and_reads_bounded_items(self):
        for n in range(150):
            self.insert(text='Action '+str(n),family='forward' if n else 'turn')
        statements = []
        self.db.set_trace_callback(statements.append)
        snapshot = ids.history_snapshot(self.db,[{'id':'walking'}],12)['walking']
        self.db.set_trace_callback(None)
        self.assertEqual(snapshot['total_saved_prompts'],150)
        self.assertEqual(snapshot['family_counts']['forward'],149)
        self.assertLessEqual(len(snapshot['examples']),12)
        self.assertTrue(all('LIMIT' in s or 'WHERE id=' in s for s in statements if 'SELECT item FROM prompts' in s))

    def test_vector_configuration_and_invalid_vectors(self):
        vectors.configure(self.db,'fake',64)
        item = self.insert()
        with self.assertRaises(ValueError):
            vectors.configure(self.db,'other',64)
        with self.assertRaises(ValueError):
            vectors.save_vector(self.db,item,[0]*64,64)
        with self.assertRaises(ValueError):
            vectors.save_vector(self.db,item,[float('nan')]*64,64)
        with self.assertRaises(RuntimeError):
            vectors.require_complete(self.db,[{'id':'walking'}])
        with self.db:
            vectors.save_vector(self.db,item,[1]+[0]*63,64)
        vectors.require_complete(self.db,[{'id':'walking'}])

    def test_backfill_resume_does_not_reembed(self):
        self.insert()
        with patch.object(vectors,'embed',return_value=[[1]+[0]*63]) as call:
            self.assertEqual(vectors.backfill(self.db,None,'fake',64,10),1)
            self.assertEqual(vectors.backfill(self.db,None,'fake',64,10),0)
            self.assertEqual(call.call_count,1)

    def test_faiss_topic_isolation_and_append(self):
        try:
            vectors.faiss_module()
        except RuntimeError:
            self.skipTest('FAISS not installed in this test environment')
        vectors.configure(self.db,'fake',64)
        a = self.insert()
        b = self.insert('running')
        unit = [1]+[0]*63
        with self.db:
            vectors.save_vector(self.db,a,unit,64)
            vectors.save_vector(self.db,b,unit,64)
        hits = vectors.nearest(self.db,self.root,'walking',[unit],5)[0]
        self.assertEqual([h['text_id'] for h in hits],[a['id']])
        c = self.insert(text='Walk backwards.',family='backward')
        other = [0,1]+[0]*62
        with self.db:
            vectors.save_vector(self.db,c,other,64)
        hits = vectors.nearest(self.db,self.root,'walking',[other],5)[0]
        self.assertEqual(hits[0]['text_id'],c['id'])
        self.assertEqual(len(hits),2)


if __name__=='__main__':
    unittest.main()
