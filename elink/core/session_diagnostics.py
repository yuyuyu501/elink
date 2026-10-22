"""Persistent session failures with transport metadata, never input or media."""
from datetime import datetime, timezone
import hashlib
import logging
import secrets

from ..storage import atomic_json


log = logging.getLogger(__name__)


def channel_state(channel):
    return dict(state=getattr(channel, 'readyState', 'absent'),
                buffered_bytes=getattr(channel, 'bufferedAmount', 0))


class SessionDiagnostics:
    def __init__(self, root, role):
        self.root, self.role = root, role
        self.reset('')

    def reset(self, identifier):
        # The protocol session ID authorizes requests. Log only a one-way tag.
        self.tag = hashlib.sha256(identifier.encode()).hexdigest()[:12] if identifier else secrets.token_hex(6)
        self.path = self.root / 'logs' / f'session-{self.role}-{self.tag}.json'

    def record(self, reason, profile, state, exc=None):
        if exc is not None:
            profile.exception(reason, exc)
        profile.event(reason, role=self.role)
        log.warning('Session %s %s: %s state=%s', self.role, self.tag, reason, state,
                    exc_info=(type(exc), exc, exc.__traceback__) if exc else None)
        try:
            atomic_json(self.path, dict(schema=1, role=self.role, session_tag=self.tag,
                                       recorded_at=datetime.now(timezone.utc).isoformat(),
                                       reason=reason, transport=state, profile=profile.snapshot()))
            files = sorted(self.path.parent.glob(f'session-{self.role}-*.json'),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[20:]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception('Cannot persist session diagnostic: %s', self.path)
