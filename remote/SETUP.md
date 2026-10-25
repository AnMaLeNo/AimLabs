# Fenêtre de réglages et touches de commande (pour les joueurs)

Tu écris ton propre programme (capture d'écran sur le PC A, algorithme sur la machine B, envoi de la touche de tir). Cette page décrit deux briques **optionnelles**
que tu peux utiliser pour qu'il soit agréable à régler et à piloter :

1. **la fenêtre de réglages** (`aimlabs.setup`) : ton programme *décrit* ce qui est réglable, la fenêtre l'affiche, tu règles, le programme récupère les valeurs ;
2. **les touches de commande** (`aimlabs.keys`) : le clavier est branché sur le Raspberry Pi, qui le relaie au PC A en Bluetooth ; il sait donc quelles touches sont enfoncées et les envoie à B.
   Tu peux ainsi activer / désactiver ton tir automatique avec une touche, ou le faire agir seulement tant qu'une touche est maintenue.

La fenêtre de réglages est **distincte de la fenêtre de debug** (`sniper_b.py --view`, qui montre l'algorithme *pendant* qu'il tourne). Elle ne sert qu'**avant** le lancement :
une fois validée elle se ferme, et rien d'elle ne reste dans le chemin chaud de ton algorithme (voir « Impact sur les performances »).

```
 clavier ──USB──► Raspberry Pi ──Bluetooth──► PC A (jeu)          ┌──────────────────────────────────────────────┐
                    │  ▲                                            │ ton programme sur B                          │
   touches (UDP)    │  │  ordre de tir (UDP signé)                  │   run(schema)  → fenêtre de réglages (avant) │
                    ▼  │                                            │   PiKeys + Bindings → états lus par l'algo   │
                  ┌────┴────────────────────────────────────────────┤   PiFire → touche de tir injectée par le Pi  │
                  │ Machine B ◄──── images (agent_a.py)             └──────────────────────────────────────────────┘
```

## 1. Utiliser la fenêtre de réglages

### En Python

```python
from aimlabs.setup import run
from aimlabs.keys import PiKeys, Bindings

schema = {
    "setup": 1, "app": "mon_algo", "title": "Mon algo",
    "pages": [
        {"id": "touches", "title": "Touches", "fields": [
            {"id": "auto", "type": "key_in", "label": "Tir automatique : bascule", "modes": ["toggle"],
             "help": "Un appui active, le suivant désactive.", "default": {"key": "CAPSLOCK", "mode": "toggle"}},
            {"id": "burst", "type": "key_in", "label": "Tir automatique : maintenu", "modes": ["hold"],
             "default": {"key": "LALT", "mode": "hold"}, "swallow": True},
            {"id": "fire", "type": "key_out", "label": "Touche de tir", "default": "G"},
        ]},
        {"id": "algo", "title": "Algorithme", "fields": [
            {"id": "conf", "type": "float", "label": "Confiance minimale", "min": 0, "max": 1, "step": 0.05, "default": 0.5},
        ]},
    ],
}

keys = PiKeys("192.168.1.101")           # le Pi qui relaie ton clavier (secret : ~/.aimlabs/pi_fire.key)
keys.start()
values = run(schema, keys=keys)          # bloque jusqu'à « Valider » (valeurs, enregistrées) ou « Annuler » (None)
if values is None:
    raise SystemExit

bind = Bindings(keys)
bind.add("auto", values["auto"])         # {"key": "CAPSLOCK", "mode": "toggle", "swallow": False}
bind.add("burst", values["burst"])
keys.set(watch=bind.keys(), swallow=[v["key"] for v in (values["auto"], values["burst"]) if v["swallow"] and v["key"]])

while True:                              # ton algorithme : lire un état = lire un attribut
    if bind.get("auto") or bind.get("burst"):
        ...
```

### Depuis n'importe quel langage

```
python -m aimlabs.setup schema.json --out valeurs.json --pi 192.168.1.101   # fenêtre ; code de sortie 0 = validé, 1 = annulé, 2 = schéma invalide
python -m aimlabs.setup schema.json --print                                 # valeurs enregistrées (ou par défaut), sans fenêtre
python -m aimlabs.setup schema.json --check                                 # vérifie le schéma
```

Les valeurs validées sont aussi écrites dans `~/.aimlabs/<app>.json` : au lancement suivant ton programme peut simplement les relire, sans rouvrir la fenêtre. Un exemple complet est dans
`sdk/python/examples/setup_schema_example.json`.

### Le schéma (version 1)

Un objet `{"setup": 1, "app": "<identifiant>", "title": "…", "pages": [ … ]}`. Chaque page a un `id`, un `title` et une liste de `fields`. Un champ a un `id` **unique dans tout le schéma**,
un `type`, et facultativement `label`, `help`, `default`.

| `type` | Champ | Valeur renvoyée |
|---|---|---|
| `bool` | interrupteur | `true` / `false` |
| `int`, `float` | nombre : `min`, `max`, `step`, `unit` ; boutons − / + ou saisie au clavier | nombre (borné) |
| `text` | texte : `max_len` | chaîne |
| `choice` | `options` : `["a", "b"]` ou `[{"value": 1, "label": "Un"}]` | la valeur de l'option |
| `key_in` | **touche d'entrée** : une touche du clavier qui commande ton programme ; `modes` parmi `toggle` (bascule), `hold` (maintien), `press` (impulsion) ; `swallow` : proposer de bloquer la touche côté PC | `{"key": "CAPSLOCK" ou null, "mode": "toggle", "swallow": false}` |
| `key_out` | **touche de sortie** : une touche que ton programme fait appuyer (par le Pi), ex. le tir ; `allowed` : liste imposée, sinon la liste blanche du Pi | `"G"` ou `null` |
| `label` | texte d'explication, sans valeur | – |

Assigner une touche : *clique sur la case, puis appuie sur la touche voulue* **sur ton clavier** (celui du Pi). Pendant l'attente, le Pi ne transmet plus rien au PC (une lettre appuyée pour
l'assigner n'écrit donc pas dans ta fenêtre de jeu) ; un clic de plus annule. Les touches de sortie refusées par le Pi (hors liste blanche) sont signalées. Une même touche utilisée deux fois
est signalée en orange.

Un schéma invalide est refusé avec un message précis (`type 'couleur' inconnu`, `id déjà utilisé`…). Les valeurs lues dans un fichier abîmé ou modifié à la main sont ramenées à des valeurs valides.

### Pages « actives »

`{"id": "capture", "title": "Capture", "type": "capture"}` n'a pas de champs : elle demande à **ton programme** de fournir son code (`run(schema, plugins={"capture": ma_page})`).
C'est ainsi que `sniper_b.py --setup` ajoute le cadrage de la capture (aperçu en direct) à côté des champs ordinaires (`remote/poc/sniper_setup.py` en est l'exemple). Une page active est un objet
avec `render(cv, rect)` et, facultativement, `start / stop / tick / mouse / key / value / validate / busy` (voir l'en-tête de `sdk/python/aimlabs/setup/ui.py`). Sans objet fourni, la page affiche « non disponible ».

## 2. Utiliser les touches du clavier dans ton algorithme

```python
from aimlabs.keys import PiKeys, Bindings
keys = PiKeys("192.168.1.101", watch=["CAPSLOCK", "LALT", "F8"], swallow=["F8"])
keys.start()
b = Bindings(keys)
b.add("auto",  {"key": "CAPSLOCK", "mode": "toggle"})   # b.get("auto")   : bascule
b.add("burst", {"key": "ALT",      "mode": "hold"})     # b.get("burst")  : vrai tant que la touche est enfoncée
b.add("mark",  {"key": "F8",       "mode": "press"})    # b.pressed("mark") : vrai UNE fois par appui
keys.add_listener(lambda nom, enfoncee: print(nom, enfoncee))      # ou réagir à chaque évènement
```

* **Noms** : `A`–`Z`, `0`–`9`, `F1`–`F12`, `SPACE`, `ENTER`, `ESC`, `TAB`, `CAPSLOCK`, `UP` `DOWN` `LEFT` `RIGHT`, `PAUSE`, `SCROLLLOCK`…, modificateurs `LCTRL` `RCTRL` `LSHIFT` `RSHIFT` `LALT` `RALT`
  `LGUI` `RGUI` ; `CTRL`, `SHIFT`, `ALT`, `GUI` désignent les deux côtés. Liste complète : `aimlabs.keys.KEY_NAMES`.
* **`watch`** : seules les touches demandées sont transmises par le Pi (ce qui évite de faire transiter tout ce que tu tapes). **`swallow`** (8 au plus) : le Pi ne les relaie **pas** au PC tant que ton
  programme est là ; il les rend dès que ton programme se désabonne (`keys.close()`), disparaît (5 s sans nouvelles) ou plante.
* Les évènements perdus par le Wi-Fi sont recomposés (chaque évènement est numéroté et renvoyé deux fois ; un battement toutes les 200 ms corrige une touche « coincée »).
* **Sans le Pi** : `sniper_b.py` retombe sur les touches que le *jeu* publie en mode `remote` (comportement d'origine : `CAPSLOCK` bascule, `ALT` maintenu) via `aimlabs.keys.ManualSource`.

### Le protocole du Pi (pour un programme qui n'est pas en Python)

UDP vers le port 7780 du Pi, comme l'ordre de tir. Tout paquet = en-tête `<BBI>` (magic `0xA7`, type, numéro) + charge + **HMAC-SHA256 tronqué à 8 octets** (secret partagé `~/.aimlabs/pi_fire.key`).
Détails et exemples : en-tête de `remote/pi/fire_server.py`.

| type | sens | charge |
|---|---|---|
| `6` KSUB | B → Pi, **à renouveler chaque seconde** (5 s sans renouvellement = fin d'abonnement, touches bloquées rendues) | drapeaux (bit0 : toutes les touches, bit1 : tout bloquer côté PC), *n*, *n* usages à surveiller, *m*, *m* usages à bloquer |
| `7` KEYS | Pi → B, à chaque évènement (2 fois) et toutes les 200 ms | `<QIBB>` horloge du Pi, numéro du dernier évènement, *n* touches enfoncées, *m* évènements ; puis *n* usages et *m* × `<IBB>` (numéro, usage, 1 = appui) |
| `9` CAPS | B → Pi puis réponse | réponse : json `{"usages": [touches de tir autorisées], "keys_all": true}` |

Les touches sont des **usages HID** (page clavier : `A` = 4, `G` = 10, `F1` = 58, modificateurs `0xE0`–`0xE7`).

## 3. Sécurité

* Chaque paquet est signé (HMAC) ; sans le secret, le Pi ignore tout. Le secret n'est jamais dans le dépôt.
* La touche de tir est limitée à une **liste blanche** du Pi (`/etc/btkbd/fire.json`, champ `usages` : lettres, chiffres, `F1`–`F12`, espace, flèches ; **jamais** de modificateur, de Échap, d'Entrée, de Suppr…),
  appui borné à 100 ms. Pour en autoriser une autre, modifier ce fichier sur le Pi (`sudo`) et redémarrer `btkbd`.
* Confidentialité : le Pi ne transmet que les touches *demandées*. Le drapeau « toutes les touches » n'est utilisé que pendant l'assignation d'une touche dans la fenêtre, jamais pendant que l'algorithme tourne.
* Le blocage de touches est borné (8 touches) et **se défait tout seul** : un programme planté ne laisse jamais le clavier bloqué plus de 5 secondes.

## 4. Impact sur les performances

À remplir après mesure (voir `tools/README.md`, « Réglages et touches »).

## 5. Tests

| Test | Ce qu'il vérifie |
|---|---|
| `remote/poc/setup_test.py [--pi HOTE]` | schéma, valeurs, fenêtre (clics et touches injectés, **sans écran**), pages actives ; avec `--pi` : assignation d'une touche par le vrai flux du Pi |
| `remote/poc/keys_test.py` | le vrai serveur du Pi avec un faux relais : abonnement, évènements, filtrage, pertes, mauvais secret, touches bloquées |
| `remote/poc/pi_keys_test.py --pi HOTE` | le vrai Pi (clavier virtuel `remote/pi/uinput_keys.py`) : évènements reçus dans l'ordre sur le vrai Wi-Fi, blocage puis restitution |
| `remote/poc/capture_ctl_test.py` | la page « Capture » côté agent |
| `python -m aimlabs.setup schema.json --snap f.png` | rend la fenêtre dans une image (documentation) |
