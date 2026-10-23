#!/usr/bin/env bash
# Local backup of a production stack: a custom-format pg_dump plus a tarball of
# uploaded content, written to backups/<UTC timestamp>/ in the data folder.
# Copying the result off the machine is the caller's job (see README); this
# script only makes and checks the local copy, and prunes old local copies.
#
# Encryption: if backup-recipients.txt (age public keys, one per line) sits in
# the data folder, both files are encrypted with `age` and only the .age files
# are kept, so a copied-off backup is unreadable without the private key, which
# never needs to be on this machine. Without the file, backups stay plain.
#
# Usage: run from the data folder (where docker-compose.yml and .env live):
#   ./backup.sh            # keeps 7 days of local copies
#   KEEP_DAYS=3 ./backup.sh
#   AGE_RECIPIENTS=/path/to/recipients.txt ./backup.sh
# Prints the new backup folder on the last line of stdout.
set -euo pipefail
cd "$(dirname "$0")"

keep_days="${KEEP_DAYS:-7}"
recipients="${AGE_RECIPIENTS:-backup-recipients.txt}"
compose="${DOCKER:-docker} compose"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="backups/$ts"

umask 077
mkdir -p "$out"

# Custom format (-Fc): compressed, and pg_restore can restore selectively.
$compose exec -T db pg_dump -U learnhouse -d learnhouse -Fc > "$out/db.dump"
# A dump that pg_restore can't list is no backup; fail loudly instead.
$compose exec -T db pg_restore -l < "$out/db.dump" > /dev/null

# Uploads live in ./content on the host (bind mount), so no container is needed.
tar -C . -czf "$out/content.tar.gz" content
tar -tzf "$out/content.tar.gz" > /dev/null

# Encrypt after the checks above, so what gets encrypted is known to be good.
if [ -s "$recipients" ]; then
  for f in db.dump content.tar.gz; do
    age -R "$recipients" -o "$out/$f.age" "$out/$f"
    rm -f -- "$out/$f"
  done
fi

# Prune local copies older than keep_days. Folder names are UTC timestamps.
find backups -mindepth 1 -maxdepth 1 -type d -name '20*T*Z' -mtime +"$keep_days" -exec rm -rf -- {} +

echo "$PWD/$out"
