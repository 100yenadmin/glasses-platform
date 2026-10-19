"""Device credentials, live sockets and the deliberately in-memory outbox."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import hmac
import re
import time
from uuid import uuid4


@dataclass(frozen=True)
class Identity:
    user: str
    device: str


def parse_devices(spec, single_token='') -> dict[str, Identity]:
    table = {}
    entries = str(spec or '').split(',') if str(spec or '').strip() else []
    if str(single_token or '').strip():
        entries.append('owner:default=' + str(single_token).strip())
    for index, entry in enumerate(entries, 1):
        label, sep, token = entry.strip().partition('=')
        user, colon, device = label.strip().partition(':')
        user, device, token = user.strip(), device.strip(), token.strip()
        if not (sep and colon and token and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', user)
                and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', device)):
            raise ValueError(f'Invalid device entry {index}')
        if token in table:
            raise ValueError(f'Duplicate credential at entry {index}')
        table[token] = Identity(user, device)
    return table


def authenticate(presented, table):
    if not isinstance(presented, str) or not presented:
        return None
    match = None
    for token, identity in table.items():
        if hmac.compare_digest(presented.encode(), token.encode()):
            match = identity
    return match


@dataclass(eq=False)
class Connection:
    ws: object
    identity: Identity
    protocol: int = 0
    capabilities: dict = field(default_factory=dict)
    connected_at: float = field(default_factory=time.monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_photo_at: float = 0.0
    flushed: bool = False  # offline queue delivered (after the protocol is known)
    last_status: dict = field(default_factory=dict)  # last status frame sent (repeats are dropped)
    pending: list = field(default_factory=list)  # pushes held until the protocol is known
    flush_task: object = None

    async def send_json(self, frame):
        async with self.send_lock:
            if self.ws.closed:
                return False
            try:
                await self.ws.send_json(frame)
                return True
            except (ConnectionError, RuntimeError, OSError):
                return False


class Registry:
    def __init__(self):
        self.by_user = {}

    async def add(self, conn):
        devices = self.by_user.setdefault(conn.identity.user, {})
        old = devices.get(conn.identity.device)
        devices[conn.identity.device] = conn
        if old is not None and old is not conn:
            await old.ws.close(code=4000, message=b'replaced')

    def remove(self, conn):
        devices = self.by_user.get(conn.identity.user, {})
        if devices.get(conn.identity.device) is conn:
            del devices[conn.identity.device]
        if not devices:
            self.by_user.pop(conn.identity.user, None)

    def live(self, user):
        return [c for c in self.by_user.get(user, {}).values() if not c.ws.closed]

    def pick_camera(self, user, prefer=None):
        cameras = [c for c in self.live(user)
                   if c.protocol == 0 or c.capabilities.get('camera') is True]
        if prefer in cameras:
            return prefer
        return max(cameras, key=lambda c: c.connected_at, default=None)


class Outbox:
    def __init__(self, maxlen=50, ttl_hours=24, clock=time.time):
        self.maxlen, self.ttl, self.clock = maxlen, ttl_hours * 3600, clock
        self._items = {}

    def _prune(self, user):
        items = self._items.get(user)
        if items is not None:
            cutoff = self.clock() - self.ttl
            while items and items[0][0] <= cutoff:
                items.popleft()

    def append(self, user, text, origin='message', message_id=None):
        self._prune(user)
        items = self._items.setdefault(user, deque(maxlen=self.maxlen))
        items.append((self.clock(), text, origin, message_id or uuid4().hex))

    def restore(self, user, items):
        """Put undelivered items back at the front, in order: they are older than anything queued since."""
        if items:
            self._items.setdefault(user, deque(maxlen=self.maxlen)).extendleft(reversed(items))

    def drain(self, user):
        self._prune(user)
        return list(self._items.pop(user, []))
