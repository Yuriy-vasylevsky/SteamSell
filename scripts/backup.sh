#!/bin/sh
set -eu
umask 077
mkdir -p /backups
chmod 700 /backups
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  if pg_dump -Fc > "/backups/$stamp.dump.tmp"; then
    mv "/backups/$stamp.dump.tmp" "/backups/$stamp.dump"
    find /backups -name '*.dump' -type f -mtime +14 -delete
  else
    echo 'Database backup failed' >&2
  fi
  sleep 86400
done
