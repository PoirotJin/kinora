"""Bounded CPU scoring batch, suitable for an independent systemd timer."""
import argparse
from contextlib import closing
import fcntl
import json
import os
import pickle
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g2h import auto_review


def run(root, assets, limit=16):
    from g2h.tmr_scoring import Scorer
    from g2h.agent_learning import analyze
    from g2h.body_quality import VERSION as BODY_VERSION
    from g2h.farm_index import lookup_motion, read_motion
    root = Path(root).resolve()
    with closing(auto_review.connect(root)) as db:
        lock = (root / 'review/auto_review.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return dict(state='already_running')
        try:
            scorer = Scorer(assets)
            base_version=scorer.version
            scorer.version += ":"+BODY_VERSION
            with db:
                db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', ('version', scorer.version))
                db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', ('started', str(time.time())))
            # Only committed, already-indexed candidates. No payload scans.
            db.execute('ATTACH DATABASE ? AS agent', (str(root / 'review/agent.sqlite3'),))
            db.execute('ATTACH DATABASE ? AS human', (str(root / 'review/reviews.sqlite3'),))
            topics = [r[0] for r in db.execute('SELECT DISTINCT topic FROM agent.checks LIMIT 30')]
            pending = [x[0] for x in db.execute("""SELECT h.motion_id FROM human.latest h
              LEFT JOIN scores s ON s.motion_id=h.motion_id AND s.version=?
              WHERE s.motion_id IS NULL AND NOT EXISTS(SELECT 1 FROM human.removed r WHERE r.motion_id=h.motion_id)
              ORDER BY h.rowid DESC LIMIT 8""",(scorer.version,))]
            per_topic = max(1, limit // max(1, len(topics)))
            for topic in topics:
                query = '''SELECT c.motion_id FROM agent.checks c
                  LEFT JOIN scores s ON s.motion_id=c.motion_id AND s.version=?
                  WHERE c.topic=? AND s.motion_id IS NULL AND NOT EXISTS
                  (SELECT 1 FROM human.removed r WHERE r.motion_id=c.motion_id) ORDER BY c.created '''
                # Keep current production moving while incrementally backfilling.
                newest=[r[0] for r in db.execute(query+'DESC LIMIT ?', (scorer.version,topic,max(1,per_topic//2)))]
                oldest=[r[0] for r in db.execute(query+'ASC LIMIT ?', (scorer.version,topic,per_topic))]
                pending += list(dict.fromkeys(newest+oldest))[:per_topic]
            from review_access import hidden
            removed = hidden(root, pending)
            done = 0
            with closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro', uri=True)) as catalog:
                for mid in list(dict.fromkeys(pending))[:limit]:
                    if mid in removed:
                        continue
                    info = lookup_motion(catalog, root, mid)
                    if not info['location']:
                        continue
                    try:
                        record = read_motion(catalog, root, mid)
                    except (OSError, ValueError, EOFError, pickle.UnpicklingError) as exc:
                        record=dict(motion_id=mid,topic=info['topic'],text_id=info['text_id'])
                        auto_review.save(root,record,info['location']['sha256'],
                            dict(score=None,version=scorer.version,error='source_read_'+type(exc).__name__),['source_unreadable'])
                        done += 1
                        continue
                    record.setdefault('topic', info['topic'])
                    from motion_operations import save_descriptor
                    try:save_descriptor(root,record,info['location']['sha256'])
                    except (ValueError,KeyError):pass
                    record['text'] = json.loads(catalog.execute('SELECT item FROM prompts WHERE id=?', (info['text_id'],)).fetchone()[0])['text']
                    try:
                        metrics, flags, _ = analyze(record)
                    except (ValueError, KeyError) as exc:
                        result=dict(score=None, version=scorer.version, integrity='invalid' if isinstance(exc,ValueError) else 'unverified', error=type(exc).__name__)
                        auto_review.save(root, record, info['location']['sha256'], result, ['invalid_motion'])
                        done += 1
                        continue
                    try:
                        cached=db.execute('SELECT body,source_sha256,score FROM scores WHERE motion_id=? AND version=?',(mid,base_version)).fetchone()
                        if cached and cached['source_sha256']==info['location']['sha256'] and cached['score'] is not None:
                            result=json.loads(cached['body']);result['version']=scorer.version;result['tmr_reused']=True
                        else:result = scorer.score(record)
                    except (ValueError, KeyError) as exc:
                        # Unsupported conversions are review candidates, not corrupt source claims.
                        flags = flags + ['scoring_failed']
                        result = dict(score=None, version=scorer.version, error=type(exc).__name__)
                    result['body_quality']=metrics.get('body_quality')
                    auto_review.save(root, record, info['location']['sha256'], result, flags)
                    done += 1
            auto_review.calibrate(root, scorer.version)
            with db:
                db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', ('completed', str(time.time())))
                db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', ('last_batch', str(done)))
                db.execute('DELETE FROM runtime WHERE key="error"')
            return dict(state='completed', scored=done, version=scorer.version)
        except Exception as exc:
            with db:
                db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', ('error', type(exc).__name__))
            raise
        finally:
            lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--assets', required=True)
    parser.add_argument('--limit', type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.limit <= 64:
        parser.error('limit must be 1..64')
    print(json.dumps(run(args.root, args.assets, args.limit)))
