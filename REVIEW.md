# REVIEW.md — how to review a change in this repo

Fork context: [FORK.md](FORK.md). Contributor rules: [AGENTS.md](AGENTS.md).

## Severity
- **Blocker** — wrong behavior, data loss, a security hole, student data or a business/staff name in the public repo, a gating or completion rule bypassed (below), a test weakened to pass.
- **Important** — works but fragile: missing test for new behavior, unhandled error path, accessibility regression, diff departs from the issue without saying so, an upstream file edited without a `Rescue Ready fork:` marker.
- **Nit** — naming, style, small simplifications. At most 5 per review; drop the rest.

## Check every time
1. Does the diff do what the issue's acceptance criteria say, no more, no less?
2. Is there a test that would fail if this change were reverted? For a bug fix: did the failing test exist before the fix?
3. Did any existing test change? If yes, which acceptance criterion required it?
4. Does this change make `AGENTS.md`, `FORK.md` or `README.md` wrong? Say which line.

## Standing rules for this repo
- PR targets `kr8vka0z/learnhouse` `main`, from a `feat/rr-*` branch. Never upstream.
- Public-repo hygiene: no business names beyond "Rescue Ready fork", no staff names, internal URLs, secrets or student data, in code, comments, fixtures, commit messages or PR text.
- Rebase-friendly: additive over rewriting upstream functions; every edit to an upstream file is small and marked `Rescue Ready fork:` with the why.
- No schema or ENUM changes when a JSON/JSONB field will do. A migration must work on a fresh database (`create_all`) and an existing one (`alembic stamp heads` + `upgrade`), form one linear chain off the upstream head, and be called out loudly in the PR.
- Tests cover risky behavior; no wording or CSS assertions. Test count before and after is reported.
- OSS mode only: no faked EE mode, no `EE_ONLY_FEATURES` edits, no analytics or time-on-task tracking.

## Fork-specific focus
- **Gating.** Any way around sequential chapter gating must go through the shared helper in `apps/api/src/services/courses/sequential.py`. Flag a second, hand-rolled lock check, or a read path (chapters, activities, AI/RAG access) that skips it.
- **Completion.** Marking an assignment activity complete must go through `_rr_sync_assignment_completion` in `services/courses/activities/assignments.py`. Flag any code that sets `TrailStep.complete` for an assignment directly, and any path where a failed attempt completes the activity.
- **Audit records are the official record.** Don't drop, rewrite or silently swallow submission/grade audit events or per-attempt history; changes to them need a test. Check that report times still go through the report time-zone setting.

## Skip
Generated files, lockfiles, vendored code, anything CI-style checks enforce (formatting, lint, types).
