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


if __name__=='__main__':
    unittest.main()
