# Changelog

## 0.2.5 — one authority, self-healing boot, housekeeping paid up

### Release train (C6): legacy-artifact retirement and a console-free watchdog

Card C6 closed the three housekeeping items the audit left open and the
watchdog defect the operator found in the field after C3 shipped.

- **Relay inbox pruning (audit F11).** `RelayState.finish()` walked the
  event table on every ack but kept every acknowledged row forever: a
  long-lived relay source grew unbounded. Rows the ack watermark has
  walked past are now deleted on the same walk. Dedupe does not regress:
  the cursor is the contiguous delivered prefix, so `stage()` treats a
  server replay of an id at or below the watermark as already delivered
  (the watermark is the tombstone the pruned row used to be). Failed and
  quarantined rows stay visible to `failures()` and `retry()`.
- **`init` keeps your hermes config's comments (audit F13).** Enabling
  `platforms.webhook` used to `safe_load`/`safe_dump` the whole
  `config.yaml`, silently destroying every comment and the key ordering
  in a file the user owns (the sibling Codex path always edited text).
  The edit is now targeted: the `platforms:`/`webhook:`/`enabled:` lines
  in scope are created or patched in place, everything else survives
  byte-for-byte, and the result is re-parsed and validated BEFORE the
  write. Unusual shapes (flow mappings) still round-trip, but only after
  a backup copy to `config.yaml.galvanize.bak`.
- **Legacy state files are gone (audit F14).** C5 kept the migrated
  originals as `*.bak` "until the next release"; this is that release, so
  `store.prune_legacy()` runs at every process boot after the authority
  is ready and retires: the `*.bak` originals, the pre-C2
  `run_hidden.vbs`, the dead `state.last_dispatch()` stub, and any legacy
  file a still-running pre-0.2.5 daemon RE-created after the migration
  (re-imported newest-wins first, so a late anchor or fire counter is
  never silently lost). Orphaned `source-<hash>.json` files and their
  `legacy:<hash>` db rows whose hash matches no live trigger's source key
  are pruned by the trigger-name set.
- **Watchdog boot persistence re-enabled with a console-free entry
  (mandatory, CoS field note 2026-10-21 23:51).** C3's vbs launched the
  app venv's `pythonw.exe`, which is a launcher STUB: it re-execs the
  base console interpreter, and the Windows Terminal pseudoconsole the
  re-exec child allocates outlives the brief `daemon ensure` pass — one
  zombie visible WT frame per minute-tick (19 counted in the field). The
  generated launcher now runs the base install's `pythonw.exe` directly
  — read from the venv's own `pyvenv.cfg` `home =`, not a vendor guess —
  GUI-subsystem, no re-exec, with `PYTHONPATH` pointing at the app venv's
  site-packages, so the identical installed package tree is imported.
  `daemon install` additionally issues `schtasks /Change /Enable` for its
  own task, so the stopgap `/disable` (which had been protecting the
  desktop from the leak) does not survive an install: boot persistence
  comes back enabled BY CONSTRUCTION. Where no base interpreter can be
  resolved the old stub entry remains, and the registration line now
  says so out loud instead of claiming a windowless guarantee it cannot
  keep.
- **Release shape.** Pushed to GitHub and tagged `v0.2.5`; uploaded to
  PyPI per the owner's GO decision (received during this card: wheel +
  sdist via twine, installability verified from the simple index and by
  importing `galvanize.winlaunch` from a clean `pip install
  galvanize==0.2.5` venv).

### Flaky-test watch (release gate)

The release gate (3 full-suite runs under `-p no:cacheprovider`) caught
one transient failure — reproducing the operator's independent 1-in-3
observation the same night — now root-caused, not rerun away:
`test_concurrent_route_registration_keeps_both` failed with
`sqlite3.OperationalError: database is locked`. The cause is a real
defect, not a test artifact: `PRAGMA journal_mode=WAL` is the one
statement documented NOT to invoke sqlite's busy handler, so on a
freshly created database a connection opened while another held the
write lock got an immediate BUSY instead of waiting. `connect()` now
retries the flip within the same bounded busy budget (the flip only ever
contends until the header says WAL; after that it is lock-free), and a
new stress test (`test_concurrent_first_opens_survive_the_wal_flip`,
8 rounds x 8 threads) pins it. The gated suite afterwards ran clean.

### One transactional authority: galvanize.db (audit F8/F9)

State was flat files read-modified-rewritten whole: one `state.json` held
the heartbeat AND every trigger's counters (main loop and dispatcher
threads clobbered each other by construction, and a torn write lost
everything at once), `imap_state.json` shared one document across
watchers, `triggers.yaml` hot-reload raced partial writes, and two
processes editing config (CLI + dashboard plugin + serve) had no ordering
at all. Now ONE local SQLite database, `~/.galvanize/galvanize.db`, is the
writable authority for trigger config, per-trigger run state, heartbeat,
source health, imap resume anchors, and webhook route INTENT. WAL mode,
`BEGIN IMMEDIATE` for every compound mutation, bounded 5 s busy_timeout,
short transactions with no network calls inside.

- **Revisions.** Trigger config carries a global revision; every edit
  (CLI, Hermes plugin, dashboard, serve API, `config import`) is one
  read-modify-write transaction through the same repository — works
  offline, no daemon IPC in the consistency path. An editor holding
  revision N that writes after the authority moved raises
  `StaleRevisionError`: `409` on serve/dashboard, nonzero + message on the
  CLI. A silent clobber is no longer a representable outcome.
- **triggers.yaml is demoted** to import/export + first-seed. It is
  rewritten as a mirror after every activated change but hand edits are
  inert until `galvanize config import <path>` (all-or-nothing
  validation). Corruption semantics: a failed parse/validation keeps the
  committed last-known-good serving, records a loud state, and fails the
  new `config authority` doctor row — watchers never die over a bad file,
  and a first boot with no valid config reports UNHEALTHY, not "no
  triggers" (`missing` vs `corrupt` distinguished in the note).
- **Migration** runs once on the first supervisor start (lazily at any
  other entry point if it beat the supervisor there): state.json,
  imap_state.json, source-*.json and existing managed routes import into
  rows inside one transaction, triggers.yaml seeds revision 1, and the
  originals are kept as `*.bak` until the next release.
- **webhook_subscriptions.json stays gateway-owned.** Galvanize's writes
  serialize through a db-locked writer (file rewrite happens under
  `BEGIN IMMEDIATE`, no network inside) and every managed route also
  lands as an INTENT row. A reconciler (daemon minute beat +
  `galvanize doctor --fix`) converges the file back to intent: deleted or
  mangled routes are re-applied, pre-C5 routes are adopted, foreign
  name-collisions are reported, never overwritten. Shared-writer risk
  (documented, accepted): a gateway write racing our transaction can
  transiently revert a route until the next reconcile; consistency comes
  from the intent rows, not from locking someone else's file.

Output changes:

- `galvanize status` gained a `config:` line (`rev N` or
  `UNHEALTHY (corrupt|never)`, SQLite authority note); when the authority
  state is not `ok` the `notes` carry a `config CORRUPT/NEVER:` line in
  status JSON too, so plugin/dashboard consumers cannot render a broken
  config as a quiet success.
- `galvanize doctor` gained three rows: `state store` (galvanize.db
  quick_check + row counts), `config authority` (revision, or the loud
  corrupt/never explanation with the failing import's error text), and
  `routes intent` (intent-vs-gateway-file drift: ✖ on a foreign name
  collision, ⚠ while drift awaits reconcile, ✔ matched).
- New verb: `galvanize config show|export|import [path]
  [--dry-run] [--expect-revision N]`.
- `galvanize daemon started` log lines now report `config rev N`; the
  daemon's hot-reload polls the revision instead of file mtime (a rebuild
  can no longer observe a half-written YAML).

### Application-owned runtime + launch-equivalent capability gate (audit F3/F4)

`daemon install` no longer adopts the invoker's interpreter — that was the
F3 root cause: any run from a foreign python (uv shim, woken agent, repo
dir via inherited PYTHONPATH) silently retargeted the .lnk to an
environment that could not import the watcher deps, and nothing noticed
for 20 h. Now install PROVISIONS or VALIDATES an app-owned venv at
`~/.galvanize/runtime/venv` (`python -m venv --upgrade-deps` + pip install
of this source with the declared `[imap]` extra; dev checkout installs
editable so working-tree code remains the runtime contract), and what it
persists is a validated capability manifest, not a hope:

- The probe is LAUNCH-EQUIVALENT: the candidate interpreter is spawned
  `-I -m galvanize.capability --json` at the .lnk's working directory with
  PYTHONPATH/PYTHONHOME/user-site scrubbed (the `-I` proven necessary: a
  `PYTHONPATH=<repo>` injection pre-C4 made a pip-less interpreter LOOK
  complete). Verified live this session: an interpreter poisoned via
  PYTHONPATH passes the isolated probe and fails a non-isolated one with
  the poison named.
- The result is a versioned schema-v1 manifest (imports, package versions,
  real keyring set/get/delete roundtrip against the OS backend, watcher
  capabilities, windowless-ness) with failures classified
  dependency / auth / env / version: dependency and env failures REFUSE
  the install; auth (no usable keyring backend) warns — the encrypted-file
  fallback is a supported degraded mode, not a missing dependency.
  Version mismatch against the installer refuses too (silent version
  retarget is the same F3 disease).
- The SAME collector runs inside the worker and rides the C2 READY post,
  so supervisor.json advertises live capabilities; doctor gained the
  three-way diff rows (install.json manifest vs actual .lnk/unit target
  read back from the OS vs the live worker's READY identity) plus a
  capability-drift row. On refusal, NOTHING on disk changes: the previous
  install.json, .lnk and watchdog task survive byte-identical (live
  drill: foreign interpreter + injected PYTHONPATH -> rc=1, md5 of .lnk
  and install.json unchanged, daemon kept serving on the recorded
  identity).
- F4 folded in: on Windows the persisted target must be `pythonw.exe`;
  a candidate with no windowless sibling is refused out loud.
- Install now CONVERGES a running supervisor whose worker exe differs
  from the freshly validated identity (no split-brain: .lnk pointing one
  way, daemon running another) and `daemon stop`/`install` re-cycles it.
- Real defect found by the live drill: WinVaultKeyring loses concurrent
  set/get probes from ONE process even on distinct keys (READY reported
  keyring=False against a manifest saying True). Fixed by making
  `keyring_status` use a per-call unique credential serialized under the
  (now reentrant) probe lock; the drift row stays green under 12-way
  concurrency.

Drill residue after this card: live supervisor 59720 runs the app-owned
venv; `doctor` runtime rows all ✔; seams documented for tests and
operators: `GALVANIZE_RUNTIME_VENV`, `GALVANIZE_RUNTIME_DISABLE`,
`GALVANIZE_RUNTIME_SKIP_PROVISION`, `GALVANIZE_INSTALL_INTERP`.

### Boot persistence: minute-cadence watchdog task with proven feasibility (audit F2)

The C2 supervisor self-heals worker crashes, but nothing recovered a
supervisor that itself died or wedged after logon, and a logon where the
Startup .lnk never fired meant silence until someone noticed. The
persistence mechanism is now PROVEN, not assumed (standard-user
interactive token, Medium IL, Administrators deny-only, this host):

| probe | result |
|---|---|
| `schtasks /Create /SC MINUTE /MO 1` (cmd action) | OK — fired every 60 s, Last Result 0, 435 ticks in ~5.5 h |
| same with `/RU <self>`, NO `/RP` | OK — but stays "Interactive only": no stored credential => no pre-logon (S4U) execution |
| `schtasks /Create /SC ONLOGON` | **Access denied** (reproduces the historical failure; minute-cadence task chosen instead) |
| console-subsystem action (`cmd.exe /c ...`) | ran fine but popped a VISIBLE Windows Terminal window EVERY trigger (verifier sample 22:08–22:12, 5/5) |
| `Set-ScheduledTask` on own task | OK un-elevated — used to clear the schtasks-default battery start-gate |

- **Watchdog task `GalvanizeWatchdog[-<profile>]`** (registered by
  `daemon install`, removed by `daemon remove`): every minute runs
  `galvanize daemon ensure` through a generated `watchdog_hidden.vbs`
  wscript trampoline — windowless BY CONSTRUCTION (wscript is
  GUI-subsystem; `WshShell.Run ..., 0, False`), not by hope. The launcher
  scrubs PYTHONPATH/HERMES_HOME for the child.
- **`daemon ensure` is an idempotent pass**: fresh snapshot => silent
  no-op; live pid with status older than the max legit respawn-backoff
  window (420 s, `GALVANIZE_WATCHDOG_WEDGE_S`) => WEDGED, kill it (job
  cascade takes the worker tree) and respawn; corpse/absent => respawn.
  Pause tombstone: `daemon stop` touches `supervisor.pause` and the
  watchdog honors it until any supervisor start (or install) clears it —
  a user-stopped daemon is not resurrected.
- **Guarantee stated, never silently downgraded**: `daemon status` and
  `doctor` print the persistence tier and its scope — post-LOGON within
  <= 60 s plus wedge recovery; PRE-logon headless start is not available
  on this host without a stored password. The .lnk is kept for instant
  start (belt); the watchdog is the guarantee (braces). If registration
  ever fails, install says "logon-only on this host" out loud.
- Live drills: killed supervisor 70972 → resurrected READY in 4 s with
  zero window/console-process delta and zero task residue;
  NtSuspendProcess'd (alive-but-frozen) 68172 + backdated status →
  watchdog killed and replaced the wedge, READY again; stop/ensure/
  install lifecycle respected the tombstone.

### Supervisor: SID-mutex authority + READY protocol kills the TOCTOU double-start (audit F1/F10/F14)

A new minimal supervisor process (`galvanize supervise`, now the startup
target) owns single-instance by CONSTRUCTION, replacing the startup-only
heartbeat peek that let two daemons coexist 20h (F1 live proof: pids
48312/61864, later 67740/71276).

- **The mutex is the authority.** One supervisor per (user SID, profile)
  claims `Global\Galvanize-<SID>-<profile>` via `CreateMutexW` with an
  SID-restricted security descriptor BEFORE heavy imports or network;
  a second supervisor exits clean and logs why. Hosts where the user
  lacks the global-namespace right fall back to `Local\` (per-session
  scope, recorded in supervisor.json, surfaced in doctor). Workers never
  race: only the supervisor spawns them (via the C1 primitive,
  suspended -> job -> resume); a direct `galvanize run` claims the same
  mutex, so it can never join a running supervisor either. POSIX gets
  the same role via flock on `supervisor.lock`.
- **Readiness is proven.** The worker posts an authenticated READY
  (`ready.json`: per-spawn token from its env, pid matched against JOB
  membership so the venv-pythonw re-exec case works, interpreter
  identity, version, capabilities, per-watcher status) within 20s; the
  supervisor then requires main-loop heartbeat progress within 30s and
  keeps a liveness contract after (45s stale = three missed 15s beats =>
  kill the whole tree, await job close, respawn with exponential backoff
  2s..300s).
- **Permanent failures halt, not loop.** A capability failure the worker
  marks permanent (e.g. imap-tools unimportable while imap triggers are
  configured) is surfaced CRITICAL and, after two such READYs, parks the
  supervisor in state `halted` instead of respawning forever.
- **No more DEVNULL silence.** `daemon install` verifies the supervisor
  posted READY and reports the supervisor.log tail on failure;
  `supervisor.json` publishes state, mutex identity, worker identity and
  spawn/failure history for `daemon status`, `doctor` (new `supervisor`
  row; `status` header shows `supervisor -> worker` lineage) and the
  C3 watchdog. Both logs now rotate (2 MB x 3; folds F10).
- `daemon stop`/`remove` stop the supervisor first (graceful
  `supervisor.shutdown` flag, force backstop); its job-close takes the
  worker tree with it, and a worker-owned heartbeat is cleared so no lie
  survives shutdown. `daemon.pid` is gone (F14): the mutex superseded it.
- Tests: 13 new (`tests/test_supervisor.py`) covering contention, the
  wedged-pre-main child, unauthenticated READY, respawn-after-kill,
  missed-ticks tree kill, capability-fail stop-retry, graceful stop.

### Centralized Windows launch primitive (audit F5/F6/F7/F12)

Every process galvanize spawns now goes through `galvanize/winlaunch.py`,
one audited primitive; an AST gate (`tests/test_launch_primitive_ast.py`)
fails the build if a new `subprocess`/`os.system`/`_winapi` call site
appears outside it without a justified allowlist entry.

- **No window, ever.** Console children are created `CREATE_SUSPENDED |
  CREATE_NO_WINDOW`, assigned to a private Job Object
  (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), then resumed — nothing runs
  before job membership exists. The daemon handoff uses the same hidden-
  console flavor plus `BREAKAWAY_FROM_JOB` (reported honestly when the
  parent job forbids it). Live Win11 evidence: `DETACHED_PROCESS` let the
  Windows-Terminal default terminal allocate a visible window for the
  venv `pythonw.exe` stub's console re-exec; `CREATE_NO_WINDOW` prevents
  it, and descendants inherit the hidden console. GUI descendants can
  still draw — shell wakes run user-declared commands.
- **Timeouts kill the whole tree** (Job terminate), closing the
  cmd.exe-orphan-keeps-the-window case; descendants that outlive the
  direct child die when the job closes.
- **Full wake output preserved.** Shell-wake details carry a 2000-char
  tail (was 400) plus a pointer to `wake_output/<trigger>.log` beside
  `state.json`, overwritten per fire, holding complete stdout/stderr —
  including partial output from timed-out wakes.
- Converted sites: `dispatch` wakes, `autostart` powershell/schtasks/
  systemctl/launchctl + the detached daemon handoff, `cli init` probes and
  pip installs, `migrate` hermes-cron-pause, `githook` repo probes, and
  the Hermes plugin's CLI-fallback branches (bootstrap allowlist entry:
  the primitive package is unreachable exactly when the import failed).

## 0.2.4 — wake sessions get real tool access

Hermes wake sessions previously always ran on the gateway's restricted
webhook toolset (4 tools, no terminal/file), so any trigger prompt with
shell steps silently failed. Routes now carry an explicit `toolsets`
override:

- **Parity by default.** `register_route` writes the gateway `default`
  distribution's toolset names (`browser, file, image_gen, terminal,
  vision, web`) into every newly registered hermes route — the same tool
  surface a cron/main Hermes session gets.
- **`--wake-tools` (CLI) / `wake_tools` (plugin + MCP trigger_add).**
  Comma-separated names grant exactly that subset; `none` opts back into
  the restricted alert-only posture (the security knob for high-volume or
  untrusted inbound feeds). Rejected for non-hermes wakes. The explicit
  choice is recorded in the trigger's wake cfg so re-adds keep it.
- **doctor** reports each hermes route's effective state: parity /
  `toolsets override: …` / restricted (explicit), and WARNs on key-less
  routes registered by pre-0.2.4 galvanize.
- **Migration.** `galvanize doctor --fix` (and `daemon install`) refresh
  managed key-less routes to parity, honoring recorded choices and
  leaving manual grants untouched. Only the `toolsets` key is rewritten.

Earlier releases: see git history (`v0.2.3` = himalaya secret import +
doctor drift warnings; `v0.2.2` = secrets/health hardening).
