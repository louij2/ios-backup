#!/bin/bash
# Build, publish and redeploy ios-backup on TOWER, from this repo (tower path:
# /mnt/user/appdata/claude-code/workspace/repos/ios-backup/deploy.sh).
#
# The container comes from its Unraid template
# (/boot/config/plugins/dockerMan/templates-user/my-ios-backup.xml). The image goes to
# Docker Hub first, so tower never holds the only copy.
set -euo pipefail
cd "$(dirname "$0")"
IMG=louij2/ios-backup
TAG=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ios_backup/__init__.py)
C=ios-backup
TPL=/boot/config/plugins/dockerMan/templates-user/my-$C.xml
RECREATE=/mnt/user/appdata/compose-to-templates/tpl-recreate.sh

docker build -t "$IMG:latest" -t "$IMG:$TAG" .
docker push "$IMG:$TAG"
docker push "$IMG:latest"

sed -i "s#<Repository>$IMG:[^<]*</Repository>#<Repository>$IMG:$TAG</Repository>#" "$TPL"
"$RECREATE" "$C"
for _ in $(seq 12); do
  curl -fs -o /dev/null "http://localhost:9871/metrics" && break
  sleep 5
done
if curl -fs -o /dev/null "http://localhost:9871/metrics"; then
  "$RECREATE" --commit "$C"
  echo "deployed $IMG:$TAG"
else
  echo "NOT answering on :9871. Roll back: docker rm -f $C; docker rename $C-old $C; docker start $C" >&2
  exit 1
fi
