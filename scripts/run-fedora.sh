#!/usr/bin/env bash
# Build the Fedora image and run UI + API + Postgres + Redis with docker OR podman.
#   ./scripts/run-fedora.sh up | down | status | logs [app|api|db|cache] | token | destroy
# Overrides (env): CONTAINER_ENGINE, FEDORA_VERSION, PYTHON_VERSION, UI_PORT, API_PORT, AUTO_SEED
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "error: $*" >&2; exit 1; }

ENGINE="${CONTAINER_ENGINE:-$(command -v docker || command -v podman || true)}"
[[ -n "$ENGINE" ]] || die "neither docker nor podman found (sudo dnf install podman)"
ENGINE="$(basename "$ENGINE")"

IMAGE="eldercare:fedora"; NET="eldercare-net"; VOL="eldercare-pgdata"
UI_PORT="${UI_PORT:-8501}"; API_PORT="${API_PORT:-8000}"; ENV_FILE=".env.fedora"
C_DB="eldercare-db"; C_CACHE="eldercare-cache"; C_APP="eldercare-app"; C_API="eldercare-api"

rand() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# Secrets are generated once and kept (chmod 600) so the Postgres volume password never drifts.
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  printf 'POSTGRES_PASSWORD=%s\nPLATFORM_ADMIN_TOKEN=%s\n' "$(rand)" "$(rand)" > "$ENV_FILE"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

running() { [[ "$($ENGINE inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]; }

wait_for() {  # wait_for <label> <seconds> <command...>
  local label="$1" secs="$2"; shift 2
  for ((i = 0; i < secs; i++)); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  die "$label did not become ready in ${secs}s (see: $0 logs)"
}

cmd_up() {
  echo ">> building $IMAGE with $ENGINE"
  $ENGINE build -f Dockerfile.fedora -t "$IMAGE" \
    --build-arg "FEDORA_VERSION=${FEDORA_VERSION:-latest}" \
    --build-arg "PYTHON_VERSION=${PYTHON_VERSION:-3.11}" .

  $ENGINE network inspect "$NET" >/dev/null 2>&1 || $ENGINE network create "$NET" >/dev/null

  if ! running "$C_DB"; then
    $ENGINE rm -f "$C_DB" >/dev/null 2>&1 || true
    $ENGINE run -d --name "$C_DB" --network "$NET" --restart unless-stopped \
      -e POSTGRES_USER=care -e "POSTGRES_PASSWORD=$POSTGRES_PASSWORD" -e POSTGRES_DB=care \
      -v "$VOL:/var/lib/postgresql/data" postgres:16-alpine >/dev/null
  fi
  if ! running "$C_CACHE"; then
    $ENGINE rm -f "$C_CACHE" >/dev/null 2>&1 || true
    $ENGINE run -d --name "$C_CACHE" --network "$NET" --restart unless-stopped redis:7-alpine >/dev/null
  fi
  echo ">> waiting for Postgres"
  wait_for postgres 60 $ENGINE exec "$C_DB" pg_isready -U care -d care

  local common=(--network "$NET" --restart unless-stopped
    -e "DATABASE_URL=postgresql://care:${POSTGRES_PASSWORD}@${C_DB}:5432/care"
    -e "REDIS_URL=redis://${C_CACHE}:6379/0" -e "DB_POOL_MAX=${DB_POOL_MAX:-8}")

  $ENGINE rm -f "$C_APP" "$C_API" >/dev/null 2>&1 || true
  $ENGINE run -d --name "$C_APP" "${common[@]}" -p "${UI_PORT}:8501" \
    -e "AUTO_SEED=${AUTO_SEED:-true}" "$IMAGE" >/dev/null
  $ENGINE run -d --name "$C_API" "${common[@]}" -p "${API_PORT}:8000" \
    -e "PLATFORM_ADMIN_TOKEN=${PLATFORM_ADMIN_TOKEN}" "$IMAGE" \
    uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers >/dev/null

  echo ">> waiting for the API"
  wait_for api 90 $ENGINE exec "$C_API" python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
  cat <<MSG

Running:
  Staff UI    http://localhost:${UI_PORT}   (first start seeds demo data; allow ~30s)
  API docs    http://localhost:${API_PORT}/docs
  Admin token $0 token      (stored in $ENV_FILE, mode 600)
MSG
}

cmd_down() { $ENGINE rm -f "$C_APP" "$C_API" "$C_CACHE" "$C_DB" >/dev/null 2>&1 || true; echo "stopped (data volume kept)"; }

cmd_destroy() {
  read -r -p "Delete containers, network AND the database volume '$VOL'? [y/N] " a
  [[ "$a" == "y" ]] || die "aborted"
  cmd_down
  $ENGINE volume rm "$VOL" >/dev/null 2>&1 || true
  $ENGINE network rm "$NET" >/dev/null 2>&1 || true
  echo "destroyed"
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  destroy) cmd_destroy ;;
  status) $ENGINE ps -a --filter "name=eldercare-" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' ;;
  logs) case "${2:-app}" in app) n=$C_APP;; api) n=$C_API;; db) n=$C_DB;; cache) n=$C_CACHE;; *) die "use: app|api|db|cache";; esac
        $ENGINE logs --tail 100 -f "$n" ;;
  token) echo "$PLATFORM_ADMIN_TOKEN" ;;
  *) die "usage: $0 up|down|status|logs [app|api|db|cache]|token|destroy" ;;
esac
