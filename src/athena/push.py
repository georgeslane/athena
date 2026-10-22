"""Notifications on your iPhone and Mac, through Web Push, for the dashboard installed as an app.

Your device's browser gives the dashboard an address at its push service (Apple's, for Safari) and a key. Athena
encrypts each notification to that key (RFC 8291), so the push service passes it on without being able to read it,
and signs its requests with a key of its own, kept in the data folder (VAPID, RFC 8292), so no one else can send
to your device. It only sends to the push services browsers use, never to an address anywhere else.

What a notification says is the start of the message, or, with [dashboard] notification_text = false, only that
there's something new.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from athena.config_files import write_private
from athena.memory import connect_db

log = logging.getLogger(__name__)

# The push services browsers use: Apple's, Google's (Chrome), Mozilla's (Firefox) and Microsoft's (Edge).
PUSH_SERVICES = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
RECORD_SIZE = 4096
TTL = 24 * 3600  # how long a push service keeps a notification for a device that's off
TOKEN_SECONDS = 12 * 3600  # how long each signed request's token lasts (at most 24 hours)
MAX_BODY = 180  # characters of a message in its notification


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF with SHA-256 (RFC 5869), for one block, which is all Web Push needs."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(
    payload: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    sender: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """``payload`` encrypted for a device (RFC 8291, aes128gcm): only the device, with its private key and
    ``auth`` secret, can read it. ``salt`` and ``sender`` are new each time, unless a test gives them."""
    device = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(p256dh))
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    sender_point, device_point = _point(sender.public_key()), _point(device)
    shared = sender.exchange(ec.ECDH(), device)
    ikm = _hkdf(unb64(auth), shared, b"WebPush: info\x00" + device_point + sender_point, 32)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    sealed = AESGCM(key).encrypt(nonce, payload + b"\x02", None)  # \x02: the last (and only) record
    return salt + struct.pack("!IB", RECORD_SIZE, len(sender_point)) + sender_point + sealed


def vapid_token(endpoint: str, key: ec.EllipticCurvePrivateKey, subject: str, now: float) -> str:
    """The Authorization header that says a request comes from Athena (RFC 8292)."""
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(now) + TOKEN_SECONDS, "sub": subject}
    header = {"typ": "JWT", "alg": "ES256"}
    signing = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims, separators=(',', ':')).encode())}"
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{signing}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={b64(_point(key.public_key()))}"


def check_subscription(sub: Any) -> tuple[str, str, str]:
    """A subscription, as the browser gives it, as (endpoint, p256dh, auth). ValueError, saying why, if it won't
    do: such as an address that isn't one of the push services."""
    try:
        endpoint, keys = sub["endpoint"], sub["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
        if not all(isinstance(v, str) for v in (endpoint, p256dh, auth)):
            raise TypeError
    except (KeyError, TypeError):
        raise ValueError(
            'Send the subscription as the browser gives it: {"endpoint", "keys": {"p256dh", "auth"}}.'
        ) from None
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if parts.scheme != "https" or not any(host == s or host.endswith(f".{s}") for s in PUSH_SERVICES):
        raise ValueError("Notifications can only go through the push services browsers use.")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(p256dh))
        if len(unb64(auth)) != 16:
            raise ValueError
    except ValueError:
        raise ValueError("The subscription's keys aren't right.") from None
    return endpoint, p256dh, auth


def plain(markdown: str, limit: int = MAX_BODY) -> str:
    """Markdown as plain text, short enough for a notification."""
    text = markdown.replace("**", "").replace("__", "").replace("`", "")
    text = " ".join(line.strip().lstrip("#>-* ").strip() for line in text.splitlines() if line.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Push:
    """Your devices' subscriptions, and sending to them."""

    def __init__(self, db_path: Path, key_path: Path, *, show_text: bool = True, http: httpx.AsyncClient | None = None):
        self.show_text = show_text
        self.http = http or httpx.AsyncClient(timeout=20)
        self.key = self._key(key_path)
        self.public_key = b64(_point(self.key.public_key()))
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS push_subscriptions (endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, "
                "auth TEXT NOT NULL, subject TEXT NOT NULL, added REAL NOT NULL)"
            )

    @staticmethod
    def _key(path: Path) -> ec.EllipticCurvePrivateKey:
        """Athena's signing key: made the first time, and kept, readable only by Athena's user."""
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(key, ec.EllipticCurvePrivateKey)
            return key
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, pem.decode(), 0o600)
        return key

    def subscribe(self, sub: Any, subject: str) -> None:
        """Send notifications to a device from now on. ``subject`` says where they come from, for the push service:
        the dashboard's address."""
        endpoint, p256dh, auth = check_subscription(sub)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO push_subscriptions VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
                "p256dh = excluded.p256dh, auth = excluded.auth, subject = excluded.subject",
                (endpoint, p256dh, auth, subject, time.time()),
            )

    def unsubscribe(self, endpoint: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).rowcount > 0

    def subscribed(self, endpoint: str | None = None) -> int:
        """How many devices get notifications, or 1 if ``endpoint``'s one of them."""
        with self._lock:
            if endpoint is not None:
                row = self._conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM push_subscriptions")
            return row.fetchone()[0]

    async def send(self, title: str, text: str, *, url: str = "/", tag: str = "", urgent: bool = False,
                   only: str | None = None) -> int:  # fmt: skip
        """Notify every device, or only the one at ``only``. Returns how many it reached. A device that's gone,
        as when you've removed the app, is forgotten."""
        body = plain(text) if self.show_text else "Something new. Open Athena to see it."
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}).encode()
        with self._lock:
            subs = self._conn.execute(
                "SELECT endpoint, p256dh, auth, subject FROM push_subscriptions"
                + (" WHERE endpoint = ?" if only else ""),
                (only,) if only else (),
            ).fetchall()
        reached = 0
        for endpoint, p256dh, auth, subject in subs:
            headers = {
                "Authorization": vapid_token(endpoint, self.key, subject, time.time()),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL),
                "Urgency": "high" if urgent else "normal",
            }
            if tag:
                headers["Topic"] = hashlib.sha256(tag.encode()).hexdigest()[:32]  # a newer one replaces it
            try:
                response = await self.http.post(endpoint, content=encrypt(payload, p256dh, auth), headers=headers)
            except httpx.HTTPError as exc:
                log.warning("Couldn't send a notification to %s: %s", urlsplit(endpoint).hostname, exc)
                continue
            if response.status_code in (404, 410):  # the device has unsubscribed
                self.unsubscribe(endpoint)
                log.info("A device stopped taking notifications, so it's been forgotten")
            elif response.status_code >= 400:
                log.warning("%s refused a notification: %d", urlsplit(endpoint).hostname, response.status_code)
            else:
                reached += 1
        return reached

    async def close(self) -> None:
        await self.http.aclose()
        self._conn.close()
