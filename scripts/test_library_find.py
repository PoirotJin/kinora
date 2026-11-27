"""Ranked library search over a temporary catalog; the embedding API is mocked."""
from contextlib import closing
from concurrent.futures import Future, ThreadPoolExecutor
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch
import numpy as np
import MotionFarm as farm
import library_find
import library_search_index
from library_find import find, id_score, keyword_score
from test_review_server import ReviewTests
from review_access import allowed, claim_owners, remove
from review_auth import Auth
from review_server import Handler, Server
from g2h import semantic_index
from g2h.collection_store import CollectionStore
from g2h.farm_index import register_motion

DIM = 64


def unit(*pairs):
    v = np.zeros(DIM, dtype='<f4')
    for i, w in pairs:
        v[i] = w
    return v / np.linalg.norm(v)


TURN, WALK, JUMP = 0, 1, 2


class LibraryFind(unittest.TestCase):
    def setUp(self):
        ReviewTests.setUp(self)
        library_find._RESULTS.clear(); library_find._VECTORS.clear(); library_find._MATRIX.clear()
        self.user = dict(username='alice', role='user')
        db = farm.catalog(self.root)
        semantic_index.configure(db, 'test-embedding', DIM)
        with db:
            semantic_index.save_vector(db, dict(id='a' * 64, topic_id='walking'), unit((WALK, 1)), DIM)
        self.ids = {'walk': self.mid}
        n = 0
        for name, text, duration, vector, generated in [
            ('left', 'A person turns left in place with small steps.', 6, unit((TURN, 1)), True),
            ('right', 'A person is turning right while walking slowly.', 10, unit((TURN, .9), (WALK, .43)), True),
            ('jump', 'A person jumps up and down twice.', 3, unit((JUMP, 1)), True),
            ('removed', 'A person turns around once.', 5, unit((TURN, 1)), True),
            ('bob', 'A person turns in a circle.', 4, unit((TURN, 1)), True),
            ('pending', 'A person turns to face backward.', 4, unit((TURN, 1)), False),
        ]:
            n += 1
            text_id = f'{n:x}' * 64
            text_id = text_id[:64]
            item = dict(id=text_id, text=text, topic_id='walking', duration_s=duration, attributes={}, source={})
            with db:
                db.execute('INSERT INTO prompts VALUES(?,?,?,?)', (text_id, 'walking', text, json.dumps(item)))
                semantic_index.save_vector(db, item, vector, DIM)
            task = 'task_' + name
            self.ids[name] = register_motion(db, text_id, 'walking', task, 100 + n)
            with CollectionStore(self.root / 'topics/walking') as store:
                store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)', (task, '{}')); store.db.commit()
                if generated:
                    store.append({'id': task, 'quality': {}, 'motion': {'fps': np.array(30),
                                  'native_keypoints3d': np.zeros((2, 2, 3)), 'native_transl': np.zeros((2, 3))}})
        db.close()
        claim_owners(self.root, [self.ids['bob']], 'bob')
        remove(self.store, self.ids['removed'], dict(role='admin', username='admin'), 'test')

    def tearDown(self):
        library_search_index.clear()
        self.tmp.cleanup()

    def search(self, **params):
        query = {k: [str(v)] for k, v in params.items()}
        return find(self.store, self.user, query)

    def mids(self, result):
        return [i['motion_id'] for i in result['items']]

    def test_chinese_query_matches_english_prompts_semantically(self):
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            result = self.search(q='转弯')
            self.assertEqual(self.mids(result), [self.ids['bob'], self.ids['left'], self.ids['right']])
            self.assertTrue(result['semantic']['used'])
            self.assertTrue(all(i['match'] == 'semantic' for i in result['items']))

    def test_shared_visibility_excludes_removed_and_ungenerated(self):
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            found = self.mids(self.search(q='turn'))
        self.assertIn(self.ids['bob'], found)
        for hidden in ('removed', 'pending'):
            self.assertNotIn(self.ids[hidden], found)
        self.assertEqual(self.search()['total'], 5)

    def test_fuzzy_keywords_without_embeddings(self):
        with patch.object(library_find, 'query_vector', side_effect=RuntimeError('no faiss')):
            result = self.search(q='turning')
        self.assertEqual(set(self.mids(result)), {self.ids['left'], self.ids['right'], self.ids['bob']})
        self.assertFalse(result['semantic']['used'])
        self.assertIn('关键词', result['semantic']['reason'])
        with patch.object(library_find, 'query_vector', side_effect=AssertionError('too short to embed')):
            self.assertEqual(set(self.mids(self.search(q='tu'))), {self.ids['left'], self.ids['right'], self.ids['bob']})
        self.assertEqual(keyword_score('A person turns left', ['turning']), 1.0)
        self.assertEqual(keyword_score('A person stepped back', ['stepping']), 1.0)
        self.assertEqual(keyword_score('A person jumps', ['turn']), 0.0)

    def test_motion_id_search_shares_the_box_and_skips_embedding(self):
        target = self.ids['jump']
        with patch.object(library_find, 'query_vector', side_effect=AssertionError('IDs must not be embedded')):
            self.assertEqual(self.mids(self.search(q=target))[0], target)
            self.assertEqual(self.mids(self.search(q=target.upper()))[0], target)
            self.assertIn(target, self.mids(self.search(q=target[1:].lstrip('0'))))
            self.assertEqual(self.search(q='mzzzzzzzz')['items'], [])
        self.assertEqual(id_score('m0000000a1', 'm0000000a1'), 3.0)
        self.assertEqual(id_score('m0000000a1', 'm00000'), 2.0)
        self.assertEqual(id_score('m0000000a1', 'a1'), 1.0)
        self.assertEqual(id_score('m0000000a1', 'b2'), 0.0)

    def test_sort_orders(self):
        visible = [self.ids[k] for k in ('walk', 'left', 'right', 'jump', 'bob')]
        self.assertEqual(self.mids(self.search(sort='created_asc')), visible)
        self.assertEqual(self.mids(self.search(sort='created_desc')), visible[::-1])
        self.assertEqual(self.mids(self.search()), visible[::-1])  # empty query: newest first
        # Unknown durations (the original fixture prompt) sort last both ways.
        self.assertEqual(self.mids(self.search(sort='duration_desc')), [self.ids[k] for k in ('right', 'left', 'bob', 'jump', 'walk')])
        self.assertEqual(self.mids(self.search(sort='duration_asc')), [self.ids[k] for k in ('jump', 'bob', 'left', 'right', 'walk')])
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            self.assertEqual(self.mids(self.search(q='转弯', sort='duration_asc')), [self.ids['bob'], self.ids['left'], self.ids['right']])

    def test_pagination_covers_every_result_once(self):
        first = self.search(limit=3)
        self.assertEqual((len(first['items']), first['total'], first['next_after']), (3, 5, 3))
        second = self.search(limit=3, after=first['next_after'])
        self.assertIsNone(second['next_after'])
        self.assertEqual(len(set(self.mids(first) + self.mids(second))), 5)

    def test_review_filters_and_changed_ownership_remain_fresh(self):
        self.store.save(self.ids['left'], self.value)
        self.assertEqual(self.mids(self.search(status='accepted')), [self.ids['left']])
        self.assertNotIn(self.ids['left'], self.mids(self.search(status='pending')))
        claim_owners(self.root, [self.ids['left']], 'alice')
        self.assertEqual(self.mids(self.search(author='alice')), [self.ids['left']])
        remove(self.store, self.ids['left'], dict(role='admin', username='admin'), 'test')
        self.assertEqual(self.search(author='alice')['items'], [])

    def test_multiple_generated_motions_share_a_prompt_vector(self):
        with closing(farm.catalog(self.root)) as db:
            other = register_motion(db, '1' * 64, 'walking', 'another_left', 999)
        with CollectionStore(self.root / 'topics/walking') as store:
            store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)', ('another_left', '{}'))
            store.db.commit()
            store.append({'id': 'another_left', 'quality': {}, 'motion': {'fps': np.array(30),
                          'native_keypoints3d': np.zeros((2, 2, 3)), 'native_transl': np.zeros((2, 3))}})
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            result = self.search(q='转弯')
        self.assertEqual(self.mids(result), [other, self.ids['bob'], self.ids['left'], self.ids['right']])
        self.assertEqual(result['items'][0]['score'], result['items'][1]['score'])

    def test_query_vector_is_embedded_once_and_billed_to_user(self):
        calls = []
        def fake_embed(texts, key_file, model, dimensions):
            from g2h.api_usage import current_user
            calls.append((texts, current_user.get(), model, dimensions))
            return [unit((TURN, 1))]
        with patch.object(semantic_index, 'embed', side_effect=fake_embed):
            first = self.search(q='转弯')
            again = self.search(q=' 转弯 ')
        self.assertEqual(calls, [(['转弯'], 'alice', 'test-embedding', DIM)])
        self.assertEqual(self.mids(first), self.mids(again))

    def test_overlapping_searches_share_the_same_billed_embedding(self):
        started = threading.Event()
        second_arrived = threading.Event()
        release = threading.Event()
        calls = []

        class ObservedFuture(Future):
            def result(self, timeout=None):
                second_arrived.set()
                return super().result(timeout)

        def fake_embed(texts, key_file, model, dimensions):
            from g2h.api_usage import current_user
            calls.append((texts, current_user.get()))
            if started.is_set():
                second_arrived.set()
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError('Timed out waiting for the second search')
            return [unit((TURN, 1))]

        with patch.object(semantic_index, 'embed', side_effect=fake_embed), patch.object(
                library_find, 'Future', ObservedFuture, create=True), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.search, q='转弯', sort='created_asc')
            try:
                self.assertTrue(started.wait(timeout=5))
                second = pool.submit(self.search, q=' 转弯 ', sort='created_desc')
                self.assertTrue(second_arrived.wait(timeout=5))
            finally:
                release.set()
            left, right = first.result(timeout=5), second.result(timeout=5)

        self.assertTrue(left['semantic']['used'])
        self.assertTrue(right['semantic']['used'])
        self.assertEqual(self.mids(left), self.mids(right)[::-1])
        self.assertEqual(calls, [(['转弯'], 'alice')])

    def test_failed_embedding_is_retried_on_the_next_search(self):
        with patch.object(semantic_index, 'embed', side_effect=[TimeoutError('offline'), [unit((TURN, 1))]]) as embed:
            self.assertFalse(self.search(q='转弯')['semantic']['used'])
            self.assertTrue(self.search(q='转弯')['semantic']['used'])
        self.assertEqual(embed.call_count, 2)

    def test_keyword_pages_keep_their_order_after_semantic_service_recovers(self):
        query = dict(q='turning', sort='created_desc', limit=1)
        with patch.object(library_find, 'query_vector', side_effect=[TimeoutError('offline'), unit((JUMP, 1))]) as embed:
            first = self.search(**query)
            self.assertFalse(first['semantic']['used'])
            self.assertEqual(self.mids(first), [self.ids['bob']])

            fresh = self.search(**query)
            self.assertTrue(fresh['semantic']['used'])
            self.assertEqual(fresh['total'], 4)

            second = self.search(**query, after=first['next_after'], keyword_only=1)
            third = self.search(**query, after=second['next_after'], keyword_only=1)
        self.assertEqual(self.mids(second) + self.mids(third), [self.ids['right'], self.ids['left']])
        self.assertFalse(second['semantic']['used'])
        self.assertEqual(second['total'], first['total'])
        self.assertIsNone(third['next_after'])
        self.assertEqual(embed.call_count, 2)

    def test_invalid_parameters_and_access(self):
        for bad in (dict(sort='random'), dict(q='x' * 201), dict(limit=0), dict(limit=51), dict(after=-1), dict(status='x'), dict(topic='nope')):
            with self.assertRaises(ValueError):
                self.search(**bad)
        self.assertTrue(allowed(dict(role='user'), '/api/library/find', False))
        self.assertFalse(allowed(dict(role='user'), '/api/library/find', True))

    def test_quota_exhaustion_falls_back_with_reason(self):
        from g2h.api_usage import QuotaExceeded
        with patch.object(library_find, 'query_vector', side_effect=QuotaExceeded('近 5 小时额度已用完')):
            result = self.search(q='转弯')
        self.assertEqual(result['items'], [])
        self.assertEqual(result['semantic'], dict(used=False, reason='近 5 小时额度已用完'))

    def test_index_matches_previous_search_results(self):
        import test_library_find_reference as reference
        from g2h import direction_index
        with closing(direction_index.connect(self.root)):
            pass
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))), \
             patch.object(reference, 'query_vector', return_value=unit((TURN, 1))):
            for q in ('', '转弯', 'turning', 'tu', self.ids['bob'], '0000', '1', 'mzzzzzzzz'):
                for sort in library_find.SORTS:
                    for author in ('', 'alice', 'bob'):
                        query = dict(q=[q], sort=[sort], author=[author], limit=['3'])
                        with self.subTest(q=q, sort=sort, author=author):
                            expected = reference.find(self.store, self.user, query)
                            actual = find(self.store, self.user, query)
                            self.assertEqual(actual, expected)

    def test_cached_pages_observe_removal_restore_and_author_changes(self):
        self.search(sort='created_asc', limit=1)
        remove(self.store, self.ids['left'], dict(role='admin', username='admin'), 'test')
        page = self.search(sort='created_asc', after=1)
        self.assertNotIn(self.ids['left'], self.mids(page))
        self.assertEqual(page['total'], 4)
        remove(self.store, self.ids['left'], dict(role='admin', username='admin'), restore=True)
        self.assertIn(self.ids['left'], self.mids(self.search(sort='created_asc', after=1)))
        self.assertEqual(self.mids(self.search(author='bob')), [self.ids['bob']])
        with closing(self.store.reviews()) as db, db:
            db.execute('UPDATE motion_owners SET username=? WHERE motion_id=?', ('alice', self.ids['bob']))
        self.assertEqual(self.search(author='bob')['items'], [])
        self.assertEqual(self.mids(self.search(author='alice')), [self.ids['bob']])

    def test_index_refreshes_new_records_and_edited_prompts(self):
        self.assertEqual(self.search()['total'], 5)
        with CollectionStore(self.root / 'topics/walking') as store:
            store.append({'id': 'task_pending', 'quality': {}, 'motion': {'fps': np.array(30),
                          'native_keypoints3d': np.zeros((2, 2, 3)), 'native_transl': np.zeros((2, 3))}})
        self.assertEqual(self.search()['total'], 6)
        with closing(farm.catalog(self.root)) as db, db:
            item = json.loads(db.execute('SELECT item FROM prompts WHERE id=?', ('1' * 64,)).fetchone()[0])
            item.update(text='A person waves both arms.', duration_s=50)
            db.execute('UPDATE prompts SET item=? WHERE id=?', (json.dumps(item), '1' * 64))
        first = self.search(sort='duration_desc')['items'][0]
        self.assertEqual((first['motion_id'], first['duration'], first['text']),
                         (self.ids['left'], 50, item['text']))

    def test_index_retries_a_catalog_revision_after_a_failed_read(self):
        query = {'sort': 'duration_desc'}
        self.assertEqual(self.search(**query)['items'][0]['motion_id'], self.ids['right'])
        with closing(farm.catalog(self.root)) as db, db:
            item = json.loads(db.execute('SELECT item FROM prompts WHERE id=?', ('1' * 64,)).fetchone()[0])
            item.update(text='A person waves both arms.', duration_s=50)
            db.execute('UPDATE prompts SET item=? WHERE id=?', (json.dumps(item), '1' * 64))

        index = library_search_index._INDEXES[str(self.root.resolve())]
        blocked = True

        def authorizer(action, table, *_):
            if blocked and action == sqlite3.SQLITE_READ and table == 'prompts':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        index.catalog.db.set_authorizer(authorizer)
        with self.assertRaises(sqlite3.DatabaseError):
            self.search(**query)
        blocked = False

        first = self.search(**query)['items'][0]
        self.assertEqual((first['motion_id'], first['duration'], first['text']),
                         (self.ids['left'], 50, item['text']))

    def test_direction_cache_updates_when_checked_count_is_unchanged(self):
        from g2h import direction_index
        from g2h.farm_index import motion_number
        with closing(direction_index.connect(self.root)) as db, db:
            direction_index.put(db, motion_number(self.ids['left']), 'test',
                                dict(version=direction_index.VERSION, status='pass'))
        self.assertEqual(self.mids(self.search(status='direction_pass')), [self.ids['left']])
        with closing(direction_index.connect(self.root)) as db, db:
            direction_index.put(db, motion_number(self.ids['left']), 'test',
                                dict(version=direction_index.VERSION, status='fail'))
        self.assertEqual(self.search(status='direction_pass')['items'], [])
        self.assertEqual(self.mids(self.search(status='direction_fail')), [self.ids['left']])

    def test_ranked_pages_match_legacy_direction_status_and_committed_state(self):
        from g2h import direction_index
        from g2h.farm_index import motion_number
        from gadget_library import search

        with closing(direction_index.connect(self.root)) as db, db:
            for name in ('left', 'right'):
                direction_index.put(db, motion_number(self.ids[name]), 'test',
                                    dict(version=direction_index.VERSION, status='pass'))
        # The append-only record remains usable even if the queue row is gone.
        with CollectionStore(self.root / 'topics/walking') as store:
            with store.db:
                store.db.execute('DELETE FROM tasks WHERE id=?', ('task_left',))

        ranked = self.search(status='direction_pass', sort='created_asc', limit=1)
        legacy = search(self.store, self.user, {'status': ['direction_pass'], 'limit': ['1']})
        expected_ids = [self.ids['left'], self.ids['right']]
        for expected in expected_ids:
            for page in (ranked, legacy):
                item, = page['items']
                self.assertEqual((item['motion_id'], item['direction_status'], item['state']),
                                 (expected, 'pass', 'done'))
                self.assertNotIn('location', item)
            if expected != expected_ids[-1]:
                ranked = self.search(status='direction_pass', sort='created_asc', limit=1,
                                     after=ranked['next_after'])
                legacy = search(self.store, self.user, {'status': ['direction_pass'], 'limit': ['1'],
                                                        'after': [str(legacy['next_after'])]})
        self.assertIsNone(ranked['next_after'])
        self.assertIsNone(legacy['next_after'])

    def test_direction_constraint_precedes_scores_and_survives_sort_and_paging(self):
        # Opposite-side prompts deliberately have equally good semantic vectors.
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            for query in ('右转', '向右转身', 'turn right', 'right turn'):
                for sort in library_find.SORTS:
                    result = self.search(q=query, sort=sort, limit=1)
                    self.assertEqual(self.mids(result), [self.ids['right']], (query, sort))
                    self.assertEqual(result['total'], 1)
                    self.assertEqual(self.search(q=query, sort=sort, after=1)['items'], [])
                    self.assertEqual(self.search(q=query, sort=sort, author='bob')['items'], [])
            for query in ('左转', 'turn left'):
                self.assertEqual(self.mids(self.search(q=query)), [self.ids['left']])
        with patch.object(library_find, 'query_vector', side_effect=RuntimeError('offline')):
            self.assertEqual(self.mids(self.search(q='turn right')), [self.ids['right']])


class LibraryFindHTTP(unittest.TestCase):
    """Exercise the desktop endpoint through real sessions and HTTP routing."""

    def setUp(self):
        self.fixture = LibraryFind()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ids = self.fixture.ids
        auth = Auth(self.fixture.root / 'review')
        for name in ('alice', 'bob'):
            auth.create(name, 'temporary-password-123')
            auth.change_password(name, 'temporary-password-123', 'changed-password-123')
        self.server = Server(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        self.server.store = self.fixture.store
        self.server.auth = auth
        self.server.web = self.fixture.root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.cookies = {}
        for name in ('alice', 'bob'):
            code, _, cookie = self.call('/api/auth/login', who=None, body={
                'username': name, 'password': 'changed-password-123'})
            self.assertEqual(code, 200)
            self.cookies[name] = cookie.split(';')[0]

    def call(self, path, who='alice', body=None):
        request = urllib.request.Request(self.url + path, headers={
            'Cookie': self.cookies.get(who, ''), 'Content-Type': 'application/json',
            'X-MotionFarm-Request': '1', 'Origin': self.url,
        }, data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers.get('Set-Cookie', '')

    def search(self, who='alice', **params):
        code, result, _ = self.call('/api/library/find?' + urllib.parse.urlencode(params), who)
        self.assertEqual(code, 200, result)
        return result

    def mids(self, result):
        return self.fixture.mids(result)

    def test_chinese_search_and_account_visibility(self):
        with patch.object(library_find, 'query_vector', return_value=unit((TURN, 1))):
            alice = self.search(q='转弯')
            self.assertTrue(alice['semantic']['used'])
            self.assertEqual(self.mids(alice), [self.ids['bob'], self.ids['left'], self.ids['right']])
            bob = self.search(who='bob', q='转弯')
            self.assertIn(self.ids['bob'], self.mids(bob))
            # Pagination reuses cached rankings, which must remain account-specific.
            page = self.search(q='转弯', after=1, limit=1)
            self.assertEqual(self.mids(page), [self.ids['left']])
            self.assertEqual(page['total'], 3)
            mine = self.search(who='bob', q='转弯', author='bob')
            self.assertEqual(self.mids(mine), [self.ids['bob']])
            self.assertEqual(self.search(q='转弯', author='alice')['items'], [])
            for hidden in ('removed', 'pending'):
                self.assertNotIn(self.ids[hidden], self.mids(bob))

    def test_id_fuzzy_fallback_sort_and_pagination(self):
        with patch.object(library_find, 'query_vector') as embed:
            self.assertEqual(self.mids(self.search(q=self.ids['jump'])), [self.ids['jump']])
            self.assertEqual(self.mids(self.search(q=self.ids['bob'])), [self.ids['bob']])
            embed.assert_not_called()
        with patch.object(library_find, 'query_vector', side_effect=RuntimeError('offline')):
            result = self.search(q='turning')
            self.assertEqual(set(self.mids(result)), {self.ids['left'], self.ids['right'], self.ids['bob']})
            self.assertFalse(result['semantic']['used'])
        first = self.search(sort='duration_desc', limit=2)
        self.assertEqual(self.mids(first), [self.ids['right'], self.ids['left']])
        self.assertEqual((first['total'], first['next_after']), (5, 2))
        second = self.search(sort='duration_desc', limit=2, after=first['next_after'])
        self.assertEqual(self.mids(second), [self.ids['bob'], self.ids['jump']])
        third = self.search(sort='duration_desc', limit=2, after=second['next_after'])
        self.assertEqual(self.mids(third), [self.ids['walk']])
        self.assertIsNone(third['next_after'])

    def test_authentication_permissions_and_validation(self):
        self.assertEqual(self.call('/api/library/find', who=None)[0], 401)
        self.assertEqual(self.call('/api/library/find', body={})[0], 403)
        self.assertEqual(self.call('/api/library/find?sort=random')[0], 400)
        self.server.auth.kick('alice')
        self.assertEqual(self.call('/api/library/find')[0], 401)


if __name__ == '__main__':
    unittest.main()
