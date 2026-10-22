# Local preview stack

Runs this checkout on your own machine with a sample course, so you can click
through the fork's features (sequential chapters, pass-only completion, essays,
the out-of-attempts notice, completion records) on a throwaway database. Not a
production setup: no HTTPS, no email, everything bound to `127.0.0.1`.

All commands run from this folder. Use `sudo docker` if your user isn't in the
`docker` group.

- **Up:** `./make-env.sh 3300` (once; writes `.env` with random secrets, the
  argument is the port), then `docker compose up -d --build`. The first boot
  creates the org and the admin from `.env` ("Sample School" unless you set
  `LEARNHOUSE_INITIAL_ORG_NAME`/`_SLUG` there first). Wait about 30 s.
  A second stack next to a running one needs its own folder (another checkout
  or worktree), port and project name: `./make-env.sh 3320 lh-second`. The
  project name prefixes the containers, volumes and app image.
- **Seed:** `./seed.py` (or `DOCKER="sudo docker" ./seed.py`). Needs `python3`,
  `curl` and Docker (test video/audio is made in a throwaway ffmpeg container).
  Builds "Nursing Assistant Theory — Sample" (3 chapters, sequential chapters on)
  and three students, then prints the URL, the logins and the course link:
  - Fresh Student: nothing done; chapters 2 and 3 are locked.
  - Progress Student: chapter 1 done (quiz failed once, then passed); chapter 2
    done with the essay submitted and awaiting grading, so chapter 3 is open.
  - Stuck Student: chapter 1 quiz failed on every allowed attempt.
  It first brands the org through the org-settings endpoints: menu bar color
  (`PREVIEW_BRAND_COLOR`, default navy), the "Powered by" watermark off (site
  and email), the email sender name set to the org name, AI features off (a
  preview has no AI key; Settings > AI turns them back on), communities,
  library, podcasts, boards and playgrounds off (the student menu shows only
  Courses; each has its own switch in the dashboard), and logo files when
  given: `PREVIEW_LOGO_FILE` (wide, light-on-dark, for the colored menu bar),
  `PREVIEW_SQUARE_LOGO_FILE` (sign-in panel, dashboard) and
  `PREVIEW_FAVICON_FILE`. PNG, JPG or WebP; SVG is refused. Set them in `.env`
  or on the command line, e.g. `PREVIEW_LOGO_FILE=~/logo.png ./seed.py`. Keep
  logo files out of the repo.
  Re-running on a seeded stack re-applies the branding and prints the summary.
- **Reset:** `docker compose down -v` deletes the database and uploads; then
  `up -d` and seed again. Keep `.env` (or delete it too and rerun `make-env.sh`).
- **Backup:** `docker compose exec db pg_dump -U learnhouse learnhouse > db.sql`
  and `docker compose cp learnhouse-app:/app/api/content ./content` for uploads.
- **Stop without losing data:** `docker compose stop`; `start` brings it back.
