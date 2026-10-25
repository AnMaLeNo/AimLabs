# Registre des outils

Chaque outil écrit « à côté » du jeu pour combler un manque est listé ici, avec **le besoin** qui l'a fait naître. Quand plusieurs
outils expriment le même besoin, c'est le signe que le jeu (ou le SDK) devrait le fournir. À tenir à jour à chaque nouvel outil.

| Outil | Pourquoi il existe (le besoin) | Côté jeu ? |
|---|---|---|
| `tools/fetch_assets.py` | récupérer les assets du jeu | – |

**Tout ce qui concerne la chaîne « deux machines »** (agent du PC A, algo de la machine B, relais clavier / tir du Raspberry Pi, bancs de mesure, tests, fenêtre de réglages et touches de commande)
a déménagé, avec son historique, dans le dépôt **[aimlabs-pipeline](https://github.com/AnMaLeNo/aimlabs-pipeline)** (registre : `docs/TOOLS.md`).
Le SDK du jeu (`aimlabs` : `AimClient`, protocole du pont, vision) reste ici, dans `sdk/python`.
