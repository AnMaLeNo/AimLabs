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
| `remote/poc/capture_ctl.py`, `capture_cfg.py`, `sniper_setup.py`, `capture_ctl_test.py` (2026-10-25) | **cadrer la capture sans éditer le code** : la fenêtre du jeu n'avait qu'un titre exact (« AimLabs (DEBUG) ») et le carré était toujours centré dessus ; maintenant écran ou fenêtre (motif de titre), recadrage à la main et aperçu de ce que reçoit l'algo, depuis B (`sniper_b.py --setup`), avec canal de réglage A↔B (`CTRL/INFO/PREVIEW`) | oui, en partie : un jeu qui annonce lui-même la position de son réticule et de sa fenêtre rendrait le réglage inutile ; en attendant, c'est le seul moyen de cadrer sans rien lire dans le jeu |
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

## Chaîne de tir par le Raspberry Pi (2026-10-24)

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `remote/poc/clocks.py`, `clock_sync.py` | rapprocher les horloges de deux machines (PC / Pi / Mac) par échanges UDP au RTT minimal : `time.time_ns()` de Python ≤ 3.12 sous Windows n'a que ~15 ms de résolution, `clocks.wall_ns()` appelle `GetSystemTimePreciseAsFileTime` | – |
| `remote/poc/kbd_probe_win.py` | **vérité terrain côté PC** : horodate (QPC) chaque touche reçue par un crochet bas niveau, pour mesurer le chemin « Pi → Bluetooth HID → Windows » | oui, probablement : le jeu pourrait journaliser lui-même l'instant de chaque entrée reçue (mode benchmark) |
| `remote/pi/bt_probe_send.py` | côté Pi de la mesure : frappes à intervalle connu par le socket local du relais, avec l'instant d'envoi | – |
| `remote/poc/bt_latency_report.py` | apparier envois et réceptions en recalant les horloges : moyenne, écart-type, percentiles, histogramme | – |

### Faits mesurés sur le chemin Bluetooth (Pi 5 → Intel AX200 du PC)

* Wi-Fi du Pi actif (2,4 GHz, canal 11) : latence **14,1 ms en moyenne, écart-type 10,4 ms, 2 à 80 ms** (300 frappes). Passer le Pi en maître du lien (`hcitool sr … master`) ou interdire le mode sniff ne change presque rien (12,6 ms / 7,9 ms ; 13,2 ms / 9,7 ms).
* **Wi-Fi du Pi coupé : 5 à 7 ms en moyenne, écart-type ≈ 2,6 ms, 2 à 11 ms** (100 frappes ; une valeur à 72 ms au moment de la coupure). C'est la coexistence Wi-Fi / Bluetooth de la puce CYW43455 qui étale la latence.
* Trace HCI (`btmon`) : le contrôleur du Pi n'accuse la transmission qu'**23 à 50 ms** après la remise du paquet (par lots de deux), Wi-Fi actif. Écho de LED (ScrollLock) : aller-retour 22,8 ms (σ 13,6 ms), symétrique.
* Réseau : Mac (Wi-Fi) → Pi (Wi-Fi) 17,7 ms de moyenne, jusqu'à 99,5 ms, 3,3 % de pertes ; PC (Ethernet) → Pi (Wi-Fi) 12 ms de moyenne ; débit TCP PC → Pi 11 Mbit/s sur un lien annoncé à 195.

### Serveur de tir du Pi (2026-10-24) : `remote/pi/` + `remote/poc/pi_fire.py`

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `remote/pi/fire_server.py` (+ 30 lignes dans `btkbd.py`) | **API réseau pour tirer par le Pi** : B envoie un ordre UDP signé (HMAC, liste blanche de touches, anti-rejeu) « appuie sur la touche à l'instant T », le Pi l'exécute avec sa propre horloge (SCHED_FIFO, attente active) ; fusion avec le clavier relayé ; keep-alive du lien | oui : la touche de tir configurable du jeu (voir `game/`) |
| `remote/poc/pi_fire.py` | client côté B : synchronisation d'horloge NTP (RTT minimal + droite de dérive), ordres signés envoyés deux fois, ACK avec le retard réel d'exécution | – |
| `remote/poc/pi_fire_test.py` | test du chemin complet B → Pi → Bluetooth → Windows avec vérité terrain côté PC | – |

Faits mesurés (Wi-Fi du Pi sur 5 GHz, Pi esclave du lien, mêmes protocoles) :
* **Inactivité du lien : après ~6 s sans trafic, la première frappe arrive 68 ms plus tard (65 à 72 ms, très stable)** ; après ≤ 2 s elle arrive en ~7 ms. Un rapport HID envoyé toutes les 100 ms (l'état *courant* des touches) supprime l'effet : 5,5 ms en moyenne (σ 2,3 ms) quelle que soit la veille. Aucune modification des réglages d'énergie de Windows n'a été nécessaire.
* Chemin complet (ordres espacés de 3 s, keep-alive actif) : **40/40 ordres livrés, latence de l'échéance demandée à la touche vue par Windows : 4,8 ms en moyenne, écart-type 2,9 ms, de −0,7 à 11,5 ms ; exécution sur le Pi à +0,007 ms ± 0,005 de l'échéance** (injection 0,04 ms).
* Le Wi-Fi du Pi perd des paquets par rafales (0 % puis 27 % de pertes sur deux essais de 100 ordres, 60 % de réponses manquantes pendant une synchronisation) : **un câble Ethernet sur le Pi est indispensable** pour un ordre de tir fiable.
* Piège : `time.monotonic()` n'a que 15,6 ms de résolution sous Windows (Python ≤ 3.12) ; les horloges des outils utilisent `time.perf_counter_ns()`.
* Piège de mesure : le rapport doit apparier les frappes **par le temps**, pas par l'ordre (une frappe perdue décalait tout et donnait des latences de 7 s), et attendre la fin de la sonde (son fichier est écrit à la fermeture).

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `remote/poc/live_pi.py` | **session complète en mode silent** (B, jeu `--mode=silent`, agent `--no-bridge`) avec une vérification que la fenêtre du jeu a le **focus clavier** avant d'armer (sinon l'ordre de tir taperait « g » dans une autre application) ; rend touches, erreur de timing et mesures du Pi | oui, probablement : un mode « benchmark silent » du jeu qui attend ses entrées et donne la fiche |
| `remote/poc/shot_timing.py` | **précision de tir mesurée par le jeu** (vérité terrain indépendante de la chaîne) : `path_x × sens / vitesse` du 1er tir de chaque passage → biais et gigue de toute la chaîne ; sert à calibrer `--bias-ms` | oui : le jeu pourrait l'afficher lui-même dans sa fiche de benchmark |

### Pièges et corrections de ce chantier (2026-10-24)

* **Le Wi-Fi du Pi a rebasculé en 2,4 GHz tout seul** après une reconnexion locale (la 5 GHz n'a pas répondu assez vite) : toutes les mesures suivantes étaient dégradées (latence Bluetooth 13–16 ms, σ 7,5 ms au lieu de 3–4 ms, 4/30 touches au lieu de 28/30) jusqu'à ce que j'aie lu le journal de `wpa_supplicant`. Connexion fixée sur 5 GHz (`nmcli … 802-11-wireless.band a`) avec un minuteur systemd de rattrapage armé avant. À surveiller : `nmcli -t -f IN-USE,CHAN,FREQ dev wifi list | grep '^\*'`.
* Le canal 5 GHz du Pi (116, 5580 MHz) est une bande **DFS** avec un signal faible (−67 dBm) : pertes par rafales. Un câble Ethernet sur le Pi est le vrai remède.
* `pkill -f motif` dans une commande SSH tue le shell distant lui-même (le motif figure dans sa ligne de commande) → code 255 sans message. Séparer l'appel, ou écrire `[m]otif`.
* `fire_server` : un ordre déjà en retard **remplaçait** l'ordre valide en attente, qui était alors perdu ; il est maintenant refusé sans le remplacer. L'annulation ne vise que les ordres plus anciens (numéro de séquence).
* Le rapport de latence appariait par l'ordre (une frappe perdue décalait tout → latences de 7 s) ; il apparie maintenant par le temps et compte les pertes. Il faut aussi attendre la fin de la sonde (fichier écrit à la fermeture).
* Les patchs de fichiers par script avec des `\n` dans des chaînes ont échoué en silence plusieurs fois ; l'outil d'édition est plus sûr pour ces cas.
