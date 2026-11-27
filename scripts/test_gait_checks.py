"""Gait direction and stance-width screening on synthetic SMPL-oriented walkers."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from g2h.gait_checks import analyze, travel_intent, NARROW_RATIO
from g2h import agent_learning as learning


def walker(text, travel=(0, 1), half_width=.11, facing_turn=0.0, n=150, fps=30, crossed=False):
    """Left hip at +X when facing +Z (SMPL). `travel` is root velocity in the
    body frame as (left, forward) metres/second; `half_width` is each ankle's
    lateral offset from the pelvis while it supports the body."""
    t = np.arange(n) / fps
    c, s = np.cos(facing_turn), np.sin(facing_turn)
    left_axis = np.array([c, 0, -s]); fwd_axis = np.array([s, 0, c])
    p = np.zeros((n, 52, 3)); p[:, :, 1] = 1.0
    def place(j, lat, up, fw):
        p[:, j] = lat[:, None] * left_axis + up[:, None] * np.array([0, 1, 0]) + fw[:, None] * fwd_axis if np.ndim(lat) else lat * left_axis + up * np.array([0, 1, 0]) + fw * fwd_axis
    ones = np.ones(n)
    for side, hip, knee, ankle, toe in [(1, 1, 4, 7, 10), (-1, 2, 5, 8, 11)]:
        phase = (t + (0 if side > 0 else .5)) % 1
        swing = phase >= .6
        lift = np.where(swing, .12 * np.sin(np.pi * (phase - .6) / .4), 0)
        lateral = side * half_width * (-1 if crossed else 1) * ones
        place(hip, side * .06 * ones, .95 * ones, 0 * ones)
        place(knee, side * .07 * ones, .55 * ones, 0 * ones)
        place(ankle, lateral, .08 + lift, 0 * ones)
        place(toe, lateral, .03 + lift, .12 * ones)
    place(0, 0 * ones, 1.0 * ones, 0 * ones)
    place(9, 0 * ones, 1.3 * ones, 0 * ones)
    place(12, 0 * ones, 1.55 * ones, 0 * ones)
    for side, sh, el, wr in [(1, 16, 18, 20), (-1, 17, 19, 21)]:
        place(sh, side * .18 * ones, 1.45 * ones, 0 * ones)
        place(el, side * .22 * ones, 1.2 * ones, 0 * ones)
        place(wr, side * .24 * ones, .95 * ones, 0 * ones)
    p = p - p[:, :1]  # root-relative, like the generator output
    v = travel[0] * left_axis + travel[1] * fwd_axis
    tr = t[:, None] * v + np.array([0, 1.0, 0])
    return dict(motion_id='m_gait', text_id='t', topic='walking', text=text,
                created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                motion=dict(fps=fps, native_keypoints3d=p, native_transl=tr,
                            native_root_rotations_mat=np.tile(np.eye(3), (n, 1, 1))))


SPRINT = ('A person explodes from a stationary crouch into a forward sprint, driving knees high and '
          'swinging arms forcefully to accelerate rapidly over eight seconds.')  # m0000002d


def out_and_back(text, n=240, fps=30, speed=4.0):
    """Sprints forward along +Z, turns around at the midpoint and sprints back,
    always facing the direction of travel."""
    rec = walker(text, travel=(0, 0), n=n, fps=fps)
    half = n // 2
    t = np.arange(n) / fps
    z = np.where(np.arange(n) < half, speed * t, speed * (2 * t[half] - t))
    rec['motion']['native_transl'][:, 2] = z
    p = rec['motion']['native_keypoints3d']
    p[half:, :, 0] *= -1; p[half:, :, 2] *= -1  # rotate the body 180 degrees about Y
    return rec


class TravelIntent(unittest.TestCase):
    def test_phrases(self):
        cases = {
            'A person walks backward slowly with relaxed arms.': 'backward',
            'A person walks slowly backward for three steps, then stops.': 'backward',
            'A person backs up carefully, keeping the left arm raised.': 'backward',
            'A person takes two steps back and stands still.': 'backward',
            'A person walks forward and turns left at the end.': 'forward',
            'A person side-steps to the left four times.': 'left',
            'A person shuffles sideways to the right.': 'right',
            'A person walks forward, then walks backward to the start.': 'mixed',
            SPRINT: 'forward',
            'A person backpedals quickly.': 'backward',
            'A person tiptoes slowly backward.': 'backward',
            'A person charges ahead.': 'forward',
            'A person walks and kicks backward.': None,
            '一个人向前冲刺。': 'forward',
            '一个人向左侧步移动。': 'left',
            'A person rises from a standstill and dashes forward.': 'forward',
            'A person turns right in place.': 'in_place',
            'A person jogs on the spot, not moving forward.': 'in_place',
            'A person jogs in place, then runs forward.': 'mixed',
            'A person jogs without moving forward.': 'in_place',
            'A person stands still and waves.': 'in_place',
            'A person runs forward and comes to a stop.': 'forward',
            'A person waves with the left hand.': None,
        }
        for text, want in cases.items():
            self.assertEqual(travel_intent(text), want, text)


class Direction(unittest.TestCase):
    def test_backward_text_walking_forward_is_flagged(self):
        q = analyze(walker('A person walks backward slowly with the left arm relaxed.', travel=(0, 1)))
        self.assertIn('possible_direction_mismatch', q['flags'])
        self.assertEqual(q['metrics']['observed_travel'], 'forward')
        self.assertEqual(q['metrics']['axes'], 'skeleton_hips')

    def test_matching_directions_pass(self):
        for text, travel in [('A person walks backward slowly.', (0, -.8)), ('A person walks forward.', (0, 1)),
                             ('A person side-steps to the left.', (.6, 0)), ('A person shuffles sideways to the right.', (-.6, 0))]:
            q = analyze(walker(text, travel=travel))
            self.assertNotIn('possible_direction_mismatch', q['flags'], text)
            self.assertTrue(q['checks']['direction'], text)

    def test_axes_follow_the_body_not_the_world(self):
        # Facing +X instead of +Z: walking backward in body terms is still backward.
        q = analyze(walker('A person walks backward.', travel=(0, -.8), facing_turn=np.pi / 2))
        self.assertEqual(q['metrics']['observed_travel'], 'backward')
        self.assertNotIn('possible_direction_mismatch', q['flags'])

    def test_lateral_mismatch_and_mixed_or_unspecified_are_not_judged(self):
        self.assertIn('possible_direction_mismatch', analyze(walker('A person side-steps to the left.', travel=(-.6, 0)))['flags'])
        for text in ('A person walks forward, then walks backward.', 'A person walks around.'):
            q = analyze(walker(text, travel=(0, 1)))
            self.assertIsNone(q['checks']['direction'], text)


class PathShape(unittest.TestCase):
    def test_stationary_start_is_not_in_place(self):
        from g2h.body_quality import intent
        self.assertEqual(intent(SPRINT, 'running'), 'travelling_gait')
        self.assertEqual(intent('A person jogs in place.', 'running'), 'in_place')
        self.assertEqual(intent('A person stands stationary and waves.', 'gestures'), 'in_place')

    def test_one_way_sprint_that_doubles_back_is_flagged(self):
        q = analyze(out_and_back(SPRINT))
        self.assertEqual(q['metrics']['travel_intent'], 'forward')
        self.assertNotIn('possible_in_place_travel', q['flags'])
        self.assertIn('possible_path_reversal', q['flags'])
        self.assertLess(q['metrics']['straightness'], .5)
        straight = analyze(walker(SPRINT, travel=(0, 4)))
        self.assertEqual(straight['flags'], [])
        self.assertTrue(straight['checks']['path'])

    def test_returns_are_allowed_when_asked_and_in_place_drift_is_flagged(self):
        q = analyze(out_and_back('A person runs forward, turns around and runs back to the start.'))
        self.assertNotIn('possible_path_reversal', q['flags'])
        self.assertIn('possible_in_place_travel', analyze(walker('A person jogs in place.', travel=(0, 1)))['flags'])
        self.assertTrue(analyze(walker('A person jogs in place.', travel=(0, .05)))['checks']['in_place'])


class Stance(unittest.TestCase):
    def test_hip_width_stance_passes_and_narrow_is_flagged(self):
        ok = analyze(walker('A person walks forward.', half_width=.11))
        self.assertNotIn('possible_narrow_stance', ok['flags'])
        self.assertGreaterEqual(ok['metrics']['stance_width_ratio'], NARROW_RATIO)
        narrow = analyze(walker('A person walks forward.', half_width=.025))
        self.assertIn('possible_narrow_stance', narrow['flags'])
        self.assertFalse(narrow['checks']['stance_width'])
        self.assertAlmostEqual(narrow['metrics']['stance_width_m'], .05, places=2)

    def test_crossed_feet_and_intentional_exemptions(self):
        crossed = analyze(walker('A person walks forward.', crossed=True))
        self.assertIn('possible_crossed_feet', crossed['flags'])
        for text in ('A person walks heel-to-toe along a line.', 'A person does a crossover step to the left.', 'A person dances forward.'):
            q = analyze(walker(text, half_width=.02, crossed=True))
            self.assertEqual([f for f in q['flags'] if f != 'possible_direction_mismatch'], [], text)
            self.assertIsNone(q['checks']['stance_width'], text)

    def test_ground_actions_skip_upright_rules(self):
        q = analyze(walker('A person walks backward.', travel=(0, 1), half_width=.02), ground=True)
        self.assertEqual(q['flags'], [])


class Screening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_screening_reports_gait_and_rescreen_keeps_reviews(self):
        record = walker('A person walks backward slowly.', travel=(0, 1), half_width=.02)
        metrics, flags, _ = learning.analyze(record)
        self.assertIn('possible_direction_mismatch', flags)
        self.assertIn('possible_narrow_stance', flags)
        self.assertEqual(metrics['gait']['version'], 'gait-v3')
        learning.index_record(self.root, record)
        import sqlite3
        body = dict(motion_id='m_gait', status='rejected', semantic='mismatch', quality='poor', issues=['direction'])
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as reviews:
            reviews.execute('CREATE TABLE latest(motion_id TEXT PRIMARY KEY, revision INTEGER, body TEXT)')
            reviews.execute('INSERT INTO latest VALUES(?,?,?)', ('m_gait', 1, json.dumps(body)))
        learning.reviewed(self.root, body)
        # Simulate a row from the previous screening version.
        with learning.connect(self.root) as db:
            db.execute("UPDATE checks SET metrics=?, flags='[]'", (json.dumps({'version': 'screen_v3'}),))
        self.assertTrue(learning.rescreen(self.root, record))
        self.assertFalse(learning.rescreen(self.root, record))  # already current
        screen = learning.screening(self.root, 'm_gait')
        self.assertIn('possible_direction_mismatch', screen['flags'])
        with learning.connect(self.root) as db:
            row = db.execute('SELECT status,semantic,issues FROM checks').fetchone()
        self.assertEqual((row[0], row[1], json.loads(row[2])), ('rejected', 'mismatch', ['direction']))


class Backfill(unittest.TestCase):
    """End to end through the catalogue, as run on the server: relative --root,
    a custom motion stamped with a trailing Z, a pending motion, a stale row."""
    def test_gaps_and_recheck(self):
        import os, subprocess, sys
        import MotionFarm as farm
        from g2h import farm_index as ids
        from g2h.collection_store import CollectionStore
        self.assertEqual(learning.utc_timestamp('2026-10-28T16:06:04Z'), 1793203564.0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'outputs/text_motion_farm'; root.mkdir(parents=True)
            db = farm.catalog(root)
            mids = []
            for i, text in enumerate(['A person walks backward slowly.', 'A person walks forward.', 'A person walks backward.']):
                item = dict(id=f'text{i}', topic_id='walking', text=text)
                with db:
                    db.execute('INSERT INTO prompts VALUES(?,?,?,?)', (item['id'], 'walking', text.lower(), json.dumps(item)))
                mids.append(ids.register_motion(db, item['id'], 'walking', f'task{i}', 42))
            db.close()
            with CollectionStore(root / 'topics/walking') as store:
                for i in (0, 1):
                    rec = walker('', travel=(0, 1), half_width=.02)
                    rec = {k: v for k, v in rec.items() if k not in ('motion_id', 'text_id', 'text')}
                    store.append({**rec, 'id': f'task{i}', 'text': ['A person walks backward slowly.', 'A person walks forward.'][i],
                                  'created_utc': '2026-10-28T16:06:04Z'})
            # mids[2] stays pending. mids[1] was screened by an older version.
            with learning.connect(root) as c:
                c.execute("INSERT INTO checks(motion_id,topic,created,metrics,flags) VALUES(?,?,?,?,?)",
                          (mids[1], 'other', 1.0, json.dumps({'version': 'screen_v3'}), '[]'))
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
            script = str(Path(__file__).resolve().parent / 'backfill_screening.py')
            def run(*args):
                out = subprocess.run([sys.executable, script, '--root', 'outputs/text_motion_farm', *args],
                                     cwd=tmp, env=env, capture_output=True, text=True, check=True).stdout
                return json.loads(out.strip().splitlines()[-1])
            gaps = run('--gaps', '--limit', '100')
            self.assertEqual((gaps['indexed'], gaps['pending'], gaps['failed']), (1, 1, 0), gaps)
            re = run('--recheck', '--limit', '100')
            self.assertEqual((re['rescreened'], re['remaining'], re['failed']), (1, 0, 0), re)
            for mid, flag in ((mids[0], 'possible_direction_mismatch'), (mids[1], 'possible_narrow_stance')):
                self.assertIn(flag, learning.screening(root, mid)['flags'])


if __name__ == '__main__':
    unittest.main()
