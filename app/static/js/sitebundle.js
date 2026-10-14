// app/static/js/sitebundle.js — look inside a decrypted site bundle, in the browser
// =============================================================================
// The same checks EMS tools/restore_site.py makes before a bundle goes near a
// controller: it is a gzip'd tar, every path is under bundle/, nothing
// absolute, no "..", no links, a manifest with bundle_version 1. Then the
// same seven-line checklist the technician sheet shows.

export class BundleError extends Error {}

export async function gunzip(bytes) {
  const ds = new DecompressionStream("gzip");
  const out = await new Response(new Blob([bytes]).stream().pipeThrough(ds)).arrayBuffer();
  return new Uint8Array(out);
}

const dec = new TextDecoder();
const field = (h, off, len) => dec.decode(h.subarray(off, off + len)).replace(/\0.*$/s, "");
const octal = (h, off, len) => parseInt(field(h, off, len).trim() || "0", 8);

/** [{name, type, size, data}] for every entry. type: "file", "dir", "link", "other". */
export function untar(tar) {
  const out = [];
  let pos = 0, longName = null;
  while (pos + 512 <= tar.length) {
    const h = tar.subarray(pos, pos + 512);
    if (h.every((b) => b === 0)) break;
    // header checksum: the module's tar checks it, so this page must too, or
    // a broken bundle looks fine here and is refused on the controller
    let sum = 0;
    for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 32 : h[i];
    if (sum !== octal(h, 148, 8)) throw new BundleError("damaged tar header (bad checksum) at " + field(h, 0, 100));
    let name = field(h, 0, 100);
    const prefix = field(h, 345, 155);
    if (prefix) name = prefix + "/" + name;
    const size = octal(h, 124, 12);
    const flag = String.fromCharCode(h[156] || 48);
    const data = tar.subarray(pos + 512, pos + 512 + size);
    pos += 512 + Math.ceil(size / 512) * 512;
    if (flag === "L") { longName = dec.decode(data).replace(/\0.*$/s, ""); continue; }   // GNU long name
    if (flag === "x" || flag === "g") {                                                   // pax header
      const m = dec.decode(data).match(/\d+ path=([^\n]*)\n/);
      if (m && flag === "x") longName = m[1];
      continue;
    }
    if (longName) { name = longName; longName = null; }
    const type = flag === "0" || flag === "\0" || flag === "7" ? "file" : flag === "5" ? "dir"
      : flag === "1" || flag === "2" ? "link" : "other";
    out.push({ name: name.replace(/^\.\//, ""), type, size, data });
  }
  return out;
}

export async function inspect(plainTarGz) {
  let entries;
  try {
    entries = untar(await gunzip(plainTarGz));
  } catch (e) {
    if (e instanceof BundleError) throw e;
    throw new BundleError("decrypted, but not a site bundle (not a gzip'd tar)");
  }
  for (const e of entries) {
    const parts = e.name.split("/");
    if (e.name.startsWith("/") || parts.includes("..") || !e.name.startsWith("bundle")) throw new BundleError("unsafe path inside bundle: " + e.name);
    if (e.type === "link") throw new BundleError("link inside bundle (refused): " + e.name);
  }
  const m = entries.find((e) => e.name === "bundle/manifest.json" && e.type === "file");
  if (!m) throw new BundleError("decrypted, but not a site bundle (no manifest)");
  let manifest;
  try { manifest = JSON.parse(dec.decode(m.data)); } catch { throw new BundleError("manifest is not JSON"); }
  if (manifest.bundle_version !== 1) throw new BundleError("unsupported bundle_version " + manifest.bundle_version);
  const files = entries.filter((e) => e.type === "file").map((e) => e.name);
  const has = (frag) => files.some((n) => n.includes(frag));
  const checks = [
    ["controller config", has("ems/config.json")],
    ["backup deploy key", has("ems/.backup_ssh/id_ed25519")],
    ["OTA deploy key", has("ems/.ota_ssh/id_ed25519")],
    ["network (netplan/NM)", has("etc/netplan/") || has("system-connections/")],
    ["Tailscale node", has("tailscaled.state")],
    ["Cloudflare tunnel", has("cloudflared/token")],
    ["ChirpStack database", has("chirpstack.sql")],
  ];
  return { manifest, files, checks };
}
