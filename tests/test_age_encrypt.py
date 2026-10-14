"""
tests/test_age_encrypt.py — the Python age encryptor used for off-site backups

deploy/age_encrypt.py is checked two ways here: a small decryptor written
from the spec is first proven against files made by the REAL age tool
(tests/fixtures/age, age 1.2.1), and then must open what the encryptor
writes, for empty, small, exactly-one-chunk and multi-chunk inputs. The real
tool opening our output is checked by hand on a controller (see the module).
"""
import base64
import hashlib
import hmac
import importlib.util
import os
import pathlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers import aead
from cryptography.hazmat.primitives.kdf import hkdf

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "age"
_spec = importlib.util.spec_from_file_location("age_encrypt", ROOT / "deploy" / "age_encrypt.py")
AE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AE)


def gen(n):
    return bytes((i * 7 + 3) % 251 for i in range(n))


def _bech32_secret(text):
    line = next(l for l in text.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
    data = line[len("AGE-SECRET-KEY-1"):].lower()
    vals = [AE._CHARSET.index(c) for c in data][:-6]
    acc = bits = 0
    out = []
    for v in vals:
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def _unb64(b):
    return base64.b64decode(b + b"=" * (-len(b) % 4))


def _hk(ikm, salt, info):
    return hkdf.HKDF(algorithm=hashes.SHA256(), length=32, salt=salt or None, info=info).derive(ikm)


def spec_decrypt(data, secret):
    """age v1 X25519 decryption, straight from the spec, for testing only."""
    sk = x25519.X25519PrivateKey.from_private_bytes(secret)
    pk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    end = data.index(b"\n---") + 4
    lines = data[:end].split(b"\n")
    assert lines[0] == b"age-encryption.org/v1"
    file_key = None
    i = 1
    while not lines[i].startswith(b"---"):
        args = lines[i][3:].split(b" ")
        body = lines[i + 1]
        i += 2
        if args[0] != b"X25519" or file_key:
            continue
        eph = _unb64(args[1])
        shared = sk.exchange(x25519.X25519PublicKey.from_public_bytes(eph))
        try:
            file_key = aead.ChaCha20Poly1305(_hk(shared, eph + pk, b"age-encryption.org/v1/X25519")).decrypt(
                bytes(12), _unb64(body), None)
        except Exception:
            pass
    assert file_key, "no stanza for this key"
    mac_line_end = data.index(b"\n", end)
    mac = _unb64(data[end + 1:mac_line_end])
    assert hmac.compare_digest(mac, hmac.new(_hk(file_key, b"", b"header"), data[:end], hashlib.sha256).digest())
    p = mac_line_end + 1
    nonce, p = data[p:p + 16], p + 16
    a = aead.ChaCha20Poly1305(_hk(file_key, nonce, b"payload"))
    out, n = [], 0
    while True:
        chunk = data[p:p + 65536 + 16]
        p += len(chunk)
        last = p >= len(data)
        out.append(a.decrypt(n.to_bytes(11, "big") + (b"\x01" if last else b"\x00"), chunk, None))
        n += 1
        if last:
            return b"".join(out)


SECRET = _bech32_secret((FIX / "test.key").read_text())
PUBLIC = next(l.split(": ")[1].strip() for l in (FIX / "test.key").read_text().splitlines() if "public key" in l)


@pytest.mark.parametrize("name,n", [("empty.age", 0), ("small.age", 37), ("onechunk.age", 65536), ("multi.age", 200001)])
def test_the_spec_decryptor_opens_files_made_by_real_age(name, n):
    assert spec_decrypt((FIX / name).read_bytes(), SECRET) == gen(n)


@pytest.mark.parametrize("n", [0, 1, 37, 65535, 65536, 65537, 200001])
def test_what_we_encrypt_opens(n):
    ct = AE.encrypt(gen(n), PUBLIC)
    assert ct.startswith(b"age-encryption.org/v1\n-> X25519 ")
    assert spec_decrypt(ct, SECRET) == gen(n)


def test_a_flipped_bit_is_caught():
    ct = bytearray(AE.encrypt(gen(1000), PUBLIC))
    ct[-20] ^= 1
    with pytest.raises(Exception):
        spec_decrypt(bytes(ct), SECRET)


def test_each_file_is_different():
    assert AE.encrypt(b"same", PUBLIC) != AE.encrypt(b"same", PUBLIC)


def test_recipient_typos_are_refused():
    assert len(AE.recipient_key(PUBLIC)) == 32
    typo = PUBLIC[:-1] + ("q" if PUBLIC[-1] != "q" else "p")
    for bad in (typo, "age1abc", "ssh-ed25519 AAAA", PUBLIC.replace("age1", "agf1")):
        with pytest.raises(AE.AgeError):
            AE.recipient_key(bad)


def test_command_line(tmp_path, monkeypatch, capsysbinary):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(gen(5000))})())
    assert AE.main(["-r", PUBLIC]) == 0
    assert spec_decrypt(capsysbinary.readouterr().out, SECRET) == gen(5000)
