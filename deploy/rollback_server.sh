#!/usr/bin/env bash
set -euo pipefail

# Roll back one Lightsail server from a known-good snapshot.
# Usage:
#   ./rollback_server.sh <instance> <snapshot> <static_ip_name> <fqdn> [region]

INSTANCE="${1:-}"
SNAPSHOT="${2:-}"
STATIC_IP_NAME="${3:-}"
FQDN="${4:-}"
REGION="${5:-us-east-1}"

if [[ -z "$INSTANCE" || -z "$SNAPSHOT" || -z "$STATIC_IP_NAME" || -z "$FQDN" ]]; then
  echo "Usage: $0 <instance> <snapshot> <static_ip_name> <fqdn> [region]"
  exit 1
fi

log(){ echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "Verify snapshot exists"
aws lightsail get-instance-snapshot --region "$REGION" --instance-snapshot-name "$SNAPSHOT" >/dev/null

log "Delete existing instance if present"
if aws lightsail get-instance --region "$REGION" --instance-name "$INSTANCE" >/dev/null 2>&1; then
  aws lightsail delete-instance --region "$REGION" --instance-name "$INSTANCE" >/dev/null
  for i in $(seq 1 90); do
    if ! aws lightsail get-instance --region "$REGION" --instance-name "$INSTANCE" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
fi

log "Create instance from snapshot"
aws lightsail create-instances-from-snapshot \
  --region "$REGION" \
  --instance-names "$INSTANCE" \
  --availability-zone "${REGION}a" \
  --instance-snapshot-name "$SNAPSHOT" \
  --bundle-id "medium_3_0" >/dev/null

log "Wait for running"
for i in $(seq 1 120); do
  ST=$(aws lightsail get-instance --region "$REGION" --instance-name "$INSTANCE" --query 'instance.state.name' --output text)
  [[ "$ST" == "running" ]] && break
  sleep 5
done
[[ "$ST" == "running" ]] || { log "Instance failed to reach running"; exit 1; }

log "Open required ports"
for p in \
  "fromPort=22,toPort=22,protocol=tcp" \
  "fromPort=80,toPort=80,protocol=tcp" \
  "fromPort=443,toPort=443,protocol=tcp" \
  "fromPort=22124,toPort=22124,protocol=udp"; do
  aws lightsail open-instance-public-ports --region "$REGION" --instance-name "$INSTANCE" --port-info "$p" >/dev/null || true
done

log "Attach static IP"
aws lightsail attach-static-ip --region "$REGION" --static-ip-name "$STATIC_IP_NAME" --instance-name "$INSTANCE" >/dev/null
IP=$(aws lightsail get-static-ip --region "$REGION" --static-ip-name "$STATIC_IP_NAME" --query 'staticIp.ipAddress' --output text)

log "Update DNS"
if [[ -f /home/nds/deploy/.cf.env ]]; then
  # shellcheck disable=SC1091
  set -a; source /home/nds/deploy/.cf.env; set +a
fi
CF_API_TOKEN="${CF_API_TOKEN:-${CF_DNS_API_TOKEN:-}}" \
  /home/nds/deploy/cloudflare_dns_upsert.py "$FQDN" "$IP" false >/dev/null

log "Done: $INSTANCE -> $IP ($FQDN)"
echo "ROLLBACK_OK instance=$INSTANCE ip=$IP fqdn=$FQDN snapshot=$SNAPSHOT region=$REGION"
