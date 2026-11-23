#!/bin/sh
# Start as root only to fix ownership of the uploads volume (it may have been created by an older,
# root-run version), then run the API as the unprivileged "app" user.
set -e
mkdir -p "${UPLOAD_DIR:-/data/uploads}"
chown -R app:app "${UPLOAD_DIR:-/data/uploads}"
exec setpriv --reuid=app --regid=app --init-groups "$@"
