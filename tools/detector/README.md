# Détecteur : données d'entraînement générées par le jeu + fine-tuning

Le réseau du sniper (`yolo11n-seg`, 256×256) ne voit qu'un **carré centré sur le réticule** (1/3 de la hauteur de l'écran, 480 px à
1440p). Le modèle livré (COCO) ne connaît pas les personnages du jeu ; ici on le réentraîne sur des images produites par le jeu
lui-même, avec des étiquettes **exactes** (issues du moteur, jamais d'un modèle).

```
prepare_backgrounds.py  photos de fond (COCO val2017) sans aucune personne  → backgrounds.json
fetch_coco_negatives.py télécharge des photos COCO train2017 sans personne (v2 : +9 390 fonds après filtre)
mine_hard_negatives.py  trouve les carrés de ces photos où un modèle voit « une personne » à tort → champ "hard"
gen_plan.py             tire le plan : personnage, angle, distance, position, éclairage, fond  → plan.json
game/tools/gen_dataset.gd  (Godot) rend le plan : caméra du joueur resserrée sur le carré, personnage seul sur fond transparent
compose.py              compose les fonds, calcule les polygones depuis l'alpha, dessine le réticule → jeu 480×480 YOLO-seg
make_view.py            vue 256×256 (interpolation tirée au hasard : bilinéaire PC / plus proche voisin Mac / aire)
train.py                fine-tuning (ultralytics)        evaluate.py   comparaison de modèles avec les critères du sniper
preview.py              planche de contrôle (polygones superposés)
```

Chemin type (données hors dépôt, dans `~/aimlabs-data/detector/`) :

```sh
python tools/detector/prepare_backgrounds.py --images <val2017> --annotations <instances_val2017.json> --out <data>/backgrounds
python tools/detector/gen_plan.py --out <data>/v1/plan.json --backgrounds <data>/backgrounds/backgrounds.json --train 500 --val 30 --test 30
godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<plan.json> --out=<data>/v1/renders      # ~20 échantillons/s
python tools/detector/compose.py --plan <plan.json> --renders <data>/v1/renders --backgrounds <backgrounds.json> --out <data>/v1/ds480
python tools/detector/make_view.py --src <data>/v1/ds480 --dst <data>/v1/ds256
python tools/detector/train.py --data <data>/v1/ds256/data.yaml --name v1            # ~1 h sur RTX 3080 (limité par le CPU)
python tools/detector/evaluate.py --dataset <data>/v1/ds480 --split test --models algos/yolo11n-seg.pt <runs>/v1/weights/best.pt
```

## v2 : données lointaines + négatifs difficiles

```sh
python tools/detector/fetch_coco_negatives.py --annotations <instances_train2017.json> --out <data>/ex_images/train2017_neg --n 10000
python tools/detector/prepare_backgrounds.py --images <data>/ex_images/train2017_neg --annotations <instances_train2017.json> --only-existing --force-split train --extend <data>/backgrounds/backgrounds.json --out <data>/backgrounds2
python tools/detector/mine_hard_negatives.py --backgrounds <data>/backgrounds2/backgrounds.json --out <data>/backgrounds2/backgrounds_hard.json        --train-models algos/yolo11n-seg-ft1.pt algos/yolo11n-seg.pt --eval-models algos/yolo11n-seg.pt --k-eval 150
python tools/detector/gen_plan.py --out <data>/v2/plan.json --backgrounds <data>/backgrounds2/backgrounds_hard.json --train 900 --val 40 --test 60        --neg-frac 0.2 --dmax 70 --far-frac 0.4 --seed 2
```
(puis rendu / compose / make_view / train comme ci-dessus, `--epochs 50` ; 33 600 images, rendu 22 min, entraînement 1,45 h.)
Les photos val / test restent celles de val2017 ; les carrés « difficiles » de val / test sont choisis par le **modèle COCO
d'origine seul**, donc ni v1 ni v2 ne les ont sélectionnés : comparer leurs fausses détections dessus est équitable.
Mesuré sur le test v2 (conf ≥ 0,5) : images sans personne « difficiles » 38 % de fausses détections (COCO), 11 % (v1), 9 % (v2) ;
carrés photo sans personne du test v1 : 6,2 % (v1) → 0 % (v2). Le rappel des personnages lointains **n'a pas bougé** (≥ 30 m : 0,47 → 0,49) :
il dépend de la taille de la cible à 256 px (surface < 100 px² : 7–16 % retrouvés ; 100–200 px² : 55 % ; > 200 px² : ≥ 88 %),
pas du nombre d'exemples.

## Choix (et pourquoi)

* **Étiquettes = canal alpha du rendu du personnage seul**, avant d'ajouter le fond. Silhouette exacte (bras, jambes, personnage
  coupé par le bord, caché par un autre) ; un gros modèle ferait des erreurs sur les cas difficiles. Le sniper utilise le **masque**
  (pixel du réticule dans le masque), d'où des polygones de segmentation, pas seulement des boîtes.
* **Même caméra que le jeu** : œil 1,65 m, FOV vertical 75°, champ resserré sur le carré, donc un pixel rendu = un pixel de l'écran ;
  vérifié sur une vraie capture (Reyna à 10 m : ~190 px de haut dans les deux).
* **Fonds** : le vrai monde du jeu (ciel + sol quadrillé, avec ou sans variations), photos COCO **sans personne** (annotations COCO
  puis `yolo11m` à seuil 0,10 ; 1958 retenues, train / val / test découpées par photo : les photos de test ne servent jamais à
  l'entraînement), fonds unis / dégradés / bruités, et ~12 % d'images sans personnage. Les personnages collés sont éclairés avec un
  ciel de la couleur du fond et reçoivent une ombre de contact.
* **Composition en linéaire** : le rendu transparent de Godot est prémultiplié en linéaire puis encodé sRGB (mesuré : rapport
  couleur/alpha = α^(1/2,2)). Composer en sRGB laisserait un halo sombre.
* **Classe 0 = person, 80 noms COCO conservés** : la forme de sortie du modèle ne change pas, `yolo_sniper`, `sniper-rs` et
  `sniper_b` le lisent sans modification.
* **Harbor exclu** (textures manquantes, il apparaît blanc) : `--exclude harbor` par défaut dans `gen_plan.py` et `compose.py`.
* Réticule vert du jeu dessiné au centre dans 90 % des images (il est dans la vraie capture) ; pas de viewmodel (l'arme est hors du
  carré de 480 px, vérifié sur une capture).

## Pièges rencontrés

* Le nom du modèle doit contenir `-seg` (`Detector` détecte la segmentation par le nom de fichier) : copier `best.pt` en
  `algos/yolo11n-seg-<nom>.pt` avant d'exporter le moteur (`python algos/yolo_sniper.py --export --model yolo11n-seg-<nom>.pt`).
* `cache="ram"` d'ultralytics sous Windows : chaque worker garde sa copie (8 × 3,5 Go) → RAM saturée, entraînement figé. `train.py`
  n'active le cache que sur `--cache`.
* Sortie Python redirigée sous Windows = cp1252 : les scripts reconfigurent stdout en UTF-8.
