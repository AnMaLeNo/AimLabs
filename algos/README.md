# algos/

Un fichier `.py` = un algo lançable depuis le menu Échap du jeu (le jeu le lance avec
`python` / `python3`, `PYTHONPATH` pointant sur `sdk/python`, et `AIMLABS_SHM` réglé sur
le segment partagé en cours). La première ligne de la docstring sert de description.

- `auto_aim.py` — vise seul la cible la plus proche du réticule ; `CAPSLOCK` on/off,
  `ALT` maintenu = vise tant qu'appuyé, `V` tir auto on/off.
- `skin_bot.py` — bot jouet toujours actif (vise + tire).

Pour pointer le jeu sur un autre dossier : `--algos=/chemin` (ou le bouton « Dossier » du menu).
