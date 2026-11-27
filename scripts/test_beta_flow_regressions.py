"""Regressions found by the 0.11 beta interactive trial."""
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
import os
from unittest.mock import patch
import numpy as np
import MotionFarm as farm
from g2h import cubic_splice
from g2h.motion_assets import MotionAssets


class BoundedFloorLiftTests(unittest.TestCase):
    def test_off_center_foot_dip_clears_floor_within_existing_budget(self):
        u=np.arange(1,19)/19
        heights=np.full(18,.06)
        heights[12:16]=[.005,-.015,-.02,.005]
        lift=cubic_splice.bounded_floor_lift(heights,u)
        self.assertGreaterEqual(float((heights+lift).min()),.035-1e-9)
        self.assertLessEqual(float(lift.max()),.08)
        # Extending with immutable endpoints retains zero displacement and a
        # bounded first step; an impossible dip never expands the lift budget.
        self.assertLess(float(lift[0]),.01)
        self.assertLess(float(lift[-1]),.01)
        impossible=cubic_splice.bounded_floor_lift(np.full(18,-.2),u)
        self.assertLessEqual(float(impossible.max()),.08)
        self.assertLess(float((impossible-.2).max()),.03)


class SceneAdmissionTests(unittest.TestCase):
    def test_prepared_scene_catalog_key_can_be_enqueued_and_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = dict(id='scene_prompt', topic_id='custom', text='A person touches a box.',
                        duration_s=4, attributes={}, source={}, scene={'kind':'human_interaction_v1'})
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(dict(schema_version=1, prompts=[item])))
            db = farm.catalog(root)
            with db:
                db.execute('INSERT INTO prompts VALUES(?,?,?,?)',
                    (item['id'], 'custom', farm.normalized(item['text'])+'\x1fscene:'+item['id'], json.dumps(item)))
            db.close()
            with patch.object(farm,'manifest_recipe',return_value={'model':farm.KIMODO_MODEL}), patch.object(farm,'validate_spec'):
                farm.enqueue_manifest(root,manifest,[1])
                farm.enqueue_manifest(root,manifest,[1])
            db = farm.connect(root/'topics/custom')
            self.assertEqual(db.execute('select count(*) from tasks').fetchone()[0],1)
            db.close()


@unittest.skipUnless(os.environ.get("KINORA_TRIAL_ROOT"), "requires external beta trial fixtures")
class CubicFloorTests(unittest.TestCase):
    def test_real_walk_join_clears_floor_and_preserves_exterior(self):
        # The interactive trial imported these immutable fixtures through the
        # existing authorized library flow; do not generate or modify originals.
        assets=MotionAssets(Path(os.environ['KINORA_TRIAL_ROOT']))
        sources=[]
        for mid in ['m0000c0g6','m0000c0g5']:
            record=assets.find_request(os.environ.get('KINORA_TRIAL_OWNER','fixture-owner'),'library:'+mid)
            arrays,_=assets.load(os.environ.get('KINORA_TRIAL_OWNER','fixture-owner'),record['motion_id'])
            sources.append(arrays)
        a,b=sources
        world_a=a['native_keypoints3d']+a['native_transl'][:,None]
        world_b=b['native_keypoints3d']+b['native_transl'][:,None]
        turn=float(np.rad2deg(cubic_splice.heading(world_a)[-1]-cubic_splice.heading(world_b)[0]))
        target=world_a[-1,0]+cubic_splice.splice._yaw(turn)@np.array([0.,0.,.6])
        offset=target-world_b[0,0]
        b=cubic_splice.transform(b,0,len(b['native_transl']),turn,float(offset[0]),float(offset[2]))
        out,report=cubic_splice.join(a,b,18)
        self.assertTrue(report['passed'],report)
        self.assertGreater(report['transition_floor_lift_m'],0)
        self.assertLessEqual(report['transition_floor_lift_m'],.08)
        for name in ['native_keypoints3d','native_transl','smpl_poses']:
            np.testing.assert_array_equal(out[name][:len(a[name])],a[name])
            np.testing.assert_array_equal(out[name][-len(b[name]):],b[name])
        world=out['native_keypoints3d']+out['native_transl'][:,None]
        self.assertGreaterEqual(world[120:138,[10,11],1].min(),.03499)


class RetargetRowTests(unittest.TestCase):
    def test_source_resolution_accepts_sqlite_rows(self):
        from review_retarget import Retarget
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        library=db.execute("select 'm0000c0g6' as motion_id, 'jfc' as owner").fetchone()
        service=object.__new__(Retarget);service.root=Path('.')
        self.assertIsNone(service._asset_detail(library))
        edited=db.execute("select 'ma_test' as motion_id, 'jfc' as owner").fetchone()
        with patch('g2h.motion_assets.MotionAssets') as assets:
            assets.return_value.get.return_value={'sha256':'fixture-sha'}
            self.assertEqual(service._asset_detail(edited),{'location':{'sha256':'fixture-sha'}})
        db.close()


if __name__=='__main__': unittest.main()
