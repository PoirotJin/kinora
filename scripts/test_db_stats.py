import sqlite3
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h import db_stats


class CounterTests(unittest.TestCase):
    def test_collection_counters_follow_commit_and_rollback(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE tasks(id PRIMARY KEY,state);CREATE TABLE records(id PRIMARY KEY);INSERT INTO tasks VALUES(1,"pending");INSERT INTO records VALUES(1);')
        db_stats.collection(db);db_stats.collection(db)
        with db:
            db.execute('UPDATE tasks SET state="done" WHERE id=1')
            db.execute('INSERT INTO tasks VALUES(2,"pending")')
            db.execute('INSERT INTO records VALUES(2)')
        db.execute('INSERT INTO records VALUES(3)');db.rollback()
        self.assertEqual(dict(db.execute('SELECT state,total FROM task_state_counts WHERE total>0')),{'done':1,'pending':1})
        self.assertEqual(db.execute('SELECT total FROM record_count').fetchone()[0],2)
        with db:db.execute('DELETE FROM tasks WHERE id=2');db.execute('DELETE FROM records WHERE id=2')
        self.assertEqual(db.execute('SELECT total FROM record_count').fetchone()[0],1)

    def test_embedding_topic_moves_and_removal(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE embeddings(id PRIMARY KEY,topic);INSERT INTO embeddings VALUES(1,"a");')
        db_stats.embeddings(db);db_stats.embeddings(db)
        with db:
            db.execute('UPDATE embeddings SET topic="b" WHERE id=1')
            db.execute('INSERT INTO embeddings VALUES(2,"b")')
            db.execute('DELETE FROM embeddings WHERE id=1')
        self.assertEqual(dict(db.execute('SELECT topic,total FROM embedding_counts WHERE total>0')),{'b':1})


if __name__=='__main__':unittest.main()
