"""Server-only object storage for source snapshots and immutable report artifacts."""

import gzip
import hashlib
import json
import os
from pathlib import Path


def encode(value: dict) -> tuple[bytes, str]:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(data, mtime=0), hashlib.sha256(data).hexdigest()


class LocalObjects:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def put(self, key: str, value: dict) -> str:
        data, sha = encode(value)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return sha

    def get(self, key: str, sha: str) -> dict:
        data = gzip.decompress(self._path(key).read_bytes())
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Object checksum mismatch")
        return json.loads(data)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("Invalid object key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid object key")
        return path


class OssObjects:
    def __init__(self, settings):
        import oss2

        access = settings.oss_access_key_id.get_secret_value()
        secret = settings.oss_access_key_secret.get_secret_value()
        token = settings.oss_security_token.get_secret_value()
        auth = oss2.StsAuth(access, secret, token) if token else oss2.Auth(access, secret)
        self.bucket = oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)
        self.prefix = settings.oss_prefix

    def _key(self, key: str) -> str:
        if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("Invalid object key")
        return self.prefix + key

    def put(self, key: str, value: dict) -> str:
        data, sha = encode(value)
        self.bucket.put_object(self._key(key), data, headers={
            "x-oss-server-side-encryption": "AES256",
            "Content-Type": "application/gzip",
        })
        return sha

    def get(self, key: str, sha: str) -> dict:
        data = gzip.decompress(self.bucket.get_object(self._key(key)).read())
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Object checksum mismatch")
        return json.loads(data)

    def delete(self, key: str) -> None:
        self.bucket.delete_object(self._key(key))


def create_objects(settings):
    return OssObjects(settings) if settings.storage_backend == "oss" else LocalObjects(
        settings.object_root
    )
