#!/usr/bin/env node
// §6.7b — the install gate IS the vetting script, and §10.2's three properties:
// the hook is the check; it fails CLOSED on a definite bad answer and OPEN with a
// recorded warning on an inconclusive one; the override is explicit and writes an
// event. Also §6.6's ground truth for `reuse-scout`: the same four checks, raw
// values printed, so the scout reports gates rather than inventing them.
//
//   node scripts/vet-dep.mjs [--pypi] <spec>...   vet packages, print JSON
//                                                 exit 0 pass · 1 block · 2 could-not-determine
//   node scripts/vet-dep.mjs --hook               PreToolUse on stdin; exit 2 blocks the Bash call
//
// No dependencies: node's own fetch, and nothing else. (A vetting script that
// needs an install is a vetting script that cannot gate the first install.)
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const COOLDOWN_DAYS = 7;                       // §6.7a·1 — days, set explicitly, never a default
const ALLOWED = [/^MIT$/i, /^Apache-2\.0$/i, /^BSD(-[\w.+-]+)?$/i, /^ISC$/i,
                 /^MIT OR Apache-2\.0$/i, /^Apache-2\.0 OR MIT$/i];
const DOIT = path.join(path.dirname(path.dirname(fileURLToPath(import.meta.url))), "doit");
const TIMEOUT = Number(process.env.VET_DEP_TIMEOUT_MS || 8000);
const day = 86400000;

// ── the four checks ────────────────────────────────────────────────────────────
// Every check returns {pass: true|false|null, value}. null is could-not-determine
// and is never a pass and never a block: it is the recorded warning.
const unknown = (why) => ({ pass: null, value: why });

async function getJSON(url, init) {
  const r = await fetch(url, { ...init, signal: AbortSignal.timeout(TIMEOUT) });
  if (r.status === 404) return { status: 404 };
  if (!r.ok) throw new Error(`${r.status}`);
  return { status: r.status, body: await r.json() };
}

async function npmFacts(name) {
  const meta = await getJSON(`https://registry.npmjs.org/${encodeURIComponent(name)}`);
  if (meta.status === 404) return { missing: true };
  const m = meta.body, latest = m["dist-tags"]?.latest, v = m.versions?.[latest] || {};
  let downloads = null;
  try {
    const d = await getJSON(`https://api.npmjs.org/downloads/point/last-month/${encodeURIComponent(name)}`);
    downloads = d.body?.downloads ?? null;
  } catch { /* alive stays could-not-determine on the download half */ }
  return {
    latest, published: m.time?.[latest],
    license: v.license ?? (Array.isArray(v.licenses) ? v.licenses.map(l => l.type).join(" OR ") : null),
    doc_url: v.homepage || m.homepage || (typeof v.repository?.url === "string" ? v.repository.url : null),
    readme_bytes: (m.readme || "").length, downloads, releases: Object.keys(m.time || {}).length,
  };
}

async function pypiFacts(name) {
  const meta = await getJSON(`https://pypi.org/pypi/${encodeURIComponent(name)}/json`);
  if (meta.status === 404) return { missing: true };
  const i = meta.body.info, latest = i.version;
  const files = meta.body.releases?.[latest] || meta.body.urls || [];
  // PEP 639 moved the license to `license_expression`; `license` is None on any
  // package published since. Read all three, newest first — found by the first
  // real reuse-scout run, which reported MIT-licensed wcwidth as undetermined.
  const cls = (i.classifiers || []).find(c => c.startsWith("License :: OSI Approved ::"));
  return {
    latest, published: files[0]?.upload_time_iso_8601,
    license: i.license_expression || i.license
             || (cls ? cls.split("::").pop().trim().replace(/\s+License$/i, "") : null),
    doc_url: i.project_urls?.Documentation || i.home_page || i.project_url,
    readme_bytes: (i.description || "").length, downloads: null,
    releases: Object.keys(meta.body.releases || {}).length,
  };
}

// ★ OSV advisories are VERSION-scoped, and a package-scoped query is not the
// question the install gate asks. Found by the first real probe run: chalk
// carries MAL-2025-46969 against `versions: ["5.6.1"]` only, so a package-scoped
// query bans chalk forever over a version nobody would install. The version we
// are about to install is the one to ask about.
async function osvMalware(name, ecosystem, version) {
  const r = await fetch("https://api.osv.dev/v1/query", {
    method: "POST", signal: AbortSignal.timeout(TIMEOUT),
    body: JSON.stringify({ package: { name, ecosystem: ecosystem === "pypi" ? "PyPI" : "npm" },
                           ...(version ? { version } : {}) }),
  });
  if (!r.ok) throw new Error(`${r.status}`);
  const ids = ((await r.json()).vulns || []).map(v => v.id).filter(id => id.startsWith("MAL-"));
  return ids;
}

export async function vet(name, ecosystem = "npm") {
  const out = { name, ecosystem, gates: {}, installed_major: null, doc_url: null };
  let f;
  try {
    f = ecosystem === "pypi" ? await pypiFacts(name) : await npmFacts(name);
  } catch (e) {
    out.gates.exists = unknown(`registry unreachable: ${e.message}`);
    out.gates.age = out.gates.malware = out.gates.license = unknown("registry unreachable");
    out.gates.legible = unknown("registry unreachable");
    return verdict(out);
  }
  if (f.missing) {
    // ★ The hallucinated-name case, and it must SAY so: an agent told only "blocked"
    // tries a variant of the invented name (§6.7b).
    out.gates.exists = { pass: false, value: "404 — no such package in the registry. THIS IS THE HALLUCINATED-NAME CASE: the name does not exist. Report it; do not try a variant." };
    return verdict(out);
  }
  out.gates.exists = { pass: true, value: `latest ${f.latest}` };
  out.installed_major = String(f.latest || "").split(".")[0] || null;
  out.doc_url = f.doc_url || null;

  const ageDays = f.published ? (Date.now() - Date.parse(f.published)) / day : null;
  out.gates.age = ageDays === null ? unknown("no publish time in the registry metadata")
    : { pass: ageDays >= COOLDOWN_DAYS, value: `${f.latest} published ${ageDays.toFixed(1)}d ago (cooldown ${COOLDOWN_DAYS}d)` };

  try {
    const mal = await osvMalware(name, ecosystem, f.latest);
    out.gates.malware = { pass: mal.length === 0,
                          value: mal.length ? `${mal.join(",")} affects ${f.latest}` : `no MAL- advisory against ${f.latest}` };
  } catch (e) {
    out.gates.malware = unknown(`OSV unreachable: ${e.message}`);
  }

  out.gates.license = f.license == null ? unknown("no license field")
    : { pass: ALLOWED.some(re => re.test(f.license.trim())), value: f.license };

  // Gate 2's other half (alive) and gate 3 (agent-legible) are raw values for the
  // scout to report; only the four mechanical gates above can block an install.
  // PyPI's JSON API carries no download counts, so "alive" there is release
  // history, not downloads — a `?` where a number belongs made the gate read
  // undetermined for every Python package (same first real run).
  out.gates.alive = f.downloads == null
    ? { pass: f.releases > 1, value: `${f.releases} releases (no download count in this registry)` }
    : { pass: f.downloads > 0 && f.releases > 1, value: `${f.downloads} downloads/month, ${f.releases} releases` };
  out.gates.legible = { pass: f.readme_bytes > 500, value: `readme ${f.readme_bytes} bytes` };
  return verdict(out);
}

const BLOCKING = ["exists", "age", "malware", "license"];
function verdict(o) {
  const bad = BLOCKING.filter(g => o.gates[g]?.pass === false);
  const unk = BLOCKING.filter(g => o.gates[g]?.pass === null);
  o.verdict = bad.length ? "block" : unk.length ? "warn" : "pass";
  o.why = bad.length ? bad.map(g => `${g}: ${o.gates[g].value}`).join(" · ")
        : unk.length ? unk.map(g => `${g}: ${o.gates[g].value}`).join(" · ") : "all four gates pass";
  return o;
}

// ── the hook ───────────────────────────────────────────────────────────────────
// Anchored at the start of a command segment, after an optional `sudo` or leading
// VAR=val assignments. Unanchored, the words "npm install x" inside a quoted
// string are indistinguishable from the command — and a gate that blocks writing
// ABOUT an install is a gate that gets disabled (§10.2).
const INSTALL = /^(?:sudo\s+)?(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:(npm|pnpm|yarn|bun)\s+(?:install|i|add)|(pip3?|uv)\s+(?:pip\s+install|install|add)|python3?\s+-m\s+pip\s+install)(\s.*)?$/;
// ponytail: heredoc bodies are dropped whole rather than shell-parsed. A file
// being WRITTEN that contains an install line is not an install; a real install
// after the heredoc closes is missed, and the merge-time acquisition-set gate
// (§6.7e) is the backstop that already exists for exactly that residual.
const stripHeredocs = (c) => (c.includes("<<") ? c.slice(0, c.indexOf("<<")) : c);

export function packagesIn(command) {
  // Every segment of a compound command, because `cd x && npm i evil` is one Bash call.
  const specs = [];
  let dir = process.cwd();
  for (const seg of stripHeredocs(command).split(/&&|\|\||;|\|/)) {
    const cd = seg.trim().match(/^cd\s+(\S+)/);          // `cd x && npm i y` installs into x, not here
    if (cd) dir = path.resolve(dir, cd[1].replace(/^['"]|['"]$/g, ""));
    const m = seg.trim().match(INSTALL);
    if (!m) continue;
    const eco = (m[2] && m[2] !== "uv") || /python3?\s+-m\s+pip/.test(seg) ? "pypi"
              : m[2] === "uv" ? "pypi" : "npm";
    const args = (m[3] || "").trim().split(/\s+/).filter(Boolean);
    let skip = false;
    for (const a of args) {
      if (skip) { skip = false; continue; }
      if (a === "-r" || a === "--requirement" || a === "-c" || a === "--constraint") { skip = true; continue; }
      if (a.startsWith("-")) continue;                       // flags: --save-dev, -g, --only-binary=:all:
      if (a === "." || a.startsWith("./") || a.startsWith("/") || a.startsWith("../")) continue;  // local path
      specs.push({ raw: a, ecosystem: eco, dir });
    }
  }
  return specs;
}

export const splitSpec = (raw) => {
  const m = raw.match(/^(@[^/]+\/[^@]+|[^@<>=!~ ]+)\s*(.*)$/);
  const rest = (m?.[2] || "").replace(/^@/, "");
  return { name: m?.[1] || raw, version: rest || null };
};

export function alreadyInManifest(name, dir = process.cwd()) {
  const pj = path.join(dir, "package.json");
  if (existsSync(pj)) {
    try {
      const j = JSON.parse(readFileSync(pj, "utf8"));
      for (const k of ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"])
        if (j[k] && name in j[k]) return `package.json ${k}`;
    } catch { /* an unreadable manifest is not evidence of a bump */ }
  }
  for (const f of ["requirements.txt", "pyproject.toml"]) {
    const p = path.join(dir, f);
    if (!existsSync(p)) continue;
    const re = new RegExp(`^\\s*["']?${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "im");
    if (re.test(readFileSync(p, "utf8"))) return f;
  }
  return null;
}

function append(type, subject, kv) {
  // The override count is itself the metric that says the window is set wrong
  // (§10.2). A gate whose override leaves no trace has no such metric.
  const r = spawnSync(DOIT, ["append", type, subject, ...Object.entries(kv).map(([k, v]) => `${k}=${v}`)],
                      { encoding: "utf8" });
  if (r.status !== 0) process.stderr.write(`vet-dep: could not record ${type}: ${r.stderr || r.error}\n`);
}

async function hook() {
  let raw = "";
  for await (const c of process.stdin) raw += c;
  let command = "";
  try { command = JSON.parse(raw)?.tool_input?.command || ""; } catch { command = ""; }
  const specs = packagesIn(command);
  if (!specs.length) process.exit(0);           // npm ci, a bare install, a lockfile reinstall

  const override = (process.env.DOIT_INSTALL_OVERRIDE || "").trim();
  const blocks = [], warns = [];
  for (const { raw: spec, ecosystem, dir } of specs) {
    const { name, version } = splitSpec(spec);
    const where = alreadyInManifest(name, dir);
    if (where) { blocks.push(`${name}: already in ${where} — agents may ADD a dependency, never BUMP one. A bump is its own spec (§6.7b).`); continue; }
    if (version && /^[0-9]/.test(version) === false && version !== "latest") { /* a range on a new package is fine */ }
    const v = await vet(name, ecosystem);
    if (v.verdict === "block") blocks.push(`${name}: ${v.why}`);
    else if (v.verdict === "warn") warns.push(`${name}: ${v.why}`);
  }
  if (blocks.length && override) {
    for (const b of blocks) append("install-override", b.split(":")[0], { reason: JSON.stringify(override), gate: JSON.stringify(b) });
    process.stderr.write(`vet-dep: OVERRIDDEN (recorded): ${blocks.join(" | ")}\n`);
    process.exit(0);
  }
  if (blocks.length) {
    process.stderr.write(`vet-dep BLOCKED this install (§6.7b):\n  ${blocks.join("\n  ")}\n` +
      `Do not retry a variant. Escalate: a new dependency is irreversible (§4.9 trigger 4), and installation is the Executor's (D73).\n` +
      `To override deliberately: DOIT_INSTALL_OVERRIDE='<why>' — it writes an install-override event.\n`);
    process.exit(2);                             // fails CLOSED on a definite bad answer
  }
  for (const w of warns) append("install-warned", w.split(":")[0], { why: JSON.stringify(w) });
  if (warns.length) process.stderr.write(`vet-dep: could not determine, allowing with a recorded warning: ${warns.join(" | ")}\n`);
  process.exit(0);                               // OPEN on an inconclusive one
}

// ── cli ────────────────────────────────────────────────────────────────────────
if (process.argv[1] && import.meta.url.endsWith(path.basename(process.argv[1]))) {
  const argv = process.argv.slice(2);
  if (argv.includes("--hook")) await hook();
  else {
    const eco = argv.includes("--pypi") ? "pypi" : "npm";
    const names = argv.filter(a => !a.startsWith("--"));
    if (!names.length) { process.stderr.write("usage: vet-dep.mjs [--pypi] <package>... | --hook\n"); process.exit(64); }
    const results = [];
    for (const n of names) results.push(await vet(splitSpec(n).name, eco));
    process.stdout.write(JSON.stringify(results.length === 1 ? results[0] : results, null, 2) + "\n");
    process.exit(results.some(r => r.verdict === "block") ? 1 : results.some(r => r.verdict === "warn") ? 2 : 0);
  }
}
