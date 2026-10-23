import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

import tornado.web
from tornado.testing import AsyncHTTPTestCase

import codex_console as c
from attachments import AttachmentStore, safe_name


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1sAAAAASUVORK5CYII=')


def picture(name='sample.png'):
    return {'name': name, 'media_type': 'image/png', 'data': base64.b64encode(PNG).decode()}


class StorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = AttachmentStore(str(self.base/'uploads'))
        patcher = mock.patch.object(c, 'ATTACHMENTS', self.store)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_image_original_bytes_unicode_name_private_permissions_and_persistence(self):
        meta = self.store.save(picture('../\u56fe\u7247.png'), 'image')
        self.assertEqual(meta['name'], '\u56fe\u7247.png')
        self.assertEqual(self.store.read(meta['id']), (meta, PNG))
        self.assertEqual(AttachmentStore(self.store.root).metadata(meta['id']), meta)
        self.assertEqual(os.stat(self.store.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store.image_path(meta)).st_mode & 0o777, 0o600)
        self.assertNotEqual(self.store.save(picture(), 'image')['id'], meta['id'])
        for path in ('/etc/passwd', self.store.root+'/../secret', self.store.root+'/'+meta['id']+'/meta.json'):
            self.assertIsNone(self.store.image_id(path))

    def test_types_invalid_base64_oversize_and_filename_traversal(self):
        for value in ({'media_type':'image/svg+xml','data':'PHN2Zy8+'},
                      {'media_type':'image/png','data':'not base64'},
                      {'media_type':'image/png','data':base64.b64encode(b'<html>bad</html>').decode()},
                      {'media_type':'image/png','data':base64.b64encode(PNG+b'x'*(5*1024*1024)).decode()}):
            with self.subTest(value=value['media_type']), self.assertRaises(ValueError):
                self.store.save(value, 'image')
        self.assertEqual(safe_name('C:\\private\\test.py\r\n'), 'test.py')
        self.assertEqual(safe_name('..'), 'attachment.txt')
        self.assertLessEqual(len(safe_name('\u4e2d'*200).encode()), 180)

    def test_refuses_symlink_metadata_payload_directory_and_nonregular_file(self):
        meta = self.store.save(picture(), 'image')
        folder = Path(self.store.root)/meta['id']
        target = self.base/'private'; target.write_bytes(PNG)
        payload = Path(self.store.image_path(meta)); payload.unlink(); payload.symlink_to(target)
        with self.assertRaises(OSError): self.store.read(meta['id'])
        payload.unlink(); os.mkfifo(payload)
        with self.assertRaises(ValueError): self.store.read(meta['id'])
        metadata = folder/'meta.json'; metadata.unlink(); metadata.symlink_to(target)
        with self.assertRaises(OSError): self.store.metadata(meta['id'])
        other_id = 'f'*32; (Path(self.store.root)/other_id).symlink_to(folder)
        with self.assertRaises(OSError): self.store.metadata(other_id)
        for aid in ('../private', '/etc/passwd', 'not-an-id'):
            with self.assertRaises(ValueError): self.store.read(aid)

    def test_save_copy_never_overwrites_and_is_home_scoped(self):
        home = self.base/'home'; home.mkdir(); (home/'code.py').write_text('existing')
        meta = self.store.save({'name':'code.py','text':'print("new")\r\n'}, 'text')
        result = self.store.copy_to(meta['id'], str(home), str(home))
        self.assertEqual(Path(result['path']).name, 'code-2.py')
        self.assertEqual((home/'code.py').read_text(), 'existing')
        self.assertEqual(Path(result['path']).read_bytes(), b'print("new")\r\n')
        (home/'escape').symlink_to(self.base)
        for folder in (str(self.base), str(home/'escape')):
            with self.assertRaises(ValueError): self.store.copy_to(meta['id'], str(home), folder)
        for name in ('../bad', 'a\\b', 'a\nb', '.', ''):
            with self.assertRaises(ValueError): self.store.copy_to(meta['id'], str(home), str(home), name)

    def test_live_echo_queue_and_input_share_one_copy_and_do_not_log_bodies(self):
        session = c.ChatSession('demo', self.base.as_posix(), 'default', 'full-access')
        session.proc = object(); session.busy = True
        session._emit = lambda _: None
        session.send_user('Review', [picture()], [{'name':'sample.py','text':'private-body'}])
        item = session.queue[0]
        self.assertEqual(len(list(Path(self.store.root).iterdir())), 2)
        inp = session._make_input(item['text'], item['images'], item['files'])
        self.assertEqual(inp[-1]['type'], 'localImage')
        self.assertEqual(Path(inp[-1]['path']).read_bytes(), PNG)
        self.assertIn('private-body', inp[1]['text'])
        session._echo_user(item['text'], item['images'], item['files'], qid=item['qid'])
        self.assertEqual(len(session.log[-1]['attachments']), 2)
        self.assertNotIn('private-body', repr(session.log))
        self.assertNotIn(picture()['data'], repr(session.log))
        self.assertEqual(len(list(Path(self.store.root).iterdir())), 2)

    def test_history_restores_refs_from_core_image_markup_and_inline_file(self):
        session = c.ChatSession('demo', self.base.as_posix(), 'default', 'full-access')
        files = c._sanitize_text_files([{'name':'report.py','text':'print("hello")\n</attached_file>\n'}])
        inp = session._make_input('Read this', [picture()], files)
        core = [{'type':'input_text','text': '\n'.join(x['text'] for x in inp if x['type']=='text')},
                {'type':'input_text','text':'<image name=[Image #1] path="'+inp[-1]['path']+'">'},
                {'type':'input_image','image_url':'data:image/png;base64,'+picture()['data']}]
        rec = {'type':'response_item','payload':{'type':'message','role':'user','content':core}}
        event = c.parse_codex(rec, 1)[0]
        self.assertEqual(event['text'], 'Read this')
        self.assertEqual(event['images'], 1)
        self.assertEqual([x['kind'] for x in event['attachments']], ['image', 'text'])
        self.assertEqual({x['name'] for x in event['attachments']}, {'report.py','sample.png'})
        self.assertNotIn('hello', repr(event))
        self.assertNotIn('base64', repr(event))
        # Missing copies still have a bounded placeholder, not a whole file dump.
        (Path(self.store.root)/files[0]['attachment']['id']/'meta.json').unlink()
        missing = c.parse_codex(rec, 1)[0]
        self.assertTrue(missing['attachments'][1]['unavailable'])
        self.assertEqual(missing['text'], 'Read this')

    def test_image_only_history_and_old_image_blocks_are_not_lost_or_inlined(self):
        session = c.ChatSession('demo', self.base.as_posix(), 'default', 'full-access')
        inp = session._make_input('', [picture()])
        rec = {'type':'response_item','payload':{'type':'message','role':'user','content':inp}}
        self.assertEqual(len(c.parse_codex(rec, 1)[0]['attachments']), 1)
        rec['payload']['content'] = [{'type':'input_image','image_url':'data:image/png;base64,private'}]
        self.assertEqual(c.parse_codex(rec, 1)[0]['images'], 1)
        self.assertNotIn('private', repr(c.parse_codex(rec, 1)))

    def test_store_failure_does_not_dispatch_or_queue_incomplete_message(self):
        session = c.ChatSession('demo', self.base.as_posix(), 'default', 'full-access')
        session.proc = object(); session._emit = lambda _: None
        with mock.patch.object(self.store, 'save', side_effect=OSError('disk full')), mock.patch.object(session, '_dispatch') as dispatch:
            session.send_user('Check', [picture()])
        dispatch.assert_not_called()
        self.assertEqual(session.queue, [])
        self.assertIn('not sent', session.log[-1]['text'])

    def test_failed_batch_removes_only_new_copies(self):
        old = picture(); c._store_attachments([old], [])
        new = picture()
        with self.assertRaises(ValueError):
            c._store_attachments([old, new, {'data':'bad','media_type':'image/png'}], [])
        self.assertNotIn('attachment', new)
        self.assertEqual([p.name for p in Path(self.store.root).iterdir()], [old['attachment']['id']])

    def test_ordinary_user_text_and_nonimage_blocks_are_preserved(self):
        self.assertEqual(c._codex_user_content('  Keep whitespace\n')['text'], '  Keep whitespace\n')
        content = [{'type':'input_text','text':'Use this'}, {'type':'reference','name':'example'}]
        self.assertEqual(c._codex_user_content(content)['text'], c._txt(content))


class AttachmentHTTPTests(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttachmentStore(self.tmp.name+'/uploads')
        self.home = Path(self.tmp.name)/'home'; self.home.mkdir()
        self.meta = self.store.save(picture('\u4e2d\u6587 a+b.png'), 'image')
        self.auth = {'Authorization':'Basic '+base64.b64encode(b'fixture:test').decode()}
        self.patchers = [mock.patch.object(c,'ATTACHMENTS',self.store), mock.patch.object(c,'HOME',str(self.home)),
                         mock.patch.object(c,'AUTH','fixture:test')]
        for patcher in self.patchers: patcher.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for patcher in self.patchers: patcher.stop()
        self.tmp.cleanup()

    def get_app(self):
        return tornado.web.Application([(r'/api/attachments/([0-9a-f]{32})',c.AttachmentHandler),
                                       (r'/api/attachments/([0-9a-f]{32})/save',c.AttachmentSaveHandler)])

    def url(self, meta=None):
        return '/api/attachments/'+(meta or self.meta)['id']

    def test_auth_original_bytes_download_filename_and_no_active_content(self):
        self.assertEqual(self.fetch(self.url()).code, 401)
        result = self.fetch(self.url(), headers=self.auth)
        self.assertEqual(result.body, PNG)
        self.assertEqual(result.headers['Content-Type'], 'image/png')
        result = self.fetch(self.url()+'?download=1', headers=self.auth)
        self.assertEqual(result.headers['Content-Disposition'], "attachment; filename*=UTF-8''"+quote(self.meta['name'], safe=''))
        meta = self.store.save({'name':'page.html','text':'<script>bad()</script>'}, 'text')
        result = self.fetch(self.url(meta), headers=self.auth)
        self.assertEqual(result.headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn('sandbox', result.headers['Content-Security-Policy'])
        self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(result.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(self.fetch('/api/attachments/'+'0'*32, headers=self.auth).code, 404)

    def test_save_is_authenticated_same_origin_json_and_never_overwrites(self):
        path = self.url()+'/save'
        headers = {**self.auth, 'Content-Type':'application/json', 'Origin':self.get_url('/').rstrip('/')}
        body = json.dumps({'dir':'~'})
        self.assertEqual(self.fetch(path, method='POST',body=body).code, 401)
        self.assertEqual(self.fetch(path, method='POST',body=body,headers={**headers,'Origin':'https://elsewhere.invalid'}).code, 403)
        self.assertEqual(self.fetch(path, method='POST',body=body,headers={**headers,'Content-Type':'text/plain'}).code, 415)
        self.assertEqual(self.fetch(path, method='POST',body='x'*8193,headers=headers).code, 413)
        self.assertEqual(self.fetch(path, method='POST',body='[]',headers=headers).code, 400)
        self.assertEqual(self.fetch(path, method='POST',body=json.dumps({'dir':self.tmp.name}),headers=headers).code, 400)
        result = self.fetch(path, method='POST',body=body,headers=headers)
        self.assertEqual(result.code, 201)
        first = Path(json.loads(result.body)['path'])
        self.assertEqual(first.read_bytes(), PNG)
        second = self.fetch(path, method='POST',body=body,headers=headers)
        self.assertNotEqual(first.as_posix(), json.loads(second.body)['path'])


if __name__ == '__main__':
    unittest.main()
