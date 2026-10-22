# Local preview stack

Runs this checkout on your own machine with a sample course, so you can click
through the fork's features (sequential chapters, pass-only completion, essays,
the out-of-attempts notice, completion records) on a throwaway database. Not a
production setup: no HTTPS, no email, everything bound to `127.0.0.1`.

All commands run from this folder. Use `sudo docker` if your user isn't in the
`docker` group.

- **Up:** `./make-env.sh 3300` (once; writes `.env` with random secrets, the
  argument is the port), then `docker compose up -d --build`. The first boot
  creates the org "Sample School" and the admin from `.env`. Wait about 30 s.
- **Seed:** `./seed.py` (or `DOCKER="sudo docker" ./seed.py`). Needs `python3`,
  `curl` and Docker (test video/audio is made in a throwaway ffmpeg container).
  Builds "Nursing Assistant Theory — Sample" (3 chapters, sequential chapters on)
  and three students, then prints the URL, the logins and the course link:
  - Fresh Student: nothing done; chapters 2 and 3 are locked.
  - Progress Student: chapter 1 done (quiz failed once, then passed); chapter 2
    done with the essay submitted and awaiting grading, so chapter 3 is open.
  - Stuck Student: chapter 1 quiz failed on every allowed attempt.
  Re-running on a seeded stack just prints the summary again.
- **Reset:** `docker compose down -v` deletes the database and uploads; then
  `up -d` and seed again. Keep `.env` (or delete it too and rerun `make-env.sh`).
- **Backup:** `docker compose exec db pg_dump -U learnhouse learnhouse > db.sql`
  and `docker compose cp learnhouse-app:/app/api/content ./content` for uploads.
- **Stop without losing data:** `docker compose stop`; `start` brings it back.
