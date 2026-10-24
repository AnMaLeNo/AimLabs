#!/usr/bin/env bash
# Copie le code de la chaîne distante vers la machine B (Mac) : remote/, sdk/python, algos/*.py (pas les modèles).
#   remote/sync_mac.sh [utilisateur@hote]     (défaut : $MAC, sinon le premier qui répond parmi le nom Bonjour, l'adresse Tailscale, la dernière IP ; clé : $MAC_KEY)
set -euo pipefail
cd "$(dirname "$0")/.."
KEY="${MAC_KEY:-$HOME/.ssh/id_ed25519_claude}"
DEST="${MAC_DEST:-aimlabs-remote/repo}"
MAC="${1:-${MAC:-}}"
if [ -z "$MAC" ]; then
  for h in 100.98.128.21 192.168.1.127 macbook-air-de-antoine.local; do
    if ssh -o BatchMode=yes -o ConnectTimeout=4 -i "$KEY" "antoine@$h" true 2>/dev/null; then MAC="antoine@$h"; break; fi
  done
fi
[ -n "$MAC" ] || { echo "Mac injoignable (nom Bonjour, Tailscale, dernière IP)" >&2; exit 1; }
tar --exclude=__pycache__ --exclude='*.pyc' -cf - remote sdk/python algos/*.py \
  | ssh -o BatchMode=yes -i "$KEY" "$MAC" "mkdir -p $DEST && cd $DEST && tar -xf -"
echo "synchronisé -> $MAC:$DEST"
