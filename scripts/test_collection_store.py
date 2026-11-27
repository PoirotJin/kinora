"""Storage guarantees with small byte limits, without model imports."""
import pickle
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g2h.collection_store import CollectionStore, iter_committed, iter_records


class StoreTests(unittest.TestCase):
    def test_rotation_roundtrip_dedup_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [{"id": str(i), "text": "站立", "motion": bytes(150)} for i in range(8)]
            size = len(pickle.dumps(records[0], protocol=5))
            with CollectionStore(tmp, max_bytes=size*2) as store:
                for rec in records:
                    self.assertTrue(store.append(rec))
                self.assertFalse(store.append(records[0]))
            files = sorted(Path(tmp).glob("motions_*.pkl"))
            self.assertEqual(len(files), 4)
            self.assertTrue(all(p.stat().st_size <= size*2 for p in files))
            self.assertEqual([r for p in files for r in iter_records(p)], records)
            self.assertEqual(list(iter_committed(tmp)), records)
            with CollectionStore(tmp, max_bytes=size*2) as store:
                self.assertFalse(store.append(records[-1]))
                self.assertTrue(store.append({"id": "8", "motion": b"new"}))
            self.assertEqual(len(list(iter_committed(tmp))), 9)

    def test_recovers_uncommitted_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            with CollectionStore(tmp) as store:
                store.append({"id": "a", "motion": b"ok"})
            path = Path(tmp) / "motions_000000.pkl"
            size = path.stat().st_size
            with path.open("ab") as f:
                f.write(b"broken uncommitted pickle")
            with CollectionStore(tmp) as store:
                self.assertEqual(path.stat().st_size, size)
                store.append({"id": "b", "motion": b"ok2"})
            self.assertEqual([r["id"] for r in iter_records(path)], ["a", "b"])

    def test_rejects_corrupt_committed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with CollectionStore(tmp) as store:
                store.append({"id": "a"})
            (Path(tmp) / "motions_000000.pkl").write_bytes(b"")
            with self.assertRaises(RuntimeError):
                CollectionStore(tmp)

    def test_oversized_record_and_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            with CollectionStore(tmp, max_bytes=100) as store:
                with self.assertRaises(ValueError):
                    store.append({"id": "a", "payload": bytes(500)})
                with self.assertRaises(RuntimeError):
                    CollectionStore(tmp)
                self.assertEqual(list(iter_committed(tmp)), [])

    def test_interrupted_shard_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with CollectionStore(tmp) as store:
                with store.db:
                    store.db.execute("INSERT INTO shards VALUES (0, 0)")
            with CollectionStore(tmp) as store:
                store.append({"id": "a"})
            self.assertEqual(len(list(iter_committed(tmp))), 1)

    def test_checksums_detect_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with CollectionStore(tmp) as store:
                store.append({"id": "a", "text": "hello"})
            p = Path(tmp) / "motions_000000.pkl"
            data = p.read_bytes().replace(b"hello", b"world")
            p.write_bytes(data)
            with self.assertRaises(ValueError):
                list(iter_committed(tmp))


if __name__ == "__main__":
    unittest.main()
