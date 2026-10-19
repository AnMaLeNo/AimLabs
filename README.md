# AimLabs

FPS dont le vrai jeu est… d'écrire la meilleure aide à la visée.

Le joueur voit une partie de FPS classique. En parallèle, le jeu expose l'image
rendue et un canal d'entrée (souris / tir) à un programme externe : la pipeline
de vision du joueur. Le meilleur joueur est celui dont le programme vise le mieux.

## État du projet

**Partie 1 — sandbox FPS** (fait) : déplacement, tir hitscan, cibles humanoïdes
statiques sur un monde plat.

**Partie 3 — graphismes Kingdom Archives** (fait, à valider sur GPU) : les 29 agents
(3e personne, animés, hitboxes calculées sur le squelette) alignés face au joueur,
les 24 armes posées au sol, et une vue première personne fidèle au viewer du site :
bras de l'agent choisi + arme sur le bone `R_WeaponMaster`, caméra sur le bone `Camera`,
animations equip / fire / reload / inspect, cadence (`FireRate`), mode automatique ou
semi-automatique (`AutomaticWeapon`) et durées de reload / equip propres à chaque arme.
Personnage et arme se choisissent dans le menu Échap. Les assets sont dans
`game/assets/ka/` (≈430 Mo, textures en WebP q92, GLB intacts) ; voir « Assets » plus bas.

**Partie 2 — AimBridge** (fait, à valider sur GPU) : GDExtension C++ (`bridge/`)
qui publie chaque frame rendue et l'état du joueur dans une **mémoire partagée**
et applique les commandes `move(dx, dy)` / `fire()` qu'un programme externe y écrit.
Le jeu distingue la souris réelle de la souris « assistée »
(`Player.LookSource.REAL` / `ASSIST`) et mesure la latence frame → commande.
SDK Python dans `sdk/python/` (`AimClient`).

## Lancer

Godot 4.7 (https://godotengine.org/download). Ouvrir `game/project.godot` dans
l'éditeur et appuyer sur F5, ou en ligne de commande :

```sh
godot --path game
```

Sans GPU (VM, CI) : `godot --path game --rendering-method gl_compatibility`.

Commandes : WASD déplacer · Shift sprint · Espace saut · clic gauche tirer (maintenu
pour les armes automatiques) · R recharger · F inspecter · T réinitialiser les cibles ·
Échap menu (personnage, arme, algos ; libère la souris) / clic hors du menu pour reprendre.

Options : `-- --agent=<slug> --weapon=<slug>` (défaut `jett` / `classic`, slugs = noms
des dossiers de `game/assets/ka/agents/` et `weapons/`).

### Maps

* **Vitrine** (défaut) : les 29 agents en ligne, les 24 armes au sol.
* **Stand de tir** : une seule cible (agent au choix, pose neutre — le site n'a pas
  d'animation de marche) qui se déplace devant le joueur en lui faisant toujours face,
  pour tester / débugger un algorithme dans des conditions reproductibles. Mouvements :
  *Immobile*, *Gauche-droite* (distance du trajet), *Carré* (côté), *Cercle* (rayon),
  plus la vitesse (m/s, le long du trajet) et la distance joueur → centre du motif.
  Tout se règle en direct dans le menu Échap (lignes « Map » et « Cible »), ou en CLI :
  `-- --map=range --target=reyna --pattern=circle --size=3 --speed=2 --distance=10`
  (`--pattern` : `none` | `line` | `square` | `circle`).

### Benchmark « tir auto » (reproductible, une commande)

Pour les algos qui **ne font que tirer** (pas de souris) : la caméra est figée droit devant,
la cible traverse le réticule de gauche à droite ; à chaque distance la vitesse monte palier
par palier (× 1,25), 4 passages par palier, et quand **aucun** des 4 passages n'est touché la
cible est éloignée de 5 m et la vitesse repart du début. Fin quand même la vitesse la plus
lente rate, ou à 40 m. Résultat : vitesse max touchée par distance, dans le HUD, la console
et `bench_results/trigger_<algo>_<date>.json` (détail tirs / touches / headshots par passage).

```bash
godot --path game -- --map=range --bench-trigger --algo=yolo_trigger.py          # lance l'algo, attend sa connexion, mesure, quitte
godot --path game -- --map=range --bench-trigger --bench-oracle --bridge=off      # référence : tire à chaque frame (plafond de l'arme)
godot --path game -- --map=range --bench-trigger --algo=yolo_trigger.py --weapon=spectre --bench-passes=3 --bench-speed-factor=1.5 --no-quit
```

Réglages `--bench-distance-start/-step/-max`, `--bench-speed-start/-factor/-max`,
`--bench-passes`, `--bench-settle`, `--bench-pause`, `--bench-out=<json>`. Depuis le menu Échap
(map Stand de tir) : bouton **Benchmark tir auto**, qui mesure l'algo en cours — ou toi, à la
souris, si aucun algo ne tourne. Le mode `--bench-trigger` pose `AIMLABS_AUTOSTART=1` dans
l'environnement de l'algo pour qu'il démarre armé sans CapsLock. Le plafond est fixé par la
cadence de l'arme (l'oracle rate déjà à 10 m / 4 m/s avec le Classic) : comparer les algos à
arme égale.

## Assets (Kingdom Archives)

Modèles, textures et animations proviennent de https://kingdomarchives.com/models
(propriété de Riot Games, redistribués par le site ; projet non commercial).
`tools/fetch_assets.py` les télécharge et écrit `game/assets/ka/manifest.json`
(matériaux PBR, réglages des armes, animations FP par arme) :

```sh
pip install pillow
python tools/fetch_assets.py          # cache brut dans .ka_cache/, sortie dans game/assets/ka/
godot --headless --path game --import
```

Deux entrées du site n'ont pas de vue première personne (`spike`, `veto-fp-ult`) :
elles sont posées au sol mais absentes du sélecteur d'arme.

## AimBridge (interface pour l'aide à la visée)

Le jeu démarre le pont automatiquement (désactiver : `-- --bridge=off`). Côté programme :

```python
from aimlabs import AimClient          # pip install -e sdk/python  (numpy)

with AimClient() as c:                 # s'attache à la mémoire partagée "aimlabs"
    while True:
        f = c.frame()                  # attend la frame suivante : f.image = (H, W, 4) uint8 RGBA
        dx, dy = ma_pipeline(f.image)  # ... vision ...
        c.move(dx, dy, frame_id=f.id)  # pixels de souris (x droite, y bas)
        c.fire(frame_id=f.id)          # un tir (cadence de l'arme respectée)
        s = c.state()                  # pos, vel, yaw, pitch, touches, souris réelle/assistée,
                                       # shots/hits/kills, latence mesurée par le jeu...
```

* **Capture** : sur Vulkan/D3D12 (Forward+/Mobile), lecture GPU→CPU asynchrone
  (`RenderingDevice.texture_get_data_async`, ne bloque pas le rendu) ; en Compatibility
  (OpenGL), lecture synchrone. La copie en mémoire partagée se fait dans un thread dédié.
* **Mémoire partagée** : `/dev/shm/aimlabs` (Linux) ou mapping nommé `aimlabs` (Windows).
  Layout dans `bridge/src/protocol.h` (miroir `sdk/python/aimlabs/protocol.py`) :
  en-tête, bloc état (seqlock), anneau de commandes (client → jeu), 3 slots de frame (seqlock).
  N'importe quel langage capable de mmap peut s'y brancher.
* **Latence** : chaque commande porte l'id de la frame qui l'a motivée ; le jeu mesure
  `capture → application` avec sa propre horloge (affiché dans le HUD et dans `state()`).
* **Modes de jeu** (`-- --mode=<nom>`) : `full` (défaut), `lowres` (image fournie en 640×360),
  `tiny` (320×180), `aim_only` (tir refusé), `fire_only` (visée refusée), `blind` (pas d'image).
  Les commandes refusées sont comptées dans `cmds_dropped`. Définis dans `game/scripts/game.gd`.
* **Clavier / souris du joueur** : l'état publie les touches enfoncées (keycodes Godot,
  disposition *et* physiques) et les boutons de souris, pour que la pipeline réagisse aux
  touches du joueur (modes, réglages, « vise seulement tant que j'appuie »…) :

  ```python
  k = c.keys()                 # instantané : k.down, k.physical_down, k.mouse_down (frozensets de noms)
  if "CAPSLOCK" in k.just_pressed: enabled = not enabled    # transitions depuis le keys() précédent
  if "ALT" in k: ...           # maintenu ; noms : "A".."Z", "0".."9", "SPACE", "SHIFT", "CTRL",
                               # "ALT", "F1".."F12", "KP_0"…, souris "LEFT"/"RIGHT"/"MIDDLE"/"XBUTTON1"…
  ```

  L'état est échantillonné à chaque frame du jeu : un appui plus court qu'une frame peut
  être manqué.
* **Exemples** : `sdk/python/examples/bridge_selftest.py` (test de bout en bout) et le dossier
  `algos/` (voir ci-dessous).

### Menu Échap : lancer ses algos sans quitter le jeu

`Échap` ouvre le menu **Algos** : il liste les `*.py` du dossier `algos/` (première ligne de la
docstring en description), **Lancer / Basculer / Relancer / Arrêter** le process depuis le jeu,
affiche sa sortie (stdout + stderr, donc les tracebacks) et le statut (en cours / terminé avec
code), et permet de changer de mode de jeu à chaud. Le jeu lance le script avec `python3`
(`python` sous Windows, ou `--python=<cmd>` / `AIMLABS_PYTHON`), `PYTHONPATH` sur `sdk/python`
et `AIMLABS_SHM` sur le segment courant ; il le tue en quittant. L'algo continue de jouer
pendant que le menu est ouvert (pratique pour lire son log en direct). Options : `--algos=<dossier>`, `--algo=<fichier.py>`
(lance au démarrage).

* `algos/auto_aim.py` — vise seul la cible la plus proche du réticule : `CAPSLOCK` on/off,
  `ALT` maintenu = vise tant qu'appuyé, `V` tir auto on/off. Heuristique couleur (pixels
  « peau »), à remplacer par ta propre vision.
* `algos/skin_bot.py` — bot jouet toujours actif (vise + tire), juste pour prouver la boucle.
* `algos/yolo_aim.py` — **détection de personnes par YOLO** (Ultralytics, modèle COCO pré-entraîné
  `yolo11n.pt` par défaut, GPU/FP16 si CUDA) : vise la tête de la cible la plus proche du réticule,
  conversion pixels→souris exacte (FOV + sensibilité lus dans l'état partagé), extrapolation du
  mouvement de la cible × latence capture→commande. `CAPSLOCK` on/off, `ALT` maintenu, `V` tir auto,
  `H` tête/corps. Installer `torch` avec CUDA (https://pytorch.org/get-started/locally/) puis
  `pip install -r algos/requirements-yolo.txt` (+ `pip install tensorrt` pour le moteur rapide).
  Sur CUDA l'algo exporte et met en cache un moteur TensorRT FP16 (`yolo11n_384x640.engine`,
  2–4 min la première fois, à faire jeu fermé avec `python algos/yolo_aim.py --export`) et fait
  pré-traitement + NMS lui-même : **5 ms/frame mesurés sur
  RTX 3080** (yolo11n comme yolo11s) contre 17–29 ms via `model.predict()`, dont le surcoût
  Python/CUDA sous Windows domine (`--no-trt` pour comparer). Réglages : `AIMLABS_YOLO_MODEL`
  (`yolo11s.pt`, un `.engine` déjà exporté…), `AIMLABS_YOLO_IMGSZ`, `AIMLABS_YOLO_DEVICE`. Banc
  hors jeu : `python algos/yolo_aim.py --bench <dossier_png>` donne les ms/frame et le taux de
  détection.
* `algos/yolo_trigger.py` — **tir auto YOLO sans toucher la souris** : même détecteur (TensorRT),
  tire dès que le réticule est dans une boîte « person », boîte avancée de sa vitesse écran ×
  latence capture→tir (`--no-lead`), `--margin 0.6` pour ne tirer que sur le torse. `CAPSLOCK`
  on/off, `ALT` maintenu. C'est l'algo de référence du benchmark « tir auto » ci-dessus.

### Compiler le pont

Binaries Linux et Windows x86_64 (release) sont commités dans `game/bin/` et rebuildés par
la CI (`.github/workflows/bridge.yml`). Pour recompiler :

```sh
git submodule update --init          # bridge/godot-cpp
pip install scons
cd bridge
scons platform=linux target=template_release api_version=4.7 -j8
scons platform=windows arch=x86_64 target=template_release api_version=4.7 use_mingw=yes -j8  # cross-compile
```

## Vérifications automatiques

Test de bout en bout du pont (lance le jeu, lit des frames, envoie `move` + `fire`, vérifie
l'état, mesure la latence) :

```sh
python sdk/python/examples/bridge_selftest.py --launch --godot godot [--mode lowres]
# sur une machine sans GPU : --godot-args "--rendering-method gl_compatibility"
```

```sh
# importe les assets (à faire une fois, ou après ajout d'assets)
godot --headless --path game --import

# vise une cible via le canal « assist », tire, vérifie le headshot ; code retour 0/1
godot --path game --rendering-method gl_compatibility -- --smoke-test --screenshot=/tmp/smoke.png

# capture d'écran seule
godot --path game --rendering-method gl_compatibility -- --screenshot=/tmp/shot.png
```

## Structure

```
game/
  project.godot          réglages, actions d'entrée, couches physiques
  scenes/main.tscn       racine : World + Player + HUD
  scenes/player.tscn     CharacterBody3D, caméra, viewmodel FP (SubViewport, FOV du viewer)
  scripts/player.gd      mouvement, visée (REAL/ASSIST), tir hitscan auto/semi, recul
  scripts/viewmodel.gd   bras FP de l'agent + arme (bone R_WeaponMaster), anims, cadence, muzzle
  scripts/enemy.gd       agent 3e personne animé, hitboxes capsules sur le squelette, PV, respawn
  scripts/world.gd       monde plat, maps « vitrine » (29 agents, 24 armes) et « stand de tir »
  scripts/target_mover.gd déplace la cible du stand de tir (ligne / carré / cercle, face au joueur)
  scripts/trigger_bench.gd benchmark « tir auto » : traversées de plus en plus rapides puis lointaines → JSON
  scripts/ka_assets.gd   lit manifest.json, instancie les GLB, matériaux PBR, retarget d'anims
  scripts/hud.gd         réticule, marqueur de touche, FPS, stats
  scripts/game.gd        câblage, modes de jeu, options --smoke-test / --screenshot / --mode / --algo
  scripts/algo_runner.gd lance/arrête les algos (process + pipes stdout/stderr)
  scripts/algo_menu.gd   menu Échap : liste, Lancer/Arrêter, log, mode
  assets/ka/             Kingdom Archives : agents/, agents_fp/, weapons/, manifest.json
  assets/weapons/vandal/   ancien viewmodel « VALORANT Weapon Vandal Rifle » par 零宽不连字 / LkblZ (Sketchfab Standard, voir license.txt), plus utilisé
  bin/                   aimbridge.gdextension + binaires Linux/Windows
bridge/
  src/protocol.h         layout de la mémoire partagée (version 2 : + clavier/souris)
  src/aim_bridge.*       nœud AimBridge : capture, thread de publication, commandes, état
  src/shared_memory.*    shm POSIX / file mapping Windows
  godot-cpp/             sous-module (bindings C++, API 4.7)
sdk/python/
  aimlabs/client.py      AimClient (frame, state, keys, move, fire)
  aimlabs/vision.py      heuristique « peau » partagée par les algos d'exemple
  examples/              bridge_selftest.py
algos/                   scripts lancés depuis le menu Échap : auto_aim.py, skin_bot.py, yolo_aim.py, yolo_trigger.py (YOLO)
bench_results/           sorties JSON du benchmark « tir auto » (ignoré par git)
tools/fetch_assets.py    télécharge/convertit les assets Kingdom Archives
```

Couches physiques : 1 `world`, 2 `player`, 3 `enemy_body`, 4 `enemy_head`.
Dégâts : corps 34, bras 20, tête 100 (PV 100).
