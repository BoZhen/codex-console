import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

import codex_console as console
from android_updates import APK_PREFIX, MANIFESTS, PACKAGE, ReleaseStore


class AndroidUpdatesTest(AsyncHTTPTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.auth = mock.patch.object(console, "AUTH", "fixture:only")
        self.auth.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.auth.stop()
        self.temp.cleanup()

    def get_app(self):
        params = {"release_dir": str(self.directory)}
        return Application([
            (r"/api/app/android/latest", console.AndroidUpdateHandler, params),
            (r"/api/app/android/releases/([0-9a-f]{64})\.apk", console.AndroidApkHandler, params),
        ])

    def published(self, content=b"APK fixture", package=PACKAGE):
        sha = hashlib.sha256(content).hexdigest()
        (self.directory / (sha + ".apk")).write_bytes(content)
        data = {"schemaVersion": 1, "packageName": package, "versionCode": 4,
                "versionName": "0.3.0", "minSdk": 29, "size": len(content),
                "sha256": sha, "signingCertificateSha256": "a" * 64,
                "apkPath": APK_PREFIX + sha + ".apk", "notes": "Fixture release", "publishedAt": "2026-01-01"}
        (self.directory / MANIFESTS[package]).write_text(json.dumps(data))
        return data

    def request(self, path, **kwargs):
        return self.fetch(path, headers={"Authorization": "Basic " + base64.b64encode(b"fixture:only").decode()}, **kwargs)

    def test_auth_required_for_manifest_apk_and_head(self):
        data = self.published()
        for path, method in [("/api/app/android/latest", "GET"), (data["apkPath"], "GET"), (data["apkPath"], "HEAD")]:
            self.assertEqual(self.fetch(path, method=method).code, 401)

    def test_unpublished_channel_is_404(self):
        self.assertEqual(self.request("/api/app/android/latest").code, 404)

    def test_preview_is_separate_and_legacy_requests_stay_stable(self):
        stable = self.published()
        path = "/api/app/android/latest?package="
        preview_package = PACKAGE + ".preview"
        self.assertEqual(self.request(path + preview_package).code, 404)
        preview = self.published(b"preview APK", preview_package)
        for suffix, expected in [("", stable), ("?package=" + PACKAGE, stable),
                                 ("?package=" + preview_package, preview)]:
            response = self.request("/api/app/android/latest" + suffix)
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body), expected)
        self.assertEqual(self.request(preview["apkPath"]).body, b"preview APK")
        self.assertEqual(self.fetch(path + preview_package).code, 401)

    def test_unknown_packages_cannot_select_arbitrary_files(self):
        self.published()
        for package in ["", "other.app", PACKAGE + ".qatest", "..%2Flatest.json", "%2Ftmp%2Flatest.json"]:
            self.assertEqual(self.request("/api/app/android/latest?package=" + package).code, 404)

    def test_wrong_channel_and_symlinked_manifests_are_rejected(self):
        stable = self.published()
        preview_package = PACKAGE + ".preview"
        preview_file = self.directory / MANIFESTS[preview_package]
        preview_file.write_text(json.dumps(stable))
        path = "/api/app/android/latest?package=" + preview_package
        self.assertEqual(self.request(path).code, 503)
        preview_file.unlink()
        preview_file.symlink_to(self.directory / "latest.json")
        self.assertEqual(self.request(path).code, 503)
        self.assertEqual(self.request("/api/app/android/latest").code, 200)

    def test_manifest_and_streamed_apk(self):
        data = self.published(b"x" * 300000)
        response = self.request("/api/app/android/latest")
        self.assertEqual(json.loads(response.body), data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = self.request(data["apkPath"])
        self.assertEqual(response.body, b"x" * 300000)
        self.assertEqual(response.headers["Content-Type"], "application/vnd.android.package-archive")
        self.assertEqual(self.request(data["apkPath"], method="HEAD").body, b"")

    def test_invalid_metadata_does_not_advertise_update(self):
        for field, value in [("size", -1), ("schemaVersion", True), ("packageName", "other.app"), ("apkPath", "https://evil.invalid/update.apk")]:
            data = self.published()
            data[field] = value
            (self.directory / "latest.json").write_text(json.dumps(data))
            self.assertEqual(self.request("/api/app/android/latest").code, 503)

    def test_symlinked_release_files_are_not_served(self):
        data = self.published()
        archive = self.directory / (data["sha256"] + ".apk")
        archive.unlink()
        os.symlink(self.directory / "latest.json", archive)
        self.assertEqual(self.request(data["apkPath"]).code, 404)
        self.assertEqual(self.request("/api/app/android/latest").code, 503)

    def test_oversized_manifest_and_missing_archive(self):
        data = self.published()
        (self.directory / (data["sha256"] + ".apk")).unlink()
        self.assertEqual(self.request("/api/app/android/latest").code, 404)
        (self.directory / "latest.json").write_text(" " * 20000)
        self.assertEqual(self.request("/api/app/android/latest").code, 503)

    def test_older_content_addressed_apks_remain_available(self):
        old = self.published(b"old APK")
        new = self.published(b"new APK")
        self.assertEqual(self.request(old["apkPath"]).body, b"old APK")
        self.assertEqual(ReleaseStore(self.directory).latest()["sha256"], new["sha256"])
