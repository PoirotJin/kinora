"""Integration regression using the immutable quarantined beta trial candidate."""
import json
from pathlib import Path
import unittest
import os
import numpy as np
from g2h.motion_assets import load_numeric_npz
from g2h.human_rig import motion_rig
from g2h.human_interaction import report
from g2h.human_interaction_repair import repair


@unittest.skipUnless(os.environ.get("KINORA_TRIAL_ROOT"), "requires external beta trial fixtures")
class ContactRepairTests(unittest.TestCase):
    def test_contact_can_be_preserved_while_penetrating_forearm_moves(self):
        path=Path(os.environ['KINORA_TRIAL_ROOT'])/Path('topics/custom/work/ab703b842eb9d3f89e6404b8cc2a7655de8f074ca95ee2a0bb16364dc1bdff9d.quarantine.npz')
        arrays=load_numeric_npz(path)
        originals={k:v.copy() for k,v in arrays.items()}
        scene=json.loads(arrays['scene_spec_json'].tobytes())
        rest,parents=motion_rig(arrays)
        before=report(arrays,scene,rest,parents)
        self.assertFalse(before['passed'])
        updated,audit=repair(arrays,scene,rest,parents)
        after=report(updated,scene,rest,parents)
        self.assertTrue(after['passed'],after)
        self.assertEqual(audit['penetrating_frames_after'],0)
        self.assertTrue(all(c['passed'] for c in after['contacts']))
        for key in scene['keyframes']:
            for name in ['native_keypoints3d','native_transl','smpl_poses']:
                np.testing.assert_array_equal(updated[name][key['frame']],arrays[name][key['frame']])
        for name in originals:
            np.testing.assert_array_equal(arrays[name],originals[name])


if __name__=='__main__': unittest.main()
