#!/usr/bin/env bash
#
# TallyBridge deploy script
#
# Run this from the repo folder on the server after a git pull. Safe to run
# repeatedly — it only changes what needs changing.
#
#   ./deploy.sh                 pull is up to you; build + start the UI
#   ./deploy.sh --pull          git pull first, then build + start
#   ./deploy.sh --auto          also start the automatic folder watcher
#   ./deploy.sh --no-build      restart without rebuilding the image
#   ./deploy.sh --status        show what's running, then exit
#   ./deploy.sh --logs          follow the logs, then exit
#   ./deploy.sh --stop          stop everything, then exit
#   ./deploy.sh --nginx         print the nginx config to serve it at a subpath
#   ./deploy.sh --help
#
set -euo pipefail

# --- defaults (override in .env, which this script creates on first run) ----
TB_DATA_DEFAULT="/srv/tallybridge"
TB_PORT_DEFAULT="8087"
TB_PREFIX_DEFAULT="/tallybridge"
PUBLIC_URL_DEFAULT="https://tools.northernfruit.com"

APP="TallyBridge"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
say()  { printf '%s\n' "${B}==>${N} $*"; }
ok()   { printf '%s\n' "  ${G}ok${N}  $*"; }
warn() { printf '%s\n' "  ${Y}!!${N}  $*"; }
die()  { printf '%s\n' "  ${R}xx${N}  $*" >&2; exit 1; }

# --- args ------------------------------------------------------------------
DO_PULL=0; DO_BUILD=1; WITH_AUTO=0; ACTION="deploy"
while [ $# -gt 0 ]; do
  case "$1" in
    --pull)      DO_PULL=1 ;;
    --auto)      WITH_AUTO=1 ;;
    --no-build)  DO_BUILD=0 ;;
    --status)    ACTION="status" ;;
    --logs)      ACTION="logs" ;;
    --stop)      ACTION="stop" ;;
    --nginx)     ACTION="nginx" ;;
    -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *)           die "unknown option: $1  (try --help)" ;;
  esac
  shift
done

# --- docker available? -----------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "docker compose plugin not found (install docker-compose-plugin)"
fi
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon — try sudo, or add your user to the docker group"

# --- .env: create on first run, then read it -------------------------------
if [ ! -f .env ]; then
  say "First run — writing .env"
  cat > .env <<EOF
# TallyBridge settings. Edit, then re-run ./deploy.sh
TB_DATA=$TB_DATA_DEFAULT
TB_PORT=$TB_PORT_DEFAULT
TB_PREFIX=$TB_PREFIX_DEFAULT
PUBLIC_URL=$PUBLIC_URL_DEFAULT
EOF
  ok "created .env (data folder, port, and subpath live here)"
fi

set -a; . ./.env; set +a
TB_DATA="${TB_DATA:-$TB_DATA_DEFAULT}"
TB_PORT="${TB_PORT:-$TB_PORT_DEFAULT}"
TB_PREFIX="${TB_PREFIX:-$TB_PREFIX_DEFAULT}"
PUBLIC_URL="${PUBLIC_URL:-$PUBLIC_URL_DEFAULT}"
PROFILE_ARGS=()
[ "$WITH_AUTO" = "1" ] && PROFILE_ARGS=(--profile auto)

# --- short-circuit actions -------------------------------------------------
case "$ACTION" in
  status)
    say "$APP status"
    $DC --profile auto ps
    exit 0 ;;
  logs)
    say "Following logs (Ctrl+C to stop)"
    exec $DC --profile auto logs -f --tail=50 ;;
  stop)
    say "Stopping $APP"
    $DC --profile auto down
    ok "stopped"
    exit 0 ;;
  nginx)
    cat <<EOF
# Add inside the server block for ${PUBLIC_URL#*://} on your nginx host,
# then: sudo nginx -t && sudo systemctl reload nginx

location ${TB_PREFIX}/ {
    proxy_pass         http://127.0.0.1:${TB_PORT}/;
    proxy_set_header   Host              \$host;
    proxy_set_header   X-Real-IP         \$remote_addr;
    proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto \$scheme;
    proxy_set_header   X-Forwarded-Prefix ${TB_PREFIX};
    client_max_body_size 25m;      # packing-line uploads
}
location = ${TB_PREFIX} { return 301 ${TB_PREFIX}/; }
EOF
    exit 0 ;;
esac

# --- deploy ----------------------------------------------------------------
say "Deploying $APP"
printf '      data folder : %s\n      host port   : %s\n      subpath     : %s\n' \
       "$TB_DATA" "$TB_PORT" "$TB_PREFIX"

if [ "$DO_PULL" = "1" ]; then
  say "Pulling latest code"
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git repo — clone it or drop --pull"
  git pull --ff-only
  ok "code up to date"
fi

# every file the image needs must be present
for f in Dockerfile docker-compose.yml timecard_converter.py timecard_web.py \
         Timecard_Import_template_CLEAN.xlsx; do
  [ -f "$f" ] || die "missing required file: $f"
done
ok "all required files present"

say "Creating data folders under $TB_DATA"
SUDO=""
if [ ! -w "$(dirname "$TB_DATA")" ] && [ "$(id -u)" != "0" ]; then SUDO="sudo"; fi
$SUDO mkdir -p "$TB_DATA"/{incoming,converted,processed,failed,staging}
ok "incoming, converted, processed, failed, staging"

if [ "$DO_BUILD" = "1" ]; then
  say "Building image"
  $DC build
  ok "image built"
fi

say "Starting containers"
$DC "${PROFILE_ARGS[@]}" up -d --remove-orphans
if [ "$WITH_AUTO" = "1" ]; then
  ok "UI and automatic watcher running"
else
  ok "UI running (watcher off — add --auto when you're ready)"
fi

# --- health check ----------------------------------------------------------
say "Checking the UI responds"
URL="http://127.0.0.1:${TB_PORT}${TB_PREFIX}/"
CODE=""
for _ in $(seq 1 20); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$URL" || true)"
  [ "$CODE" = "200" ] && break
  sleep 1
done
if [ "$CODE" = "200" ]; then
  ok "$URL returned 200"
else
  warn "no 200 from $URL yet (got '${CODE:-no response}')"
  warn "check the log:  $DC logs web"
fi

# --- summary ---------------------------------------------------------------
say "Done"
printf '      Public URL   : %s%s/\n' "${PUBLIC_URL%/}" "$TB_PREFIX"
printf '      Direct URL   : http://%s:%s%s/\n' "$(hostname -f 2>/dev/null || hostname)" "$TB_PORT" "$TB_PREFIX"
printf '      Logs         : %s --profile auto logs -f\n' "$DC"
printf '      Status       : ./deploy.sh --status\n'
if [ "$WITH_AUTO" != "1" ]; then
  printf '      Automation   : ./deploy.sh --auto   (enables folder pickup)\n'
fi
printf '      nginx config : ./deploy.sh --nginx\n'
