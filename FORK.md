# Rescue Ready fork of LearnHouse

This is a fork of [learnhouse/learnhouse](https://github.com/learnhouse/learnhouse) (AGPL-3.0). Our `main` is an upstream release tag (currently `1.4.1`) plus the commits listed here. Upstream's own docs (`README.md`, `docs/`) still describe everything not mentioned below.

## Why it exists

A single-organization school runs self-paced online courses on LearnHouse and has to keep an official record of module completion: per student, which activities and chapters were completed and when, and quiz attempts with pass/fail. Upstream either hides that record behind enterprise features or counts an activity as complete even when its quiz was failed. The fork closes those gaps. It deliberately does not track time-on-task and does not touch analytics.

The fork runs in OSS mode (no `ee/` folders). We do not fake EE mode or edit `EE_ONLY_FEATURES`.

## What differs from upstream

- **Records export unlocked in OSS mode.** The student audit dossier and export (`/audit/*`) work without EE.
- **Completion report.** Per student, per course, as the official record (`services/audit/completion_report.py`, `routers/completion_report.py`). Times are shown in one zone, see Configuration.
- **Sequential chapter gating.** A course setting (`Course.extra_metadata.rr_sequential_chapters`) locks each chapter until the previous one is finished. All of the logic is in `services/courses/sequential.py`.
- **Pass-only assignment completion.** An assignment counts as complete only when passed, via `_rr_sync_assignment_completion` in `services/courses/activities/assignments.py`. Every attempt is kept in the audit history, and an instructor can grant a student one more attempt.
- **Video auto-complete.** A video that plays to the end marks its activity complete.
- **Essay questions.** An essay is a `CUSTOM` assignment task with `contents.kind == "essay"` (no new task-type enum value, so no migration). It is never auto-graded; an instructor grades it by hand. Optional word limits and a size ceiling are enforced at hand-in (`services/courses/activities/essay.py`). The model answer is stored in `contents.explanation`, so students see it only after grading and only when the assignment shows correct answers.
- **Out-of-attempts notice.** A student who failed and has no attempts left is told to ask the instructor for another (`apps/web/lib/assignments/retryState.ts`).
- **Trustworthy records.** Every sign-in path (password, Google, both magic links, email-verify sign-in, admin-API session token) writes a LOGIN audit row, and every enrollment path writes COURSE_ENROLLED. Actions that remove records write their own row on the student's log, saying who removed it (`removed_by`: `admin_api` with the acting token, or `student`) and what was removed (step counts, previous enrollment status): unenrolling, by the admin API (single or bulk, one row per student) or by the student leaving a course, writes COURSE_UNENROLLED; un-completing an activity, by either, writes ACTIVITY_UNCOMPLETED; an admin-API progress reset writes COURSE_PROGRESS_RESET. A call that removes nothing writes no row. A user with learning records (enrollments, progress, submissions, certificates, or audit rows other than sign-in/sign-out) can't be hard-deleted, because the record tables cascade on delete (`services/audit/record_retention.py`); remove them from the organization or anonymize them instead. An account that only ever signed in can still be deleted. Instructors can't "mark as done" an assignment, through the web or the admin API: they change the grade or grant another attempt. Audit writes retry once; if that fails too, an ERROR log line names the lost event (type, ids, grade fields, metadata key names) without any answer text or network identifiers. Custom-task answers (essays) are never shortened in the dossier.
- **White-label.** The site presents itself as the organization's own. Name, logo, square logo, favicon, menu bar color and email sender name come from org settings; turning the org's watermark off hides the site badge and, in the fork, the "Powered by LearnHouse" email line too (upstream forces it on in OSS; `services/email/branding.py`). What settings can't hide is gated on `RR_WHITE_LABEL` in `apps/web/lib/rr/branding.ts`: the help/docs/feedback menus, the LearnHouse marks on the sign-in panel, video player, editor and mobile dashboard, the first-run welcome tour, LearnHouse's own terms and copyright lines and support address (hidden when no platform URL is set). Auth pages get the org favicon. `apps/web/public/empty_thumbnail.png` (the placeholder for anything without a thumbnail) is replaced with a plain navy gradient, because upstream's carries the LearnHouse mark; a binary file can't hold a marker comment, so on an upstream conflict keep ours. The color/type theme (navy primary, accessible cyan accent, Inter body, Sora headings) is one file, `apps/web/styles/rr-theme.css`. A small "Source code" link in the student footer meets the AGPL source offer.

Every edit to an upstream file carries a `Rescue Ready fork:` marker comment, so `grep -rn "Rescue Ready fork" apps` lists everything we changed.

To try these features locally on a fresh database with a seeded sample course and students, see [deploy/local-preview/README.md](deploy/local-preview/README.md).

## How we track upstream

- `origin` is this fork; `upstream` is learnhouse/learnhouse. Pull requests go to this fork only, never to upstream.
- We pin to upstream **release tags**, not `dev`. Upstream releases roughly every 10 days, and we merge each release into `main`:
  `git fetch upstream --tags && git merge <tag>`.
- Keep merges cheap:
  - Prefer additive code (new modules and functions, new settings) over rewriting upstream functions.
  - Keep edits to upstream files small, and mark each with `# Rescue Ready fork:` (Python) or `// Rescue Ready fork:` (TypeScript) plus the reason.
  - Prefer existing JSON/JSONB fields (`Course.extra_metadata`, `AssignmentTask.contents`, audit event `metadata`) over schema changes, and avoid adding Postgres ENUM values.
- **Migrations**, only if unavoidable: one linear chain of fork migrations off the upstream head. It must work both ways:
  - Fresh database: the app builds tables with `create_all` at startup and there is no `alembic_version`, so the migration must not be required.
  - Existing database: `alembic stamp heads`, then `alembic upgrade head`.
- GitHub Actions are disabled on the fork. Run the tests locally (see `AGENTS.md`).

## Configuration

Upstream documents its variables in `docs/content/self-hosting/configuration/environment-variables.mdx`. The fork adds these:

| Variable | Description | Default |
| --- | --- | --- |
| `NEXT_PUBLIC_RR_SOURCE_URL` | Target of the "Source code" footer link (AGPL source offer). Read at runtime; only `http(s)` URLs are accepted. | `https://github.com/kr8vka0z/learnhouse` |
| `LEARNHOUSE_REPORT_TIMEZONE` | IANA time zone (for example `America/Denver`) used to display times in completion reports. An unknown zone falls back to UTC. | `America/Denver` |

## AGPL source obligation

LearnHouse is AGPL-3.0. Anyone who uses this modified version over a network, including the students of a deployment, is entitled to its complete corresponding source. This public repository is how we meet that: deploy from a commit that exists here, and push every change made to a deployed instance before or when it goes live. Keep the upstream `LICENSE` and copyright notices intact. The student footer's "Source code" link (`NEXT_PUBLIC_RR_SOURCE_URL`) is the offer users see; point it at the exact public repository the deployment runs from.
