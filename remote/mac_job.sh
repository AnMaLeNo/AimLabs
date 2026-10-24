#!/usr/bin/env bash
# Exécute un script dans la VRAIE session graphique du Mac (fenêtres, capture d'écran, clavier) et rapporte sa sortie.
#   remote/mac_job.sh 'screencapture -x /tmp/x.png'        # une commande
#   remote/mac_job.sh < script.sh                          # un script sur l'entrée standard
#   remote/mac_job.sh --host                               # imprime utilisateur@hôte (Tailscale, dernière IP ou nom Bonjour)
#   remote/mac_job.sh --stop                               # arrête le « runner » (sa fenêtre reste ouverte : la fermer à la main)
#
# Pourquoi : depuis SSH on n'est pas dans la session graphique (`screencapture`, fenêtres OpenCV, touches échouent ou ne se voient pas).
# Un seul Terminal « runner » est ouvert (une fois) ; il surveille ~/aimlabs-remote/runner/job.sh et l'exécute : pas une nouvelle fenêtre
# par tâche (les fenêtres Terminal terminées s'empilaient et ignoraient `close`). Le runner hérite des autorisations données à Terminal
# (Enregistrement de l'écran, Accessibilité) dans Réglages Système > Confidentialité et sécurité. MAC / MAC_KEY comme sync_mac.sh.
set -euo pipefail
# La tâche est lue AVANT tout appel ssh : ssh avale l'entrée standard (sans cela, un script passé par un tube était perdu).
JOB=""
case "${1:-}" in --host|--stop) ;; *) if [ $# -gt 0 ]; then JOB="$*"; else JOB="$(cat)"; fi ;; esac
JOB="${JOB//$''/}"      # un appelant Windows envoie des CRLF, que bash sur le Mac prend pour des arguments
KEY="${MAC_KEY:-$HOME/.ssh/id_ed25519_claude}"
MAC="${MAC:-}"
if [ -z "$MAC" ]; then
  for h in 100.98.128.21 192.168.1.127 macbook-air-de-antoine.local; do
    if ssh -n -o BatchMode=yes -o ConnectTimeout=4 -i "$KEY" "antoine@$h" true 2>/dev/null; then MAC="antoine@$h"; break; fi
  done
fi
[ -n "$MAC" ] || { echo "Mac injoignable" >&2; exit 1; }
if [ "${1:-}" = "--host" ]; then echo "$MAC"; exit 0; fi      # pour les outils qui ont besoin de l'adresse trouvée (scp…)
ssh_r() { local rc i; for i in 1 2 3; do ssh "$@" && return 0; rc=$?; [ $rc -ne 255 ] && return $rc; sleep 1; done; return 255; }   # refus passager : on réessaie
SSH=(ssh_r -o BatchMode=yes -i "$KEY" "$MAC")
R='~/aimlabs-remote/runner'

if [ "${1:-}" = "--stop" ]; then
  "${SSH[@]}" "touch $R/stop"; echo "runner arrêté"; exit 0
fi

# 1. le runner tourne-t-il ? sinon on le lance (une seule fenêtre Terminal)
alive=$("${SSH[@]}" "mkdir -p $R; p=\$(cat $R/pid 2>/dev/null); [ -n \"\$p\" ] && kill -0 \$p 2>/dev/null && echo 1 || echo 0")
if [ "$alive" != "1" ]; then
  "${SSH[@]}" "cat > ~/aimlabs-remote/runner.command && chmod +x ~/aimlabs-remote/runner.command" <<'EOS'
#!/bin/bash
D=~/aimlabs-remote/runner
mkdir -p $D; echo $$ > $D/pid; rm -f $D/stop
echo "AimLabs runner (pid $$) : exécute les tâches de ~/aimlabs-remote/runner/job.sh. Fermer cette fenêtre l'arrête."
while [ ! -f $D/stop ]; do
  if [ -f $D/job.sh ]; then
    mv $D/job.sh $D/job.run
    bash $D/job.run > $D/job.log 2>&1
    echo $? > $D/job.rc
    touch $D/job.done
  fi
  sleep 0.2
done
echo "runner arrêté, vous pouvez fermer cette fenêtre"
EOS
  "${SSH[@]}" "open -g -a Terminal ~/aimlabs-remote/runner.command"
  for _ in $(seq 1 40); do
    [ "$("${SSH[@]}" "p=\$(cat $R/pid 2>/dev/null); [ -n \"\$p\" ] && kill -0 \$p 2>/dev/null && echo 1 || echo 0")" = "1" ] && break
    sleep 0.5
  done
fi

# 2. la tâche (lue plus haut)
"${SSH[@]}" "rm -f $R/job.done $R/job.rc $R/job.log; cat > $R/job.tmp && mv $R/job.tmp $R/job.sh" <<<"$JOB"
for _ in $(seq 1 600); do
  [ "$("${SSH[@]}" "[ -f $R/job.done ] && echo 1 || echo 0")" = "1" ] && break
  sleep 0.5
done
"${SSH[@]}" "cat $R/job.log; exit \$(cat $R/job.rc 2>/dev/null || echo 99)"
