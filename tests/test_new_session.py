import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

import tornado.web
from tornado.testing import AsyncHTTPTestCase
import codex_console as c


class DirectoryListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        for name in ('Project Alpha', 'beta', '.hidden'):
            (self.home / name).mkdir()
        (self.home / 'file.txt').write_text('not a directory')
        self.outside = Path(self.tmp.name) / 'outside'
        self.outside.mkdir()
        (self.home / 'external').symlink_to(self.outside, target_is_directory=True)
        patcher = mock.patch.object(c, 'HOME', str(self.home))
        patcher.start();self.addCleanup(patcher.stop)

    def test_browse_home_spaces_empty_hidden_and_parent(self):
        with mock.patch.object(c, '_projects_cached') as projects:
            listing = c.browse_directories('~')
        projects.assert_not_called()
        self.assertEqual([r['name'] for r in listing['folders']], ['beta','Project Alpha'])
        self.assertEqual(listing['path'], str(self.home))
        self.assertEqual(listing['parent'], str(self.home))
        self.assertFalse(listing['truncated'])
        child = c.browse_directories('~/Project Alpha')
        self.assertEqual(child['folders'], [])
        self.assertEqual(child['parent'], str(self.home))
        self.assertEqual(c.browse_directories('beta')['path'], str(self.home/'beta'))
        self.assertEqual(len(c.browse_directories('~',hidden=True)['folders']), 3)
        limited = c.browse_directories('~',limit=1)
        self.assertEqual(len(limited['folders']), 1)
        self.assertTrue(limited['truncated'])

    def test_non_directory_traversal_and_symlink_escape_are_rejected(self):
        for path in ('~/missing','~/file.txt',str(self.home/'external'),
                     str(self.home/'../outside'),str(self.outside)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                c.browse_directories(path)

    def test_unreadable_folder_reports_error(self):
        with mock.patch.object(c.os,'scandir',side_effect=PermissionError()):
            with self.assertRaisesRegex(ValueError,'Cannot read'):
                c.browse_directories('~')

    def test_prefix_filters_before_the_limit_without_project_scanning(self):
        for name in ('Notebook archive', 'Notebooks', 'notes.txt'):
            path=self.home/name
            if name.endswith('.txt'): path.write_text('not a directory')
            else: path.mkdir()
        with mock.patch.object(c, '_projects_cached') as projects:
            listing=c.browse_directories('~', prefix='NOTE', limit=1)
        projects.assert_not_called()
        self.assertEqual([r['name'] for r in listing['folders']], ['Notebook archive'])
        self.assertTrue(listing['truncated'])
        listing=c.browse_directories('~', prefix='pRoJeCt a')
        self.assertEqual([r['name'] for r in listing['folders']], ['Project Alpha'])
        self.assertFalse(listing['truncated'])
        self.assertEqual(c.browse_directories('~', prefix='external')['folders'], [])
        self.assertEqual(c.browse_directories('~', prefix='no match')['folders'], [])

    def test_prefix_supports_unicode_hidden_names_and_rejects_invalid_values(self):
        name='\u5b9e\u9a8c records'
        (self.home/name).mkdir()
        self.assertEqual(c.browse_directories('~', prefix='\u5b9e')['folders'][0]['name'], name)
        self.assertEqual(c.browse_directories('~', prefix='.hi')['folders'], [])
        self.assertEqual(c.browse_directories('~', True, prefix='.hi')['folders'][0]['name'], '.hidden')
        for prefix in ('../', 'bad\0name', 'x'*256, {}, 1):
            with self.subTest(prefix=repr(prefix)), self.assertRaises(ValueError):
                c.browse_directories('~', prefix=prefix)

    def test_create_single_child_with_spaces_unicode_and_inside_home_symlink(self):
        name='New \u5b9e\u9a8c'
        result=c.create_directory('~/Project Alpha',name)
        path=self.home/'Project Alpha'/name
        self.assertEqual(result,{'path':str(path)})
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()),[])
        (self.home/'alias').symlink_to(self.home/'beta',target_is_directory=True)
        self.assertEqual(c.create_directory(str(self.home/'alias'),'child')['path'],str(self.home/'beta/child'))

    def test_create_rejects_invalid_names_parents_and_path_traversal(self):
        for name in ('',' ','..','.','../escape','a/b','a\\b','/absolute','bad\0name','bad\nname','x'*256,None,{},'\ud800'):
            with self.subTest(name=repr(name)),self.assertRaises(ValueError):
                c.create_directory('~',name)
        for parent in (None,{},'',str(self.outside),str(self.home/'external'),str(self.home/'missing')):
            with self.subTest(parent=parent),self.assertRaises(ValueError):
                c.create_directory(parent,'child')
        self.assertEqual(list(self.outside.iterdir()),[])
        self.assertEqual(set(p.name for p in self.home.iterdir()),{'Project Alpha','beta','.hidden','file.txt','external'})

    def test_create_never_overwrites_existing_entries(self):
        for name in ('Project Alpha','file.txt','external'):
            with self.subTest(name=name),self.assertRaises(FileExistsError):
                c.create_directory('~',name)
        self.assertEqual((self.home/'file.txt').read_text(),'not a directory')
        self.assertTrue((self.home/'external').is_symlink())

    def test_create_refuses_symlink_swap_after_parent_validation(self):
        original=c._directory_path
        def swap(path):
            resolved=original(path)
            (self.home/'beta').rmdir()
            (self.home/'beta').symlink_to(self.outside,target_is_directory=True)
            return resolved
        with mock.patch.object(c,'_directory_path',side_effect=swap),self.assertRaises(OSError):
            c.create_directory(str(self.home/'beta'),'child')
        self.assertFalse((self.outside/'child').exists())


class DirectoryHandlerTests(AsyncHTTPTestCase):
    def get_app(self):
        return tornado.web.Application([(r'/api/directories',c.DirectoriesHandler)])

    def test_requires_auth_and_returns_controlled_errors(self):
        with mock.patch.object(c,'AUTH','test:not-a-real-password'):
            self.assertEqual(self.fetch('/api/directories').code,401)
        with mock.patch.object(c,'AUTH',''), mock.patch.object(c,'browse_directories',side_effect=ValueError('Cannot read this directory')):
            response = self.fetch('/api/directories?'+urlencode({'path':'/missing'}))
            self.assertEqual(response.code,400)
            self.assertEqual(json.loads(response.body),{'error':'Cannot read this directory'})

    def test_passes_path_and_hidden_flag(self):
        result={'path':'/home/demo','parent':'/home/demo','home':'/home/demo','folders':[],'truncated':False}
        with mock.patch.object(c,'AUTH',''), mock.patch.object(c,'browse_directories',return_value=result) as browse:
            response=self.fetch('/api/directories?'+urlencode({'path':'~/Project Alpha','hidden':1}))
        self.assertEqual(response.code,200)
        self.assertEqual(json.loads(response.body),result)
        browse.assert_called_once_with('~/Project Alpha',True)

    def test_completion_passes_literal_prefix_and_disables_cache(self):
        result={'path':'/home/demo','parent':'/home/demo','home':'/home/demo','folders':[],'truncated':False}
        with mock.patch.object(c,'AUTH',''), mock.patch.object(c,'browse_directories',return_value=result) as browse:
            response=self.fetch('/api/directories?'+urlencode({'path':'~','hidden':1,'prefix':'.hi'}))
        self.assertEqual(response.code,200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(response.body),result)
        browse.assert_called_once_with('~',True,prefix='.hi')

    def test_post_creates_folder_and_reports_conflict(self):
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(c,'HOME',tmp),mock.patch.object(c,'AUTH',''):
            headers={'Content-Type':'application/json','Origin':self.get_url('/').rstrip('/').replace('http:','https:')}
            body=json.dumps({'path':'~','name':'New project'})
            response=self.fetch('/api/directories',method='POST',headers=headers,body=body)
            self.assertEqual(response.code,201)
            self.assertEqual(json.loads(response.body),{'path':str(Path(tmp)/'New project')})
            self.assertTrue((Path(tmp)/'New project').is_dir())
            repeated=self.fetch('/api/directories',method='POST',headers=headers,body=body)
            self.assertEqual(repeated.code,409)

    def test_post_rejects_unauthenticated_cross_origin_and_non_json_writes(self):
        body=json.dumps({'path':'~','name':'child'})
        with mock.patch.object(c,'create_directory') as create:
            with mock.patch.object(c,'AUTH','test:not-a-real-password'):
                self.assertEqual(self.fetch('/api/directories',method='POST',body=body,
                    headers={'Content-Type':'application/json'}).code,401)
            with mock.patch.object(c,'AUTH',''):
                for origin in ('https://untrusted.example','null'):
                    response=self.fetch('/api/directories',method='POST',body=body,
                        headers={'Content-Type':'application/json','Origin':origin})
                    self.assertEqual(response.code,403)
                response=self.fetch('/api/directories',method='POST',body=body,headers={'Content-Type':'text/plain'})
                self.assertEqual(response.code,415)
                response=self.fetch('/api/directories',method='POST',body='x'*8193,headers={'Content-Type':'application/json'})
                self.assertEqual(response.code,413)
                for invalid in ('[]','{'):
                    response=self.fetch('/api/directories',method='POST',body=invalid,headers={'Content-Type':'application/json'})
                    self.assertEqual(response.code,400)
            create.assert_not_called()

    def test_post_permission_and_os_errors_are_visible(self):
        for error,status in ((PermissionError(),403),(OSError(),500),(ValueError('Invalid name'),400)):
            with self.subTest(status=status),mock.patch.object(c,'AUTH',''), \
                 mock.patch.object(c,'create_directory',side_effect=error):
                response=self.fetch('/api/directories',method='POST',headers={'Content-Type':'application/json'},
                    body=json.dumps({'path':'~','name':'child'}))
                self.assertEqual(response.code,status)
                self.assertTrue(json.loads(response.body)['error'])


class NewSessionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_correlation_and_failed_start_keeps_previous_view(self):
        socket=object.__new__(c.ChatSocket);socket._say=mock.Mock()
        previous=mock.Mock();socket.session=previous
        failed=c.ChatSession('new-id',os.getcwd(),'default','read-only')
        failed.start=mock.AsyncMock(side_effect=RuntimeError('start failed'))
        with mock.patch.object(c,'CODEX_BIN',sys.executable), mock.patch.object(c,'CHAT_SESSIONS',{}), \
             mock.patch.object(c,'ChatSession',return_value=failed):
            await socket.on_message(json.dumps({'type':'start','requestId':'request-a','cwd':os.getcwd()}))
            self.assertEqual(c.CHAT_SESSIONS,{})
        self.assertIs(socket.session,previous)
        previous.detach.assert_not_called()
        self.assertEqual(socket._say.call_args.args[0],{'type':'error','error':'spawn failed: start failed','requestId':'request-a'})

    async def test_success_echoes_request_id_and_attaches_only_after_start(self):
        socket=object.__new__(c.ChatSocket);socket._say=mock.Mock()
        previous=mock.Mock();socket.session=previous
        created=c.ChatSession('new-id',os.getcwd(),'default','read-only')
        async def start():
            self.assertIs(socket.session,previous)
            previous.detach.assert_not_called()
        created.start=mock.AsyncMock(side_effect=start)
        with mock.patch.object(c,'CODEX_BIN',sys.executable), mock.patch.object(c,'CHAT_SESSIONS',{}), \
             mock.patch.object(c.secrets,'token_hex',return_value='new-id'), \
             mock.patch.object(c,'ChatSession',return_value=created):
            await socket.on_message(json.dumps({'type':'start','requestId':'request-a','cwd':os.getcwd()}))
            self.assertIs(c.CHAT_SESSIONS['new-id'],created)
        self.assertIs(socket.session,created)
        previous.detach.assert_called_once_with(socket)
        reply=socket._say.call_args.args[0]
        self.assertEqual(reply['type'],'started')
        self.assertEqual(reply['requestId'],'request-a')

    async def test_invalid_directory_reply_is_correlated(self):
        socket=object.__new__(c.ChatSocket);socket._say=mock.Mock();socket.session=None
        with mock.patch.object(c,'CODEX_BIN',sys.executable), mock.patch.object(c,'safe_cwd',return_value=None):
            await socket.on_message(json.dumps({'type':'start','requestId':'request-a','cwd':'/invalid'}))
        self.assertEqual(socket._say.call_args.args[0],{'type':'error','error':'invalid working directory','requestId':'request-a'})


class NamedSessionControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.names = Path(self.tmp.name) / 'names.json'
        self.cc = '01234567-89ab-cdef-0123-456789abcdef'
        for name, value in (('NAMES_FILE', str(self.names)), ('_names_cache', {'mtime': -1.0, 'v': {}}),
                            ('CODEX_BIN', sys.executable), ('CHAT_SESSIONS', {})):
            patcher = mock.patch.object(c, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.session_type = c.ChatSession
        self.session = c.ChatSession('new-id', os.getcwd(), 'default', 'read-only')
        async def start():
            self.session.cc_id = self.cc
            self.session.thread_id = self.cc
        self.session.start = mock.AsyncMock(side_effect=start)
        self.factory = mock.patch.object(c, 'ChatSession', return_value=self.session)
        self.created = self.factory.start(); self.addCleanup(self.factory.stop)
        sid = mock.patch.object(c.secrets, 'token_hex', return_value='new-id')
        sid.start(); self.addCleanup(sid.stop)
        self.socket = object.__new__(c.ChatSocket)
        self.socket._say = mock.Mock()
        self.previous = mock.Mock()
        self.socket.session = self.previous

    async def start(self, **extra):
        await self.socket.on_message(json.dumps({'type': 'start', 'cwd': os.getcwd(),
                                                'requestId': 'named-request', **extra}))
        return self.socket._say.call_args.args[0]

    async def test_name_is_persisted_and_visible_before_the_first_message(self):
        name = '\u5b9e\u9a8c A <comparison>'
        reply = await self.start(name='  ' + name + '  ')
        self.assertEqual(reply['type'], 'started')
        self.assertEqual(reply['title'], name)
        self.assertEqual(reply['cc'], self.cc)
        self.assertEqual(reply['requestId'], 'named-request')
        self.assertEqual(reply['name'], os.path.basename(os.getcwd()))
        self.assertEqual(json.loads(self.names.read_text()), {self.cc: name})
        self.assertEqual(self.session.log, [])
        self.session.log.append({'kind': 'user_text', 'text': 'Do not replace the custom name'})
        self.assertEqual(self.session.title(), name)
        await self.socket.on_message(json.dumps({'type': 'list'}))
        self.assertEqual(self.socket._say.call_args.args[0]['sessions'][0]['title'], name)
        await self.socket.on_message(json.dumps({'type': 'attach', 'id': 'new-id'}))
        self.assertEqual(self.socket._say.call_args.args[0]['title'], name)
        c._names_cache['mtime'] = -1
        restored = self.session_type('restored', os.getcwd(), 'default', 'read-only', resume_cc=self.cc)
        self.assertEqual(restored.title(), name)
        self.assertTrue(c.set_name(self.cc, 'Changed later'))
        self.assertEqual(restored.title(), 'Changed later')

    async def test_blank_or_missing_name_keeps_automatic_naming(self):
        with mock.patch.object(c, 'set_name') as save:
            for extra in ({}, {'name': ''}, {'name': '   '}):
                reply = await self.start(**extra)
                self.assertEqual(reply['type'], 'started')
                self.assertEqual(reply['title'], '')
                self.assertNotIn('warning', reply)
            save.assert_not_called()
        self.assertFalse(self.names.exists())
        self.session.log.append({'kind': 'user_text', 'text': 'Automatic title'})
        self.assertEqual(self.session.title(), 'Automatic title')

    async def test_name_uses_existing_rename_length_limit(self):
        reply = await self.start(name='x' * 150)
        self.assertEqual(reply['title'], 'x' * 120)
        self.assertEqual(json.loads(self.names.read_text())[self.cc], 'x' * 120)

    async def test_non_text_name_is_rejected_before_starting_a_process(self):
        for value in (None, 42, False, [], {}):
            reply = await self.start(name=value)
            self.assertEqual(reply, {'type': 'error', 'error': 'invalid session name', 'requestId': 'named-request'})
        self.created.assert_not_called()
        self.session.start.assert_not_awaited()
        self.assertIs(self.socket.session, self.previous)
        self.assertFalse(self.names.exists())

    async def test_failed_start_does_not_save_a_name(self):
        self.session.start.side_effect = RuntimeError('start failed')
        with mock.patch.object(c, 'set_name') as save:
            reply = await self.start(name='Retained draft name')
            save.assert_not_called()
        self.assertEqual(reply['type'], 'error')
        self.assertFalse(self.names.exists())
        self.assertIs(self.socket.session, self.previous)
        self.assertEqual(c.CHAT_SESSIONS, {})

    async def test_name_save_failure_keeps_the_created_session_and_reports_a_warning(self):
        with mock.patch.object(c, 'set_name', return_value=False):
            reply = await self.start(name='Cannot persist')
        self.assertEqual(reply['type'], 'started')
        self.assertIn('name could not be saved', reply['warning'])
        self.assertEqual(reply['title'], '')
        self.assertIs(self.socket.session, self.session)
        self.assertIs(c.CHAT_SESSIONS['new-id'], self.session)
        self.session.start.assert_awaited_once()


if __name__=='__main__':
    unittest.main()
