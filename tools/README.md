# Registre des outils

Chaque outil écrit « à côté » du jeu pour combler un manque est listé ici, avec **le besoin** qui l'a fait naître. Quand plusieurs
outils expriment le même besoin, c'est le signe que le jeu (ou le SDK) devrait le fournir : la colonne « côté jeu ? » garde cette idée.
À tenir à jour à chaque nouvel outil (et à chaque suppression : dire pourquoi).

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `tools/fetch_assets.py` | récupérer les assets du jeu | – |
| `remote/level1/screencap.py` | niveau 1 du mode « deux machines » : sortir l'image par l'écran, sans lien avec le jeu | non : c'est le principe du mode |
| `remote/poc/agent_a.py` | agent de référence du PC A (capture → réseau → commandes → SDK) | non |
| `remote/poc/sniper_b.py` | algo de référence de la machine B (Core ML) | non |
| `remote/poc/latency_probe.py`, `net_echo.py`, `age_probe.py`, `mac_infer_bench.py` | isoler une à une les latences de la chaîne (transport, réseau, âge de l'image, inférence) | un banc de latence intégré au jeu éviterait `age_probe` |
| `remote/poc/replay_agent.py` + `agent_a.py --record` (2026-10-24) | **rejouer des images réelles enregistrées vers B, sans jeu ni capture d'écran** : comparer deux réglages de B (mode normal / mode debug…) *à entrée identique*, en rafales de plusieurs milliers d'images, sans le bruit d'une session de jeu. Il a aussi servi à isoler le plantage de l'agent (voir ci-dessous) | peut-être : un « mode relecture » du jeu qui publie des images enregistrées servirait aussi aux algos locaux |
| `remote/poc/recinfo.py` | lire un enregistrement (cadence, taille, trous) | – |
| `remote/sync_mac.sh` | pousser le code vers le Mac (la copie du Mac avait dérivé : `remote.py` et `agent_a.py` en retard) | un `--sync` du dépôt ou un paquet installable |

## Bogues trouvés en chemin

* **`agent_a.py` mourait en silence à sa première ligne de stats** quand sa sortie est redirigée sous Windows (console en cp1252 :
  le « → » de « capture→cmd » ne s'encode pas), puis `os._exit(0)` avalait la trace. Côté B cela ressemblait à « jeu fermé »
  (le Mac voyait la connexion se fermer ~2 s après le début). Corrigé : sortie en UTF-8 et trace affichée.
* `dxcam` et `mss` manquaient dans l'environnement conda `yolo11` du PC (dépendances documentées de `remote/level1/requirements.txt`) :
  installés.
