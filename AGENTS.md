# AGENTS.md

Upstream LearnHouse has no `AGENTS.md`, so this file is entirely the Rescue Ready fork's. Background: [FORK.md](FORK.md). Reviewing a change? Read [REVIEW.md](REVIEW.md).

## Rescue Ready fork

Stack: `apps/web` (Next.js/React), `apps/api` (FastAPI, SQLModel, Alembic, Postgres, Redis). OSS mode only: no `ee/` folders, no faking EE mode, don't edit `EE_ONLY_FEATURES`. Scope is module completion records and white-label branding (see FORK.md); no time-on-task tracking, don't touch Tinybird/analytics.

### Hard rules

1. **PRs go to this fork only:** `gh pr create --repo kr8vka0z/learnhouse --base main`. Never open a PR, issue or comment on learnhouse/learnhouse. Push the branch, open the PR, then stop; do not merge.
2. **Branches** are `feat/rr-<slug>` off the latest `main`.
3. **Public-repo hygiene.** This repo is public. Code, comments, commit messages, PR text and test fixtures stay generic: no business names beyond "Rescue Ready fork", no staff names, no internal URLs or hostnames, no secrets, no student data.
4. **Stay rebase-friendly.** We merge each upstream release. Prefer additive code over rewriting upstream functions. Keep edits to upstream files small and mark each with `# Rescue Ready fork:` (or `// Rescue Ready fork:`) giving the why.
5. **Database.** Prefer existing JSON/JSONB fields (`Course.extra_metadata`, `AssignmentTask.contents`, audit event `metadata`) over schema changes. Postgres ENUMs are real enums here (`assignmenttasktypeenum`, `activitylocktype`, ...): don't add values. If a migration is truly unavoidable, follow the migration rule in [FORK.md](FORK.md) and say so loudly in the PR.
6. **Tests** cover risky behavior only; no wording or CSS assertions. Report the test count before and after.

### Running tests

- **API** (`apps/api/src/tests`, in-memory SQLite via `conftest.py`):
  `cd apps/api && uv sync && TESTING=true uv run pytest src/tests/`. Add a path to run one suite. Video tests skip without `ffmpeg`.
- **Web** (`apps/web/tests`): `cd apps/web && bun install && bun run test` (runs `bun test --isolate ./tests`).
- Run the touched suites, then the whole API suite, before opening a PR.
- **4 known failures on a non-UTC host:** `test_auth`, `test_security_all`, and two in `test_account_lockout_service` depend on the clock being UTC. Everything else should pass. On a UTC host all pass; don't "fix" them in a fork PR.

### Tooling notes

- Use Bun, not npm, for `apps/web` (it has `bun.lock`).
- GitHub Actions are disabled on the fork; leave them off.
- Upstream conventions (lockfile freezing, CLI dev setup) are in [CONTRIBUTING.md](CONTRIBUTING.md).
