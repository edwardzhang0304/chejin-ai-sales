"""Revocable authorization for one elevated helper, including a delayed UAC.

The helper holds the same Windows byte-range lock across all clock changes.
Recovery revokes the ticket under that lock before reopening runtime services.
The file contains data only; privileged code never executes a writable script.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import uuid


def valid_operation(value):
    try:
        return (isinstance(value, str) and value.startswith('clock-sync-')
                and str(uuid.UUID(value[11:])) == value[11:])
    except ValueError:
        return False


class ClockSyncTicket:
    def __init__(self, root):
        self.path = Path(root) / 'runtime' / 'clock-sync.ticket'

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError('CLOCK_TICKET_LINK_REJECTED')
        with self.path.open('a+b', buffering=0) as stream:
            if not stream.seek(0, os.SEEK_END):
                stream.write(b' ')
            stream.seek(0)
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BlockingIOError('CLOCK_HELPER_STILL_ACTIVE') from exc
            try:
                yield stream
            finally:
                stream.seek(0)
                if sys.platform == 'win32':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def authorize(self, operation):
        if not valid_operation(operation):
            raise ValueError('CLOCK_OPERATION_INVALID')
        with self.locked() as stream:
            # Never replace the inode carrying the lock.
            self._write(stream, operation.encode('ascii'))

    def revoke(self):
        with self.locked() as stream:
            self._write(stream, b'revoked')

    @staticmethod
    def _write(stream, value):
        # a+b appends regardless of seek; truncate under exclusive ownership.
        stream.truncate(0)
        stream.write(value)
        os.fsync(stream.fileno())
