import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_console as c


class RenameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.file=Path(tmp.name)/'names.json'
        self.live=c.ChatSession('live',tmp.name,'default','full-access')
        self.live.cc_id='fixture-thread-one'
        self.live.log=[{'kind':'user_text','text':'Original task title'}]
        for patcher in (mock.patch.object(c,'NAMES_FILE',str(self.file)),
                        mock.patch.object(c,'_names_cache',{'mtime':-1.0,'v':{}}),
                        mock.patch.object(c,'CHAT_SESSIONS',{'live':self.live})):
            patcher.start();self.addCleanup(patcher.stop)
        self.socket=object.__new__(c.ChatSocket);self.socket.session=None
        self.socket._say=mock.Mock();self.socket._broadcast_favorites=mock.Mock();self.socket._broadcast_tree=mock.Mock()

    async def send(self, **message):
        await self.socket.on_message(json.dumps(message))
        return self.socket._say.call_args.args[0]

    async def test_save_and_clear_persist_without_starting_or_resuming(self):
        reply=await self.send(type='rename',cc=self.live.cc_id,name='  New \u540d\u79f0  ',requestId='rename-one')
        self.assertTrue(reply['ok']);self.assertEqual(reply['requestId'],'rename-one')
        self.assertEqual(reply['name'],'New \u540d\u79f0');self.assertEqual(reply['title'],reply['name'])
        self.assertEqual(json.loads(self.file.read_text())[self.live.cc_id],reply['name'])
        self.assertEqual(self.live.title(),reply['name'])
        reply=await self.send(type='rename',cc=self.live.cc_id,name='',requestId='reset-one')
        self.assertTrue(reply['ok']);self.assertEqual(reply['title'],'Original task title')
        self.assertNotIn(self.live.cc_id,json.loads(self.file.read_text()))
        self.assertIsNone(self.live.proc)

    async def test_historic_session_and_old_clients_remain_supported(self):
        reply=await self.send(type='rename',cc='historic-thread',name='x'*140)
        self.assertTrue(reply['ok']);self.assertEqual(reply['name'],'x'*120)
        self.assertIsNone(reply['requestId'])
        self.assertEqual(json.loads(self.file.read_text())['historic-thread'],'x'*120)

    async def test_bad_input_or_storage_failure_returns_correlated_error(self):
        for fields in ({'cc':None,'name':'test'},{'cc':{},'name':'test'},
                       {'cc':self.live.cc_id,'name':{}},{'cc':'../bad','name':'test'}):
            reply=await self.send(type='rename',requestId='bad-request',**fields)
            self.assertFalse(reply['ok']);self.assertEqual(reply['requestId'],'bad-request');self.assertTrue(reply['error'])
        with mock.patch.object(c,'set_name',return_value=False):
            reply=await self.send(type='rename',cc=self.live.cc_id,name='test',requestId='write-failed')
        self.assertFalse(reply['ok']);self.assertEqual(reply['requestId'],'write-failed')
        self.socket._broadcast_tree.assert_not_called()

    async def test_folder_label_has_success_and_failure_acknowledgements(self):
        for success in (True,False):
            with mock.patch.object(c,'save_project_meta',return_value=success) as save:
                reply=await self.send(type='proj_rename',path='/demo/project',name='Project label',requestId='folder-one')
                save.assert_called_once_with('/demo/project',name='Project label')
            self.assertEqual(reply['type'],'project_renamed');self.assertEqual(reply['ok'],success)
            self.assertEqual(reply['requestId'],'folder-one');self.assertEqual(reply['path'],'/demo/project')
