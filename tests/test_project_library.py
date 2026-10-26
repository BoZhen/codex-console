import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application
import codex_console as c
from project_library import ProjectGroups, SessionCatalog


class CatalogTests(unittest.TestCase):
    def test_complete_catalog_cache_missing_folders_and_duplicate_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'work').mkdir()
            paths = []
            for i in range(2005):
                path = root / f'thread-{i:06}.jsonl'
                path.write_text('fixture'); paths.append(str(path))
            child = root / 'child-thread.jsonl'; child.write_text('fixture'); paths.append(str(child))
            duplicate = root / 'copy.jsonl'; duplicate.write_text('fixture'); paths.append(str(duplicate))
            def peek(path):
                return str(root / ('missing' if path.endswith('thread-000000.jsonl') else 'work')), '', Path(path).stem, path == str(child)
            def identity(path):
                return 'thread-000001' if path == str(duplicate) else Path(path).stem
            catalog = SessionCatalog()
            with mock.patch.object(c, '_peek_codex', side_effect=peek) as reader:
                rows = catalog.read(paths, reader, identity, {}, tmp)
                self.assertEqual(len(rows), 2005)
                self.assertFalse(next(r for r in rows if r['cc'] == 'thread-000000')['available'])
                calls = reader.call_count
                rows = catalog.read(paths, reader, identity, {'thread-000001': 'Renamed'}, tmp)
                self.assertEqual(reader.call_count, calls)
                self.assertEqual(next(r for r in rows if r['cc'] == 'thread-000001')['title'], 'Renamed')
                Path(paths[2]).write_text('changed size')
                catalog.read(paths, reader, identity, {}, tmp)
                self.assertEqual(reader.call_count, calls + 1)
                self.assertEqual(len(catalog.read(paths[3:], reader, identity, {}, tmp)), 2003)

    def test_snapshot_preserves_missing_and_runtime_folders_and_all_members(self):
        rows = [{'cc': f'thread-{i:06}', 'cwd': '/missing/old-project', 'title': 'Old', 'mtime': i, 'available': False} for i in range(101)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(c, 'PROJECT_GROUPS_FILE', tmp + '/groups.json'), \
             mock.patch.object(c._library_catalog, 'read', return_value=rows), mock.patch.object(c, '_rollout_paths', return_value=[]), \
             mock.patch.object(c, 'load_names', return_value={}), mock.patch.object(c, 'load_project_meta', return_value={}):
            data = c.project_library([{'cc': 'live-thread', 'cwd': '/tmp/work', 'title': 'Active', 'mtime': 200, 'available': True}])
        self.assertEqual(len(data['sessions']), 102)
        self.assertEqual({f['path'] for f in data['folders']}, {'/missing/old-project', '/tmp/work'})


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = ProjectGroups(Path(self.tmp.name) / 'groups.json')
        self.rows = [{'cc': 'thread-one', 'cwd': '/demo/a'}, {'cc': 'thread-two', 'cwd': '/demo/b'}]
        self.folders = {'/demo/a', '/demo/b'}

    def update(self, **body):
        return self.store.update(body, self.rows, self.folders)

    def test_create_rename_and_delete_preserve_sessions(self):
        group = self.update(op='create', name='  Project One  ')
        gid = group['id']; self.assertEqual(group['groups'][0]['name'], 'Project One')
        self.update(op='add', id=gid, kind='session', value='thread-one')
        self.update(op='rename', id=gid, name='\u9879\u76ee1')
        self.assertEqual(ProjectGroups(self.store.path).read()[0]['sessions'], ['thread-one'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.update(op='delete', id=gid)
        self.assertEqual(self.store.read(), [])
        self.assertEqual(len(self.rows), 2)

    def test_memberships_are_idempotent_and_allow_multiple_groups(self):
        first = self.update(op='create', name='One')['id']; second = self.update(op='create', name='Two')['id']
        for gid in (first, second, first):
            self.update(op='add', id=gid, kind='session', value='thread-one')
        for group in self.store.read():
            self.assertEqual(group['sessions'], ['thread-one'])
        self.update(op='add', id=first, kind='folder', value='/demo/a')
        self.update(op='remove', id=first, kind='session', value='thread-one')
        first_group = self.store.read()[0]
        self.assertEqual(first_group['folders'], ['/demo/a'])
        self.assertEqual(first_group['excluded'], ['thread-one'])
        self.update(op='add', id=first, kind='session', value='thread-one')
        self.assertEqual(self.store.read()[0]['excluded'], [])
        self.update(op='remove', id=first, kind='folder', value='/demo/a')
        self.assertEqual(self.store.read()[0]['sessions'], [])
        self.assertEqual(self.store.read()[1]['sessions'], ['thread-one'])

    def test_invalid_changes_never_replace_existing_metadata(self):
        gid = self.update(op='create', name='One')['id']; before = self.store.path.read_bytes()
        for body in ({'op': 'create', 'name': 'one', 'id': gid}, {'op': 'rename', 'id': gid, 'name': ''},
                     {'op': 'create', 'name': []}, {'op': 'create', 'name': 'x' * 121},
                     {'op': 'add', 'id': gid, 'kind': 'folder', 'value': '/unknown'},
                     {'op': 'add', 'id': gid, 'kind': 'session', 'value': 'thread-unknown'},
                     {'op': 'delete', 'id': 'missing'}):
            with self.subTest(body=body), self.assertRaises(ValueError): self.update(**body)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_corrupt_or_symlinked_metadata_is_not_overwritten(self):
        self.store.path.write_text('not json')
        with self.assertRaises(ValueError): self.update(op='create', name='One')
        self.assertEqual(self.store.path.read_text(), 'not json')
        self.store.path.unlink(); target = Path(self.tmp.name) / 'other'; target.write_text('private')
        self.store.path.symlink_to(target)
        with self.assertRaises(OSError): self.update(op='create', name='One')
        self.assertEqual(target.read_text(), 'private')

    def test_concurrent_devices_do_not_lose_updates(self):
        def add(index):
            return ProjectGroups(self.store.path).update({'op': 'create', 'name': f'Group {index}'}, self.rows, self.folders)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(12)))
        self.assertEqual(len(self.store.read()), 12)

    def test_limits_and_stale_folder_removal(self):
        gid = self.update(op='create', name='One')['id']
        self.update(op='add', id=gid, kind='folder', value='/demo/a')
        self.folders.clear(); self.rows.clear()
        self.update(op='remove', id=gid, kind='folder', value='/demo/a')
        self.assertEqual(self.store.read()[0]['folders'], [])
        with mock.patch.object(self.store, 'MAX_GROUPS', 1), self.assertRaises(ValueError):
            self.update(op='create', name='Two')


class LibraryTranscriptTests(unittest.TestCase):
    def test_open_by_session_id_keeps_missing_directory_history_readable(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(c, 'INDEX_DB', tmp + '/history.db'), \
             mock.patch.object(c, 'reindex') as refresh, mock.patch.object(c, 'load_names', return_value={}):
            db = c._index_conn()
            db.execute('INSERT INTO files(fid,path,cc,cwd,title) VALUES(1,?,?,?,?)',
                       ('/fixture/log.jsonl', 'thread-one', '/missing/project', 'Saved work'))
            db.executemany('INSERT INTO msgs(fid,uid,ts,role,txt) VALUES(1,?,?,?,?)',
                           [('one', '', 'user', 'First message'), ('two', '', 'assistant', 'Second message')])
            db.commit(); db.close()
            result = c.load_thread_for_session('thread-one')
            self.assertEqual([m['txt'] for m in result['messages']], ['First message', 'Second message'])
            self.assertEqual(result['cwd'], '/missing/project')
            self.assertTrue(result['atStart'] and result['atEnd'])
            self.assertIn('error', c.load_thread_for_session('thread-missing'))
            refresh.reset_mock()
            self.assertIn('error', c.load_thread_for_session('../not-a-session'))
            refresh.assert_not_called()


class LibraryHandlerTests(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'groups.json'
        for name, value in (('PROJECT_GROUPS_FILE', str(self.path)), ('CHAT_SESSIONS', {}), ('AUTH', '')):
            patcher = mock.patch.object(c, name, value); patcher.start(); self.addCleanup(patcher.stop)
        snapshot = mock.patch.object(c, 'project_library', side_effect=lambda live: {
            'sessions': [{'cc': 'thread-one', 'cwd': '/demo/a', 'title': 'One'}],
            'folders': [{'path': '/demo/a', 'name': 'A'}], 'groups': ProjectGroups(self.path).read()})
        snapshot.start(); self.addCleanup(snapshot.stop)
        super().setUp()

    def get_app(self):
        return Application([(r'/api/project-library', c.ProjectLibraryHandler), (r'/api/tree', c.TreeHandler)])

    def post(self, body, **headers):
        return self.fetch('/api/project-library', method='POST', body=json.dumps(body), headers={'Content-Type': 'application/json', **headers})

    def test_mutation_persists_and_notifies_other_views(self):
        client = mock.Mock()
        with mock.patch.object(c.ChatSocket, 'clients', {client}):
            response = self.post({'op': 'create', 'name': 'Project One'})
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        client._say.assert_called_once_with({'type': 'project_groups_changed'})
        result = json.loads(response.body)
        self.assertEqual(self.post({'op': 'add', 'id': result['id'], 'kind': 'session', 'value': 'thread-one'}).code, 200)
        restored = json.loads(self.fetch('/api/project-library').body)
        self.assertEqual(restored['groups'][0]['sessions'], ['thread-one'])

    def test_auth_origin_content_type_and_size_guards(self):
        with mock.patch.object(c, 'AUTH', 'fixture:only'):
            self.assertEqual(self.fetch('/api/project-library').code, 401)
            self.assertEqual(self.fetch('/api/tree').code, 401)
            self.assertEqual(self.post({'op': 'create', 'name': 'No'}).code, 401)
        for origin in ('https://elsewhere.invalid', 'null'):
            self.assertEqual(self.post({'op': 'create', 'name': 'No'}, Origin=origin).code, 403)
        self.assertEqual(self.post({}, **{'Content-Type': 'text/plain'}).code, 415)
        self.assertEqual(self.post({'name': 'x' * 9000}).code, 413)
        self.assertEqual(self.post([]).code, 400)
        self.assertFalse(self.path.exists())

    def test_missing_store_and_write_failure_are_explicit(self):
        self.assertEqual(json.loads(self.fetch('/api/project-library').body)['groups'], [])
        with mock.patch.object(ProjectGroups, 'update', side_effect=OSError('disk')):
            self.assertEqual(self.post({'op': 'create', 'name': 'One'}).code, 503)


if __name__ == '__main__':
    unittest.main()
