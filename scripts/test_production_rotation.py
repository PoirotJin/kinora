import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import MotionFarm as farm
from production_rotation import calendar,replenish


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'review/production').mkdir(parents=True)
        (self.root/'review/production/supply_policy.json').write_text(json.dumps(dict(enabled=True,rolling_variants=True,daily_variants_per_description=8)))
        self.item=dict(id='reviewed-text',topic_id='walking',text='A person walks forward, then stops calmly.',duration_s=8,attributes=dict(direction='forward',speed='slow',style='walking'),source=dict(semantic_review=dict(verdict='distinct')))
        manifest=self.root/'original.json';manifest.write_text(json.dumps(dict(schema_version=1,prompts=[self.item])))
        with contextlib.redirect_stdout(io.StringIO()):farm.enqueue_manifest(self.root,manifest,[42,43])

    def tearDown(self):self.temp.cleanup()

    def sample(self,key,now=None):
        with contextlib.redirect_stdout(io.StringIO()):return replenish(self.root,'walking',key,8,now)

    def test_daily_cap_includes_existing_pending_and_manifest_replay(self):
        self.assertEqual(self.sample('first'),6)
        self.assertEqual(self.sample('first'),6)
        self.assertEqual(self.sample('second'),0)
        with sqlite3.connect(self.root/'topics/walking/index.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],8)
        saved=json.loads((self.root/'agent_runs/first_rolling/manifest.json').read_text())
        self.assertNotIn(42,saved['variant_seeds']);self.assertNotIn(43,saved['variant_seeds'])
        self.assertFalse(saved['provenance']['new_semantic_description'])
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('SELECT item FROM prompts').fetchone()[0]),self.item)

    def test_next_day_resets_quota_and_cursor_without_replaying_old_work(self):
        self.assertEqual(self.sample('today'),6)
        tomorrow=time.time()+86400
        self.assertEqual(self.sample('today',tomorrow),0)
        self.assertEqual(self.sample('tomorrow',tomorrow),8)
        day,_=calendar(tomorrow)
        saved=json.loads((self.root/'agent_runs/tomorrow_rolling/manifest.json').read_text())
        self.assertEqual(saved['provenance']['day'],day)

    def test_human_rejection_excludes_description(self):
        from g2h.farm_index import short_id
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            number=db.execute('SELECT number FROM motion_links LIMIT 1').fetchone()[0]
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:
            db.execute('CREATE TABLE latest(motion_id TEXT PRIMARY KEY,status TEXT)')
            db.execute('INSERT INTO latest VALUES(?,?)',(short_id(number),'rejected'))
        self.assertEqual(self.sample('rejected'),0)

    def test_non_distinct_description_is_never_reused(self):
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            bad=dict(self.item,source=dict(semantic_review=dict(verdict='duplicate')))
            db.execute('UPDATE prompts SET item=?',(json.dumps(bad),))
        self.assertEqual(self.sample('duplicate'),0)

    def test_disabled_mode_does_not_generate_variants(self):
        (self.root/'review/production/supply_policy.json').write_text('{"enabled":true}')
        self.assertEqual(self.sample('disabled'),0)

    def test_new_prompt_not_yet_enqueued_cannot_race_original_seed_batch(self):
        with sqlite3.connect(self.root/'topics/walking/index.sqlite3') as db:
            db.execute('DELETE FROM tasks')
        self.assertEqual(self.sample('not-enqueued'),0)

    def test_api_prompt_lock_does_not_block_reviewed_variant_queue(self):
        import fcntl
        with (self.root/'prompt_generation.lock').open('a') as gate:
            fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.sample('during-api'),6)


if __name__=='__main__':unittest.main()
