// app/static/js/agecrypt.js — age v1 (X25519) decrypt + encrypt, in the browser
// =============================================================================
// The restore-onto-a-module page decrypts a site bundle with the operator's
// MASTER key and re-encrypts it to the new module's own key, all inside the
// operator's browser: the master key and the plaintext never reach the
// portal (FLEET_RESILIENCE_PLAN.md; decided 2026-10-13).
//
// Deliberately no third-party code in a page that handles the master key.
// The browser's own WebCrypto does X25519, HKDF-SHA-256 and HMAC-SHA-256;
// ChaCha20-Poly1305 (which WebCrypto lacks) is implemented here from
// RFC 8439 and checked against its published test vectors. The age format
// matches EMS tools/age_decrypt.py and is checked against files made by the
// real age 1.2.1 tool, and the real tool decrypts what this produces
// (tests/js/agecrypt.test.mjs, tests/test_agecrypt_js.py).
//
// Works unchanged in Node >= 20 (same WebCrypto), which is how it is tested.

const subtle = globalThis.crypto.subtle;
const enc = new TextEncoder();
const INTRO = "age-encryption.org/v1\n";
export const CHUNK = 64 * 1024;
const TAG = 16;

export class AgeError extends Error {}

// ── bytes ──────────────────────────────────────────────────────────────────
const concat = (...arrs) => {
  const out = new Uint8Array(arrs.reduce((n, a) => n + a.length, 0));
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
};
const u32le = (b, i) => (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;
const le64 = (n) => { const b = new Uint8Array(8); let v = BigInt(n); for (let i = 0; i < 8; i++) { b[i] = Number(v & 0xffn); v >>= 8n; } return b; };

export function b64encode(bytes) {             // standard alphabet, no padding (age)
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/=+$/, "");
}
export function b64decode(str) {
  if (/[=\r\s]/.test(str)) throw new AgeError("non-canonical base64 in header");
  const s = atob(str + "=".repeat((4 - (str.length % 4)) % 4));
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// ── ChaCha20 (RFC 8439 §2.3–2.4) ───────────────────────────────────────────
function chachaBlock(key, counter, nonce) {
  const s = new Uint32Array(16);
  s[0] = 0x61707865; s[1] = 0x3320646e; s[2] = 0x79622d32; s[3] = 0x6b206574;
  for (let i = 0; i < 8; i++) s[4 + i] = u32le(key, 4 * i);
  s[12] = counter >>> 0;
  for (let i = 0; i < 3; i++) s[13 + i] = u32le(nonce, 4 * i);
  const x = s.slice();
  const rotl = (v, c) => ((v << c) | (v >>> (32 - c))) >>> 0;
  const qr = (a, b, c, d) => {
    x[a] = (x[a] + x[b]) >>> 0; x[d] = rotl(x[d] ^ x[a], 16);
    x[c] = (x[c] + x[d]) >>> 0; x[b] = rotl(x[b] ^ x[c], 12);
    x[a] = (x[a] + x[b]) >>> 0; x[d] = rotl(x[d] ^ x[a], 8);
    x[c] = (x[c] + x[d]) >>> 0; x[b] = rotl(x[b] ^ x[c], 7);
  };
  for (let i = 0; i < 10; i++) {
    qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15);
    qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14);
  }
  const out = new Uint8Array(64);
  for (let i = 0; i < 16; i++) {
    const v = (x[i] + s[i]) >>> 0;
    out[4 * i] = v & 0xff; out[4 * i + 1] = (v >>> 8) & 0xff; out[4 * i + 2] = (v >>> 16) & 0xff; out[4 * i + 3] = v >>> 24;
  }
  return out;
}

export function chacha20(key, counter, nonce, data) {
  const out = new Uint8Array(data.length);
  for (let off = 0, ctr = counter; off < data.length; off += 64, ctr++) {
    const ks = chachaBlock(key, ctr, nonce);
    const n = Math.min(64, data.length - off);
    for (let i = 0; i < n; i++) out[off + i] = data[off + i] ^ ks[i];
  }
  return out;
}

// ── Poly1305 (RFC 8439 §2.5) ───────────────────────────────────────────────
const P1305 = (1n << 130n) - 5n;
const leBig = (b) => { let v = 0n; for (let i = b.length - 1; i >= 0; i--) v = (v << 8n) | BigInt(b[i]); return v; };
export function poly1305(key, msg) {
  const r = leBig(key.subarray(0, 16)) & 0x0ffffffc0ffffffc0ffffffc0fffffffn;
  const s = leBig(key.subarray(16, 32));
  let acc = 0n;
  for (let i = 0; i < msg.length; i += 16) {
    const block = msg.subarray(i, Math.min(i + 16, msg.length));
    const n = leBig(block) + (1n << BigInt(8 * block.length));
    acc = ((acc + n) * r) % P1305;
  }
  acc = (acc + s) & ((1n << 128n) - 1n);
  const tag = new Uint8Array(16);
  for (let i = 0; i < 16; i++) { tag[i] = Number(acc & 0xffn); acc >>= 8n; }
  return tag;
}

// ── AEAD_CHACHA20_POLY1305 (RFC 8439 §2.8) ────────────────────────────────
const pad16 = (n) => new Uint8Array((16 - (n % 16)) % 16);
function macData(aad, ct) { return concat(aad, pad16(aad.length), ct, pad16(ct.length), le64(aad.length), le64(ct.length)); }
function tagEqual(a, b) { if (a.length !== b.length) return false; let d = 0; for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i]; return d === 0; }

export function aeadSeal(key, nonce, plaintext, aad = new Uint8Array(0)) {
  const otk = chachaBlock(key, 0, nonce).subarray(0, 32);
  const ct = chacha20(key, 1, nonce, plaintext);
  return concat(ct, poly1305(otk, macData(aad, ct)));
}
export function aeadOpen(key, nonce, sealed, aad = new Uint8Array(0)) {
  if (sealed.length < TAG) return null;
  const ct = sealed.subarray(0, sealed.length - TAG), tag = sealed.subarray(sealed.length - TAG);
  const otk = chachaBlock(key, 0, nonce).subarray(0, 32);
  if (!tagEqual(poly1305(otk, macData(aad, ct)), tag)) return null;
  return chacha20(key, 1, nonce, ct);
}

// ── bech32 (BIP-173), for AGE-SECRET-KEY-1… and age1… ─────────────────────
const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function polymod(values) {
  const G = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  let chk = 1;
  for (const v of values) {
    const b = chk >>> 25;
    chk = (((chk & 0x1ffffff) << 5) ^ v) >>> 0;
    for (let i = 0; i < 5; i++) if ((b >>> i) & 1) chk = (chk ^ G[i]) >>> 0;
  }
  return chk;
}
export function bech32Decode(str) {
  const s0 = str.trim();
  if (s0 !== s0.toLowerCase() && s0 !== s0.toUpperCase()) throw new AgeError("mixed-case key");
  const s = s0.toLowerCase();
  const pos = s.lastIndexOf("1");
  if (pos < 1 || pos + 7 > s.length) throw new AgeError("malformed key");
  const hrp = s.slice(0, pos), vals = [];
  for (const c of s.slice(pos + 1)) { const v = CHARSET.indexOf(c); if (v < 0) throw new AgeError("invalid character in key"); vals.push(v); }
  const hv = [...hrp].map((c) => c.charCodeAt(0) >> 5).concat([0], [...hrp].map((c) => c.charCodeAt(0) & 31));
  if (polymod(hv.concat(vals)) !== 1) throw new AgeError("key checksum does not match (typo?)");
  let acc = 0, bits = 0; const out = [];
  for (const v of vals.slice(0, -6)) { acc = (acc << 5) | v; bits += 5; while (bits >= 8) { bits -= 8; out.push((acc >> bits) & 0xff); } }
  if (bits >= 5 || (acc & ((1 << bits) - 1))) throw new AgeError("key padding error");
  return { hrp, data: new Uint8Array(out) };
}

// ── WebCrypto pieces ────────────────────────────────────────────────────────
async function hkdf(ikm, salt, info, n = 32) {
  const k = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info: enc.encode(info) }, k, 8 * n));
}
async function hmac(key, data) {
  const k = await subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await subtle.sign("HMAC", k, data));
}
const PKCS8_X25519 = new Uint8Array([0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x04, 0x22, 0x04, 0x20]);
const b64url = (s) => b64decode(s.replace(/-/g, "+").replace(/_/g, "/"));

export async function loadIdentities(text) {
  const ids = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const { hrp, data } = bech32Decode(line);
    if (hrp !== "age-secret-key-" || data.length !== 32) throw new AgeError("not an age secret key (AGE-SECRET-KEY-1…)");
    const priv = await subtle.importKey("pkcs8", concat(PKCS8_X25519, data), { name: "X25519" }, true, ["deriveBits"]);
    const jwk = await subtle.exportKey("jwk", priv);
    ids.push({ priv, pub: b64url(jwk.x) });
    data.fill(0);
  }
  if (!ids.length) throw new AgeError("no AGE-SECRET-KEY found in the key file");
  return ids;
}

export function parseRecipient(str) {
  const { hrp, data } = bech32Decode(str);
  if (hrp !== "age" || data.length !== 32) throw new AgeError("not an age public key (age1…)");
  return data;
}

async function x25519(priv, pubBytes) {
  const pub = await subtle.importKey("raw", pubBytes, { name: "X25519" }, false, []);
  return new Uint8Array(await subtle.deriveBits({ name: "X25519", public: pub }, priv, 256));
}

// ── age header ──────────────────────────────────────────────────────────────
function parseHeader(data) {
  const intro = enc.encode(INTRO);
  for (let i = 0; i < intro.length; i++) if (data[i] !== intro[i]) throw new AgeError("not an age v1 file");
  const dec = new TextDecoder();
  let pos = intro.length; const stanzas = [];
  const nextLine = () => { const nl = data.indexOf(10, pos); if (nl < 0) throw new AgeError("truncated header"); const l = dec.decode(data.subarray(pos, nl)); const start = pos; pos = nl + 1; return [l, start]; };
  for (;;) {
    const [line, start] = nextLine();
    if (line.startsWith("--- ")) return { stanzas, macInput: data.subarray(0, start + 3), mac: b64decode(line.slice(4)), offset: pos };
    if (!line.startsWith("-> ")) throw new AgeError("malformed header line");
    const args = line.slice(3).split(" ");
    let body = "";
    for (;;) { const [chunk] = nextLine(); if (chunk.length > 64) throw new AgeError("stanza line too long"); body += chunk; if (chunk.length < 64) break; }
    stanzas.push({ args, body: b64decode(body) });
  }
}

// ── decrypt ─────────────────────────────────────────────────────────────────
export async function decrypt(data, identities) {
  const { stanzas, macInput, mac, offset } = parseHeader(data);
  let fileKey = null;
  for (const st of stanzas) {
    if (st.args[0] !== "X25519" || st.args.length !== 2) continue;
    const share = b64decode(st.args[1]);
    if (share.length !== 32 || st.body.length !== 32) throw new AgeError("malformed X25519 stanza");
    for (const id of identities) {
      const shared = await x25519(id.priv, share);
      if (shared.every((b) => b === 0)) throw new AgeError("X25519 low-order point");
      const wrap = await hkdf(shared, concat(share, id.pub), "age-encryption.org/v1/X25519");
      fileKey = aeadOpen(wrap, new Uint8Array(12), st.body);
      if (fileKey) break;
    }
    if (fileKey) break;
  }
  if (!fileKey) throw new AgeError("this file was encrypted to a different key");
  if (fileKey.length !== 16) throw new AgeError("bad file key");
  const expect = await hmac(await hkdf(fileKey, new Uint8Array(0), "header"), macInput);
  if (!tagEqual(expect, mac)) throw new AgeError("header MAC mismatch: damaged or tampered with");

  const nonce = data.subarray(offset, offset + 16);
  if (nonce.length !== 16) throw new AgeError("truncated payload");
  const key = await hkdf(fileKey, nonce, "payload");
  const payload = data.subarray(offset + 16);
  const parts = []; let pos = 0, counter = 0;
  for (;;) {
    const chunk = payload.subarray(pos, pos + CHUNK + TAG);
    pos += chunk.length;
    const last = pos >= payload.length;
    if (chunk.length < TAG) throw new AgeError("truncated payload chunk");
    const n = concat(new Uint8Array(4), le64Be(counter), new Uint8Array([last ? 1 : 0]));
    const plain = aeadOpen(key, n, chunk);
    if (!plain) throw new AgeError("payload authentication failed: damaged, truncated or tampered with");
    if (last && plain.length === 0 && counter > 0) throw new AgeError("empty final chunk");
    parts.push(plain);
    if (last) break;
    counter++;
  }
  fileKey.fill(0); key.fill(0);
  return concat(...parts);
}
// 11-byte big-endian counter = 4 zero bytes + this 7-byte BE value (counters stay far below 2^53)
function le64Be(c) { const b = new Uint8Array(7); for (let i = 6; i >= 0; i--) { b[i] = c % 256; c = Math.floor(c / 256); } return b; }

// ── encrypt (one X25519 recipient) ──────────────────────────────────────────
export async function encrypt(plaintext, recipient) {
  const rcpt = parseRecipient(recipient);
  const fileKey = crypto.getRandomValues(new Uint8Array(16));
  const eph = await subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
  const share = new Uint8Array(await subtle.exportKey("raw", eph.publicKey));
  const shared = await x25519(eph.privateKey, rcpt);
  if (shared.every((b) => b === 0)) throw new AgeError("X25519 low-order point");
  const wrap = await hkdf(shared, concat(share, rcpt), "age-encryption.org/v1/X25519");
  const body = aeadSeal(wrap, new Uint8Array(12), fileKey);
  const headerNoMac = enc.encode(INTRO + "-> X25519 " + b64encode(share) + "\n" + b64encode(body) + "\n---");
  const mac = await hmac(await hkdf(fileKey, new Uint8Array(0), "header"), headerNoMac);
  const header = concat(headerNoMac, enc.encode(" " + b64encode(mac) + "\n"));
  const nonce = crypto.getRandomValues(new Uint8Array(16));
  const key = await hkdf(fileKey, nonce, "payload");
  const parts = [header, nonce];
  const nChunks = Math.max(1, Math.ceil(plaintext.length / CHUNK));
  for (let i = 0; i < nChunks; i++) {
    const last = i === nChunks - 1;
    const n = concat(new Uint8Array(4), le64Be(i), new Uint8Array([last ? 1 : 0]));
    parts.push(aeadSeal(key, n, plaintext.subarray(i * CHUNK, Math.min((i + 1) * CHUNK, plaintext.length))));
  }
  fileKey.fill(0); key.fill(0);
  return concat(...parts);
}
