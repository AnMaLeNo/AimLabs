# AimLabs

FPS dont le vrai jeu est… d'écrire la meilleure aide à la visée.

Le joueur voit une partie de FPS classique. En parallèle, le jeu expose l'image
rendue et un canal d'entrée (souris / tir) à un programme externe : la pipeline
de vision du joueur. Le meilleur joueur est celui dont le programme vise le mieux.

## État du projet

**Partie 1 — sandbox FPS** (ce dépôt, en cours) : déplacement, tir hitscan,
cibles humanoïdes statiques, arène quasi infinie avec blocs et murs.

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

Commandes : WASD déplacer · Shift sprint · Espace saut · clic gauche tirer ·
T réinitialiser les cibles · Échap libérer / clic recapturer la souris.

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
* **Exemples** : `sdk/python/examples/bridge_selftest.py` (test de bout en bout),
  `skin_bot.py` (bot jouet : masque de pixels « peau », recentre, tire — ~15 % de précision,
  juste pour prouver la boucle).

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
  scenes/player.tscn     CharacterBody3D, caméra, viewmodel de l'arme
  scripts/player.gd      mouvement, visée (REAL/ASSIST), tir hitscan, recul
  scripts/enemy.gd       cible humanoïde : hitboxes tête/corps/bras, PV, respawn
  scripts/world.gd       génération procédurale (seed), lumière, ciel, brouillard
  scripts/hud.gd         réticule, marqueur de touche, FPS, stats
  scripts/game.gd        câblage, modes de jeu, options --smoke-test / --screenshot / --mode
  assets/characters/     Kenney « Blocky Characters » (CC0)
  bin/                   aimbridge.gdextension + binaires Linux/Windows
bridge/
  src/protocol.h         layout de la mémoire partagée (version 1)
  src/aim_bridge.*       nœud AimBridge : capture, thread de publication, commandes, état
  src/shared_memory.*    shm POSIX / file mapping Windows
  godot-cpp/             sous-module (bindings C++, API 4.7)
sdk/python/
  aimlabs/client.py      AimClient (frame, state, move, fire)
  examples/              bridge_selftest.py, skin_bot.py
```

Couches physiques : 1 `world`, 2 `player`, 3 `enemy_body`, 4 `enemy_head`.
Dégâts : corps 34, bras 20, tête 100 (PV 100).
