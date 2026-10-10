#!/bin/bash
# Wire the Telegram alert bot into prod-aus-cartwatch once its token is in Infisical, then verify.
#
# Needs: HOMELAB_KOMODO_API_KEY / HOMELAB_KOMODO_API_SECRET, an Infisical machine identity
# (INFISICAL_CLIENT_ID / INFISICAL_CLIENT_SECRET) and ssh to node b (winrarhost).
# Order matters: Komodo fails a deploy closed on a reference to a secret that does not exist
# (even on a commented line), so this script checks the secret before touching the stack env.
set -euo pipefail
APPS=76b1ebe1-3656-4cef-952c-30d5d489c6e7
NAME=HOMELAB_AUS_CARTWATCH_TELEGRAM_TOKEN
STACK=prod-aus-cartwatch
KOMODO=${KOMODO_URL:-http://100.92.54.45:3011}
export INFISICAL_API_URL=${INFISICAL_API_URL:-http://100.92.54.45:8400}

token=$(infisical login --method=universal-auth --client-id="$INFISICAL_CLIENT_ID" \
  --client-secret="$INFISICAL_CLIENT_SECRET" --plain --silent)
if ! infisical export --projectId="$APPS" --env=prod --format=json --token="$token" | jq -e --arg n "$NAME" 'any(.[]; .key == $n and (.value | length) > 0)' >/dev/null; then
  echo "apps/prod/$NAME does not exist (or is empty) yet: create the bot and store its token first." >&2
  exit 1
fi
echo "secret $NAME present"

K=(-H "X-Api-Key: $HOMELAB_KOMODO_API_KEY" -H "X-Api-Secret: $HOMELAB_KOMODO_API_SECRET" -H "Content-Type: application/json")
env=$(curl -sf "$KOMODO/read" "${K[@]}" -d "{\"type\":\"GetStack\",\"params\":{\"stack\":\"$STACK\"}}" | jq -r .config.environment)
if grep -q '^TELEGRAM_TOKEN=' <<<"$env"; then
  echo "stack env already has TELEGRAM_TOKEN"
else
  env="$env
TELEGRAM_TOKEN=[[infisical://apps/prod/$NAME]]"
  jq -n --arg env "$env" --arg s "$STACK" '{type:"UpdateStack",params:{id:$s,config:{environment:$env}}}' \
    | curl -sf "$KOMODO/write" "${K[@]}" -d @- >/dev/null
  echo "added TELEGRAM_TOKEN ref to $STACK"
fi

id=$(curl -sf "$KOMODO/execute" "${K[@]}" -d "{\"type\":\"DeployStack\",\"params\":{\"stack\":\"$STACK\"}}" | jq -r '._id."$oid"')
for _ in $(seq 1 60); do
  update=$(curl -sf "$KOMODO/read" "${K[@]}" -d "{\"type\":\"GetUpdate\",\"params\":{\"id\":\"$id\"}}")
  [ "$(jq -r .status <<<"$update")" = Complete ] && break
  sleep 3
done
[ "$(jq -r .success <<<"$update")" = true ] || { echo "deploy failed: see Komodo update $id" >&2; exit 1; }
echo "deployed; checking the bot (no message is sent)"
sleep 15
ssh -o BatchMode=yes winrarhost "docker exec ${STACK}-aus-cartwatch-1 python -m aus_cartwatch telegram check" 2>&1 \
  | grep -viE "post-quantum|store now|vuln|upgraded|openssh.com"
