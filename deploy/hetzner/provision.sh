#!/usr/bin/env bash
# Provision (or tear down) the two ApplyForge servers on Hetzner Cloud with the hcloud CLI.
#
#   deploy/hetzner/provision.sh plan      [--config FILE]   print every command; changes nothing (default)
#   deploy/hetzner/provision.sh apply     [--config FILE]   create what is missing (idempotent), print IPs
#   deploy/hetzner/provision.sh close-ssh [--config FILE]   remove the bootstrap SSH rule from both firewalls
#   deploy/hetzner/provision.sh status    [--config FILE]
#   deploy/hetzner/provision.sh destroy   [--config FILE] --confirm <PREFIX>   delete servers + firewalls
#
# The API token is read by hcloud from HCLOUD_TOKEN (export it in the shell; never put it in a file in the
# repository). Config: deploy/hetzner/hetzner.env.example. Everything created carries the label
# applyforge=<PREFIX>; destroy deletes only resources with that label and the expected names.
set -euo pipefail

CMD=${1:-plan}; shift || true
CONFIG=deploy/hetzner/hetzner.env
CONFIRM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG=$2; shift 2 ;;
    --confirm) CONFIRM=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
die() { echo "provision: $*" >&2; exit 1; }
[ -f "$CONFIG" ] || die "config $CONFIG missing (copy deploy/hetzner/hetzner.env.example)"

# KEY=VALUE parser (no shell evaluation of the config).
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  k=${line%%=*}; v=${line#*=}; v=${v%\"}; v=${v#\"}
  [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "bad config line: $k"
  printf -v "C_$k" '%s' "$v"
done < "$CONFIG"
: "${C_PREFIX:?}" "${C_IMAGE:?}" "${C_APP_TYPE:?}" "${C_APP_LOCATION:?}" "${C_BACKUP_TYPE:?}" "${C_BACKUP_LOCATION:?}"
: "${C_SSH_PUBKEY_FILE:?}" "${C_ADMIN_CIDR:?}"
C_BACKUP_SERVER_BACKUPS=${C_BACKUP_SERVER_BACKUPS:-yes}
[[ "$C_PREFIX" =~ ^[a-z][a-z0-9-]{2,20}$ ]] || die "PREFIX: 3-21 chars, lower-case"
[[ "$C_ADMIN_CIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/(3[0-2]|2[4-9])$ ]] || die "ADMIN_CIDR must be your IPv4 /24-/32 (bootstrap SSH only)"
[ "$C_APP_LOCATION" != "$C_BACKUP_LOCATION" ] || echo "WARN: both servers in $C_APP_LOCATION; another location protects against a site outage" >&2

APP=$C_PREFIX-app; BAK=$C_PREFIX-backup; FW_APP=$C_PREFIX-app-fw; FW_BAK=$C_PREFIX-backup-fw; KEY=$C_PREFIX-admin
LABEL="applyforge=$C_PREFIX"

DRY=1; [ "$CMD" = plan ] || DRY=0
run() {   # print, and execute unless planning
  printf '+ %s\n' "$*"
  [ $DRY = 1 ] || "$@"
}
exists() { [ $DRY = 0 ] && hcloud "$1" describe "$2" >/dev/null 2>&1; }
ipv4_of() { if [ $DRY = 1 ]; then echo "<$1-ipv4>"; else hcloud server ip "$1"; fi; }

preflight() {
  command -v hcloud >/dev/null || die "hcloud CLI not installed"
  if [ $DRY = 0 ]; then
    [ -n "${HCLOUD_TOKEN:-}" ] || hcloud context active >/dev/null 2>&1 || die "export HCLOUD_TOKEN (project API token) first"
    hcloud server-type describe "$C_APP_TYPE" >/dev/null || die "unknown server type $C_APP_TYPE"
    hcloud server-type describe "$C_BACKUP_TYPE" >/dev/null || die "unknown server type $C_BACKUP_TYPE"
    hcloud image describe "$C_IMAGE" >/dev/null || die "image $C_IMAGE not available"
  fi
  [ -f "$C_SSH_PUBKEY_FILE" ] || die "SSH public key $C_SSH_PUBKEY_FILE missing"
  grep -q PRIVATE "$C_SSH_PUBKEY_FILE" && die "$C_SSH_PUBKEY_FILE is a PRIVATE key"
  return 0
}

rule() { printf '{"direction":"in","protocol":"%s","port":"%s","source_ips":[%s],"description":"%s"}' "$1" "$2" "$3" "$4"; }
ANY='"0.0.0.0/0","::/0"'
SSH_RULE() { rule tcp 22 "\"$C_ADMIN_CIDR\"" "bootstrap SSH (close-ssh removes)"; }
fw_rules_app() {   # fw_rules_app yes|no (bootstrap SSH)
  local r; r=$(rule udp 51820 "$ANY" "WireGuard devices")
  [ "$1" = yes ] && r="$r,$(SSH_RULE)"
  printf '[%s]' "$r"
}
fw_rules_backup() {   # fw_rules_backup APP_IPV4 yes|no
  local r; r="$(rule udp 51820 "$ANY" "WireGuard admin"),$(rule udp 51821 "\"$1/32\"" "backup link from the app server")"
  [ "$2" = yes ] && r="$r,$(SSH_RULE)"
  printf '[%s]' "$r"
}

set_rules() {   # set_rules FIREWALL JSON
  local f; f=$(mktemp); printf '%s\n' "$2" > "$f"
  printf '+ hcloud firewall replace-rules %s --rules-file <<%s>>\n' "$1" "$(tr -d '\n ' < "$f")"
  [ $DRY = 1 ] || hcloud firewall replace-rules "$1" --rules-file "$f" >/dev/null
  rm -f "$f"
}

create_server() {   # name type location firewall
  if exists server "$1"; then echo "= server $1 exists"; return; fi
  run hcloud server create --name "$1" --type "$2" --image "$C_IMAGE" --location "$3" --ssh-key "$KEY" \
      --firewall "$4" --label "$LABEL" --label "role=${1##*-}"
}

do_apply() {
  preflight
  if exists ssh-key "$KEY"; then echo "= ssh-key $KEY exists"; else run hcloud ssh-key create --name "$KEY" --public-key-from-file "$C_SSH_PUBKEY_FILE" --label "$LABEL"; fi
  for fw in "$FW_APP" "$FW_BAK"; do
    if exists firewall "$fw"; then echo "= firewall $fw exists"; else run hcloud firewall create --name "$fw" --label "$LABEL"; fi
  done
  set_rules "$FW_APP" "$(fw_rules_app yes)"
  create_server "$APP" "$C_APP_TYPE" "$C_APP_LOCATION" "$FW_APP"
  local app_ip; app_ip=$(ipv4_of "$APP")
  set_rules "$FW_BAK" "$(fw_rules_backup "$app_ip" yes)"
  create_server "$BAK" "$C_BACKUP_TYPE" "$C_BACKUP_LOCATION" "$FW_BAK"
  if [ "$C_BACKUP_SERVER_BACKUPS" = yes ]; then run hcloud server enable-backup "$BAK"; fi
  local bak_ip; bak_ip=$(ipv4_of "$BAK")
  cat <<EOF

Servers:  $APP $app_ip   $BAK $bak_ip
Put into /etc/applyforge/site.env on both servers:
  APP_PUBLIC_IPV4=$app_ip
  BACKUP_PUBLIC_IPV4=$bak_ip
Bootstrap SSH (from $C_ADMIN_CIDR only): ssh root@$app_ip / ssh root@$bak_ip
After VPN access is confirmed on both servers: deploy/hetzner/provision.sh close-ssh
EOF
}

do_close_ssh() {
  preflight
  local app_ip; app_ip=$(ipv4_of "$APP")
  set_rules "$FW_APP" "$(fw_rules_app no)"
  set_rules "$FW_BAK" "$(fw_rules_backup "$app_ip" no)"
  echo "SSH is now reachable only through the WireGuard tunnels."
}

do_status() {
  command -v hcloud >/dev/null || die "hcloud CLI not installed"
  hcloud server list -l "$LABEL" -o columns=name,status,ipv4,ipv6,type,location
  hcloud firewall list -l "$LABEL"
}

do_destroy() {
  [ "$CONFIRM" = "$C_PREFIX" ] || die "destroy deletes $APP and $BAK and ALL their data: pass --confirm $C_PREFIX"
  DRY=0; preflight
  local s; for s in "$APP" "$BAK"; do
    if hcloud server describe "$s" -o format='{{index .Labels "applyforge"}}' 2>/dev/null | grep -qx "$C_PREFIX"; then
      run hcloud server delete "$s"; else echo "= server $s absent or not labelled $LABEL; skipped"; fi
  done
  for s in "$FW_APP" "$FW_BAK"; do
    if hcloud firewall describe "$s" >/dev/null 2>&1; then run hcloud firewall delete "$s"; fi
  done
  hcloud ssh-key describe "$KEY" >/dev/null 2>&1 && run hcloud ssh-key delete "$KEY" || true
}

case "$CMD" in
  plan|apply) do_apply ;;
  close-ssh) do_close_ssh ;;
  status) do_status ;;
  destroy) do_destroy ;;
  *) die "plan|apply|close-ssh|status|destroy" ;;
esac
