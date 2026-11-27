"""Probe plan validity and geometry measurements; no generation."""
import unittest
import numpy as np
import probe_controllability as probe
from test_gait_checks import walker, out_and_back


def turning(degrees, n=120, fps=30):
    """Stands in place and turns by `degrees` (+ left) about Y."""
    rec = walker('', travel=(0, 0), n=n, fps=fps)
    p = rec['motion']['native_keypoints3d']
    for i, a in enumerate(np.radians(degrees) * np.clip(np.arange(n) / (n * .6), 0, 1)):
        c, s = np.cos(a), np.sin(a)
        # Rotating the body to the left: +X (left) turns toward -Z when facing +Z.
        x, z = p[i, :, 0].copy(), p[i, :, 2].copy()
        p[i, :, 0] = c * x + s * z; p[i, :, 2] = -s * x + c * z
    return rec


class ProbeTests(unittest.TestCase):
    def test_plan_is_valid_and_ids_are_stable(self):
        probe.validate()
        ids = {probe.request_id(g, k, 'hy') for g, k, _, _ in probe.prompts()}
        self.assertEqual(len(ids), len(list(probe.prompts())))
        self.assertTrue(all(len(i) <= 80 for i in ids))
        for group, a, b, metric, _ in probe.PAIRS:
            keys = {k for k, _, _ in probe.GROUPS[group]}
            self.assertTrue({a, b} <= keys)

    def test_measurements_follow_geometry(self):
        fwd = probe.measure(walker('', travel=(0, 1)))
        self.assertEqual(fwd['observed_travel'], 'forward')
        self.assertTrue(probe.expect('travel:forward', fwd))
        self.assertTrue(probe.expect('straight', probe.measure(walker('', travel=(0, 1.5)))))
        self.assertFalse(probe.expect('travel:forward', probe.measure(walker('', travel=(0, -1)))))
        self.assertTrue(probe.expect('return', probe.measure(out_and_back(''))))
        self.assertTrue(probe.expect('in_place', probe.measure(walker('', travel=(0, .02)))))
        left, right = probe.measure(turning(90)), probe.measure(turning(-90))
        self.assertAlmostEqual(left['net_turn_deg'], 90, delta=2)
        self.assertTrue(probe.expect('turn:left', left)); self.assertFalse(probe.expect('turn:left', right))
        self.assertTrue(probe.expect('turn:right', right))
        wide, narrow = probe.measure(walker('', half_width=.12)), probe.measure(walker('', half_width=.03))
        self.assertGreater(wide['stance_width_ratio'], 1.2 * narrow['stance_width_ratio'])
        self.assertEqual(probe.wilson(0, 0), (None, None))


if __name__ == '__main__':
    unittest.main()
