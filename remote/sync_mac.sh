#!/usr/bin/env bash
# Copie le code de la chaîne distante vers la machine B (Mac) : remote/, sdk/python, algos/*.py (pas les modèles).
#   remote/sync_mac.sh [utilisateur@hote]        (défaut : $MAC ou antoine@192.168.1.7 ; clé : $MAC_KEY)
set -euo pipefail
cd "$(dirname "$0")/.."
MAC="${1:-${MAC:-antoine@192.168.1.7}}"
KEY="${MAC_KEY:-$HOME/.ssh/id_ed25519_claude}"
DEST="${MAC_DEST:-aimlabs-remote/repo}"
tar --exclude=__pycache__ --exclude='*.pyc' -cf - remote sdk/python algos/*.py \
  | ssh -o BatchMode=yes -i "$KEY" "$MAC" "mkdir -p $DEST && cd $DEST && tar -xf -"
echo "synchronisé -> $MAC:$DEST"
