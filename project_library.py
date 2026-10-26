"""Session metadata catalog and user-defined project collections."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading


class SessionCatalog:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, paths, peek, thread_id, names, home):
        with self.lock:
            fresh, rows = {}, {}
            home = os.path.realpath(home)
            for path in paths:
                try:
                    info = os.stat(path)
                    stamp = (info.st_mtime_ns, info.st_size)
                    cached = self.cache.get(path)
                    if cached and cached[0] == stamp:
                        record = cached[1]
                    else:
                        cwd, _, title, child = peek(path)
                        cwd = cwd if isinstance(cwd, str) else ""
                        title = title if isinstance(title, str) else ""
                        cc = thread_id(path)
                        record = None if child or not valid_thread(cc) else {
                            "cc": cc, "cwd": os.path.realpath(os.path.expanduser(cwd)) if cwd else "",
                            "title": (title or cc)[:180], "mtime": info.st_mtime}
                    fresh[path] = (stamp, record)
                    if record and (record["cc"] not in rows or record["mtime"] > rows[record["cc"]]["mtime"]):
                        rows[record["cc"]] = record
                except OSError:
                    continue
            self.cache = fresh
            result, available = [], {}
            for record in rows.values():
                cwd = record["cwd"]
                if cwd not in available:
                    available[cwd] = bool(cwd and os.path.isdir(cwd) and (cwd == home or cwd.startswith(home + os.sep)))
                result.append({**record, "title": names.get(record["cc"]) or record["title"],
                               "available": available[cwd]})
            return sorted(result, key=lambda row: (-row["mtime"], row["cc"]))


def valid_thread(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{6,64}", value))


class ProjectGroups:
    MAX_BYTES = 2 * 1024 * 1024
    MAX_GROUPS = 100
    MAX_MEMBERS = 20000

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def name(value):
        if not isinstance(value, str):
            raise ValueError("Enter a project name")
        value = value.strip()
        if not value or len(value) > 120 or any(ord(c) < 32 for c in value):
            raise ValueError("Use a project name between 1 and 120 characters")
        return value

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_BYTES:
                raise ValueError("Project groups file is invalid")
            data = json.load(source)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("groups"), list):
            raise ValueError("Project groups file is invalid")
        groups = data["groups"]
        if len(groups) > self.MAX_GROUPS:
            raise ValueError("Too many project groups")
        ids = set()
        for group in groups:
            if not isinstance(group, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(group.get("id", ""))) or group["id"] in ids:
                raise ValueError("Project groups file is invalid")
            ids.add(group["id"])
            self.name(group.get("name"))
            for key in ("folders", "sessions", "excluded"):
                values = group.get(key)
                if not isinstance(values, list) or len(values) > self.MAX_MEMBERS:
                    raise ValueError("Project groups file is invalid")
                for value in values:
                    valid = (isinstance(value, str) and os.path.isabs(value) and len(value) <= 4096
                             and not any(ord(c) < 32 for c in value)) if key == "folders" else valid_thread(value)
                    if not valid:
                        raise ValueError("Project groups file is invalid")
        return groups

    def update(self, body, rows, folders):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            groups = self.read()
            op, gid = body.get("op"), body.get("id")
            group = next((g for g in groups if g["id"] == gid), None)
            if op in ("create", "rename"):
                name = self.name(body.get("name"))
                if any(g["name"].casefold() == name.casefold() and (op == "create" or g["id"] != gid) for g in groups):
                    raise ValueError("A project with that name already exists")
            if op == "create":
                if len(groups) >= self.MAX_GROUPS:
                    raise ValueError("Project limit reached")
                gid = secrets.token_hex(16)
                groups.append({"id": gid, "name": name, "folders": [], "sessions": [], "excluded": []})
            elif group is None:
                raise ValueError("Project no longer exists")
            elif op == "rename":
                group["name"] = name
            elif op == "delete":
                groups.remove(group)
            elif op in ("add", "remove"):
                kind, value = body.get("kind"), body.get("value")
                by_cc = {row["cc"]: row for row in rows}
                if kind == "folder":
                    if not isinstance(value, str) or not value or (value not in folders and not (op == "remove" and value in group["folders"])):
                        raise ValueError("Unknown folder")
                    members = {row["cc"] for row in rows if row["cwd"] == value}
                    group["folders"] = [v for v in group["folders"] if v != value]
                    if op == "add":
                        group["folders"].append(value)
                    else:
                        group["sessions"] = [v for v in group["sessions"] if v not in members]
                    group["excluded"] = [v for v in group["excluded"] if v not in members]
                elif kind == "session":
                    if not valid_thread(value) or value not in by_cc:
                        raise ValueError("Unknown session")
                    group["sessions"] = [v for v in group["sessions"] if v != value]
                    group["excluded"] = [v for v in group["excluded"] if v != value]
                    if op == "add":
                        group["sessions"].append(value)
                    elif by_cc[value]["cwd"] in group["folders"]:
                        group["excluded"].append(value)
                else:
                    raise ValueError("Choose a folder or session")
            else:
                raise ValueError("Unknown project operation")
            if any(len(g[key]) > self.MAX_MEMBERS for g in groups for key in ("folders", "sessions", "excluded")):
                raise ValueError("Project membership limit reached")
            payload = json.dumps({"version": 1, "groups": groups}, ensure_ascii=False).encode("utf-8")
            if len(payload) > self.MAX_BYTES:
                raise ValueError("Project groups file is too large")
            fd, temporary = tempfile.mkstemp(prefix=".project-groups-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(payload); target.flush(); os.fsync(target.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {"id": gid, "groups": groups}
