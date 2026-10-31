import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote, urlencode

import tornado.web
from tornado.testing import AsyncHTTPTestCase

import codex_console as c
from attachments import MAX_BYTES, read_local_image


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1sAAAAASUVORK5CYII=')


class LocalImageTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base=Path(tmp.name)
        self.home=self.base/'home';self.home.mkdir()
        self.folder=self.home/'Results with spaces';self.folder.mkdir()
        self.image=self.folder/'result (1).png';self.image.write_bytes(PNG)
        self.outside=self.base/'outside';self.outside.mkdir()
        (self.outside/'result.png').write_bytes(PNG)

    def test_absolute_tilde_spaces_unicode_and_internal_alias(self):
        meta,raw=read_local_image(str(self.image),str(self.home))
        self.assertEqual(raw,PNG)
        self.assertEqual(meta,{'name':self.image.name,'media_type':'image/png','size':len(PNG)})
        self.assertEqual(read_local_image('~/Results with spaces/result (1).png',str(self.home))[1],PNG)
        named=self.folder/'\u7ed3\u679c.png';named.write_bytes(PNG)
        self.assertEqual(read_local_image(str(named),str(self.home))[0]['name'],named.name)
        alias=self.home/'alias';alias.symlink_to(self.folder,target_is_directory=True)
        self.assertEqual(read_local_image(str(alias/self.image.name),str(self.home))[1],PNG)
        self.assertEqual(set(p.name for p in self.folder.iterdir()),{self.image.name,named.name})

    def test_extension_and_signature_must_agree(self):
        for ext,raw,mt in (('.jpeg',b'\xff\xd8\xffdata','image/jpeg'),
                           ('.gif',b'GIF89a'+b'data','image/gif'),
                           ('.webp',b'RIFF0000WEBPdata','image/webp')):
            file=self.folder/('test'+ext);file.write_bytes(raw)
            self.assertEqual(read_local_image(str(file),str(self.home))[0]['media_type'],mt)
        for name,raw in (('bad.png',b'<script>not an image</script>'),('wrong.png',b'\xff\xd8\xff'),
                         ('active.svg',b'<svg onload="bad()"/>'),('page.html',PNG),('no-extension',PNG)):
            file=self.folder/name;file.write_bytes(raw)
            with self.subTest(name=name),self.assertRaises(ValueError):
                read_local_image(str(file),str(self.home))

    def test_outside_paths_symlinks_missing_files_and_bad_input(self):
        alias=self.home/'escape';alias.symlink_to(self.outside,target_is_directory=True)
        for path in (str(self.outside/'result.png'),str(alias/'result.png'),
                     str(self.home/'../outside/result.png'),'relative.png','https://example.invalid/image.png',
                     'file:///etc/image.png','','bad\0.png','bad\n.png','x'*4097,None):
            with self.subTest(path=repr(path)),self.assertRaises((OSError,ValueError)):
                read_local_image(path,str(self.home))
        with self.assertRaises(FileNotFoundError):
            read_local_image(str(self.home/'missing.png'),str(self.home))

    def test_size_and_nonregular_files_are_rejected_without_blocking(self):
        large=self.home/'large.png'
        with large.open('wb') as out:
            out.write(PNG);out.truncate(MAX_BYTES+1)
        empty=self.home/'empty.png';empty.touch()
        directory=self.home/'directory.png';directory.mkdir()
        pipe=self.home/'pipe.png';os.mkfifo(pipe)
        for path in (large,empty,directory,pipe):
            with self.subTest(path=path.name),self.assertRaises(ValueError):
                read_local_image(str(path),str(self.home))

    def test_parent_symlink_swap_after_resolution_is_refused(self):
        original=os.path.realpath
        def swap(path):
            resolved=original(path)
            if path==str(self.image):
                self.folder.rename(self.home/'old-results')
                self.folder.symlink_to(self.outside,target_is_directory=True)
            return resolved
        with mock.patch('attachments.os.path.realpath',side_effect=swap),self.assertRaises(OSError):
            read_local_image(str(self.image),str(self.home))


class InlineImageHTTPTests(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)/'home';self.home.mkdir()
        self.image=self.home/'\u7ed3\u679c a+b.png';self.image.write_bytes(PNG)
        self.auth={'Authorization':'Basic '+base64.b64encode(b'fixture:test').decode()}
        self.patchers=[mock.patch.object(c,'HOME',str(self.home)),mock.patch.object(c,'AUTH','fixture:test')]
        for patcher in self.patchers:patcher.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for patcher in self.patchers:patcher.stop()
        self.tmp.cleanup()

    def get_app(self):
        return tornado.web.Application([(r'/api/inline-image',c.InlineImageHandler)])

    def url(self,path=None,download=False):
        return '/api/inline-image?'+urlencode({'path':str(path or self.image),'download':int(download)})

    def test_authenticated_original_bytes_private_headers_and_download_name(self):
        self.assertEqual(self.fetch(self.url()).code,401)
        response=self.fetch(self.url(),headers=self.auth)
        self.assertEqual(response.code,200);self.assertEqual(response.body,PNG)
        self.assertEqual(response.headers['Content-Type'],'image/png')
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual(response.headers['Cross-Origin-Resource-Policy'],'same-origin')
        self.assertEqual(response.headers['Cache-Control'],'private, no-store')
        self.assertIn('sandbox',response.headers['Content-Security-Policy'])
        response=self.fetch(self.url(download=True),headers=self.auth)
        self.assertEqual(response.headers['Content-Disposition'],"attachment; filename*=UTF-8''"+quote(self.image.name,safe=''))
        self.assertEqual(response.body,PNG)

    def test_head_and_safe_failures(self):
        response=self.fetch(self.url(),method='HEAD',headers=self.auth)
        self.assertEqual(response.code,200);self.assertEqual(response.body,b'')
        self.assertEqual(int(response.headers['Content-Length']),len(PNG))
        self.assertEqual(self.fetch(self.url(),method='HEAD').code,401)
        bad=self.home/'bad.png';bad.write_text('<html>private text</html>')
        for path in (bad,self.home/'missing.png',Path(self.tmp.name)/'other.png'):
            response=self.fetch(self.url(path),headers=self.auth)
            self.assertEqual(response.code,404)
            self.assertNotIn(b'private text',response.body)


if __name__=='__main__':
    unittest.main()
