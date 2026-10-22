# Production stack

Runs the fork for one organization behind an HTTPS reverse proxy or tunnel
that terminates TLS and forwards to `127.0.0.1:<LH_HTTP_PORT>`. One compose
project (`lh-prod`), one data folder, one `.env`, so moving to another machine
means copying the folder and rebuilding the image.

Files here: `docker-compose.yml` (the stack), `.env.example` (every setting),
`backup.sh` (local dump + content tarball). Upstream's own docs under
`docs/content/self-hosting/` explain each variable.

Use `sudo docker` if your user isn't in the `docker` group. Every `docker
compose` command runs from the data folder (below: `/srv/learnhouse`).

## What the proxy must do

- Forward `Host` unchanged and send `X-Forwarded-Proto: https`. The app marks
  session cookies `Secure` only when it sees https; the image's internal nginx
  passes an outer proxy's value through.
- Allow WebSocket upgrades on `/collab`.
- Its own request-size limit is the real upload ceiling (the app allows 6 GB;
  many CDNs and tunnels stop at about 100 MB).

Server-side rendering inside the container fetches the public `https://` URL,
so the stack only renders pages once DNS and the proxy are live, and the
container needs outbound internet access.

## First install

1. **Build the image** from a commit on this repo's `main` (the AGPL source
   obligation in [FORK.md](../../FORK.md) needs the deployed commit to be
   public):
   ```sh
   git archive <sha> | tar -x -C /tmp/lh-build
   docker build -t lh-prod:<shortsha> --build-arg LEARNHOUSE_PUBLIC=true /tmp/lh-build
   ```
   `LEARNHOUSE_PUBLIC=true` makes the community (OSS) build.
2. **Data folder:** create it, copy `docker-compose.yml` and `backup.sh` into
   it, and write `.env` from `.env.example` with fresh secrets; `chmod 600 .env`.
   Keep a copy of each secret in your password manager.
3. **Start:** `docker compose up -d`. The first boot of an empty database
   creates the organization and the admin from `.env` (no sample data). Wait
   until `docker compose ps` shows the app `healthy`.
4. **Stamp the migration history, right away.** A fresh database is built by
   `create_all`, which leaves no `alembic_version` row; without one, the next
   upstream update can't migrate. Run once:
   ```sh
   docker compose exec -T learnhouse-app sh -c 'cd /app/api && .venv/bin/alembic stamp heads'
   docker compose exec -T db psql -U learnhouse -tAc 'select version_num from alembic_version'
   ```
   The second command must print a revision.
5. **Close the door** in the admin dashboard, under Users settings:
   - *Signups & Invite Codes*: set Join method to **Closed** (invite only), then
     generate an invite code. In closed mode an emailed invite carries that
     code; without one the invitee can't register.
   - *Sign-in*: untick sign-in methods you don't offer (for example Google when
     no Google client is configured) and turn off session sharing with the
     hosted service.
   - New courses: leave them non-public.
6. **Check from outside:** the public URL answers 200, signing in sets cookies
   marked `Secure`, `/api/v1/docs` returns 404 (development mode is off), an
   invite email's link uses the public host, and a public sign-up attempt
   (`POST /api/v1/users/<org_id>`) returns 403.

## Backups

`./backup.sh` writes `backups/<UTC timestamp>/db.dump` (custom-format
`pg_dump`, checked with `pg_restore -l`) and `content.tar.gz` (uploads), and
deletes local copies older than `KEEP_DAYS` (default 7). Copy each new folder
off the machine on a schedule; the script prints its path on the last line.

**Restore** (to a fresh stack or a scratch Postgres):
```sh
docker compose exec -T db pg_restore -U learnhouse -d learnhouse --clean --if-exists --no-owner < db.dump
tar -xzf content.tar.gz -C /srv/learnhouse    # restores ./content
```
Use a `pgvector/pgvector` image for a scratch restore; the dump uses the
`vector` extension. Test a restore now and then: count rows in `user`,
`course`, `trailstep` and `user_audit_event` on both sides.

## Update

1. Merge the upstream release into `main` (see [FORK.md](../../FORK.md)) and
   build a new tag from it, as in First install step 1. The running stack is
   untouched until step 4.
2. Back up: `./backup.sh` (and copy it off the machine).
3. Set `LH_IMAGE` in `.env` to the new tag, then migrate with the new image
   while the old one keeps serving:
   ```sh
   docker compose run --rm --no-deps learnhouse-app sh -c 'cd /app/api && .venv/bin/alembic upgrade heads'
   ```
4. Swap: `docker compose up -d` (recreates the app on the new tag).
5. Check: `docker compose ps` shows `healthy`, the public URL answers, and you
   can sign in.

`docker compose restart` does not re-read `.env`; `up -d` does.

## Rollback

Set `LH_IMAGE` back to the previous tag and `docker compose up -d`. If the
update ran a migration the old code can't handle, also restore the dump taken
in Update step 2 (`docker compose stop learnhouse-app`, restore, then `up -d`).
Keep the previous image tag until the new one has run cleanly for a while.
