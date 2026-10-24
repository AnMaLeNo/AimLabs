#!/usr/bin/env bash
# Capture l'écran du Mac (la VRAIE session graphique) et rapatrie le PNG.
#   remote/mac_screenshot.sh [sortie.png]        (défaut : mac_screen.png)
# Passe par le « runner » de mac_job.sh (une seule fenêtre Terminal, déjà autorisée dans Enregistrement de l'écran).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-mac_screen.png}"
KEY="${MAC_KEY:-$HOME/.ssh/id_ed25519_claude}"
bash remote/mac_job.sh 'rm -f /tmp/aim_shot.png; screencapture -x /tmp/aim_shot.png' >/dev/null
scp -q -i "$KEY" "$(bash remote/mac_job.sh --host):/tmp/aim_shot.png" "$OUT"
echo "capture -> $OUT"
