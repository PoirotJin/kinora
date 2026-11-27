import unittest
import numpy as np
from g2h.kimodo_root import review
from g2h.kimodo_edit import compose, window
from test_kimodo_edit import fixture, generated

class RootTests(unittest.TestCase):
    def test_smooth_motion_and_exterior_scope(self):
        r=np.zeros((180,3));r[:,0]=np.arange(180)/30
        self.assertTrue(review({'native_transl':r},30,150)['passed'])
        r[:5]+=10
        self.assertTrue(review({'native_transl':r},30,150)['passed'])
    def test_both_seams_interior_and_vertical_jump(self):
        for frame in (30,80,149,150):
            for axis in range(3):
                r=np.zeros((180,3));r[frame:,axis]=.2
                self.assertFalse(review({'native_transl':r},30,150)['passed'])
    def test_velocity_jump_without_large_step(self):
        r=np.zeros((180,3));r[80:,0]=np.arange(100)*.04
        v=review({'native_transl':r},30,150)
        self.assertFalse(v['passed']);self.assertLess(v['max_step_m'],.1)
    def test_nonfinite_and_motion_edges(self):
        r=np.zeros((30,3));self.assertTrue(review({'native_transl':r},0,30)['passed'])
        r[10,1]=np.nan;self.assertFalse(review({'native_transl':r},0,30)['passed'])
    def test_composition_cannot_accept_internal_root_jump(self):
        src,rest,parents=fixture(240);plan=window(240,30,210);plan['staged_bridge']=True
        candidate=generated(src,plan,rest,parents,angle=.01)
        candidate['native_transl'][80:100,0]+=.2
        _,report=compose(src,candidate,30,210,plan,rest,parents)
        self.assertFalse(report['passed']);self.assertFalse(report['root_continuity']['passed'])

if __name__=='__main__': unittest.main()
