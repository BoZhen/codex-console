"""Read-only, bounded access to explicitly published Android releases."""
import json
import os
import re
import stat
from pathlib import Path

PACKAGE = "org.codexconsole.android"
MAX_APK_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
APK_PREFIX = "/api/app/android/releases/"


def validate_manifest(data):
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise ValueError("Unsupported update manifest")
    if data.get("packageName") != PACKAGE:
        raise ValueError("Unexpected Android package")
    for field, low, high in (("versionCode", 1, 2147483647), ("minSdk", 1, 100),
                             ("size", 1, MAX_APK_BYTES)):
        if type(data.get(field)) is not int or not low <= data[field] <= high:
            raise ValueError("Invalid update metadata")
    for field, limit in (("versionName", 80), ("notes", 4000), ("publishedAt", 80)):
        if not isinstance(data.get(field), str) or len(data[field]) > limit:
            raise ValueError("Invalid update text")
    for field in ("sha256", "signingCertificateSha256"):
        if not isinstance(data.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", data[field]):
            raise ValueError("Invalid update digest")
    if data.get("apkPath") != APK_PREFIX + data["sha256"] + ".apk":
        raise ValueError("Invalid APK path")
    return {k: data[k] for k in ("schemaVersion", "packageName", "versionCode", "versionName",
                                "minSdk", "size", "notes", "publishedAt", "sha256",
                                "signingCertificateSha256", "apkPath")}


class ReleaseStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def _open(path, limit):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise ValueError("Invalid release file")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def open_apk(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid APK digest")
        return self._open(self.directory / (digest + ".apk"), MAX_APK_BYTES)

    def latest(self):
        with self._open(self.directory / "latest.json", MAX_MANIFEST_BYTES) as source:
            data = validate_manifest(json.loads(source.read(MAX_MANIFEST_BYTES + 1)))
        with self.open_apk(data["sha256"]) as apk:
            if os.fstat(apk.fileno()).st_size != data["size"]:
                raise ValueError("APK size mismatch")
        return data
