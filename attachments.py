"""Private upload copies and bounded raster reads for the chat interface."""

import base64
from contextlib import contextmanager
import json
import os
import re
import secrets
import stat


IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
IMAGE_EXTENSIONS = {ext: media_type for media_type, ext in IMAGE_TYPES.items()}
IMAGE_EXTENSIONS[".jpeg"] = "image/jpeg"
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
MAX_BYTES = 5 * 1024 * 1024


def safe_name(value, fallback="attachment.txt"):
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c >= " " and c != "\x7f")
    name = name.encode("utf-8", "replace")[:180].decode("utf-8", "ignore").strip()
    return name if name and name not in (".", "..") else fallback


def image_media_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_bytes(image):
    mt = image.get("media_type")
    if mt not in IMAGE_TYPES:
        raise ValueError("Unsupported image type")
    try:
        raw = base64.b64decode(image["data"], validate=True)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid image data") from exc
    if not raw or len(raw) > MAX_BYTES or image_media_type(raw) != mt:
        raise ValueError("Invalid or oversized image")
    return raw


def read_local_image(path, home):
    if (not isinstance(path, str) or not path or len(path) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError("Invalid image path")
    home = os.path.realpath(home)
    if path.startswith("~/"):
        path = os.path.join(home, path[2:])
    if not os.path.isabs(path):
        raise ValueError("Use an absolute image path")
    path = os.path.realpath(path)
    if path == home or os.path.commonpath((home, path)) != home:
        raise ValueError("Image is outside the home directory")
    media_type = IMAGE_EXTENSIONS.get(os.path.splitext(path)[1].lower())
    if not media_type:
        raise ValueError("Unsupported image type")
    # Resolve first, then walk from HOME without following components that can be swapped.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(home, flags)
    try:
        parts = os.path.relpath(path, home).split(os.sep)
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                raise ValueError("Invalid or oversized image")
            with os.fdopen(child, "rb", closefd=False) as source:
                raw = source.read(MAX_BYTES + 1)
        finally:
            os.close(child)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES or image_media_type(raw) != media_type:
        raise ValueError("Invalid image data")
    return {"name": safe_name(os.path.basename(path)), "media_type": media_type, "size": len(raw)}, raw


class AttachmentStore:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))

    @contextmanager
    def _directory(self, aid=None, create=False):
        if aid is not None and not ID_RE.fullmatch(aid):
            raise ValueError("Invalid attachment ID")
        if create:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.root, flags)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid():
                raise PermissionError("Attachment directory is not owned by this user")
            if create:
                os.fchmod(fd, 0o700)
            if aid is not None:
                child = os.open(aid, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _read(fd, name, limit):
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Invalid attachment file")
            raw = source.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("Attachment exceeds size limit")
            return raw

    @staticmethod
    def _write(fd, name, data):
        child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(child, "wb") as out:
                out.write(data)
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise

    def save(self, item, kind):
        mt = item.get("media_type") if kind == "image" else "text/plain"
        raw = image_bytes(item) if kind == "image" else item["text"].encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("Attachment exceeds size limit")
        ext = IMAGE_TYPES[mt] if kind == "image" else ".txt"
        aid = secrets.token_hex(16)
        meta = {"id": aid, "kind": kind, "name": safe_name(item.get("name"), "image" + ext if kind == "image" else "attachment.txt"),
                "size": len(raw), "media_type": mt}
        with self._directory(create=True) as root:
            os.mkdir(aid, mode=0o700, dir_fd=root)
            fd = os.open(aid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                self._write(fd, "content" + ext, raw)
                self._write(fd, "meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            except BaseException:
                for name in ("content" + ext, "meta.json"):
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                os.rmdir(aid, dir_fd=root)
                raise
            finally:
                os.close(fd)
        return meta

    def _metadata(self, fd, aid):
        meta = json.loads(self._read(fd, "meta.json", 4096))
        if (not isinstance(meta, dict) or meta.get("id") != aid or meta.get("kind") not in ("image", "text")
                or not isinstance(meta.get("size"), int) or not 0 <= meta["size"] <= MAX_BYTES
                or not isinstance(meta.get("name"), str) or safe_name(meta["name"]) != meta["name"]
                or meta.get("media_type") not in (IMAGE_TYPES if meta["kind"] == "image" else ("text/plain",))):
            raise ValueError("Invalid attachment metadata")
        return {k: meta[k] for k in ("id", "kind", "name", "size", "media_type")}

    def metadata(self, aid):
        with self._directory(aid) as fd:
            return self._metadata(fd, aid)

    def read(self, aid):
        with self._directory(aid) as fd:
            meta = self._metadata(fd, aid)
            ext = IMAGE_TYPES[meta["media_type"]] if meta["kind"] == "image" else ".txt"
            raw = self._read(fd, "content" + ext, MAX_BYTES)
            if len(raw) != meta["size"]:
                raise ValueError("Incomplete attachment")
            return meta, raw

    def discard(self, meta):
        """Rollback a copy created by a failed upload batch."""
        aid = meta["id"]
        with self._directory(aid) as fd:
            ext = IMAGE_TYPES[meta["media_type"]] if meta["kind"] == "image" else ".txt"
            os.unlink("content" + ext, dir_fd=fd)
            os.unlink("meta.json", dir_fd=fd)
        with self._directory() as fd:
            os.rmdir(aid, dir_fd=fd)

    def image_path(self, meta):
        return os.path.join(self.root, meta["id"], "content" + IMAGE_TYPES[meta["media_type"]])

    def image_id(self, path):
        # Recognize only our own generated path, never an arbitrary transcript path.
        if not isinstance(path, str) or not path.startswith(self.root + os.sep):
            return None
        parts = path[len(self.root) + 1:].split(os.sep)
        if len(parts) == 2 and ID_RE.fullmatch(parts[0]) and parts[1] in {"content" + e for e in IMAGE_TYPES.values()}:
            return parts[0]
        return None

    def copy_to(self, aid, home, folder, name=None):
        meta, raw = self.read(aid)
        if name is not None and (not isinstance(name, str) or not name or safe_name(name) != name):
            raise ValueError("Enter a file name without path separators")
        name = name or meta["name"]
        home, folder = os.path.realpath(home), os.path.realpath(folder)
        if os.path.commonpath((home, folder)) != home:
            raise ValueError("Choose a folder inside your home directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(home, flags)
        try:
            for part in os.path.relpath(folder, home).split(os.sep):
                if part == ".":
                    continue
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            stem, ext = os.path.splitext(name)
            for n in range(1, 1000):
                chosen = name if n == 1 else "%s-%d%s" % (stem, n, ext)
                try:
                    self._write(fd, chosen, raw)
                except FileExistsError:
                    continue
                path = os.path.join(folder, chosen)
                return {"path": path, "shown": "~" + path[len(home):]}
            raise ValueError("Too many files with this name")
        finally:
            os.close(fd)
