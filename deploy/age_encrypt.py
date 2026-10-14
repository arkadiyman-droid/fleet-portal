#!/usr/bin/env python3
"""
deploy/age_encrypt.py — encrypt to an age X25519 recipient, in Python.

For the off-site copy of the portal's nightly backup (deploy/backup-db.sh):
it is encrypted to the fleet's master PUBLIC key before it leaves beverly-pi,
so only someone holding the master private key (in the vault) can read it.
The host has no `age` binary and installing one needs root; this needs only
the `cryptography` package the portal already uses. Output is a standard age
v1 file: open it with the real tool,  age -d -i master.key file.age.

    age_encrypt.py -r age1...  < plain  > plain.age

The format (https://age-encryption.org/v1):
  header   "age-encryption.org/v1\\n"
           "-> X25519 <ephemeral public key, b64>\\n<wrapped file key, b64>\\n"
           wrap key  = HKDF-SHA256(X25519(eph, recipient), salt = eph_pub || recipient,
                                   info "age-encryption.org/v1/X25519")
           wrapped   = ChaCha20-Poly1305(wrap key, zero nonce).encrypt(16-byte file key)
  MAC      "--- " + b64(HMAC-SHA256(HKDF(file key, info "header"), header up to "---"))
  payload  16-byte nonce, then STREAM: 64 KiB chunks, each sealed with
           ChaCha20-Poly1305(HKDF(file key, salt = nonce, info "payload"))
           under nonce = 11-byte big-endian counter || last-chunk flag.
b64 is standard base64 without padding. Checked against age 1.2.1 (tests/test_age_encrypt.py
and by hand on a controller).
"""
import argparse
import base64
import hashlib
import hmac
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CHUNK = 64 * 1024
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class AgeError(ValueError):
    pass


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def recipient_key(recipient: str) -> bytes:
    """The 32-byte X25519 public key in an age1... recipient (bech32, checked)."""
    s = recipient.strip().lower()
    if not s.startswith("age1"):
        raise AgeError("not an age X25519 recipient (age1...)")
    hrp, data = "age", s[4:]
    if any(c not in _CHARSET for c in data) or len(data) < 7:
        raise AgeError("malformed age recipient")
    vals = [_CHARSET.index(c) for c in data]
    if _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + vals) != 1:
        raise AgeError("age recipient checksum does not match (typo?)")
    acc, bits, out = 0, 0, []
    for v in vals[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or acc & ((1 << bits) - 1):          # leftover bits must be < 5 and zero
        raise AgeError("malformed age recipient padding")
    if len(out) != 32:
        raise AgeError("age recipient is not 32 bytes")
    return bytes(out)


def _b64(b: bytes) -> bytes:
    return base64.b64encode(b).rstrip(b"=")


def _hkdf(ikm: bytes, salt: bytes, info: bytes, n: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt or None, info=info).derive(ikm)


def encrypt(plain: bytes, recipient: str) -> bytes:
    pk = recipient_key(recipient)
    file_key = os.urandom(16)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(pk))
    if shared == bytes(32):
        raise AgeError("recipient key is a low-order point")
    wrap = _hkdf(shared, eph_pub + pk, b"age-encryption.org/v1/X25519")
    body = ChaCha20Poly1305(wrap).encrypt(bytes(12), file_key, None)
    header = b"age-encryption.org/v1\n-> X25519 " + _b64(eph_pub) + b"\n" + _b64(body) + b"\n---"
    mac = hmac.new(_hkdf(file_key, b"", b"header"), header, hashlib.sha256).digest()
    out = [header + b" " + _b64(mac) + b"\n"]

    nonce = os.urandom(16)
    aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    out.append(nonce)
    chunks = [plain[i:i + CHUNK] for i in range(0, len(plain), CHUNK)] or [b""]
    for i, c in enumerate(chunks):
        last = i == len(chunks) - 1
        out.append(aead.encrypt(i.to_bytes(11, "big") + (b"\x01" if last else b"\x00"), c, None))
    return b"".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Encrypt stdin to an age X25519 recipient, to stdout.")
    ap.add_argument("-r", "--recipient", required=True, help="age1... public key")
    a = ap.parse_args(argv)
    try:
        sys.stdout.buffer.write(encrypt(sys.stdin.buffer.read(), a.recipient))
    except AgeError as e:
        print(f"age_encrypt: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
