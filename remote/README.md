# remote/ — mode « deux machines »

Le jeu tourne sur le **PC A**, l'algo sur une **autre machine B** (Mac, Linux, un serveur GPU…). Le jeu ne
fournit **aucune image** ni aucune information sur elle (`-- --mode=remote`) : il accepte seulement les commandes (`move`, `fire`, `mark`) par le
pont habituel (mémoire partagée du PC A). Tout le reste — comment sortir l'image du PC A, la transporter,
la décoder, où et comment calculer, comment ramener la décision — est le sujet du mode : c'est là que se gagne
la latence.

```
 PC A (jeu)                                              machine B (algo)
 ┌────────────┐  écran   ┌───────────────┐  réseau   ┌──────────────┐
 │ godot      │ ───────► │ capture       │ ────────► │ algo         │
 │ --mode=    │          │ (niveau 1)    │           │ (YOLO, …)    │
 │  remote    │ ◄─────── │ relais → SDK  │ ◄──────── │              │
 └────────────┘ commandes└───────────────┘ commandes └──────────────┘
```

## Niveaux

| Niveau | Ce que le jeu fournit | Ce que le joueur écrit |
|---|---|---|
| **1** | `level1/screencap.py` : capture l'écran (fenêtre du jeu, carré central), **sans aucun lien avec le jeu** | transport vers B, algo sur B, retour des commandes, script du PC A qui parle au SDK |
| **2** | rien : le jeu tourne et accepte des commandes | tout, y compris la capture d'image |

`poc/` est une **implémentation de référence complète** (preuve de concept et base de mesure), pas un
passage obligé : n'importe quel transport / codage / langage fait l'affaire tant que le script du PC A pousse
des commandes dans le SDK (Python `aimlabs.AimClient`, Rust `sdk/rust/aimlabs`).

## Lancer la chaîne de référence

**Machine B** (testé : MacBook Air M5, Python 3.12 ; Python 3.15 n'a pas de roues torch/coremltools) :

```
python3.12 -m venv venv && venv/bin/pip install numpy opencv-python torch==2.7.0 torchvision==0.22.0 ultralytics coremltools
# une fois : export du modèle (YOLO11n-seg 256×256, FP16) pour le Neural Engine
venv/bin/python remote/poc/mac_infer_bench.py --pt algos/yolo11n-seg.pt     # écrit algos/yolo11n-seg_256.mlpackage + compare les moteurs
# l'algo ÉCOUTE ; c'est l'agent du PC A qui se connecte (aucune règle de pare-feu entrante sur le PC)
AIMLABS_AUTOSTART=1 venv/bin/python remote/poc/sniper_b.py --connect listen:7777 --model algos/yolo11n-seg_256.mlpackage
```

**PC A** (Windows) : `pip install -r remote/level1/requirements.txt`, puis

```
godot --path game --always-on-top -- --map=range --mode=remote
python remote/poc/agent_a.py --connect <ip de B>:7777 --transport tcp --jpeg 60 --crop 384 --resize 256
```

(`--always-on-top` : une capture d'écran voit ce qui est devant la fenêtre.) Benchmark :

```
godot --path game --always-on-top -- --map=range --mode=remote --bench-sniper --bench-wait-client \
      --bench-criterion=hit --bench-distance-start=5 --bench-distance-max=5 --bench-heights=chest \
      --bench-fixed-speed=36 --bench-passes=30
```

`--bench-fixed-speed=S --bench-passes=N` fait N traversées à la vitesse S et affiche `touches/N` (un taux de
réussite, bien plus stable que la recherche de la vitesse limite, qui sature et sort des valeurs aberrantes à haute
vitesse). `--bench-wait-client` attend le relais ; garder `--bench-criterion=hit` : un `mark` distant n'est pas
rattaché à une image connue du jeu. Pour lancer un algo **existant sans le modifier** sur B :
`python remote/poc/run_remote_algo.py --connect listen:7777 algos/yolo_sniper.py` (remplace `AimClient` par
`aimlabs.remote.RemoteClient`).

## Mode debug de B : `sniper_b.py --view`

Une fenêtre sur la machine B montre **ce que l'IA reçoit, détecte et décide** : image reçue (agrandie ×3, pixels réels), toutes
les boîtes (les gardées par B, plus les candidats faibles en gris), masques de segmentation, cible choisie, zone de tir, ligne
du masque lue au niveau du réticule, vitesse mesurée et position prédite au moment du tir, suivi, état de la décision
(armé / verrouillé / refroidissement / tir programmé dans X ms), latences (inférence, réception→commande, écart entre images,
images perdues), raisons pour lesquelles B ne tire pas, et les 4 dernières images qui ont décidé un tir.
Touches : `1`–`4` revoir en grand l'image d'un tir, `0` direct, `espace` pause, `s` capture, `m` masques, `c` candidats, `i` lissage, `q` quitter.

```
AIMLABS_AUTOSTART=1 venv/bin/python remote/poc/sniper_b.py --connect listen:7777 --model algos/yolo11n-seg_256.mlpackage --view
```

Options : `--view-fps` (30), `--view-scale` (3), `--view-qos` (utility), `--view-headless` + `--view-snap DIR` (PNG sans écran),
`--view-log fichier.csv` (télémétrie de chaque image), `--view-linger`. Désarmé, la vue montre quand même l'image et les détections.
Sans `--view`, aucun code de debug ne tourne dans la boucle de B. La fenêtre (1442×814) tient sur un écran de 1470×956 ; `--view-scale 2` sinon.

**Architecture** (`sniper_view.py`) : la fenêtre est un *processus séparé* ; B ne fait que copier des octets en mémoire partagée,
*après* avoir envoyé sa commande : un enregistrement de télémétrie de 64 octets par image, et un instantané complet (image BGR
d'origine, sorties du réseau) toutes les 1/(2 × fps) s dans deux tampons alternés — pas de poignée de main avec la fenêtre, donc
rien qui dépende de ses minuteries ; un compteur de version détecte une lecture déchirée. L'image qui a décidé un tir est conservée
à part. Si la fenêtre se ferme ou meurt, B continue ; si B est tué, la fenêtre s'arrête et supprime la mémoire partagée.

**Ce qui a été mesuré sur le M5** (la vraie fenêtre, vue par captures d'écran, `view_check.py`) :
* Cadence réelle : 30 rendus/s, 33,3 ms entre rendus (max 36), un instantané frais par rendu, rendu 1,3 ms + `imshow` 0,8 ms.
  Pièges de macOS trouvés en chemin : `cv2.waitKey` *et* `cv2.pollKey` bloquent ≈ 15 ms (le programme s'en sert comme temps d'attente) ;
  la QoS « utility » seule fait dépasser `sleep(10 ms)` de 73 ms (14 images/s au lieu de 30) — la *latence* « tier 0 »
  (`THREAD_LATENCY_QOS_POLICY`) la ramène à 2 ms sans changer la classe de débit.
* **Coût pour B** (réception→commande, même session rejouée, tours entrelacés, `ab_bench.py`) :

| Configuration | réception→commande | écart à « sans vue » |
|---|---|---|
| ancien `sniper_b` (avant ce travail) | 0,972 ms | – |
| `sniper_b` courant, sans `--view` | 0,567 ms | 0 |
| `--view` sans fenêtre (`--view-headless`) | 0,572 ms | indiscernable |
| fenêtre à 15 images/s | 0,580 ms | +0,013 ms |
| fenêtre à 20 images/s | 0,585 ms | +0,019 ms |
| fenêtre à 30 images/s (défaut) | 0,597 ms | +0,030 ms |

  Le coût de la fenêtre est proportionnel à sa cadence (≈ 1 µs par image affichée), soit ≈ 0,3 % de la chaîne complète (≈ 7 ms) à 30
  images/s. Ce n'est ni le GPU de B (`--units CPU_AND_NE` ne change rien), ni la classe de priorité du processus (« utility » +
  latence, « default » et « background » donnent la même latence). `powermetrics` montre où va le travail : le processus de la fenêtre
  prend ≈ 0,24 cœur sur les cœurs d'efficacité, **mais** les cœurs « Super » (ceux de B) gagnent ≈ 0,22 cœur et la puissance GPU passe
  de 1 à 17 mW : c'est le compositeur de fenêtres de macOS qui travaille à chaque rafraîchissement, sur les cœurs rapides, quoi
  qu'on demande au processus. On ne peut donc pas le déplacer, seulement le réduire (`--view-fps 15`).
* En direct avec le vrai jeu (30 traversées) le taux de touches ne change pas : 36 m/s, 27 et 30/30 sans vue, 30 et 30/30 avec ;
  48 m/s, 23 et 30/30 sans vue, 29 et 29/30 avec (bruit ±3).

Le gain « hors debug » vient d'une correction : `Image.fromarray` de la vue RGB renversée de `cv2.imdecode(...)[:, :, ::-1]`
coûtait 496 µs par image ; `Detector` convertit maintenant l'image BGR d'origine (34 µs, pixels identiques) : B passe de 0,97 à 0,57 ms.

**Prérequis sur le Mac pour tester la fenêtre à distance** (une fois) : Terminal autorisé dans *Confidentialité et sécurité →
Enregistrement de l'écran* et *Accessibilité* ; `sudo` sans mot de passe pour `/usr/bin/powermetrics` (optionnel). `remote/mac_job.sh`
exécute un script dans la session graphique (un seul Terminal « runner », jamais une fenêtre par tâche), `remote/mac_screenshot.sh` capture
l'écran, `poc/view_check.py` vérifie la fenêtre (captures, touches, survie de B), `poc/pm_check.py` mesure l'activité par cœur.

## Mode silent : le jeu ne publie rien, le tir passe par le Raspberry Pi

`--mode=silent` : **aucun pont**, aucune image, aucun état, aucune commande ; le jeu ne réagit qu'à de vrais évènements d'entrée. Le joueur doit donc lui-même
capturer l'image, décider, et **appuyer sur la touche de tir** : ici via le Pi, qui se fait passer pour un clavier Bluetooth auprès du PC.

```
 PC A (jeu --mode=silent)         machine B (Mac)                    Raspberry Pi 5                    PC A
 ┌──────────┐ capture d'écran ┌───────────────────┐  UDP signé   ┌────────────────────┐  Bluetooth   ┌──────────┐
 │ godot    │ ──────────────► │ sniper_b.py       │ ───────────► │ btkbd + fire_server│ ───────────► │ touche G │
 │ (rien)   │  agent_a        │ YOLO + décision   │ « appuie à   │ exécute à l'instant│  HID clavier │ = tir    │
 └──────────┘  --no-bridge    │ --fire-via pi:IP  │   l'instant T»│ T (horloge du Pi)  │              └──────────┘
                              └───────────────────┘              └────────────────────┘
```

* **Jeu** : `--mode=silent` (force `--bridge=off` et le profil d'information « réel »), touche de tir configurable (`--fire-key=G`, ou bouton « Touche de tir » du menu Échap,
  sauvegardée dans `user://settings.cfg`), qui s'ajoute au clic gauche dans l'action `fire`. *Le clic droit est l'action « viser » du jeu, pas le tir.*
* **PC A** : `agent_a.py --no-bridge ...` (capture d'écran et envoi des images à B, rien d'autre).
* **B** : `sniper_b.py --fire-via pi:HOTE[:PORT]` (réglages : `--pi-usage` touche HID, `--pi-lead-ms` avance Bluetooth, `--pi-return-ms` marge réseau B→Pi, `--pi-late-ms`
  tolérance de retard, `--bias-ms`). B synchronise son horloge avec celle du Pi (méthode NTP, `pi_fire.py`) et demande « touche à l'instant T de l'horloge de B » :
  le Pi l'exécute avec sa propre horloge, la gigue du réseau disparaît tant que l'ordre arrive avant l'échéance.
* **Pi** (`remote/pi/`) : `fire_server.py` (UDP 7780, ~270 lignes) branché sur le relais existant `btkbd.py` (≈ 60 lignes ajoutées : fusion des touches injectées avec celles du clavier
  relayé, keep-alive du lien, bascule en maître du lien, commandes de diagnostic). Sécurité : chaque paquet porte un HMAC-SHA256 (secret `/etc/btkbd/fire.json`, mode 0600 ;
  côté B `~/.aimlabs/pi_fire.key`, jamais dans le dépôt), **liste blanche de touches** (par défaut la seule touche de tir), appui borné, anti-rejeu, annulation limitée aux ordres plus anciens.
  Modifications du Pi, toutes réversibles : original sauvegardé dans `/opt/btkbd/btkbd.py.orig-20261024` ; `sudo rm /etc/btkbd/fire.json` désactive le serveur de tir ;
  la connexion Wi-Fi `preconfigured` est fixée sur la bande 5 GHz (`sudo nmcli con modify preconfigured 802-11-wireless.band ""` pour revenir à l'automatique).
* **Lancer une session complète** : `python remote/poc/live_pi.py --speed 36 --passes 30` (B, jeu, agent ; vérifie que la fenêtre du jeu a le focus clavier avant d'armer quoi que ce soit ;
  rend les touches, l'erreur de timing mesurée par le jeu, `remote/poc/shot_timing.py`, et les mesures du Pi).

### Ce qui a été mesuré (Pi 5, Intel AX200 du PC, Mac M5 ; détails : `tools/README.md`)

| Maillon | Mesure |
|---|---|
| Exécution sur le Pi | à **+0,005 ms** de l'échéance (±0,002), injection 0,03 ms |
| Bluetooth Pi → Windows, Pi maître du lien, Wi-Fi du Pi en 5 GHz, keep-alive | **médiane 2,9 à 3,9 ms**, 70 % des frappes entre 2 et 4 ms, queue jusqu'à ~12 ms (σ 2,3 ms) |
| idem, Pi esclave du lien | 5,7 ms (étalement uniforme 2–12 ms, σ 2,6 ms) |
| **Wi-Fi du Pi en 2,4 GHz** (canal 11) | 14 ms en moyenne, étalement 2–35 ms, σ 10 ms : la puce CYW43455 partage la radio entre Wi-Fi et Bluetooth |
| **Lien inactif ≥ 3–6 s sans keep-alive** | la 1re frappe arrive **68 ms** plus tard (65–72 ms) ; un rapport HID de l'état courant toutes les 100 ms l'évite |
| Réseau Mac (Wi-Fi) → Pi (Wi-Fi) | RTT 17,7 ms en moyenne, jusqu'à 99,5 ms, 3 % de pertes ; PC (Ethernet) → Pi (Wi-Fi) : pertes par rafales (0 à 57 %) |
| Précision de tir de bout en bout (36 m/s, 30 passages, Wi-Fi/Wi-Fi) | 28/30 (Wi-Fi 5 GHz, bonne passe), biais +3,2 ms, σ 3,9 ms ; l'ancienne chaîne (agent A) : 30/30, σ 1,6 ms |

**Limites actuelles et ce qui les lève.** Le créneau utilisable est court : à 36 m/s la cible traverse le champ capturé en ~50 ms, il reste ~20 ms entre la première détection fiable et le passage au
réticule, et il doit contenir l'acheminement B → Pi **plus** l'avance Bluetooth (~3,5 ms). Avec un Wi-Fi lossy des deux côtés (marge réseau 6–10 ms), B n'émet un ordre que dans 12 à 20 passages sur 30 ;
avec une marge plus courte, 20 à 40 % des ordres arrivent après leur échéance. **Un câble Ethernet sur le Pi** (et, mieux, sur le Mac) ramène l'acheminement à ~1–2 ms et libère la radio du Pi.
Autre levier, plus radical : une liaison **USB filaire** (le Pi en périphérique HID, ~1 ms, gigue quasi nulle) à la place du Bluetooth. Sans Ethernet, **ne pas laisser le Pi basculer en 2,4 GHz**
(il le fait de lui-même si la 5 GHz ne répond pas assez vite à une reconnexion : c'est arrivé pendant les tests).

## Outils de mesure (`poc/`)

* `replay_agent.py` : rejoue des images enregistrées par `agent_a.py --record` vers B (même protocole, même cadence), sans jeu
  ni capture : mêmes entrées pour comparer deux réglages de B. `recinfo.py` résume un enregistrement.
* `ab_bench.py` : banc A/B de B sur le Mac (configurations entrelacées sur plusieurs tours, statistiques de B, CPU par processus).
* `live_bench.py` : session de jeu complète (B sur le Mac, jeu, agent, nettoyage) : touches sur N traversées + statistiques de B, pour
  des configurations de `ab_bench.py` (ex. avec / sans `--view`).
* `sync_mac.sh` : copie `remote/`, `sdk/python` et `algos/*.py` vers le Mac. Les outils trouvent le Mac seuls : adresse Tailscale (fixe), dernière IP du réseau
  local, puis nom Bonjour (le `.local` ne se résout pas toujours depuis Windows) ; `MAC=utilisateur@hôte` force le choix.

* `latency_probe.py` : renvoie un `mark` par image, sans inférence → coût pur du transport.
* `net_echo.py` : RTT réseau seul TCP/UDP selon la taille des messages (sans capture ni inférence).
* `mac_infer_bench.py` : latence d'inférence Core ML (Neural Engine / GPU / CPU) et PyTorch MPS.
* `age_probe.py` : âge du contenu d'une capture d'écran, avec le pont interne comme vérité terrain.

## Résultats (PC A : RTX 3080, écran 2560×1440 @ 164 Hz, Ethernet ; B : MacBook Air M5 en Wi-Fi 5 GHz)

**Score** : touches sur 30 traversées, cible à 5 m, une seule balle autorisée. Le profil d'information `real` est le
profil de référence : **le jeu ne donne rien d'autre que les touches** (ni état, ni horloge, ni FPS, ni métadonnées d'image,
ni texte de HUD ; `sdk/python/examples/info_check.py --expect real` le vérifie). La chaîne distante n'utilise donc que des
horloges locales (agent du PC et Mac).

| Vitesse de la cible | 8 m/s | 16 | 24 | 36 | 48 | 64 |
|---|---|---|---|---|---|---|
| PC seul, pont interne, TensorRT (`sniper-rs`) — profil **real** | 25 | – | 30 | 30 | 30 | 29 |
| **PC + Mac distant** — profil **real** | 29 | – | 28 | 30 | 28 | 25 |
| PC seul — profil training (état du jeu disponible) | 27 | 27 | 28 | 29 | 30 | 27 |
| PC + Mac distant — profil training (état disponible, avant la version sans état) | 30 | 29 | 30 | 30 | 29 | 23 |

La chaîne distante égale le PC seul jusqu'à 48 m/s dans les deux profils ; l'écart aux vitesses extrêmes est la limite
physique de la capture d'écran (voir plus bas). Bruit d'échantillonnage : ±2 touches.

**Latence** (mesurée sur le PC, par traversée) : capture → commande reçue **≈ 7 ms** (p99 8–10 ms ; pointes
occasionnelles à 17 ms en Wi-Fi), dont encodage JPEG 0,55 ms, réseau ≈ 5 ms (Wi-Fi), inférence + décision sur le Mac
**0,93 ms**. Précision du tir (écart-type du décalage entre l'instant du tir et le passage de la cible au
réticule) : 1,2–2,8 ms, contre 1,4–2,8 ms pour le PC seul.

### Ce qui a compté (dans l'ordre d'effet)

1. **Mac : le ralentissement à froid.** Un Mac sollicité seulement toutes les 6 ms baisse ses horloges : l'inférence
   Core ML passe de **0,43 ms (en rafale) à 1,5 ms**, et à **3,8 ms** dans la chaîne réelle. Un **processus
   « chauffeur » séparé** (`--warm cpu`, une boucle à vide au QoS maximal) ramène 0,9 ms dans la chaîne ; un thread du
   même processus ne suffit pas (GIL). Pas de dérive sur 4 minutes en continu (MacBook Air, sans ventilateur).
2. **Neural Engine** : 0,40 ms contre 2,4 ms CPU, 3,4 ms GPU Core ML, 9,3 ms PyTorch MPS (YOLO11n-seg 256²,
   FP16 ; écart avec la référence PyTorch ≤ 0,005 de confiance sur les meilleures détections).
3. **Champ plus large** : capturer 384 px et les réduire à 256 avant l'envoi (`--crop 384 --resize 256`) garde la
   cible visible plus longtemps sur 165 images/s : 4 → 12 touches sur 20 à 48 m/s, sans coût réseau ni inférence.
4. **Tir programmé** (`fire_at`) : au lieu de tirer « dès que c'est sûr » (quantifié au pas de 6 ms de la capture),
   B calcule l'instant où le centre de la cible croise le réticule et l'agent du PC attend cet instant.
   Le jitter de timing passe de 3,5–4 ms à ≈ 2 ms ; avec la bande de tolérance à 0,7 : 24 → 29–30 sur 30 à 48 m/s.
5. **Transport** : JPEG qualité 60 (≈ 6 Ko) ; les pixels bruts (170 Ko) saturent le Wi-Fi (RTT 20–97 ms).
   Encodage : conversion BGR directe et `INTER_LINEAR` (0,44 ms → 0,10 ms pour la réduction).
6. Réglages de l'algo : latence supplémentaire 3–5 ms (plateau), `--lead-scale` 1,0, `--conf` 0,5, `--track` 2 :
   s'en écarter fait perdre 4 à 10 touches sur 30.

### Essayé sans gain

* **UDP** (images en datagrammes, commandes dédoublées) : meilleur sur le réseau seul (2,5 ms contre 3,6 ms pour
  200 o) mais **moins bon dans la chaîne** (21–22 contre 24–25 sur 30, RTT p99 21 ms) : conservé dans le protocole
  (`--transport udp`), pas recommandé.
* JPEG qualité 40 ou 92, champ de 320 ou 448 px, `INTER_AREA` : dans le bruit ou moins bon.
* `--lead-scale` 0,85 / 1,15, `--track 1`, `--conf 0,4` : moins bon.

### Limites et pistes

* **Physique de la capture d'écran** : une image par rafraîchissement de l'écran (164 Hz ici, contre ~380/s pour le
  pont interne) et un contenu **en retard de 3,0–3,6 ms en moyenne, écart-type 1,5–1,7 ms** sur le rendu (mesuré avec
  `age_probe.py` — outil de développement qui lit le pont interne — et, indépendamment, avec le code temporel du profil
  `training`, qui donne 3,63 ± 1,48 ms). En profil `real` le jeu ne donne **aucune** information sur l'image : ce biais
  et cette gigue sont à estimer et à compenser par le joueur (ici : `--bias-ms`, `--lag-ms`, calibrés avec le benchmark).
* **Entraînement** : pour construire ce genre de chaîne, le profil `training` (menu Échap, ou `--info=training`) donne
  tout : état complet, métadonnées d'image, textes du HUD, code temporel dans l'image
  (`game/scripts/timecode.gd`, cellules de 4 px à 110 px au-dessus du réticule ; décodeur et mesure d'âge :
  `sdk/python/examples/timecode_decode.py`) et vérité terrain en UDP (`127.0.0.1:7790`). Chaque source se coche séparément.
* Le Mac était en **Wi-Fi** : un Ethernet (adaptateur USB-C) réduirait la gigue ; non mesuré.
* L'agent du PC tourne en priorité haute et attend activement les derniers ms avant un tir programmé (un cœur).
