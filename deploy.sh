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
#   ./deploy.sh --install-nginx install that config into nginx, test, and reload
#   ./deploy.sh --check         verify the public URL actually answers
#   ./deploy.sh --smb           print the Samba config for the drop folder
#   ./deploy.sh --install-smb   install it, test it, restart Samba
#   ./deploy.sh --help
#
set -euo pipefail

# --- defaults (override in .env, which this script creates on first run) ----
TB_DATA_DEFAULT="/srv/tallybridge"
TB_PORT_DEFAULT="8087"
TB_PREFIX_DEFAULT="/tallybridge"
PUBLIC_URL_DEFAULT="https://tools.northernfruit.com"
TB_SHARE_DEFAULT="/mnt/payroll/STAMPER"
TB_AUTH_DEFAULT="auto"     # auto | on | off — gate behind M365 SSO (oauth2-proxy)

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
    --install-nginx) ACTION="install-nginx" ;;
    --check)     ACTION="check" ;;
    --smb)       ACTION="smb" ;;
    --install-smb) ACTION="install-smb" ;;
    -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *)           die "unknown option: $1  (try --help)" ;;
  esac
  shift
done

# --- docker available? -----------------------------------------------------
# The config-printing actions don't touch containers, so don't demand docker.
DC="docker compose"
case "$ACTION" in
  nginx|smb|install-smb) ;;
  *)
    command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
    if docker compose version >/dev/null 2>&1; then
      DC="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
      DC="docker-compose"
    else
      die "docker compose plugin not found (install docker-compose-plugin)"
    fi
    docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon — try sudo, or add your user to the docker group"
    ;;
esac

# --- .env: create on first run, then read it -------------------------------
if [ ! -f .env ]; then
  say "First run — writing .env"
  cat > .env <<EOF
# TallyBridge settings. Edit, then re-run ./deploy.sh
TB_DATA=$TB_DATA_DEFAULT
TB_PORT=$TB_PORT_DEFAULT
TB_PREFIX=$TB_PREFIX_DEFAULT
PUBLIC_URL=$PUBLIC_URL_DEFAULT
# Where the payroll share is mounted on this host (used by --auto)
TB_SHARE=$TB_SHARE_DEFAULT
# M365 SSO: auto = gate it if oauth2-proxy is already set up in nginx
TB_AUTH=$TB_AUTH_DEFAULT
EOF
  ok "created .env (data folder, port, and subpath live here)"
fi

set -a; . ./.env; set +a
TB_DATA="${TB_DATA:-$TB_DATA_DEFAULT}"
TB_PORT="${TB_PORT:-$TB_PORT_DEFAULT}"
TB_PREFIX="${TB_PREFIX:-$TB_PREFIX_DEFAULT}"
PUBLIC_URL="${PUBLIC_URL:-$PUBLIC_URL_DEFAULT}"
TB_AUTH="${TB_AUTH:-$TB_AUTH_DEFAULT}"
TB_SHARE="${TB_SHARE:-$TB_SHARE_DEFAULT}"
# If the watcher/fetcher are already running, keep them in scope for this
# deploy. Without this a rebuild recreates only the web container and the
# watcher silently keeps running the OLD image.
if [ "$WITH_AUTO" != "1" ] && command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null \
       | grep -qE '^tallybridge-(watcher|fetcher)$'; then
    WITH_AUTO=1
    AUTO_INHERITED=1
  fi
fi

PROFILE_ARGS=()
[ "$WITH_AUTO" = "1" ] && PROFILE_ARGS=(--profile auto)

NGINX_ROOT="${NGINX_ROOT:-/etc/nginx}"      # override only for testing
PUBLIC_HOST="${PUBLIC_URL#*://}"; PUBLIC_HOST="${PUBLIC_HOST%%/*}"
SNIPPET_REL="snippets/tallybridge.conf"
SNIPPET_PATH="$NGINX_ROOT/$SNIPPET_REL"

# Is oauth2-proxy (M365 SSO) already configured on this host?
sso_available() {
  grep -rqs "location @oauth2_signin" \
    "$NGINX_ROOT/sites-enabled" "$NGINX_ROOT/sites-available" "$NGINX_ROOT/conf.d" 2>/dev/null
}

# Should this location be gated behind SSO?
use_sso() {
  case "$TB_AUTH" in
    on)  return 0 ;;
    off) return 1 ;;
    *)   sso_available ;;
  esac
}

# The proxy config, printed by --nginx and written by --install-nginx.
nginx_config() {
  local auth="" authset=""
  if use_sso; then
    auth='    auth_request /oauth2/auth;
    error_page 401 = @oauth2_signin;
'
    authset='
    # pass the signed-in M365 user through, so saves can be attributed
    auth_request_set $tb_email $upstream_http_x_auth_request_email;
    proxy_set_header X-Auth-Request-Email $tb_email;'
  fi
  cat <<EOF
# TallyBridge — managed by deploy.sh, edits here are overwritten
location = ${TB_PREFIX} { return 301 ${TB_PREFIX}/; }

location ^~ ${TB_PREFIX}/ {
${auth}    proxy_pass         http://127.0.0.1:${TB_PORT}/;
    proxy_http_version 1.1;
    proxy_set_header   Host               \$host;
    proxy_set_header   X-Real-IP          \$remote_addr;
    proxy_set_header   X-Forwarded-For    \$proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto  \$scheme;
    proxy_set_header   X-Forwarded-Prefix ${TB_PREFIX};
    client_max_body_size 25m;      # packing-line uploads
    proxy_read_timeout 120s;       # conversion of a large day${authset}
}
EOF
}

SMB_USER="${SMB_USER:-packline}"          # writes source files into incoming
SMB_USER_OUT="${SMB_USER_OUT:-payroll}"   # reads finished workbooks (may be the same)
SMB_SHARE="${SMB_SHARE:-incoming}"
SMB_SHARE_OUT="${SMB_SHARE_OUT:-converted}"

# Samba config for the drop folder — writable by the line.
smb_config_incoming() {
  cat <<EOF

[${SMB_SHARE}]
   comment = TallyBridge - drop packing line files here
   path = ${TB_DATA}/incoming
   browseable = yes
   read only = no
   valid users = ${SMB_USER}
   force user = root
   force group = root
   create mask = 0664
   directory mask = 0775
   # Windows scratch files should never look like a packing line export
   veto files = /.DS_Store/Thumbs.db/desktop.ini/~\$*/
   delete veto files = yes
EOF
}

# Samba config for finished workbooks — read only, so an import can't alter or
# delete them by accident. Removing them is done from the web page.
smb_config_converted() {
  cat <<EOF

[${SMB_SHARE_OUT}]
   comment = TallyBridge - finished workbooks for Paycom (read only)
   path = ${TB_DATA}/converted
   browseable = yes
   read only = yes
   valid users = ${SMB_USER} ${SMB_USER_OUT}
   force user = root
   force group = root
EOF
}

smb_config() { smb_config_incoming; smb_config_converted; }

# Does the public URL answer? Used by --check and after --install-nginx.
check_public() {
  local url="${PUBLIC_URL%/}${TB_PREFIX}/"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || true)"
  printf '      %s -> %s\n' "$url" "${code:-no response}"
  [ "$code" = "200" ]
}

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
    printf '# Add inside the server block for %s, then reload nginx.\n' "$PUBLIC_HOST"
    printf '# Or let this script do it:  ./deploy.sh --install-nginx\n\n'
    nginx_config
    exit 0 ;;

  check)
    say "Checking $APP is reachable"
    printf '      container   : '
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://127.0.0.1:${TB_PORT}${TB_PREFIX}/" 2>/dev/null || true
    printf '  (expect 200)\n'
    say "Public URL"
    if check_public; then
      ok "reachable"
    else
      warn "not answering yet — if the container is up, nginx probably isn't"
      warn "proxying this path. Run:  ./deploy.sh --install-nginx"
    fi
    exit 0 ;;

  smb)
    printf '# Append to /etc/samba/smb.conf, then: sudo testparm -s && sudo systemctl restart smbd\n'
    printf '# Or let this script do it:  ./deploy.sh --install-smb\n\n'
    smb_config
    exit 0 ;;

  install-smb)
    say "Setting up the Samba shares"

    if ! command -v smbd >/dev/null 2>&1 && ! command -v testparm >/dev/null 2>&1; then
      warn "Samba isn't installed. Run:  sudo apt install -y samba"
      exit 1
    fi

    SUDO=""
    [ "$(id -u)" != "0" ] && SUDO="sudo"
    CONF="/etc/samba/smb.conf"
    [ -f "$CONF" ] || die "$CONF not found — is Samba installed?"

    $SUDO mkdir -p "$TB_DATA/incoming" "$TB_DATA/converted"
    ok "folders exist: $TB_DATA/{incoming,converted}"

    NEEDED=""
    grep -qE "^\[${SMB_SHARE}\]" "$CONF"     || NEEDED="$NEEDED $SMB_SHARE"
    grep -qE "^\[${SMB_SHARE_OUT}\]" "$CONF" || NEEDED="$NEEDED $SMB_SHARE_OUT"

    if [ -z "$NEEDED" ]; then
      ok "[$SMB_SHARE] and [$SMB_SHARE_OUT] already in $CONF — leaving them alone"
    else
      BACKUP="${CONF}.tallybridge-backup.$(date +%Y%m%d%H%M%S)"
      $SUDO cp "$CONF" "$BACKUP"
      ok "backed up to $BACKUP"

      for share in $NEEDED; do
        if [ "$share" = "$SMB_SHARE" ]; then
          smb_config_incoming | $SUDO tee -a "$CONF" >/dev/null
        else
          smb_config_converted | $SUDO tee -a "$CONF" >/dev/null
        fi
        ok "added [$share]"
      done

      if ! $SUDO testparm -s >/dev/null 2>&1; then
        $SUDO cp "$BACKUP" "$CONF"
        warn "Samba rejected the config — your original file has been restored"
        $SUDO testparm -s 2>&1 | tail -12 | sed 's/^/      /' || true
        exit 1
      fi
      ok "testparm passed"
    fi

    if command -v systemctl >/dev/null 2>&1; then
      $SUDO systemctl restart smbd 2>/dev/null || $SUDO systemctl restart smb 2>/dev/null || true
      ok "Samba restarted"
    fi

    HOSTSHORT="$(hostname -s 2>/dev/null || hostname)"
    say "Accounts"
    printf '      %-9s writes files into  \\\\%s\\%s\n' "$SMB_USER" "$HOSTSHORT" "$SMB_SHARE"
    printf '      %-9s reads workbooks at \\\\%s\\%s  (read only)\n\n' \
           "$SMB_USER_OUT" "$HOSTSHORT" "$SMB_SHARE_OUT"
    printf '      Create either one that does not exist yet:\n'
    for u in "$SMB_USER" "$SMB_USER_OUT"; do
      if id "$u" >/dev/null 2>&1; then
        printf '        %-9s exists — set its SMB password with: sudo smbpasswd -a %s\n' "$u" "$u"
      else
        printf '        sudo useradd -M -s /usr/sbin/nologin %s && sudo smbpasswd -a %s && sudo smbpasswd -e %s\n' "$u" "$u" "$u"
      fi
    done
    printf '\n      Map from Windows:\n'
    printf '        net use S: \\\\%s\\%s /user:%s /persistent:yes\n' "$HOSTSHORT" "$SMB_SHARE" "$SMB_USER"
    printf '        net use P: \\\\%s\\%s /user:%s /persistent:yes\n\n' "$HOSTSHORT" "$SMB_SHARE_OUT" "$SMB_USER_OUT"
    printf '      If the server is firewalled, allow SMB from the LAN:\n'
    printf '        sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp\n'
    exit 0 ;;

  install-nginx)
    say "Installing the nginx proxy config"

    if ! command -v nginx >/dev/null 2>&1; then
      warn "no nginx on this host"
      printf '\n  If nginx runs in a container or you use Nginx Proxy Manager,\n'
      printf '  add the proxy there instead:\n'
      printf '    Forward to      : %s port %s\n' "$(hostname -I 2>/dev/null | awk '{print $1}')" "$TB_PORT"
      printf '    Location        : %s/\n' "$TB_PREFIX"
      printf '    Custom header   : X-Forwarded-Prefix: %s\n' "$TB_PREFIX"
      printf '    Max body size   : 25m\n\n'
      printf '  Full config to paste:  ./deploy.sh --nginx\n'
      exit 1
    fi

    SUDO=""
    [ "$(id -u)" != "0" ] && SUDO="sudo"

    if use_sso; then
      ok "M365 SSO detected — TallyBridge will require sign-in"
    else
      warn "no SSO gate on this location (TB_AUTH=$TB_AUTH)"
    fi

    # 1. write the snippet (safe: its own file, nothing else touched)
    $SUDO mkdir -p "$NGINX_ROOT/snippets"
    nginx_config | $SUDO tee "$SNIPPET_PATH" >/dev/null
    ok "wrote $SNIPPET_PATH"

    # 2. find the server block for the public host
    TARGET=""
    for d in "$NGINX_ROOT/sites-enabled" "$NGINX_ROOT/sites-available" "$NGINX_ROOT/conf.d"; do
      [ -d "$d" ] || continue
      while IFS= read -r f; do
        [ -n "$f" ] && TARGET="$f" && break
      done < <(grep -rls -E "server_name[[:space:]]+[^;]*${PUBLIC_HOST}" "$d" 2>/dev/null || true)
      [ -n "$TARGET" ] && break
    done

    if [ -z "$TARGET" ]; then
      warn "couldn't find a server block for $PUBLIC_HOST under $NGINX_ROOT"
      printf '      Add this line inside that server block yourself:\n'
      printf '        include %s;\n' "$SNIPPET_REL"
      printf '      then: %s nginx -t && %s systemctl reload nginx\n' "$SUDO" "$SUDO"
      exit 1
    fi
    ok "found server block: $TARGET"

    # 3. already included? then nothing to edit
    if grep -q "$SNIPPET_REL" "$TARGET"; then
      ok "include line already present"
    else
      BACKUP="${TARGET}.tallybridge-backup.$(date +%Y%m%d%H%M%S)"
      $SUDO cp "$TARGET" "$BACKUP"
      ok "backed up to $BACKUP"

      # A hostname usually appears in two server blocks: the :80 redirect and
      # the real :443 one. The include has to land in the TLS block, or the
      # location would sit in a block that only issues redirects.
      TMP="$(mktemp)"
      awk -v host="$PUBLIC_HOST" -v inc="    include $SNIPPET_REL;" '
        {
          lines[NR] = $0
          last = NR
          opens  = gsub(/\{/, "{")
          closes = gsub(/\}/, "}")

          if (depth == 0 && $0 ~ /server[[:space:]]*\{/) {
            inblock = 1; hasHost = 0; isTLS = 0; snLine = 0
          }
          if (inblock) {
            if ($1 == "server_name" && index($0, host)) { hasHost = 1; snLine = NR }
            if ($0 ~ /listen[[:space:]]+[^;]*443/ || $0 ~ /ssl_certificate/) isTLS = 1
          }

          depth += opens - closes

          if (inblock && depth == 0) {
            if (hasHost && isTLS && !tlsLine)      tlsLine = snLine
            else if (hasHost && !plainLine)        plainLine = snLine
            inblock = 0
          }
        }
        END {
          target = tlsLine ? tlsLine : plainLine
          if (!target) exit 3
          for (i = 1; i <= last; i++) {
            print lines[i]
            if (i == target) print inc
          }
          printf("chose line %d (%s)\n", target, tlsLine ? "TLS block" : "only match") > "/dev/stderr"
        }
      ' "$TARGET" > "$TMP" 2>"$TMP.why"
      AWK_RC=$?

      if [ "$AWK_RC" != "0" ] || ! grep -q "$SNIPPET_REL" "$TMP"; then
        rm -f "$TMP" "$TMP.why"
        warn "could not place the include line automatically"
        printf '      Add it inside the :443 server block yourself:  include %s;\n' "$SNIPPET_REL"
        exit 1
      fi
      ok "$(sed 's/^/insert point: /' "$TMP.why" | tr -d '\n')"
      rm -f "$TMP.why"

      $SUDO cp "$TMP" "$TARGET"; rm -f "$TMP"
      ok "added: include $SNIPPET_REL"

      # 4. test, and undo the edit if nginx is unhappy
      if ! $SUDO nginx -t >/dev/null 2>&1; then
        $SUDO cp "$BACKUP" "$TARGET"
        warn "nginx rejected the config — your original file has been restored"
        printf '\n'
        $SUDO nginx -t 2>&1 | sed 's/^/      /' || true
        exit 1
      fi
      ok "nginx -t passed"
    fi

    # 5. reload
    if $SUDO nginx -t >/dev/null 2>&1; then
      if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl reload nginx
      else
        $SUDO nginx -s reload
      fi
      ok "nginx reloaded"
    else
      warn "nginx -t is failing for an unrelated reason; not reloading"
      $SUDO nginx -t 2>&1 | sed 's/^/      /' || true
      exit 1
    fi

    say "Verifying the public URL"
    if check_public; then
      ok "$APP is live"
    else
      warn "nginx is configured but the URL didn't return 200"
      warn "is the container running?  ./deploy.sh --status"
    fi
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

if [ "$WITH_AUTO" = "1" ]; then
  # TB_SHARE is usually a folder INSIDE the mount (e.g. /mnt/payroll/STAMPER),
  # so ask what filesystem it sits on rather than whether it is itself a mount.
  if [ ! -d "$TB_SHARE" ]; then
    warn "$TB_SHARE does not exist — the fetcher will idle until the share is"
    warn "mounted. Set TB_SHARE in .env if it lives elsewhere."
  else
    FSTYPE=""
    if command -v findmnt >/dev/null 2>&1; then
      FSTYPE="$(findmnt -n -o FSTYPE -T "$TB_SHARE" 2>/dev/null || true)"
    elif command -v stat >/dev/null 2>&1; then
      FSTYPE="$(stat -f -c %T "$TB_SHARE" 2>/dev/null || true)"
    fi
    case "$FSTYPE" in
      cifs|smb*|nfs*)  ok "payroll share mounted ($FSTYPE) at $TB_SHARE" ;;
      "")              warn "could not determine what $TB_SHARE sits on — check the mount" ;;
      *)               warn "$TB_SHARE is on a local filesystem ($FSTYPE), not the payroll"
                       warn "share — the fetcher will read an empty folder. Check 'mount -a'." ;;
    esac
    if [ -w "$TB_SHARE" ]; then
      ok "share is writable (handled files can be archived)"
    else
      warn "share is not writable — files will still convert, but each one must"
      warn "be cleared off the share by hand"
    fi
  fi
fi

say "Starting containers"
if [ "${AUTO_INHERITED:-0}" = "1" ]; then
  ok "watcher/fetcher already running — including them so they pick up this build"
fi
RECREATE=""
[ "$DO_BUILD" = "1" ] && RECREATE="--force-recreate"
$DC "${PROFILE_ARGS[@]}" up -d --remove-orphans $RECREATE
if [ "$WITH_AUTO" = "1" ]; then
  ok "UI, folder watcher and share fetcher running"
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

# --- confirm the containers are on the new build ---------------------------
if [ "$DO_BUILD" = "1" ]; then
  say "Verifying containers are running the current code"
  for c in tallybridge-web tallybridge-watcher tallybridge-fetcher; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${c}$"; then
      BUILT="$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null | cut -c1-19)"
      CURRENT="$(docker image inspect -f '{{.Id}}' tallybridge 2>/dev/null | cut -c1-19)"
      if [ -n "$BUILT" ] && [ "$BUILT" = "$CURRENT" ]; then
        ok "$c is on the current image"
      else
        warn "$c is running an OLDER image — recreate it with:"
        warn "  $DC --profile auto up -d --force-recreate"
      fi
    fi
  done
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
