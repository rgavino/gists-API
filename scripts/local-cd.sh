#!/usr/bin/env bash
# Local rehearsal of the CI/CD pipeline on Docker Desktop.
#
# Same stages as .github/workflows: test in image -> build -> push -> DEV -> approve -> UAT.
# A local registry (localhost:5000) stands in for GHCR, and each environment is
# a long-running container you can see in Docker Desktop:
#   DEV  http://localhost:8081
#   UAT  http://localhost:8082
#
# Usage: scripts/local-cd.sh [--yes]     (--yes skips the UAT approval prompt)
#        scripts/local-cd.sh down        (remove the environments and registry)
set -euo pipefail

REGISTRY=localhost:5000
REPO="$REGISTRY/gists-api"
declare -A PORTS=([dev]=8081 [uat]=8082)

stage() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "${1:-}" = "down" ]; then
  docker rm -f gists-api-dev gists-api-uat local-registry >/dev/null 2>&1 || true
  echo "Removed environments and registry."
  exit 0
fi

cd "$(dirname "$0")/.."
sha=$(git rev-parse --short HEAD)
# Uncommitted changes get their own tag, so they are never mistaken for HEAD.
git diff --quiet HEAD || sha="$sha-dirty"

stage "Registry (stand-in for GHCR)"
if ! docker ps --format '{{.Names}}' | grep -qx local-registry; then
  docker rm -f local-registry >/dev/null 2>&1 || true
  docker run -d --name local-registry -p 5000:5000 registry:2 >/dev/null
fi
echo "$REGISTRY"

stage "Unit tests inside the image"
docker build -q --target test . >/dev/null
echo "passed"

stage "Build and push $REPO:$sha"
docker build -q -t "$REPO:$sha" . >/dev/null
docker push -q "$REPO:$sha" >/dev/null
# Deploy by digest, exactly as the real pipeline does.
image=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REPO:$sha" | grep -m1 "^$REPO@")
echo "$image"

deploy() {
  local env=$1 port=${PORTS[$1]}
  stage "Deploy to ${env^^} (http://localhost:$port)"
  docker tag "$image" "$REPO:$env"
  docker push -q "$REPO:$env" >/dev/null
  docker rm -f "gists-api-$env" >/dev/null 2>&1 || true
  docker run -d --name "gists-api-$env" -p "$port:8080" \
    --label env="$env" --label image="$image" \
    -e GITHUB_TOKEN="${GISTS_GITHUB_TOKEN:-}" \
    "$image" >/dev/null

  for _ in $(seq 1 10); do
    curl -fsS "http://localhost:$port/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS "http://localhost:$port/healthz" | grep -q '"ok"'
  test "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port/not--valid")" = "404"
  echo "smoke test passed"
}

deploy dev

if [ "${1:-}" != "--yes" ]; then
  read -r -p $'\nPromote this image to UAT? [y/N] ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "UAT not updated."; exit 0; }
fi

deploy uat

stage "Deployed"
docker ps --filter label=env --format 'table {{.Names}}\t{{.Ports}}\t{{.Label "image"}}'
