# Registre des outils

Chaque outil écrit « à côté » du jeu pour combler un manque est listé ici, avec **le besoin** qui l'a fait naître. Quand plusieurs
outils expriment le même besoin, c'est le signe que le jeu (ou le SDK) devrait le fournir. À tenir à jour à chaque nouvel outil.

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `tools/fetch_assets.py` | récupérer les assets du jeu | – |
| `game/scripts/behavior_log.gd` (`--behavior`) + `tools/behavior/` (`aimbeh.py`, `human_score.py`, `test_behavior.py`) | juger si une partie ressemble à celle d'un humain (score « humain » pour le classement) : le jeu enregistre pour lui-même souris, bouton de tir, visibilité des cibles, ce qui est sous le réticule et les tirs ; `human_score.py` en tire des mesures par appui, une référence faite de parties humaines (`fit`) et un score (`score`). Sur la carte `--map=arena` (murs, cible qui apparaît à des moments et endroits imprévisibles : `game/scripts/scenario_director.gd`, `--scenario`, `--seed`) le score juge aussi le délai entre l'apparition d'une cible et le tir qui la touche (moins de 100 ms = pas une réaction humaine) | l'enregistrement et la carte **sont dans le jeu** ; le calcul du score est encore en Python (à porter dans le jeu quand la formule sera réglée sur de vraies parties) |

**Tout ce qui concerne la chaîne « deux machines »** (agent du PC A, algo de la machine B, relais clavier / tir du Raspberry Pi, bancs de mesure, tests, fenêtre de réglages et touches de commande)
a déménagé, avec son historique, dans le dépôt **[aimlabs-pipeline](https://github.com/AnMaLeNo/aimlabs-pipeline)** (registre : `docs/TOOLS.md`).
Le SDK du jeu (`aimlabs` : `AimClient`, protocole du pont, vision) reste ici, dans `sdk/python`.
