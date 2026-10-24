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
| `remote/poc/ab_bench.py` (2026-10-24) | **comparer des configurations de B à entrée identique**, entrelacées sur plusieurs tours (le bruit de la machine et la dérive thermique ne favorisent personne), avec le CPU de chaque processus : a chiffré le coût du mode debug et la correction de la conversion d'image | non : outil de mesure |
| `remote/poc/live_bench.py` (2026-10-24) | **lancer proprement une session de jeu complète** (B, jeu, agent dans le bon ordre + nettoyage) : mes lancements manuels se télescopaient (agent lancé avant le relais, benchmark démarré à vide, processus restants) | oui, probablement : un « mode benchmark distant » du jeu qui attend le relais et sait se relancer |
| `remote/poc/sniper_view.py` (2026-10-24) | fenêtre debug de B (`sniper_b.py --view`) : voir ce que l'IA reçoit, détecte et décide | non : concerne l'algo du joueur ; mais il révèle ce que le jeu pourrait publier pour le debug (cf. profil `training`) |
| `remote/mac_job.sh` (2026-10-24) | **exécuter un script dans la VRAIE session graphique du Mac** (fenêtres, capture d'écran, clavier) : depuis SSH on n'y est pas, je ne pouvais donc rien vérifier de visible. Un seul Terminal « runner » (mes premiers essais ouvraient une fenêtre par tâche, qui s'empilaient et ne se fermaient pas) | oui, peut-être : un « mode vérification visuelle » du jeu / SDK qui sait se capturer lui-même |
| `remote/mac_screenshot.sh` (2026-10-24) | capturer l'écran du Mac et rapatrier le PNG : base de toute itération sur des faits visuels | – |
| `remote/poc/view_check.py` (2026-10-24) | vérifier la fenêtre debug sur la vraie session : captures à chaque étape, touches simulées (envoyées seulement si la fenêtre est au premier plan), survie de B | non : spécifique à la vue |
| `remote/poc/pm_check.py` (2026-10-24) | savoir sur QUELS cœurs (E / S) tourne chaque configuration (`powermetrics`, `sudo` sans mot de passe pour cette commande) : a réfuté mon hypothèse « la fenêtre reste sur les cœurs d'efficacité » | – |
| `remote/poc/recinfo.py` | lire un enregistrement (cadence, taille, trous) | – |
| `remote/sync_mac.sh` | pousser le code vers le Mac (la copie du Mac avait dérivé : `remote.py` et `agent_a.py` en retard) | un `--sync` du dépôt ou un paquet installable |

## Bogues trouvés en chemin

* **`agent_a.py` mourait en silence à sa première ligne de stats** quand sa sortie est redirigée sous Windows (console en cp1252 :
  le « → » de « capture→cmd » ne s'encode pas), puis `os._exit(0)` avalait la trace. Côté B cela ressemblait à « jeu fermé »
  (le Mac voyait la connexion se fermer ~2 s après le début). Corrigé : sortie en UTF-8 et trace affichée.
* `dxcam` et `mss` manquaient dans l'environnement conda `yolo11` du PC (dépendances documentées de `remote/level1/requirements.txt`) :
  installés.
* **`_spin` (processus « chauffeur » de `sniper_b.py`) devenait orphelin** quand B était tué (`pkill`) et brûlait un cœur du Mac
  indéfiniment (deux processus, dont un actif depuis plus de 3 h, trouvés sur le Mac). Il s'arrête maintenant avec son parent.
* **`Image.fromarray(vue RGB renversée)` coûtait 496 µs par image** dans `Detector` (contre 34 µs depuis le BGR d'origine) : 0,97 → 0,58 ms par image sur B.
* **La fenêtre debug tournait à 14 images/s avec 381 ms de retard, alors que j'annonçais 30** (vu seulement quand j'ai pu capturer l'écran) : la QoS
  « utility » multiplie par 10 la marge des minuteries, `cv2.waitKey`/`pollKey` bloquent ≈ 15 ms sous Cocoa, et mon protocole demande/accusé s'effondrait
  dès que les réveils étaient tardifs. Corrigé (latence « tier 0 », attente par `pollKey`, publication à rythme fixe) : 30 images/s mesurées.
* **Le Mac change d'adresse IP** et son nom `.local` ne se résout pas toujours depuis Windows : refus SSH intermittents. Les outils essaient Tailscale, la dernière IP,
  puis le nom Bonjour, avec des nouveaux essais sur les erreurs SSH passagères.
* **`ssh` avale l'entrée standard** : un script passé par un tube à un wrapper qui appelait `ssh` avant de le lire était perdu (tâche vide, sans erreur).
