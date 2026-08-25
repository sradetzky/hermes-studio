# Code Quality Remediation Implementation Plan

> **For Hermes:** Execute this plan directly, one verified slice at a time. Use
> read-only reviewers where useful, but do not delegate the large refactor itself.
> Do not start Phase 6 additions until the remediation gate below is complete.

**Goal:** Remove the verified correctness, reproducibility, ownership, and
maintainability blockers before extending Hermes Studio with new interactions or
generation inputs.

**Architecture:** Establish one dependency-neutral `studio_core` layer used by
both the webapp and CLI, keep scheduling separate from execution adapters, and
move browser behavior into controllers that own their state. Preserve the
existing descriptor-safe filesystem, transactional SQLite, immutable generation,
and sequential-GPU guarantees.

**Tech stack:** Python 3.11+, FastAPI, SQLite, vanilla ES modules, Node test
runner, raw CDP Chromium coverage, FFmpeg, Hermes Agent, ComfyUI, pinned
`mcporter`/`comfyui-mcp` transport.

---

## 1. Why this work blocks new features

The 2026-08-24 thermo-nuclear review denied structural approval for Phase 6.
The application is behaviorally strong, but the next features would otherwise
add special cases to already oversized and bidirectionally coupled modules.

Audit snapshot:

- `scripts/design_studio.py`: 3,276 lines; CLI, migration engine, project domain,
  profile dispatch, generation archival, and render entry points.
- `webapp/studio_manager.py`: 1,043 lines; scheduling, process supervision,
  recovery, agent transport, generation command construction, movie execution,
  verification, and cleanup.
- `webapp/static/app.js`: 1,034 lines over a shared singleton containing 43
  mutable fields; extracted modules still mutate that singleton.
- Documented Python discovery ran 213 tests while two MCP submission tests in
  `tests/mcp_submit_cases.py` were silently omitted. Explicit `*_cases.py`
  discovery ran 215 tests. Frontend/Chromium ran 18 tests.
- A profile-isolated environment made `webapp.config.Settings` resolve both the
  Studio state database and ComfyUI output incorrectly; shell fleet tools used a
  third interpretation of the same environment variables.
- `pip-audit` was not installed in the project environment.

No item below may weaken these established strengths:

- descriptor-based, symlink-safe filesystem access;
- project locks and SQLite uniqueness/transaction guarantees;
- immutable generation/movie contracts and provenance;
- exact Hermes job/session correlation and stale-response rejection;
- parent-death supervision, PID identity checks, lease recovery, and proven
  termination before releasing ownership;
- stable media element identity and sequential GPU execution.

`webapp/safe_files.py` is large but cohesive and is **not** a split target.

---

## 2. Mandatory order and approval gate

Work in this order:

1. **P0 — Correctness and reproducibility fixes.**
2. **P1 — Canonical runtime and orchestration boundaries.**
3. **P2 — Browser ownership and existing scroll defect.**
4. **Remediation release gate.**
5. **P3 — Phase 6 additions:** interactive `clarify`, then previous-take
   continuity.

P3 must not begin while any P0–P2 acceptance criterion is open. The existing
session-output jump/scroll behavior is a defect and belongs to P2, not the new
feature wave.

### Slice protocol

Every implementation slice must:

1. Add or strengthen a focused regression that fails for the intended reason.
2. Run that focused test and record the expected failure.
3. Make the smallest architectural change that establishes one canonical owner.
4. Run the focused test, the directly affected suite, and `git diff --check`.
5. Update this plan, `PLAN.md`, `AGENTS.md`, the affected detailed document, and
   `CHANGELOG.md` when user-visible behavior changes.
6. Commit the verified slice; never push unless explicitly requested.

Avoid large rename-only commits mixed with behavior changes. Preserve public CLI
commands while moving their implementations.

---

## 3. P0 — Correctness and reproducibility fixes

### P0.1 Make test discovery complete and canonical

**Status (2026-08-25): complete.** Standard discovery now includes the renamed
MCP module and its ordered-reference/failure coverage. `scripts/check.sh` is the
single clean-commit entry point for local non-GPU checks; P0.3 will extend its
dependency section with the measured audit/version contract.

**Problem:** The advertised Python command omits the MCP submission tests because
`mcp_submit_cases.py` does not match `test_*.py` and is not imported by a wrapper.
The test claiming serial uploads uses one reference and does not prove ordering.

**Files:**

- Rename: `tests/mcp_submit_cases.py` → `tests/test_mcp_submit.py`
- Modify or remove after all cases are discoverable: `tests/test_webapp.py`,
  `tests/test_studio.py`
- Modify: `README.md`
- Create later in this slice: `scripts/check.sh`

**Work:**

- Make one standard discovery command execute every Python test module.
- Remove manual test aggregation once it is redundant.
- Add ordered two-reference upload/substitution coverage.
- Add prompt-hash mismatch, settings revision mismatch, failed upload, failed
  batch result, and duplicate-reservation coverage.
- Make `scripts/check.sh` the executable source of truth for Python, frontend,
  compilation, JavaScript syntax, dependency, profile-drift, CSS, archive, and
  checksum gates. Keep live GPU/browser/service gates explicit if they cannot be
  safely run on every invocation.

**Acceptance:**

- `.venv/bin/python -m unittest discover -s tests` discovers every test without
  a second pattern or wrapper import.
- The MCP module appears in verbose discovery output.
- A deliberately failing MCP test makes the canonical command fail.
- README and release documentation call only the canonical gate.

### P0.2 Establish one profile-safe path model

**Status (2026-08-25): complete.** `studio_core.paths` now owns account, fleet,
active-profile, and ComfyUI roots for the CLI, webapp, profile sync, and model
switcher. Normal/profile-isolated matrices, literal-value preservation, exact
state DB resolution, sync targeting, and switch failure/reporting are covered.

**Problem:** `HERMES_HOME`, `HERMES_REAL_HOME`, account home, fleet root, active
profile root, and ComfyUI root are independently inferred in Python and shell.
This recreates the path bug already repaired in `design_studio.py`.

**Files:**

- Create: `studio_core/__init__.py`, `studio_core/paths.py`
- Modify: `webapp/config.py`, `scripts/design_studio.py`
- Modify: `scripts/sync-profiles.sh`, `scripts/switch-model.sh`
- Test: new `tests/test_paths.py` plus affected script/config tests

**Canonical model:**

- `real_home`: account resources such as `~/ComfyUI`.
- `hermes_root`: fleet root containing default state and `profiles/`.
- `active_profile_home`: current profile's skills/config/state.
- `comfy_root`: explicit `$COMFYUI_PATH` or `real_home/ComfyUI`.

Do not derive `hermes_root` by blindly appending `profiles/` to
`$HERMES_HOME`. Shell tools should call the same resolver or consume the same
explicit values. Preserve literal environment values rather than normalizing
malformed input into a different target.

**Acceptance:**

- Normal account and profile-isolated test matrices resolve the same fleet and
  ComfyUI resources.
- `Settings.profile_state_path("studio")` resolves one exact profile state DB.
- `sync-profiles.sh --check` inspects the fleet root, not a nested
  `profiles/studio/profiles/studio` path.
- `switch-model.sh` fails when no intended profile can be updated and reports the
  exact changed profiles.
- No runtime path uses `Path.home()` when the resource belongs to `real_home`.

### P0.3 Make installation and dependency gates reproducible

**Status (2026-08-25): complete.** A stable user launcher now binds the service
to the installing checkout and is verified after installation. The canonical
gate verifies a complete Python lock, installed consistency, `pip-audit`, the
minimum supported Hermes CLI behavior, exact MCP package pins, and an incremental
Ruff correctness baseline. The initial full default Ruff scan measured 263
pre-existing findings; broad style cleanup remains deliberately separate.

**Problems:** The systemd unit assumes `%h/repos/hermes-studio`; release checks
are spread across prose; transitive Python/Node and Hermes/external tool versions
are not represented by one verified contract; no root static-analysis baseline
exists; `pip-audit` was unavailable during review.

**Files:**

- Modify/template: `webapp/hermes-studio.service`
- Create: `scripts/install-web-service.sh` or equivalent minimal renderer
- Modify: `requirements*.txt`, `README.md`, relevant pipeline docs
- Create only if justified by the chosen tools: `pyproject.toml`
- Modify: `scripts/check.sh`

**Work:**

- Render/install the user service from the actual checkout path, or install one
  stable launcher in `~/.local/bin`; do not assume the repository location.
- Pin and check the supported Hermes CLI behavior and existing MCP package
  versions at the boundary that relies on them.
- Add `pip-audit` to the documented development/release environment and run it
  through the canonical gate.
- Lock reproducible dependency inputs without introducing a new package manager
  unless it materially simplifies the current setup.
- Add an incremental `ruff`/type-checking baseline only after measuring the
  existing violations. Do not combine mass formatting with architecture work and
  do not claim whole-project typing while untyped boundaries remain.

**Acceptance:**

- A clone at a different absolute path installs a service whose `ExecStart`
  targets that checkout or the stable launcher.
- One command runs every local non-GPU release check and propagates any failure.
- Unsupported Hermes/MCP versions fail with an actionable message instead of
  drifting silently.
- Dependency audit runs in the project development environment.

### P0.4 Decide and document runtime event retention

**Status (2026-08-25): complete.** Read-only measurement of the running preview
database found 1,348 events across 16 finished jobs in an 843,776-byte database.
Event storage projected to about 40 MiB per 1,000 similarly active jobs. The
explicit preview policy is therefore durable retention with measured revisit
triggers, not speculative pruning; see `docs/runtime-event-retention.md`.

**Problem:** `job_events` is append-only with no explicit retention policy.
This is not yet a proven production defect, so do not add speculative pruning.

**Files:** `webapp/job_store.py`, `webapp/runtime_schema.py`, operational docs;
only modify code if measurements justify it.

**Work and acceptance:**

- Measure event volume and database size on representative completed jobs.
- Document either an intentional durable-retention policy or a transaction-safe
  bounded policy that preserves active jobs, transcript exports, and audit needs.
- If pruning is implemented, add migration and concurrent-reader tests.

---

## 4. P1 — Canonical runtime and orchestration boundaries

### P1.1 Extract the one-off migration engine

**Status (2026-08-25): complete.** The 2,077-line migration implementation now
lives in `studio_core.migration` and is imported lazily only by the migration CLI
branch. Its four case suites are directly discoverable, all race/recovery cases
remain green, and the cohesive safe-file implementation moved to
`studio_core.safe_files` so the extracted engine has no `webapp` dependency.

**Problem:** Roughly 2,000 lines of legacy clip migration code are imported on
every normal CLI and webapp use.

**Files:**

- Create: `studio_core/migration.py`
- Modify: `scripts/design_studio.py`
- Test: existing migration cases, renamed into discoverable test modules

**Work:** Move the migration state machine and its private helpers without
changing CLI arguments, dry-run/apply semantics, output, locks, or safe-file
behavior. The CLI keeps only parsing and presentation.

**Acceptance:** Existing migration fixtures and malformed/symlink/race cases pass;
normal webapp imports no longer import migration implementation transitively.

### P1.2 Create a dependency-neutral Studio core

**Status (2026-08-25): complete.** Project/clip/prompt operations,
authoritative generation contracts and archival, profile dispatch, runtime
models/schema/persistence, identifiers, and Hermes event projection now have
canonical `studio_core` ownership. Web modules retain compatibility aliases for
the old import paths, the movie CLI moved under its web runner owner, and static
tests reject both `studio_core -> webapp/scripts` and `scripts -> webapp` edges.

**Problem:** The webapp imports `scripts.design_studio` as a domain layer while
that script imports `webapp.*`, creating bidirectional layering.

**Target modules:**

- `studio_core/projects.py` — project/clip/prompt domain operations.
- `studio_core/generation_archive.py` — authoritative ComfyUI history traversal,
  archive validation, and publication contract.
- `studio_core/dispatch.py` — profile/session command construction that is not a
  web scheduler concern.
- `studio_core/paths.py` — canonical roots from P0.2.

**Modify:** `scripts/design_studio.py`, the movie runner, `webapp/app.py`,
`webapp/routes.py`, `webapp/studio_manager.py`, and imports/tests that target the
old ownership.

**Rules:**

- `studio_core` must not import `webapp` or `scripts`.
- `scripts/*` and `webapp/*` may import `studio_core`.
- FastAPI routes remain HTTP adapters; CLI remains argument/output adapter.
- Do not redesign domain behavior during moves.

**Acceptance:** An import-graph test or static check rejects `studio_core ->
webapp/scripts` and `scripts -> webapp` dependencies; all existing CLI and web
contracts remain unchanged.

### P1.3 Remove the LLM from deterministic generation execution

**Status (2026-08-25): complete.** Web generation now starts a supervised
deterministic worker rather than `hermes chat`. The worker loads the immutable
running-job contract, builds the H3 graph with the profile runner in dry-run
mode, uploads ordered references, submits and waits through the pinned MCP
service, selects the exact `SaveVideo` output, archives through authoritative
history validation, and always performs direct queue/VRAM cleanup. Hermes is no
longer charged for deterministic render choreography.

**Problem:** The generation contract is deterministic, but the webapp still
launches Hermes so a model can execute exact graph-build and MCP-submit commands,
poll, archive, and clean up.

**Files:**

- Create: `webapp/generation_runner.py`
- Move/reuse: `scripts/submit_h3_graph_mcp.py` behind a canonical submission
  service, using `webapp.safe_files` or dependency-neutral equivalent rather than
  bespoke check-then-read operations.
- Modify: `webapp/studio_manager.py`
- Test: generation contract, process, MCP submission, history, archive, recovery,
  and cleanup suites

**Work:** `GenerationJobRunner` directly executes graph build, serial uploads,
submission, two-second status polling, authoritative archive verification, and
mandatory cleanup from the immutable job contract. Hermes remains responsible
for creative planning and prompt writing, not deterministic shell choreography.

**Acceptance:**

- Generation does not start `hermes chat`.
- Exact graph bytes and ordered references reach the pinned MCP boundary without
  model context/transcription.
- Subprocess exit zero cannot complete a job without matching artifact,
  prompt ID, prompt/settings snapshot, history, and provenance.
- Cancellation/recovery preserves exact queue and VRAM ownership rules.

### P1.4 Split scheduling from job execution

**Status (2026-08-25): complete.** `StudioJobManager` is now a 475-line FIFO,
lease, lifecycle, and dispatch owner. One `JobRunner` protocol and dispatch table
select agent, generation, or movie execution. `SupervisedProcessRunner` is the
single owner of process groups, PID/start-time identity, timeout communication,
termination, `/proc` recovery, and parent-death supervision. Agent session/event
bridging lives in `agent_runner.py`; deterministic generation and movie lifecycle
handling live beside their transactions. Structural tests enforce the ownership,
the 1,000-line ceiling, and the GPU-cleanup boundary.

**Problem:** `StudioJobManager` owns unrelated scheduling and execution domains.

**Target shape:**

- `webapp/studio_manager.py` — FIFO ownership, leases, worker lifecycle, dispatch.
- `webapp/process_runner.py` — supervised process groups, PID identity,
  termination and timeout behavior.
- `webapp/agent_runner.py` — scoped Studio/specialist chat execution and event
  bridging.
- `webapp/generation_runner.py` — P1.3 deterministic generation transaction.
- `webapp/movie_runner.py` — deterministic movie assembly transaction.

**Acceptance:**

- One dispatch table selects a typed runner; no repeated `if job.kind` lifecycle
  trees.
- Recovery uses one process-ownership implementation.
- CPU movie jobs never invoke ComfyUI cleanup; generation jobs always do.
- No extracted handwritten manager/controller exceeds 1,000 lines; lower counts
  are an outcome, not a reason to split cohesive safety code.

### P1.5 Type job and persistence boundaries

**Status (2026-08-25): complete.** `studio_core.job_contracts` now owns
discriminated job kinds, chat scopes, activity phases/event types, and typed
chat/generation/movie payload codecs. `JobStore` validates combinations and
decodes payloads at enqueue and row-read boundaries, so runners receive domain
payloads rather than reparsing SQLite text. Ordered schema migration 6 rejects
unknown or mismatched kind/scope writes without rewriting stable rows. Route
contract coverage now proves `JobManager` declares every submission method.

**Problem:** Raw `kind`, `scope`, status/event strings and nested dictionaries
allow runtime drift; the current `JobManager` protocol does not describe all
route usage.

**Files:** `webapp/models.py`, `webapp/app.py`, `webapp/job_store.py`,
`webapp/runtime_schema.py`, runners and route tests.

**Work:** Introduce discriminated job kinds, chat scopes, phases, and per-kind
payload codecs. Keep SQLite rows and JSON dictionaries at explicit adapters.
Add ordered schema migrations rather than opportunistic `PRAGMA table_info`
branches for new state. Do not migrate stable persisted values gratuitously.

**Acceptance:** Invalid kind/scope/payload combinations fail at enqueue/read
boundaries; runners receive validated domain payloads; the manager protocol
matches every route call.

---

## 5. P2 — Browser ownership and existing defects

### P2.1 Extract the conversation controller and fix scroll anchoring

**Status (2026-08-25): complete.** `conversation-controller.js` now owns scoped
chat revisions/cursors, jobs, activity, and viewport policy. Activity cards and
event rows reconcile by persisted identity without replacing existing details.
Deferred real-Chromium updates prove initial and follow-latest pinning, deliberate
scroll-away preservation, and stable open state, nested scroll, focus/selection,
and node identity.

**Problems:** `app.js` coordinates chat, activity, jobs, navigation and refresh
planes over shared mutable state. `renderJobActivity` replaces activity subtrees,
losing nested open/scroll state and changing outer scroll height. Appending a new
response does not have one authoritative follow-latest rule.

**Files:**

- Create: `webapp/static/conversation-controller.js`
- Modify: `webapp/static/app.js`, `webapp/static/shared.js`
- Test: frontend DOM contracts and real Chromium tests

**Work:**

- Give the conversation controller ownership of chat scope, cursors, jobs,
  activity, future clarify state, and viewport anchoring.
- Reconcile activity rows/cards by stable job/event identity rather than
  replacing the subtree.
- Preserve `<details>` open state, nested activity scroll, outer transcript
  position, focus, and selection.
- Define follow-latest behavior: if the relevant output subwindow is following
  the end, append and remain pinned to `scrollHeight`; if the user intentionally
  scrolled away, preserve their position. Initial load of a newly selected
  conversation should land at the end.

**Acceptance:** Real Chromium forces deferred chat/activity updates and proves
new session output is visible, user scroll-away is respected, and nested state is
not reset. This completes the existing auto-scroll defect before Phase 6.

### P2.2 Give references and media explicit controller ownership

**Status (2026-08-25): complete.** `reference-controller.js` now owns reference
signatures, guarded uploads, rendering, and refresh. Media review, generation
settings, project-dialog, refresh-coordinator, and queue-request flags are local
to their owning modules. `shared.js` exposes only workspace identity/context;
the existing stale-dialog, stable-playback, upload, and settings paths remain
covered by frontend and real-Chromium tests.

**Files:**

- Create: `webapp/static/reference-controller.js`
- Keep/refine: `webapp/static/media-review.js`,
  `webapp/static/generation-settings.js`
- Modify: `webapp/static/app.js`, `webapp/static/shared.js`

**Work:** Move uploads, static reference refresh, readiness display, and future
derived-input eligibility behind a reference controller. Keep media dialog and
settings state local to their modules instead of mutating the global singleton.
Keep `app.js` as bootstrap/navigation wiring; do not add a frontend framework.

**Acceptance:** Shared state contains only workspace identity/context and no
module-owned dialog or operation flags. Existing stale-response, playback,
upload, and generation-settings tests remain green.

### P2.3 Split the browser regression harness from scenarios

**Status (2026-08-25): complete.** `frontend_browser_harness.mjs` now owns
fixture-app startup, isolated Chromium/CDP lifecycle, interception helpers,
runtime-error collection, diagnostics, and cleanup. Five independently named
tests cover media playback, stale dialogs, stale chat, latest-request queue
sequencing, and conversation scrolling/stable-node behavior.

**Problem:** `tests/test_frontend_browser.mjs` contains one large scenario,
making failures hard to isolate and Phase 6 coverage risky to extend.

**Files:** Extract a reusable CDP fixture/harness and separate tests for dialogs,
stale chat, queue sequencing, playback, scrolling, and later clarify.

**Acceptance:** Scenarios run independently, preserve the same behavioral
coverage, and a failure names the affected contract instead of one mega-test.

---

## 6. Remediation release gate

**Status (2026-08-25): complete.** The canonical gate passed from clean,
committed source after one live-data defect was found and fixed: a completed
pre-contract generation row used the exact legacy three-field request metadata,
so strict schema-6 decoding made that project's jobs endpoint return HTTP 500.
The persistence adapter now recognizes only that exact legacy shape and only for
terminal generation jobs; active jobs and arbitrary malformed historical
payloads still fail closed. The regression is covered at the store/list boundary.

The final verification included complete Python discovery, 24 frontend
contract/DOM/Chromium tests, compilation, Ruff correctness lint, dependency
lock/audit, supported Hermes and pinned MCP contracts, profile drift,
reproducible CSS, clean source archive/checksum, and repository integrity. The
restarted user service returned HTTP 200 for project, movie, jobs, and ComfyUI
queue APIs across both live projects (16 historical jobs total), with no active
Studio or ComfyUI work. Raw-CDP smoke checks passed at 1440×900 and 390×844 with
zero horizontal overflow and correct narrow-pane visibility/inert state.

P0–P2 are complete only when all of the following are true:

- [x] One Python discovery command runs every test, including MCP tests.
- [x] Canonical non-GPU release script passes and propagates failures.
- [x] Normal and profile-isolated path matrices pass for Python and shell tools.
- [x] A checkout-independent systemd installation is verified by read-back.
- [x] The webapp and CLI depend on `studio_core`; dependency direction is one-way.
- [x] Deterministic generation no longer uses an LLM as command executor.
- [x] Scheduler, process, agent, generation, and movie ownership are explicit.
- [x] Job kinds/scopes/phases/payloads are validated at boundaries.
- [x] Conversation activity reconciles stable nodes and the scroll regression is
  covered in real Chromium.
- [x] Shared browser state is reduced to genuine workspace context.
- [x] Runtime event retention is measured and explicitly decided.
- [x] Compilation, dependency/audit, profile drift, CSS, clean archive,
  service/API, desktop, narrow-browser, and checksum gates pass.

The 2026-08-25 thermo-nuclear follow-up closed three post-gate contradictions:
generation and movie payloads now decode into fully validated frozen nested
contracts at the SQLite boundary; only exact generation jobs own ComfyUI cleanup;
and every repo/live Studio profile guard reserves web H3 execution for the
deterministic worker. Negative nested-contract, chat failure/timeout/shutdown,
generation recovery, toolset, and profile-semantic tests enforce those boundaries.

A subsequent maintainability pass removed the eight `sys.modules` compatibility
aliases under `webapp/` after all internal imports and patch targets moved to the
canonical `studio_core` owners. The dependency-boundary suite now asserts those
deprecated module paths remain absent instead of preserving their identity.
The same pass consolidated three identical copies of the disposable web-app
test base, passive manager, concurrent-job helper, and generation-settings
fixture into `tests/webapp_test_support.py`; the route suite now carries only its
genuinely different manager behavior.
Tracemalloc isolated the remaining three SQLite `ResourceWarning` reports to
transaction context managers in one interaction migration test: `sqlite3`
commits on context exit but does not close. Explicit `closing(...)` ownership now
makes the full webapp suite warning-clean under `PYTHONWARNINGS=always::ResourceWarning`.

The final lower-priority follow-up split the remaining manager, route/media, and
project/archive case monoliths so every `*_cases.py` module stays below 1,000
lines, with a static regression ceiling. Canonical Python discovery now runs
through `scripts/run_python_tests.py`, which rejects visible `ResourceWarning`
output and destructor-time unraisable diagnostics after otherwise successful
unittest discovery. The browser asset boundary is explicitly ESM, while the CSS
builder installs an exact npm lock containing Tailwind 3.4.17, Browserslist
4.28.8, and caniuse-lite 1.0.30001810 before rebuilding. The latest available
dataset is therefore reproducible even though the host's future-dated clock
requires disabling Browserslist's age heuristic. Finally, architecture tests
cap the intentionally cohesive `safe_files.py` and lazy migration engine at
1,000 and 2,200 lines respectively so growth forces an ownership review without
splitting either module speculatively.

The final committed-source gate passed after these follow-ups: 279 Python tests
and 28 frontend/Chromium tests, compilation, full Ruff, dependency lock and
audit, external-tool contracts, profile drift, reproducible CSS, source archive,
and repository integrity all succeeded.

The requested lower-priority closure gate then passed from committed source with
284 Python tests and the same 28 frontend/Chromium tests plus every compilation,
lint, dependency/audit, tool, profile, CSS, archive, and integrity check. A fresh
web-owned chained-R2V generation followed: clip-002 generation 002 consumed the
project reference in slot 1 and the exact clip-001 selected-take frame in slot 2,
archived matching typed inputs and source bytes, passed API byte-range read-back,
emitted `comfyui.cleanup`, and left no running or pending ComfyUI work.

The canonical local non-GPU gate is:

```bash
scripts/check.sh
```

Live service/API and GPU generation checks remain explicit so this command cannot
interrupt a running Studio instance or ComfyUI job. `scripts/check.sh` is the
source of truth and must propagate every local check failure.

---

## 7. P3 — New additions, only after the gate

### P3.1 Add interactive Hermes `clarify` transport

**Status: complete (2026-08-25).** Studio chat now uses Hermes' supported TUI
gateway JSON-RPC transport. Schema 7 persists strict scoped interaction contracts;
atomic revision checks guard answers; the browser restores pending requests after
reload; and `clarify.respond` resumes the same supervised turn. Unit, API, fake-
gateway, real-Hermes, DOM, and Chromium reload tests cover the four input modes.
Studio profiles use unlimited Hermes clarify waits under the existing bounded
outer job supervisor; startup rejects configuration drift, and an unexpected
`clarify.expire` closes the exact interaction and fails the run instead of
accepting Hermes' deliberately graceful but non-delivering late RPC response.

**Verified blocker:** A one-shot `hermes chat -Q` subprocess plus read-only
session projection has no response callback, durable pending-input state, or way
to resume the exact waiting job. Active-job rules correctly reject disguising an
answer as another chat job. The event bridge currently strips clarify question
structure.

Before implementation, load the `hermes-agent` skill and verify the current
canonical Hermes callback/gateway API in the official documentation. Do not
scrape terminal output, mutate Hermes session databases, or invent chat-message
control syntax.

**Target shape:**

- Interactive `AgentRun`/`AgentRunner` adapter with a supported clarify callback.
- Revisioned `interaction_requests` persistence keyed by request, job, Hermes
  session, project/clip scope, and profile.
- Structured single-select, multi-select, batch, and free-text payloads.
- Explicit `waiting_for_user` job phase without releasing process/job ownership.
- Dedicated scoped answer endpoint with atomic exactly-once resolution.
- Conversation controller rendering and submitting the pending interaction.
- Unlimited inner clarify waits for dedicated Studio profiles, guarded before
  launch and bounded by the existing job supervisor instead.
- Structured gateway expiration closes pending or just-answered interactions
  and fails the run before a late `clarify.respond` can appear successful.

**Acceptance:** Reload/reconnect shows the exact pending request; stale, duplicate,
wrong-scope, and post-completion answers fail closed; the same waiting run resumes;
all question modes pass backend, DOM, and Chromium tests; no browser-visible
request can outlive the corresponding Hermes wait.

### P3.2 Introduce typed generation inputs and previous-take continuity

**Status: complete.** Generation contract schema 2 replaces new filename-only
execution snapshots with ordered discriminated inputs while retaining strict
read compatibility for terminal schema-1 history. The backend resolves the
immediately preceding enabled clip, opens its exact selected video, extracts the
last frame at `-0.250s`, and snapshots source and derived SHA-256 identities under
the project/job coordination lock. Worker-start and pre-submit validation reject
order, enablement, selection, deletion, project-reference, source-byte, or
derived-byte drift.

**Thermo-nuclear follow-up (2026-08-25): complete.** One typed generation job
service now owns immutable contract creation, worker-start revalidation, and
archive postcondition verification; `StudioJobManager` retains only coordinated
submission and dispatch. Input snapshots remain discriminated contract objects
through the domain instead of round-tripping through magic dictionaries. Mode
compatibility is checked before frame extraction, so non-R2V continuity requests
return an intentional HTTP 409 without leaving a derived artifact, and the
browser disables the still-visible eligible option until R2V is selected.

**Verified blocker:** `list[str]` prompt-parsed filenames assume every input lives
under project `references/`. A derived previous-take frame needs source clip,
order, selected generation/file identity, video hash, extraction point, derived
image hash, slot/order, and invalidation semantics.

**Target shape:** A discriminated ordered `GenerationInput` union:

- `project_reference`
- `previous_selected_take_last_frame`

Resolve eligibility and materialize the derived frame under the project lock when
creating the immutable contract. The backend, not the browser, determines the
immediately preceding enabled/ordered clip and selected video identity. Snapshot
source and derived hashes into execution and archive provenance.

**Acceptance:** The checkbox appears only when eligible; reorder, disable,
selection change, deletion, or byte change invalidates stale readiness/contracts;
queue execution uses the exact materialized frame in declared order; archived
metadata identifies both source video and derived frame; concurrent mutation
fails closed. Backend extraction tests prove the final blue frame is selected from
a red-to-blue source, API tests prove immutable materialization/provenance, and a
real Chromium scenario proves the eligible checkbox submits the exact typed flag.

**Live acceptance (2026-08-25): complete.** The final committed-source gate
passed 278 Python tests and 28 frontend/Chromium tests plus compilation, Ruff,
dependency/audit, external-tool, profile-drift, CSS, and repository-integrity
checks. A real two-clip R2V checkpoint then rendered clip 2 at 928×544 with the
project reference in slot 1 and the exact selected clip-1 final frame in slot 2.
The immutable contract, archived metadata, source-video hash, derived-frame hash,
prompt hash, and source/archive video bytes matched; ComfyUI finished with an
empty queue and the generation-owned cleanup event.

---

## 8. Traceability: every review finding

| Review finding | Priority | Owning work |
|---|---:|---|
| Deterministic generation still LLM-mediated | Blocker | P1.3 |
| MCP submission tests omitted by discovery | Blocker | P0.1 |
| Profile/fleet/real-home paths disagree | Blocker | P0.2 |
| No viable web-answerable clarify protocol | New-wave blocker | P3.1 after gate |
| Filename-only references cannot carry derived provenance | New-wave blocker | P3.2 after gate |
| 1,034-line browser controller and mutable singleton | New-wave blocker | P2.1–P2.2 |
| Bidirectional `webapp`/`scripts` dependency | Blocker | P1.1–P1.2 |
| 1,043-line manager owns scheduling and every executor | Blocker | P1.3–P1.5 |
| 2,000-line migration imported at runtime | Major | P1.1 |
| Raw job/scope/event strings and dictionaries | Major | P1.5 |
| One 286-line Chromium scenario | Major | P2.3 |
| Activity subtree replacement loses nested state | Existing defect | P2.1 |
| No single reproducible release command | Major | P0.1/P0.3 |
| Hard-coded checkout path in user service | Major | P0.3 |
| Incomplete dependency/tool version contract | Major | P0.3 |
| No incremental root lint/type baseline | Major | P0.3 |
| Append-only event store lacks stated retention | Recommendation | P0.4 |
| `safe_files.py` is large but cohesive | Preserve | No split |

---

## 9. Suggested verified commit slices

1. `test: make every Studio test discoverable`
2. `fix: centralize Studio runtime paths`
3. `build: make Studio checks and service setup reproducible`
4. `refactor: isolate legacy clip migration`
5. `refactor: establish dependency-neutral Studio core`
6. `refactor: execute generation without an agent shell proxy`
7. `refactor: split Studio job runners`
8. `refactor: type Studio job boundaries`
9. `fix: preserve live conversation viewport state`
10. `refactor: give frontend panels local state ownership`
11. `test: split behavioral browser scenarios`
12. `feat: answer Hermes clarify requests in scoped chat`
13. `feat: use previous selected take as a typed generation input`

Re-run the remediation release gate after slices 3, 8, 11, and before either
feature commit. Stop and update this document if implementation reveals a
materially different canonical boundary.

---

## 10. Second thermo-nuclear closure gate

A fresh codebase-level review on 2026-08-25 found four remaining ownership seams.
They are being closed in this order before another feature wave:

1. **Typed movie execution — complete.** `studio_core.movie_contracts` now owns
   canonical contract construction and assembly policy. `MovieStore` and
   `MovieJobRunner` retain `MovieContract` objects through export and verification;
   serialization occurs only at persistence/provenance boundaries. The supervised
   worker reloads the exact persisted running job instead of accepting an entire
   contract through process arguments.
2. **Explicit generation archive context — complete.** The worker loads the
   exact running job once through `JobStore` and passes an immutable archive
   context containing the typed contract, target, prompt identity, job identity,
   and ComfyUI endpoint. Archival no longer reads SQLite or ambient job variables,
   and graph construction receives the exact validated input paths, including
   clip-local derived continuity frames.
3. **Unified specialist dispatch lifecycle — pending.**
4. **Single coordinated generation preflight owner — pending.**

Each item follows the slice protocol in section 2. Run the complete committed-
source gate after all four are verified.