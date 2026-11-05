# Registre des outils

Chaque outil écrit « à côté » du jeu pour combler un manque est listé ici, avec **le besoin** qui l'a fait naître. Quand plusieurs
outils expriment le même besoin, c'est le signe que le jeu (ou le SDK) devrait le fournir. À tenir à jour à chaque nouvel outil.

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `tools/fetch_assets.py` | récupérer les assets du jeu | – |
| `game/tools/gen_dataset.gd` (+ `.tscn`) | produire des milliers d'images d'entraînement du détecteur avec étiquettes exactes (silhouette = alpha du rendu), même caméra que le joueur | **oui, déjà dans le jeu** (rendu piloté par un plan JSON) |
| `tools/detector/` (`prepare_backgrounds`, `fetch_coco_negatives`, `mine_hard_negatives`, `gen_plan`, `compose`, `make_view`, `train`, `evaluate`, `preview`) | le modèle COCO ne reconnaît pas bien les personnages du jeu (rappel 0,64, 0,50 sur fonds photo) : plan de données, composition sur photos sans personne, fine-tuning, comparaison avec les critères du sniper (voir `tools/detector/README.md`) | le plan et le rendu pourraient devenir une option du jeu (`--gen-dataset`) ; l'entraînement reste hors jeu |

**Tout ce qui concerne la chaîne « deux machines »** (agent du PC A, algo de la machine B, relais clavier / tir du Raspberry Pi, bancs de mesure, tests, fenêtre de réglages et touches de commande)
a déménagé, avec son historique, dans le dépôt **[aimlabs-pipeline](https://github.com/AnMaLeNo/aimlabs-pipeline)** (registre : `docs/TOOLS.md`).
Le SDK du jeu (`aimlabs` : `AimClient`, protocole du pont, vision) reste ici, dans `sdk/python`.
