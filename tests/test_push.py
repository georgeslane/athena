"""Notifications through Web Push (push.py): encrypted for the device alone, signed by Athena, and only ever sent
to the push services browsers use."""

import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from athena.push import Push, _hkdf, _point, b64, check_subscription, encrypt, plain, unb64, vapid_token


def private_key(scalar_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64(scalar_b64), "big"), ec.SECP256R1())


def test_encrypting_as_rfc_8291s_example_does():
    """Appendix A of RFC 8291: the same keys, secret and salt make the same message."""
    sender = private_key("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    assert b64(_point(sender.public_key())) == (
        "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    body = encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        salt=unb64("DGv6ra1nlYgDCS1FRnbzlw"),
        sender=sender,
    )
    assert b64(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK"
        "6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


class Device:
    """A browser's side: its keys, and reading what's sent to it, as RFC 8291 says."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"sixteen byte key"

    @property
    def subscription(self):
        keys = {"p256dh": b64(_point(self.key.public_key())), "auth": b64(self.auth)}
        return {"endpoint": "https://web.push.apple.com/QGcd3x", "keys": keys}

    def read(self, body: bytes) -> dict:
        salt, sender_point = body[:16], body[21:86]
        assert int.from_bytes(body[16:20], "big") == 4096 and body[20] == 65
        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_point)
        shared = self.key.exchange(ec.ECDH(), sender)
        ikm = _hkdf(self.auth, shared, b"WebPush: info\x00" + _point(self.key.public_key()) + sender_point, 32)
        key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        padded = AESGCM(key).decrypt(nonce, body[86:], None)
        assert padded.endswith(b"\x02")
        return json.loads(padded[:-1])


def verify(authorization: str, endpoint_origin: str):
    """The signed token: who it's for, from whom, and that Athena's key signed it."""
    token, key = authorization.removeprefix("vapid t=").split(", k=")
    header, claims, signature = token.split(".")
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(key))
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    claims = json.loads(unb64(claims))
    assert claims["aud"] == endpoint_origin
    return claims, key


def test_a_signed_token_says_who_its_for_and_lasts_half_a_day():
    key = ec.generate_private_key(ec.SECP256R1())
    claims, sent_key = verify(
        vapid_token("https://web.push.apple.com/abc", key, "https://pi.example", 1000), "https://web.push.apple.com"
    )
    assert claims == {"aud": "https://web.push.apple.com", "exp": 1000 + 12 * 3600, "sub": "https://pi.example"}
    assert sent_key == b64(_point(key.public_key()))


def test_only_the_push_services_browsers_use():
    device = Device()
    assert check_subscription(device.subscription)[0] == "https://web.push.apple.com/QGcd3x"
    for endpoint in ("https://evil.example/push", "http://web.push.apple.com/x", "https://push.apple.com.evil.example/x",
                     "https://192.168.1.2/x"):  # fmt: skip
        with pytest.raises(ValueError, match="push services browsers use"):
            check_subscription({**device.subscription, "endpoint": endpoint})
    with pytest.raises(ValueError, match="keys aren't right"):
        check_subscription({"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "AAAA", "auth": "AAAA"}})
    with pytest.raises(ValueError, match="as the browser gives it"):
        check_subscription({"endpoint": "https://fcm.googleapis.com/x"})


async def test_sending_to_each_device_and_forgetting_one_thats_gone(tmp_path):
    phone, mac = Device(), Device()
    seen = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(410 if request.url.path == "/gone" else 201)

    push = Push(
        tmp_path / "a.db", tmp_path / "push.pem", http=httpx.AsyncClient(transport=httpx.MockTransport(service))
    )
    push.subscribe(phone.subscription, "https://pi.example")
    push.subscribe({**mac.subscription, "endpoint": "https://fcm.googleapis.com/gone"}, "https://pi.example")
    assert push.subscribed() == 2
    assert await push.send("Athena", "**Rain** at 4pm.\n\n- Take an umbrella", url="#chat/2", tag="reply-2") == 1
    to_phone = seen[0]
    assert to_phone.headers["content-encoding"] == "aes128gcm" and to_phone.headers["ttl"] == "86400"
    assert to_phone.headers["urgency"] == "normal" and len(to_phone.headers["topic"]) == 32
    verify(to_phone.headers["authorization"], "https://web.push.apple.com")
    said = {"title": "Athena", "body": "Rain at 4pm. Take an umbrella", "url": "#chat/2", "tag": "reply-2"}
    assert phone.read(to_phone.content) == said
    assert push.subscribed() == 1  # the Mac's subscription had gone

    # The same key after a restart, kept where only Athena's user can read it.
    again = Push(tmp_path / "a.db", tmp_path / "push.pem")
    assert again.public_key == push.public_key and oct((tmp_path / "push.pem").stat().st_mode)[-3:] == "600"
    push.show_text = False
    await push.send("Athena", "Your locker is number 42.")
    assert "42" not in json.dumps(phone.read(seen[-1].content))
    assert push.unsubscribe(phone.subscription["endpoint"]) and push.subscribed() == 0
    await push.close()
    await again.close()


def test_a_message_made_plain_and_short():
    assert (
        plain("# Briefing\n\n**Dentist** at `14:00`.\n> Bring the form") == "Briefing Dentist at 14:00. Bring the form"
    )
    assert len(plain("word " * 100)) == 180 and plain("word " * 100).endswith("…")
