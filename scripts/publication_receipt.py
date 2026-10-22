"""Durable create receipts and an exclusive per-account/title publication lock."""
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class ReceiptError(Exception):
    pass


class Receipt:
    def __init__(self, root, profile, title):
        key = hashlib.sha256(json.dumps([profile, title], ensure_ascii=False).encode()).hexdigest()
        self.path = Path(root) / '.publish-receipts' / (key + '.json')
        self.identity = {'profile': profile, 'title': title}

    def read(self):
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or any(value.get(k) != v for k, v in self.identity.items()):
                raise ValueError('invalid receipt identity')
            return value
        except (OSError, ValueError) as error:
            raise ReceiptError('cannot read publication receipt') from error

    def save(self, value):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             delete=False) as handle:
                temporary = handle.name
                json.dump(dict(value, **self.identity), handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError) as error:
            raise ReceiptError('cannot persist publication receipt') from error
        finally:
            if temporary is not None:
                os.unlink(temporary)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(str(self.path) + '.lock', 'a') as handle:
            os.chmod(handle.name, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ReceiptError('publication already in progress') from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
