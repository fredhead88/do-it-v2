#!/usr/bin/env node
// Every check in the install gate, against the failure it was written for.
// The registry is stubbed: a guard whose test needs the network is a guard
// whose test gets skipped. The two network-free hook paths — pass-through and
// the bump block — run as real subprocesses, because exit 2 is the whole
// mechanism and only a subprocess proves it.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, "..", "scripts", "vet-dep.mjs");
const DOIT = path.join(HERE, "..", "doit");
const { packagesIn, alreadyInManifest, splitSpec, vet } = await import(SCRIPT);
let n = 0;
const ok = (c, m) => { assert.ok(c, m); n++; };
const eq = (a, b, m) => { assert.deepEqual(a, b, m); n++; };
const names = (cmd) => packagesIn(cmd).map(s => s.raw);
const INSTALL_ = ["npm", "install"].join(" ");        // built, not written, so this file does not trip its own gate

// ── what the hook must let through (§6.7b: "passes through") ──────────────────
eq(names("npm ci"), [], "npm ci is a lockfile reinstall, not an acquisition");
eq(names("npm install"), [], "a bare install resolves the lockfile and adds nothing");
eq(names("npm ci --ignore-scripts"), [], "flags are not packages");
eq(names("pip install -r requirements.txt"), [], "-r installs a pinned set, not a new name");
eq(names("pip install -e ."), [], "the local project is not an acquisition");
eq(names(INSTALL_ + " ./vendor/thing"), [], "a local path is not a registry package");
eq(names("git status && ls"), [], "a command with no install in it is not the gate's business");
eq(names(`echo "run ${INSTALL_} left-pad to fix it"`), [],
   "★ writing ABOUT an install is not an install — an unanchored match blocks documentation and gets the gate disabled");
eq(names(`cat > note.md <<'EOF'\n${INSTALL_} left-pad\nEOF`), [],
   "★ …and a heredoc body is a file being written, not a command being run");

// ── what it must catch ───────────────────────────────────────────────────────
eq(names(INSTALL_ + " left-pad"), ["left-pad"], "the plain case");
eq(names("npm i -g some-cli"), ["some-cli"], "a global install is still an install");
eq(names("pnpm add @scope/pkg@2.1.0"), ["@scope/pkg@2.1.0"], "scoped names survive the split");
eq(names("cd /tmp/x && " + INSTALL_ + " evil"), ["evil"], "a compound command is one Bash call");
eq(names("sudo " + INSTALL_ + " -g evil"), ["evil"], "sudo does not hide it");
eq(names("pip3 install requests"), ["requests"], "pip3");
eq(names("uv add httpx"), ["httpx"], "uv");
eq(names("python3 -m pip install flask"), ["flask"], "pip behind python -m");
eq(packagesIn("pip install requests")[0].ecosystem, "pypi", "the ecosystem is read off the command");
eq(packagesIn("yarn add lodash")[0].ecosystem, "npm", "…and npm is npm");
eq(packagesIn("cd /tmp/proj && npm i evil")[0].dir, "/tmp/proj",
   "the manifest that decides ADD-vs-BUMP is the one at the install's cwd, not ours");
eq(splitSpec("@scope/pkg@2.1.0"), { name: "@scope/pkg", version: "2.1.0" }, "scoped name, pinned version");
eq(splitSpec("requests==2.31.0"), { name: "requests", version: "==2.31.0" }, "pip pin");
eq(splitSpec("left-pad"), { name: "left-pad", version: null }, "bare name");

// ── ADD vs BUMP (§6.7b: "on a version bump, blocks") ─────────────────────────
const d = mkdtempSync(path.join(tmpdir(), "vetdep-"));
writeFileSync(path.join(d, "package.json"), JSON.stringify({ dependencies: { zod: "^3.0.0" } }));
ok(alreadyInManifest("zod", d), "a package already in package.json is a BUMP");
ok(!alreadyInManifest("zod-fork", d), "a prefix match is not the same package");
ok(!alreadyInManifest("left-pad", d), "a package not in the manifest is an ADD");
const dp = mkdtempSync(path.join(tmpdir(), "vetdep-"));
writeFileSync(path.join(dp, "requirements.txt"), "requests>=2.0\nflask\n");
ok(alreadyInManifest("flask", dp), "requirements.txt counts");
ok(!alreadyInManifest("fla", dp), "…and only on a whole name");
writeFileSync(path.join(dp, "package.json"), "{ not json");
ok(!alreadyInManifest("anything", dp), "an unreadable manifest is not evidence of a bump");

// ── the four gates, registry stubbed ─────────────────────────────────────────
const realFetch = globalThis.fetch;
const iso = (daysAgo) => new Date(Date.now() - daysAgo * 86400000).toISOString();
function stub({ status = 200, license = "MIT", ageDays = 400, mal = [], throwOn = null } = {}) {
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (throwOn && u.includes(throwOn)) throw new Error("ECONNRESET");
    if (u.includes("osv.dev")) return { ok: true, json: async () => ({ vulns: mal.map(id => ({ id })) }) };
    if (u.includes("api.npmjs.org")) return { ok: true, status: 200, json: async () => ({ downloads: 1234 }) };
    if (status === 404) return { ok: false, status: 404 };
    return { ok: true, status: 200, json: async () => ({
      "dist-tags": { latest: "4.2.1" }, time: { "4.2.1": iso(ageDays), "4.0.0": iso(900) },
      readme: "x".repeat(900),
      versions: { "4.2.1": { license, homepage: "https://example.test/docs" } } }) };
  };
}
const after = async (p) => { const r = await p; globalThis.fetch = realFetch; return r; };

stub();
let v = await after(vet("good-pkg"));
eq(v.verdict, "pass", "MIT, 400 days old, no MAL-, exists → pass");
eq(v.installed_major, "4", "the scout is told which major it would get");
eq(v.doc_url, "https://example.test/docs", "…and where that major is documented");

stub({ status: 404 });
v = await after(vet("no-such-pkg"));
eq(v.verdict, "block", "a 404 blocks");
ok(/HALLUCINATED-NAME/.test(v.gates.exists.value),
   "…and it SAYS so: an agent told only 'blocked' tries a variant of the invented name");

stub({ ageDays: 2 });
v = await after(vet("fresh"));
eq(v.verdict, "block", "a release 2 days old is inside the 7-day cooldown");
ok(/2\.0d ago/.test(v.gates.age.value), "the raw value is the age, not a yes/no");

stub({ ageDays: 7.5 });
eq((await after(vet("just-old-enough"))).verdict, "pass",
   "…and 7.5 days is outside it — the boundary is not off by one");

stub({ mal: ["MAL-2025-0001"] });
v = await after(vet("poisoned"));
eq(v.verdict, "block", "a MAL- advisory blocks");
ok(/affects 4\.2\.1/.test(v.gates.malware.value),
   "…and names the version it affects, because OSV advisories are version-scoped");
// ★ Found by the first real probe run: chalk's MAL-2025-46969 affects 5.6.1
// only, so a package-scoped query bans chalk forever over a version nobody
// would install. The query must carry the version we are about to install.
let asked = null;
stub();
globalThis.fetch = (() => { const inner = globalThis.fetch;
  return async (url, init) => { if (String(url).includes("osv.dev")) asked = JSON.parse(init.body);
                                return inner(url, init); }; })();
await after(vet("versioned"));
eq(asked.version, "4.2.1", "the OSV query asks about the version that would be installed");
eq(asked.package.name, "versioned", "…for that package");
pypiStub({ license_expression: "MIT" });        // hoisted; defined with the PEP 639 checks below
globalThis.fetch = (() => { const inner = globalThis.fetch;
  return async (url, init) => { if (String(url).includes("osv.dev")) asked = JSON.parse(init.body);
                                return inner(url, init); }; })();
await after(vet("pypi-versioned", "pypi"));
eq(asked.package.ecosystem, "PyPI", "…and OSV's ecosystem name for PyPI is not `pypi`");
stub({ mal: ["GHSA-xxxx-yyyy"] });
eq((await after(vet("merely-vulnerable"))).verdict, "pass",
   "a plain GHSA is not this gate's question — §6.7 gates malice, not bugs");

stub({ license: "WTFPL" });
eq((await after(vet("wtfpl"))).verdict, "block",
   "a license off the allowlist blocks — deliverables ship to paying clients");
for (const l of ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "mit"]) {
  stub({ license: l });
  eq((await after(vet("x"))).verdict, "pass", `${l} is on the allowlist`);
}

// ★ Three-state discipline (§10.1): could-not-determine is never a pass and
// never a block. It is the recorded warning.
stub({ throwOn: "registry.npmjs.org" });
v = await after(vet("unreachable"));
eq(v.verdict, "warn", "an unreachable registry is inconclusive, not clean and not dirty");
ok(v.gates.exists.pass === null, "…and the gate says null, never false");
stub({ throwOn: "osv.dev" });
eq((await after(vet("osv-down"))).verdict, "warn",
   "OSV down is inconclusive — a guard that wedges when OSV is slow gets disabled");
stub({ license: null });
eq((await after(vet("no-license"))).verdict, "warn", "a missing license field is unknown, not a fail");
stub({ license: "WTFPL", throwOn: "osv.dev" });
eq((await after(vet("both"))).verdict, "block",
   "a definite bad answer beats an inconclusive one — fails CLOSED");

// ★ Both of these were found by the FIRST REAL reuse-scout run, not by a test:
// it reported MIT-licensed, 42-release `wcwidth` as license-undetermined and
// alive-undetermined, and correctly refused to call that clean.
function pypiStub({ license = null, license_expression = null, classifiers = [] } = {}) {
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes("osv.dev")) return { ok: true, json: async () => ({ vulns: [] }) };
    return { ok: true, status: 200, json: async () => ({
      info: { version: "0.8.3", license, license_expression, classifiers,
              description: "x".repeat(900), home_page: "https://example.test" },
      releases: Object.fromEntries([...Array(42)].map((_, i) => [`0.8.${i}`, i === 3
        ? [{ upload_time_iso_8601: iso(400) }] : []])) }) };
  };
}
pypiStub({ license_expression: "MIT" });
v = await after(vet("pep639-pkg", "pypi"));
eq(v.gates.license.value, "MIT", "PEP 639 moved the license to `license_expression` — read it");
eq(v.verdict, "pass", "…so a modern PyPI package is not undetermined for want of a dead field");
pypiStub({ classifiers: ["License :: OSI Approved :: MIT License"] });
eq((await after(vet("classifier-only", "pypi"))).gates.license.value, "MIT",
   "…and an older package's trove classifier still answers it, with the trailing 'License' trimmed");
pypiStub({ license_expression: "MIT" });
v = await after(vet("alive-pypi", "pypi"));
eq(v.gates.alive.pass, true, "PyPI publishes no download count, so `alive` there is release history");
ok(/no download count/.test(v.gates.alive.value),
   "…and the raw value says which measure it is, rather than printing `?` where a number belongs");
pypiStub({});
eq((await after(vet("truly-unlicensed", "pypi"))).verdict, "warn",
   "a package with none of the three license fields is still undetermined, not a pass");
globalThis.fetch = realFetch;

// ── the hook, as a subprocess: exit 2 is the whole mechanism ──────────────────
const root = mkdtempSync(path.join(tmpdir(), "vetdep-root-"));
mkdirSync(path.join(root, "events"), { recursive: true });
const run = (cmd, env = {}) => spawnSync(process.execPath, [SCRIPT, "--hook"], {
  input: JSON.stringify({ tool_name: "Bash", tool_input: { command: cmd } }),
  encoding: "utf8", env: { ...process.env, DOIT_ROOT: root, ...env },
});
eq(run("npm ci").status, 0, "the hook lets a lockfile reinstall through");
eq(run("git commit -m x").status, 0, "…and everything that is not an install");
const bump = run(`cd ${d} && npm i zod@4`);
eq(bump.status, 2, "a BUMP is refused before any network call — agents may ADD, never BUMP");
ok(/never BUMP/.test(bump.stderr), "…and the message says why, so the agent escalates instead of retrying");
ok(/DOIT_INSTALL_OVERRIDE/.test(bump.stderr),
   "…and names the sanctioned override, because a guard with none gets disabled");
eq(readdirSync(path.join(root, "events")).length, 0, "a block writes no event; only the override does");

const over = run(`cd ${d} && npm i zod@4`, { DOIT_INSTALL_OVERRIDE: "operator ratified at the sweep" });
eq(over.status, 0, "an explicit override lets it through");
const files = readdirSync(path.join(root, "events"));
eq(files.length, 1, "…and writes exactly one event");
const ev = JSON.parse(readFileSync(path.join(root, "events", files[0]), "utf8").trim());
eq(ev.type, "install-override", "the event is install-override");
eq(ev.subject, "zod", "…attributed to the package");
ok(/ratified/.test(ev.reason), "…carrying the operator's reason");
ok(!("actor" in ev), "…and never an actor field: the filename is the actor (D90)");
const board = spawnSync(DOIT, [], { encoding: "utf8", env: { ...process.env, DOIT_ROOT: root } }).stdout;
ok(/dependency install overrides: 1/.test(board),
   "★ the override count reaches HEALTH — a count nobody renders is not the metric §6.7b claims");

console.log(`vet-dep: ${n} checks pass`);
