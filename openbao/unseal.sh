#!/bin/bash
# Unseal OpenBao after a container restart. Requires /root/openbao-rebuild-keys.txt or hardcoded keys.
set -euo pipefail
export BAO_ADDR=http://127.0.0.1:8200
STATUS=$(docker exec -e BAO_ADDR=http://127.0.0.1:8200 openbao bao status -format=json 2>/dev/null || true)
if echo "$STATUS" | grep -q '"sealed": false'; then
  echo "OpenBao already unsealed."
  exit 0
fi
for key in   e5b5567b84c62810c8cb9ccdeb9130603ea3e86727deb8fea31d8e59890f5aa0c6   86173c980dc041fe334c37a12f6a88360990cca0d3f0a8bc910fe08a5a81c22af0   f1ef41931a3dba840823c708a5a0eb734bec91ba1266947657b31597bf74332c22
do
  docker exec -e BAO_ADDR=http://127.0.0.1:8200 openbao bao operator unseal "$key" >/dev/null
done
echo "OpenBao unsealed."
