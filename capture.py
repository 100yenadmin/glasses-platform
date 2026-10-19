"""Visual heuristic (MIT hermes-glasses) and connection-scoped photo requests."""
import asyncio
import base64
import binascii
import re
from uuid import uuid4

from .protocol import capture_photo

VISUAL_KEYWORDS = [
    "look at", "looking at", "see this", "what am i seeing",
    "what is this", "what's this", "read this", "in front of me",
    "take a picture", "take a photo", "snap a photo", "use the camera",
    "through the camera",
]


def is_visual_query(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in VISUAL_KEYWORDS)


# "this drink", "that building", "the one on the left" - references to
# something the user is (probably) looking at
DEICTIC_RE = re.compile(r"\b(this|that|these|those)\s+([a-z]+)|\bthe one\b",
                        re.IGNORECASE)


# "this morning", "that question" - deictic in grammar but not about
# anything visible. Nouns here never trigger a photo.
DEICTIC_STOP_NOUNS = {
    "morning", "afternoon", "evening", "night", "time", "day", "week",
    "month", "year", "moment", "question", "answer", "idea", "point",
    "case", "way", "reason", "sense", "stuff", "conversation", "chat",
    "session", "app", "voice", "sound", "response", "reply", "much",
    "long", "short", "many", "far", "fast", "slow", "bit", "lot", "kind",
    "sort", "type",
    # Auxiliary/copular verbs: "this is a test", "that was fun" are not
    # about anything visible
    "is", "was", "are", "were", "be", "being", "been", "isn", "wasn",
    "will", "would", "can", "could", "should", "might", "may", "must",
    "does", "did", "done", "has", "had", "have", "gets", "got", "goes",
    "went", "seems", "means", "works", "sounds", "feels", "happened",
}


# Deictic follow-up captures only when no photo was taken within this many
# seconds - within the window, conversation memory already knows what the
# user is looking at.
PHOTO_MEMORY_WINDOW_S = 120.0


def should_capture_photo(text: str, last_photo_at: float, now: float) -> bool:
    """Decide whether a query needs a fresh photo.

    Explicit visual keywords always capture. Deictic references ("this X")
    capture only for plausibly-visible nouns, and only when no photo was
    taken recently - within the window, conversation memory already knows
    what the user is looking at.
    """
    if is_visual_query(text):
        return True
    for match in DEICTIC_RE.finditer(text):
        noun = (match.group(2) or "").lower()
        if match.group(0).lower() == "the one" or (
            noun and noun not in DEICTIC_STOP_NOUNS
        ):
            return (now - last_photo_at) > PHOTO_MEMORY_WINDOW_S
    return False



def decode_photo(data, max_bytes):
    if not isinstance(data, str) or len(data) > 4 * ((max_bytes + 2) // 3):
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None
    return decoded if 0 < len(decoded) <= max_bytes else None


class PhotoBroker:
    def __init__(self, max_frame_mb=8):
        self.max_bytes = int(max_frame_mb * 1024 * 1024)
        self._pending = {}

    async def request(self, conn, reason, timeout):
        request_id = uuid4().hex
        key = (id(conn), request_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            if not await conn.send_json(capture_photo(conn.protocol, request_id, reason)):
                return None
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(key, None)

    def resolve(self, conn, frame):
        if conn.protocol == 1:
            request = frame.get('request')
            if not isinstance(request, str):
                return
            key = (id(conn), request)
        else:
            key = next((key for key in self._pending if key[0] == id(conn)), None)
        future = self._pending.get(key)
        if future is not None and not future.done():
            data = decode_photo(frame.get('data'), self.max_bytes) if frame['type'] == 'photo' else None
            future.set_result(data)

    def cancel_all(self, conn):
        for key, future in list(self._pending.items()):
            if key[0] == id(conn) and not future.done():
                future.set_result(None)
