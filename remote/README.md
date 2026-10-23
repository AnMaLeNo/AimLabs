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
images perdues), raisons pour lesquelles B ne tire pas, et les 4 dernières images qui ont décidé un tir (touches `1`–`4` pour
les revoir en grand, `0` pour le direct ; `espace` pause, `s` capture, `m` masques, `c` candidats, `i` lissage, `q` quitter).

```
AIMLABS_AUTOSTART=1 venv/bin/python remote/poc/sniper_b.py --connect listen:7777 --model algos/yolo11n-seg_256.mlpackage --view
```

Options : `--view-fps` (30), `--view-scale` (3), `--view-headless` + `--view-snap DIR` (PNG sans écran, pour tester à distance),
`--view-log fichier.csv` (télémétrie de chaque image), `--view-linger`. Désarmé, la vue montre quand même l'image et les
détections (à la cadence de la fenêtre seulement). Sans `--view`, aucun code de debug ne tourne dans la boucle de B.

**Pourquoi il ne ralentit pas B** (`sniper_view.py`) : la fenêtre est un *processus séparé* en priorité basse (macOS : QoS
« utility », donc cœurs d'efficacité) ; B ne fait que copier des octets en mémoire partagée, *après* avoir envoyé sa commande
(≈ 7 µs par image : un enregistrement de télémétrie de 64 octets, et un instantané complet — image BGR d'origine, sorties du
réseau — seulement quand la fenêtre en réclame un, ≤ 30/s, protocole demande/accusé : B n'écrit jamais pendant que la fenêtre
lit). L'image décisive d'un tir est conservée à part. Si la fenêtre se ferme ou meurt, B continue ; si B est tué, la fenêtre
s'arrête et supprime la mémoire partagée. Le rendu coûte 1,6 ms sur le M5 (panneau de droite recalculé à 8 Hz seulement).

**Coût mesuré** (M5, même session de jeu rejouée, 4 tours entrelacés de 45 s ; `ab_bench.py`) : réception→commande de B

| Configuration | réception→commande | inférence | CPU de la vue |
|---|---|---|---|
| ancien `sniper_b` (avant ce travail) | 0,972 ms | 0,931 ms | – |
| `sniper_b` courant, sans `--view` | 0,570–0,585 ms | 0,525–0,540 ms | – |
| `--view`, sans fenêtre (`--view-headless`) | 0,572 ms | 0,531 ms | 5 % d'un cœur |
| `--view`, fenêtre réelle 30 images/s | 0,589–0,600 ms | 0,538 ms | 25 % |
| `--view`, fenêtre réelle 15 images/s | 0,591 ms | 0,530 ms | 17 % |

La vue elle-même ne coûte rien mesurable (7 µs sur le thread de décision ; sans fenêtre, aucune différence avec le mode normal).
Une **fenêtre affichée** ajoute ≈ 0,01–0,02 ms (2–3 % de B, ≈ 0,2 % de la chaîne de ≈ 7 ms) quels que soient la cadence et
l'agrandissement : c'est la présence d'une fenêtre à l'écran (macOS), pas le rendu. En direct (jeu réel, 30 traversées) le taux de
touches ne bouge pas : 36 m/s, 27 et 30/30 sans vue, 30 et 30/30 avec ; 48 m/s, 23 et 30/30 sans vue, 29 et 29/30 avec (bruit ±3).
`--view-fps 15` allège encore la charge ; ne pas laisser la fenêtre au premier plan d'une machine déjà chargée.

Le gain « hors debug » vient d'une correction : `Image.fromarray` de la vue RGB renversée de `cv2.imdecode(...)[:, :, ::-1]`
coûtait 496 µs par image ; `Detector` convertit maintenant l'image BGR d'origine (34 µs, pixels identiques) : B passe de
0,97 à 0,58 ms.

## Outils de mesure (`poc/`)

* `replay_agent.py` : rejoue des images enregistrées par `agent_a.py --record` vers B (même protocole, même cadence), sans jeu
  ni capture : mêmes entrées pour comparer deux réglages de B. `recinfo.py` résume un enregistrement.
* `ab_bench.py` : banc A/B de B sur le Mac (configurations entrelacées sur plusieurs tours, statistiques de B, CPU par processus).
* `live_bench.py` : session de jeu complète (B sur le Mac, jeu, agent, nettoyage) : touches sur N traversées + statistiques de B, pour
  des configurations de `ab_bench.py` (ex. avec / sans `--view`).
* `sync_mac.sh` : copie `remote/`, `sdk/python` et `algos/*.py` vers le Mac.

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
