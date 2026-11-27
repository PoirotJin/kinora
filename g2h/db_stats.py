"""Transactional SQLite counters for frequently sampled, append-heavy tables."""


def _initialize(db,kind,statements):
    db.execute('CREATE TABLE IF NOT EXISTS mf_stats_meta(kind TEXT PRIMARY KEY)')
    if db.execute('SELECT 1 FROM mf_stats_meta WHERE kind=?',(kind,)).fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM mf_stats_meta WHERE kind=?',(kind,)).fetchone():
            for statement in statements:db.execute(statement)
            db.execute('INSERT INTO mf_stats_meta VALUES(?)',(kind,))
        db.commit()
    except BaseException:
        db.rollback();raise


def collection(db):
    _initialize(db,'collection_v1',[
        'CREATE TABLE task_state_counts(state TEXT PRIMARY KEY,total INTEGER NOT NULL)',
        'INSERT INTO task_state_counts SELECT state,count(*) FROM tasks GROUP BY state',
        'CREATE TABLE record_count(id INTEGER PRIMARY KEY CHECK(id=1),total INTEGER NOT NULL)',
        'INSERT INTO record_count SELECT 1,count(*) FROM records',
        '''CREATE TRIGGER task_count_insert AFTER INSERT ON tasks BEGIN
          INSERT INTO task_state_counts VALUES(new.state,1) ON CONFLICT(state) DO UPDATE SET total=total+1; END''',
        '''CREATE TRIGGER task_count_delete AFTER DELETE ON tasks BEGIN
          UPDATE task_state_counts SET total=total-1 WHERE state=old.state; END''',
        '''CREATE TRIGGER task_count_update AFTER UPDATE OF state ON tasks WHEN old.state!=new.state BEGIN
          UPDATE task_state_counts SET total=total-1 WHERE state=old.state;
          INSERT INTO task_state_counts VALUES(new.state,1) ON CONFLICT(state) DO UPDATE SET total=total+1; END''',
        'CREATE TRIGGER record_count_insert AFTER INSERT ON records BEGIN UPDATE record_count SET total=total+1 WHERE id=1; END',
        'CREATE TRIGGER record_count_delete AFTER DELETE ON records BEGIN UPDATE record_count SET total=total-1 WHERE id=1; END',
    ])


def embeddings(db):
    _initialize(db,'embeddings_v1',[
        'CREATE TABLE embedding_counts(topic TEXT PRIMARY KEY,total INTEGER NOT NULL)',
        'INSERT INTO embedding_counts SELECT topic,count(*) FROM embeddings GROUP BY topic',
        '''CREATE TRIGGER embedding_count_insert AFTER INSERT ON embeddings BEGIN
          INSERT INTO embedding_counts VALUES(new.topic,1) ON CONFLICT(topic) DO UPDATE SET total=total+1; END''',
        '''CREATE TRIGGER embedding_count_delete AFTER DELETE ON embeddings BEGIN
          UPDATE embedding_counts SET total=total-1 WHERE topic=old.topic; END''',
        '''CREATE TRIGGER embedding_count_update AFTER UPDATE OF topic ON embeddings WHEN old.topic!=new.topic BEGIN
          UPDATE embedding_counts SET total=total-1 WHERE topic=old.topic;
          INSERT INTO embedding_counts VALUES(new.topic,1) ON CONFLICT(topic) DO UPDATE SET total=total+1; END''',
    ])
