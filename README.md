# AimLabs

FPS dont le vrai jeu est… d'écrire la meilleure aide à la visée.

Le joueur voit une partie de FPS classique. En parallèle, le jeu expose l'image
rendue et un canal d'entrée (souris / tir) à un programme externe : la pipeline
de vision du joueur. Le meilleur joueur est celui dont le programme vise le mieux.

## État du projet

**Partie 1 — sandbox FPS** (ce dépôt, en cours) : déplacement, tir hitscan,
cibles humanoïdes statiques, arène quasi infinie avec blocs et murs.

**Partie 2 — AimBridge** (à venir) : GDExtension C++ qui copie chaque frame dans
une mémoire partagée et accepte des commandes `move(dx, dy)` / `shoot()`, avec un
SDK Python. Le jeu distingue déjà la souris réelle de la souris « assistée »
(`Player.LookSource.REAL` / `ASSIST`).

## Lancer

Godot 4.7 (https://godotengine.org/download). Ouvrir `game/project.godot` dans
l'éditeur et appuyer sur F5, ou en ligne de commande :

```sh
godot --path game
```

Sans GPU (VM, CI) : `godot --path game --rendering-method gl_compatibility`.

Commandes : WASD déplacer · Shift sprint · Espace saut · clic gauche tirer ·
T réinitialiser les cibles · Échap libérer / clic recapturer la souris.

## Vérifications automatiques

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
  scripts/game.gd        câblage + options --smoke-test / --screenshot
  assets/characters/     Kenney « Blocky Characters » (CC0)
  assets/weapons/vandal/   « VALORANT Weapon Vandal Rifle » par 零宽不连字 / LkblZ (Sketchfab Standard, voir license.txt)
```

Couches physiques : 1 `world`, 2 `player`, 3 `enemy_body`, 4 `enemy_head`.
Dégâts : corps 34, bras 20, tête 100 (PV 100).
