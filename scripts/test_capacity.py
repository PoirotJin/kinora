"""Small deterministic quota tests; no GPU, API, or large directory scans."""
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h import capacity


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        capacity.ensure(self.root)

    def tearDown(self):self.tmp.cleanup()

    def finish(self,scan):
        for _ in range(100):
            if scan.step(seconds=.05,max_entries=1000):return
        self.fail('small reconciliation did not finish')

    def test_fast_path_never_walks_again(self):
        with patch('g2h.cold_storage.usage',side_effect=AssertionError('unexpected walk')):
            before=capacity.status(self.root)['total']
            with capacity.reservation(self.root,100,'motion') as budget:
                budget.commit(40)
            self.assertEqual(capacity.check(self.root)['total'],before+40)

    def test_atomic_reservations_prevent_overcommit(self):
        base=capacity.status(self.root)['total']
        limit=base+10_000_000_000+capacity.METADATA_HEADROOM+150
        with patch('g2h.cold_storage.LIMIT_BYTES',limit):
            with capacity.reservation(self.root,100,'first') as first:
                with self.assertRaises(RuntimeError):capacity.reservation(self.root,100,'second')
                first.commit(0)
            with capacity.reservation(self.root,100,'second') as second:second.commit(0)

    def test_concurrent_claims_admit_only_one_writer(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        base=capacity.status(self.root)['total'];start=Barrier(2)
        def claim(number):
            start.wait(timeout=2)
            try:return capacity.reservation(self.root,100,'writer'+str(number))
            except RuntimeError:return None
        limit=base+10_000_000_000+capacity.METADATA_HEADROOM+150
        with patch('g2h.cold_storage.LIMIT_BYTES',limit),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,range(2)))
            self.assertEqual(sum(item is not None for item in results),1)
            for item in results:
                if item:item.commit(0)

    def test_abandoned_partial_is_counted_then_reconciled(self):
        with capacity.reservation(self.root,10000,'partial'):
            (self.root/'partial.bin').write_bytes(b'x'*9000)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],10000)
        self.finish(capacity.Reconciler(self.root))
        result=capacity.status(self.root)
        self.assertEqual(result['reserved_bytes'],0)
        self.assertGreaterEqual(result['total'],9000)

    def test_missing_process_identity_keeps_live_writer_until_exit(self):
        with patch.object(capacity, 'identity', return_value=''):
            with capacity.reservation(self.root, 1000, 'writer without procfs'):
                self.finish(capacity.Reconciler(self.root))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'], 1000)
                with patch.object(capacity.os, 'kill', side_effect=ProcessLookupError):
                    self.finish(capacity.Reconciler(self.root))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'], 0)

    def test_failure_during_scan_waits_for_next_scan(self):
        scan=capacity.Reconciler(self.root)
        self.assertFalse(scan.step(seconds=.05,max_entries=1))
        with capacity.reservation(self.root,10000,'late partial'):
            (self.root/'partial.bin').write_bytes(b'x'*9000)
        self.finish(scan)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],10000)
        self.finish(scan)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

    def test_incomplete_or_unsafe_scan_does_not_grant_space(self):
        with closing(capacity.connect(self.root)) as db,db:
            db.execute('UPDATE capacity SET sampled=0')
        with self.assertRaises(RuntimeError):capacity.reservation(self.root,1,'stale')
        self.finish(capacity.Reconciler(self.root))
        (self.root/'symlink').symlink_to(self.root)
        with self.assertRaises(RuntimeError):self.finish(capacity.Reconciler(self.root))
        self.assertTrue(capacity.status(self.root)['stale'])

    def test_growth_during_scan_is_never_lost(self):
        scan=capacity.Reconciler(self.root)
        scan.step(seconds=.05,max_entries=1)
        with capacity.reservation(self.root,9000,'late write') as budget:
            (self.root/'motion').write_bytes(b'x'*9000);budget.commit(9000)
        self.finish(scan)
        self.assertGreaterEqual(capacity.status(self.root)['total'],9000)

    def test_replacement_after_new_baseline_keeps_peak_bound(self):
        old=self.root/'old';old.write_bytes(b'x'*9000)
        self.finish(capacity.Reconciler(self.root))
        with capacity.reservation(self.root,1000,'compact') as budget:
            old.unlink()
            self.finish(capacity.Reconciler(self.root))
            before=capacity.status(self.root)['accounted_bytes']
            (self.root/'new').write_bytes(b'x'*100)
            budget.commit(-8900)
        self.assertGreaterEqual(capacity.status(self.root)['accounted_bytes'],before+100)
        self.finish(capacity.Reconciler(self.root))
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

    def test_initial_reconciliation_does_not_require_full_scan(self):
        other=self.root/'fresh';other.mkdir()
        with patch('g2h.cold_storage.usage',side_effect=RuntimeError('initial walk too large')):
            self.assertFalse(capacity.status(other,initialize=False)['ready'])
            scan=capacity.Reconciler(other)
            self.finish(scan)
            self.assertTrue(capacity.status(other,initialize=False)['ready'])


if __name__=='__main__':unittest.main()
