"""
tests/test_agecrypt_js.py — run the browser crypto's tests from pytest

The restore page's age implementation (app/static/js/agecrypt.js) is
JavaScript; its own tests are tests/js/*.test.mjs under Node's test
runner. This runs them as part of the normal suite (skips without Node),
and adds a check Node cannot do alone: the hand-written ChaCha20-Poly1305
must agree with Python's independent one (`cryptography`) on random keys,
nonces, associated data and lengths around the 16- and 64-byte edges.

Also verified once by hand on 2026-10-13: files this JavaScript encrypted
were opened byte-identical by the real age 1.2.1 on the bench controller
(0, 37, 65536 and 200001 bytes).
"""
import json
import os
import shutil
import subprocess
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="Node.js not installed")


@needs_node
def test_node_suite_passes():
    r = subprocess.run([NODE, "--test", str(ROOT / "tests" / "js" / "agecrypt.test.mjs")],
                       capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@needs_node
def test_aead_agrees_with_python_cryptography():
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    cases = []
    for n in (0, 1, 15, 16, 17, 63, 64, 65, 200, 1000):
        cases.append({"key": os.urandom(32).hex(), "nonce": os.urandom(12).hex(),
                      "aad": os.urandom(n % 23).hex(), "pt": os.urandom(n).hex()})
    script = """
      const A = await import(process.argv[1]);
      const h = (s) => Uint8Array.from(Buffer.from(s, 'hex'));
      const cases = JSON.parse(process.argv[2]);
      console.log(JSON.stringify(cases.map(c => Buffer.from(A.aeadSeal(h(c.key), h(c.nonce), h(c.pt), h(c.aad))).toString('hex'))));
    """
    mod = (ROOT / "app" / "static" / "js" / "agecrypt.js").as_uri()
    r = subprocess.run([NODE, "--input-type=module", "-e", script, mod, json.dumps(cases)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for c, js in zip(cases, got):
        want = ChaCha20Poly1305(bytes.fromhex(c["key"])).encrypt(bytes.fromhex(c["nonce"]), bytes.fromhex(c["pt"]),
                                                                bytes.fromhex(c["aad"]))
        assert js == want.hex(), f"mismatch at length {len(c['pt']) // 2}"
