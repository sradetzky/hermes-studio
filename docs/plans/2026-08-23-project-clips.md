# Project Clip Hierarchy Implementation Plan

> **For Hermes:** Implement this plan slice-by-slice with focused tests and a verified commit after each completed slice.

**Goal:** Replace the single project-level prompt/generation workflow with an ordered Project → Clips → Takes hierarchy that can later assemble selected video takes into a final product.

**Architecture:** `project.json` atomically owns immutable clip IDs, display titles, order, enabled state, and one optional selected take per clip. Project chat, references, research, and final exports stay shared; each clip owns its current prompt, generation settings, and immutable generation/take archive. Web routes and agent jobs carry exact project and clip IDs; no component guesses or fuzzily resolves paths.

**Tech stack:** Python 3, FastAPI/Pydantic, SQLite, vanilla ES modules, local Tailwind bundle, unittest, Chromium CDP.

---

## Locked contract

```text
projects/<project-id>/
  project.json
  brief.md
  chat.jsonl
  references/
  research/
  clips/
    clip-001/
      current_prompt.txt
      current_generation.json
      generations/
        001/
          <media>
          prompt.txt
          settings.json
          meta.json
  final/
```

- Code/storage calls the child entity `clip`; scene remains a title/grouping concept.
- `project.json` schema 1 contains ordered clip entries: `id`, `title`, `enabled`, and optional `selected_take` (`generation`, `filename`).
- Clip IDs are immutable (`clip-001`, `clip-002`, …); array order controls assembly order.
- Project references and chat remain shared.
- A take is one generation directory under one clip.
- Selecting a take records provenance; it does not copy media.
- Existing promote/export and use-as-reference actions remain distinct.
- Migration is explicit, dry-runnable, resumable, and never silently triggered.
- Actual ffmpeg stitching is outside this change.

## Task 1: Filesystem clip contract and project manifest

**Files:**
- Create: `webapp/clip_store.py`
- Modify: `scripts/design_studio.py`
- Test: `tests/test_studio.py`

**Steps:**
1. Add failing tests for new-project manifest/default clip, exact clip resolution, creation, rename, enable/disable, reorder, take selection, traversal/symlink rejection, and atomic manifest publication.
2. Implement a single resolver/store used by both CLI and web code.
3. Make `create_project()` create `project.json`, `clips/clip-001/`, an empty prompt, and `generations/`.
4. Change `write_prompt()`, `next_generation_dir()`, and `archive_outputs()` to require an exact clip ID.
5. Archive `settings.json` alongside each prompt and metadata snapshot.
6. Add CLI commands for list/create/update/reorder/select clips.
7. Run `python -m unittest tests.test_studio` and commit.

## Task 2: Explicit legacy migration

**Files:**
- Modify: `scripts/design_studio.py`
- Test: `tests/test_studio.py`

**Steps:**
1. Add failing tests for dry-run, apply, idempotency, interrupted-journal resume, unsafe path refusal, and preservation of prompts/settings/generation media/review metadata.
2. Add `migrate-clips --dry-run|--apply [project-id]`.
3. Under a project lock, journal and same-filesystem rename root prompt/settings/generations into `clips/clip-001/`, then atomically publish `project.json`.
4. Refuse migration while project jobs are active at the operational call site.
5. Verify counts, sizes, and hashes before removing the journal.
6. Run focused tests and commit.

## Task 3: Clip-scoped runtime jobs and agent context

**Files:**
- Modify: `webapp/models.py`
- Modify: `webapp/job_store.py`
- Modify: `webapp/studio_manager.py`
- Modify: `webapp/routes.py`
- Test: `tests/test_webapp.py`

**Steps:**
1. Add failing tests for persisted `clip_id`, schema migration, project-level chat continuity, and exact active-clip agent context.
2. Add a non-null `clip_id` to new jobs with a backward-compatible SQLite column default for historical jobs.
3. Require clip ID on new chat submissions and validate it through the shared clip resolver.
4. Prefix the Hermes query with exact project/clip IDs and paths; expose `HERMES_STUDIO_CLIP` and `HERMES_STUDIO_CLIP_PATH`.
5. Preserve one active project job and one global GPU-running job invariants.
6. Run job/concurrency/lifecycle tests and commit.

## Task 4: Nested clip/take APIs and stores

**Files:**
- Modify: `webapp/routes.py`
- Modify: `webapp/generation_settings_store.py`
- Modify: `webapp/media_review_store.py`
- Modify: `webapp/reference_store.py` only if resolver plumbing requires it
- Test: `tests/test_webapp.py`

**Steps:**
1. Add failing API tests for list/create/update/reorder clips and clip detail.
2. Add clip-scoped generation-settings and generations/takes endpoints.
3. Keep reference upload/listing project-scoped; make settings readiness resolve prompt references against the project reference directory.
4. Make media review resolve `project/clips/<clip>/generations/<take>` and emit nested guarded URLs.
5. Add select-take endpoint validating an exact supported media file.
6. Keep export-to-final and use-as-reference transactional/idempotent behavior.
7. Replace project-level prompt/settings/generation endpoints rather than maintaining a long-lived dual layout.
8. Run API/security/concurrency tests and commit.

## Task 5: Project → clip frontend

**Files:**
- Modify: `webapp/static/index.html`
- Modify: `webapp/static/shared.js`
- Modify: `webapp/static/app.js`
- Modify: `webapp/static/generation-settings.js`
- Modify: `webapp/static/media-review.js`
- Modify: `webapp/styles.css`
- Rebuild: `webapp/static/studio.css`

**Steps:**
1. Add nested clip buttons beneath each active project plus Add, Rename, Up/Down, and Enable controls; omit permanent deletion.
2. Track `state.currentClip`; reset clip-scoped state safely on project/clip switches.
3. Scope prompt/readiness/settings/take polling to the exact active clip.
4. Rename the generation panel to Takes and show selected-take badges.
5. Add `Select take for clip`; retain Export to final and Use as reference.
6. Show the active clip in the composer and submit `clip_id` with every chat job.
7. Preserve stable media DOM/playback and dialog focus behavior.
8. Rebuild CSS, run JS syntax checks, and commit.

## Task 6: Migration and documentation

**Files:**
- Modify: `PLAN.md`, `AGENTS.md`, `README.md`, `CHANGELOG.md`
- Modify: `docs/agents.md`, `docs/frontend-plan.md`, `docs/studio-cli.md`
- Modify: `hermes/skills/design-studio/SKILL.md`
- Modify relevant Studio profile SOULs

**Steps:**
1. Stop the web app only after confirming no active job.
2. Run migration dry-run against every current project and review exact operations.
3. Apply migration; programmatically verify file counts, sizes, hashes, manifests, selected clip defaults, and no leftover journals.
4. Update docs and skills to the clip-aware contract and CLI.
5. Synchronize profiles and verify zero drift.
6. Restart the web app and verify live nested APIs.
7. Commit.

## Task 7: Final verification

1. Run `python -m unittest tests.test_webapp tests.test_studio`.
2. Run Python compile checks, JS syntax checks, CSS build, `git diff --check`, and profile drift check.
3. Exercise desktop and 390px layouts in real Chromium over HTTP: project/clip switching, prompt/settings/takes isolation, clip creation/reorder/rename/disable, chat payload, take selection, export/reference actions, focus, and polling stability.
4. Read back every state-changing API/file target before reporting success.
5. Verify live app status, clean Git status, and no push.

## Acceptance criteria

- Every project has at least one ordered clip.
- Switching clips cannot leak prompt, settings, or takes between clips.
- Project chat and references remain shared.
- Every new job persists and receives an exact clip ID/path.
- Every take archives prompt, settings, metadata, and media beneath exactly one clip.
- One valid video take may be selected per enabled clip without copying it.
- Existing project data migrates without byte loss or review-history loss.
- Traversal, symlink, collision, concurrency, and interrupted-publication protections remain intact.
- Desktop/narrow browser QA and the full test suite pass.
