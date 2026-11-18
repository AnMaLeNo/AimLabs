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

## Exporter vers le Mac (Core ML) et sniper-rs (ONNX)

* **sniper-rs** : le `.onnx` écrit par `yolo_sniper.py --export --model yolo11n-seg-<nom>.pt` a les mêmes entrées / sorties que
  `yolo11n-seg_256x256.onnx` (`images` 1×3×256×256 → `output0` 1×116×1344, `output1` 1×32×64×64) : `sniper-rs --model algos/yolo11n-seg-<nom>.onnx`
  (les variantes `--u8` / `--decode` passent par `algos/rust/sniper-rs/tools/make_gpu_model.py`, non refaites ici).
* **Mac** : copier le `.pt` sur le Mac et le convertir **sur le Mac** (coremltools), comme `tools/mac_infer_bench.py` :
  `YOLO("x.pt").export(format="coreml", imgsz=256, half=True, nms=False)` puis renommer en `models/x_256.mlpackage`.
* **Vérifier** avec `eval_chain.py` : `prepare` (PC) réduit des images de test comme `fit_crop` de sniper_b (plus proche voisin),
  `run` mesure sur le Mac (`--backend coreml`) et sur le PC (`--backend torch`), `score` donne TPR / FPR du réticule, les faux positifs sur
  images sans personnage et l'accord des deux backends. Mesuré pour v2 (800 images de test) : Core ML = PyTorch sur 100 % des décisions
  et des nombres de détections, écart de boîte max 1,1 px.

## Trouver la tête : vérité du moteur, banc rapide, modèle pose (2026-11-17)

Le tir « uniquement sur la tête » de la chaîne deux machines déduit la tête d'une silhouette (le haut de la boîte). Pour faire mieux il fallait deux choses :
savoir, pour n'importe quelle image, **quels pixels sont un tir à la tête d'après le jeu**, et pouvoir **juger une idée en quelques secondes** au lieu de faire
jouer le stand de tir sur la chaîne complète (5 à 10 minutes par réglage).

```
game/tools/gen_dataset.gd --truth=<f.jsonl> [--no-render] [--probe=N]   par image : caméra, capsules de tir de chaque personnage, os du squelette (≈ 1 500 images/s sans rendu)
head_truth.py     le rayon → capsule du jeu refait en numpy : zone sous chaque pixel ; `check` compare aux vrais rayons du moteur (--probe)
head_plan.py      2 700 situations de tir à la tête (tous les personnages, 3 à 60 m, réticule posé autour de la tête) : le plan du banc, ou des images d'entraînement
head_bench.py     LE BANC RAPIDE : chaque candidat jugé hors ligne sur ces images (personnage entier, derrière un muret, derrière un angle de mur)
pose_labels.py    étiquettes YOLO-pose (20 points) pour un jeu de données déjà rendu ; murs dessinés devant un quart des personnages
train_pose.py     fine-tuning d'un yolo11*-pose (tolérances par point données à la perte)
kpt_sheet.py      planches de contrôle : les points posés sur chaque personnage (face, côté, dos), à vérifier à l'œil
head_parse_check.py   second avis : un modèle public de découpage du visage dit où est la tête dessinée, comparé à la zone du jeu
```

```sh
# vérité des images déjà rendues (rien n'est re-rendu) et contrôle du calcul contre le moteur
godot --headless --path game res://tools/gen_dataset.tscn -- --plan=<data>/v2/plan.json --out=<tmp> --truth=<data>/head/v2_truth.jsonl --no-render
godot --headless --path game res://tools/gen_dataset.tscn -- --plan=<data>/v2/plan.json --out=<tmp> --truth=<probe.jsonl> --no-render --probe=60 --count=400
python tools/detector/head_truth.py check --truth <probe.jsonl>                     # 18 812 rayons du moteur : 0,14 % de désaccords (bords des capsules)
# le banc : rendu une fois (2 min), puis 20 à 60 s par candidat
python tools/detector/head_plan.py --out <data>/head/bench_plan.json
godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<bench_plan.json> --out=<data>/head/bench_renders --truth=<data>/head/bench_truth.jsonl
python tools/detector/head_bench.py --renders <bench_renders> --truth <bench_truth.jsonl> --plan <bench_plan.json> \
    --models seg8=seg,algos/yolo11n-seg-ft2.pt pose=jeu,<runs>/pose_n4/weights/best.pt,shrink=0.8
# jeu de données sans réticule + situations de tir à la tête, étiquettes pose, entraînement
python tools/detector/compose.py --plan <data>/v2/plan.json --renders <data>/v2/renders --backgrounds <backgrounds_hard.json> --out <data>/v3/ds480 --reticle none
python tools/detector/head_plan.py --out <train_plan.json> --per-agent 200 --seed 11 --split train --prefix k --env-frac 0.5 --spread 3     # puis rendu + compose, images ajoutées à v3/ds480
python tools/detector/make_view.py --src <data>/v3/ds480 --dst <data>/v3/ds256 --eval-interp nearest
python tools/detector/pose_labels.py --src <data>/v3/ds256 --truth <data>/v3/truth.jsonl --dst <data>/v3/ds256p
python tools/detector/train_pose.py --data <data>/v3/ds256p/data.yaml --name pose_n4           # ~50 min sur RTX 3080
# planches de contrôle des points
python tools/detector/kpt_sheet.py plan --out <sheet_plan.json>   # puis rendu avec --truth, puis :
python tools/detector/kpt_sheet.py draw --renders <rendus> --truth <truth.jsonl> --out <data>/head/planches_points
```

**Les 20 points** (`pose_labels.NAMES`) : 0 à 4 = la zone où le jeu compte un tir à la tête (centre, bord gauche, bord droit, haut, bas) ; 5 nez (l'os est
entre le nez et la lèvre), 6 et 7 les yeux (absents quand le visage est de dos, et pour kayo et omen qui n'ont pas de visage) ; 8 à 19 épaules, coudes, mains,
hanches, genoux, pieds. Les points viennent du squelette et des capsules, jamais d'un modèle.

**Ce que le banc mesure** : tête trouvée, erreur sur son centre (px à 256), « si on tirait sur le centre annoncé, serait-ce un tir à la tête ? », et la décision
du tir automatique (réticule au centre de l'image : tire-t-il quand il est sur la tête, et à tort quand il n'y est pas). Décision sur UNE image : la chaîne réelle
ajoute un suivi sur plusieurs images. Le banc trie les idées ; le stand de tir sur la chaîne complète ne sert plus qu'à confirmer le candidat retenu.

### Ce qui a été constaté

* **Le réticule dessiné dans les images d'entraînement fausse tout de loin.** Les jeux v1 / v2 portent l'ancien réticule du jeu (4 traits de 9 px avec contour noir)
  dans 90 % des images. Un modèle pose entraîné dessus, puis jugé avec ce réticule posé sur une tête à plus de 40 m, annonce la tête 5 px trop haut : il prend le
  trait du haut pour la personne. Le réticule n'est pas dans les rendus (compose.py l'ajoutait) : **le jeu v3 est recomposé sans aucun réticule** (`--reticle none`),
  et il compte 5 600 situations où la tête est près du centre de l'image (les jeux v1 / v2 n'en ont presque pas).
* **Les modèles pose livrés (COCO) ne conviennent pas tels quels**, quelle que soit leur taille (n, s, m, l, x) : 64 à 70 % de têtes trouvées, 6 à 7 px d'erreur,
  aucune au-delà de 40 m. Vitesse sur le Mac de la chaîne (Core ML, Neural Engine, 256 px) : n 0,37 ms · s 0,65 · m 1,34 · l 1,60 · x 3,30 ; yolo11n-seg actuel : 0,42.
* **La zone « tête » du jeu était trop haute** (du nez à 6 cm au-dessus des cheveux) : descendue le 2026-11-17 sur la tête dessinée (menton 1,83 m → 2,14 m ;
  `game/scripts/enemy.gd`, voir le README du dépôt). Un modèle public de découpage du visage (`jonathandinu/face-parsing`, SegFormer, photos de vraies personnes),
  à qui on donne la tête recadrée, confirme de face : 97 % de ce qu'il appelle « tête » tombe dans la nouvelle zone, son menton est à 0,8 cm du bas de la zone
  (médiane sur 28 personnages ; de −8 à +8 cm : barbes, capuches, masques). De côté et de dos il est moins sûr (62 % et 69 %), et sur l'image entière il ne vaut
  rien (épaulettes prises pour de la peau) : ce n'est pas un outil d'étiquetage, seulement un second avis.
* **gekko** : sa pose neutre affichée est à 8 cm de la pose de repos sur laquelle le jeu posait les capsules (corrigé dans enemy.gd). **veto** s'affiche blanc
  (modèle abîmé) : écarté des données, du banc et des planches (`--exclude veto` par défaut). harbor, écarté des jeux v1 / v2 pour la même raison, s'affiche
  normalement aujourd'hui mais reste écarté du jeu (world.gd).
* Quelques personnages récents (clove, miks, tejo, waylay) nomment autrement les os du visage : `gen_dataset.gd` (`BONE_ALT`) les ramène aux mêmes points.

### Résultats du banc (2 700 situations, nouvelle zone « tête », sans veto ; décision sur une image, confiance ≥ 0,5)

`seg` = le modèle actuel de la chaîne (`yolo11n-seg-ft2`) avec sa règle « tête = 8 % du haut de la boîte » ; `pose` = `yolo11n-pose` réentraîné sur le jeu v3
(37 741 images, aucun réticule, 20 points, 40 passes : `runs/pose_n4`), zone annoncée réduite à 80 % (`shrink=0.8`). Vitesse sur le Mac (Core ML, Neural Engine) :
pose 0,38 ms, seg 0,42 ms.

Images de test SANS réticule :

| | tête trouvée | erreur du centre (médiane) | viser le centre = tir à la tête | tire quand le réticule est sur la tête | tire à tort | tirs justes |
|---|---|---|---|---|---|---|
| seg, personnage entier | 89,9 % | 1,51 px | 86,7 % | 38,9 % | 3,7 % | 75,5 % |
| **pose**, personnage entier | 96,4 % | 0,70 px | 96,4 % | 65,0 % | 1,2 % | 94,1 % |
| seg, derrière un muret | 66,6 % | 5,15 px | 62,0 % | 9,4 % | 0,9 % | 75,3 % |
| **pose**, derrière un muret | 85,9 % | 0,87 px | 85,9 % | 57,7 % | 1,7 % | 91,0 % |
| seg, derrière un angle de mur | 81,3 % | 2,19 px | 73,6 % | 35,9 % | 3,8 % | 68,6 % |
| **pose**, derrière un angle de mur | 90,9 % | 0,91 px | 87,5 % | 55,8 % | 1,2 % | 91,5 % |

Par distance (personnage entier, sans réticule), « viser le centre = tir à la tête » : < 10 m seg 96,9 % / pose 100 % ; 10–20 m 99,4 / 100 ; 20–40 m 81,1 / 98,1 ;
≥ 40 m 45,5 / 77,6 (au-delà de 40 m la tête fait 1 à 1,5 px de rayon à 256 px ; le modèle ne trouve plus 22 % des personnages).
`shrink` règle le compromis : 1,0 → tire 81,8 %, à tort 6,1 % ; 0,8 → 65,0 % / 1,2 % ; 0,65 → 43,4 % / 0,1 %.

**Avec le réticule du jeu dessiné dans l'image de test** (le modèle n'en a jamais vu à l'entraînement), personnage entier, pose `shrink=0.8` :

| réticule dans l'image | viser = tête, < 20 m | viser = tête, 20–40 m | viser = tête, ≥ 40 m | tire à tort, < 20 m | tire à tort, 20–40 m | tire à tort, ≥ 40 m |
|---|---|---|---|---|---|---|
| aucun | 100 % | 98,1 % | 77,6 % | 0,4–1,0 % | 2,2 % | 2,4 % |
| « small » (celui du jeu aujourd'hui) | 100 % | 96,2 % | 63,8 % | 0,6–1,2 % | 7,2 % | 14,9 % |
| « classic » (l'ancien) | 98–100 % | 39,9 % | 1,0 % | 0,8–7,5 % | 10,1 % | 3,5 % |

Jusqu'à 20 m le réticule ne change rien. Au-delà, un modèle qui n'a jamais vu de réticule prend ses traits pour un morceau de la personne quand il est posé dessus :
avec l'ancien réticule il « trouve » une personne dans 100 % des images lointaines, 5 px à côté de la tête.

**Comparaison : mêmes images, mêmes réglages, mais des réticules variés dessinés à l'entraînement** (`compose.py --reticle mix` : 55 % « small », 20 % « classic »,
10 % point, 15 % aucun ; `runs/pose_n5_reticules`, résultats dans `<data>/head/res_compare_{none,small,classic}.json`). Personnage entier, `shrink=0.8`,
« sans » = `pose_n4` (aucun réticule à l'entraînement), « variés » = `pose_n5_reticules` :

| réticule dans l'image de test | modèle | tête trouvée | viser = tête | viser = tête, 20–40 m | viser = tête, ≥ 40 m | tire | tire à tort | à tort, 20–40 m | à tort, ≥ 40 m | tirs justes |
|---|---|---|---|---|---|---|---|---|---|---|
| aucun | sans | 96,4 % | 96,4 % | 98,1 % | 77,6 % | 65,0 % | 1,2 % | 2,2 % | 2,4 % | 94,1 % |
| aucun | variés | 96,4 % | 96,4 % | 98,6 % | 76,9 % | 69,0 % | 1,1 % | 2,0 % | 1,7 % | 95,1 % |
| « small » (le jeu aujourd'hui) | sans | 97,2 % | 94,0 % | 96,2 % | 63,8 % | 69,2 % | 4,1 % | 7,2 % | 14,9 % | 83,2 % |
| « small » (le jeu aujourd'hui) | variés | 96,6 % | 96,6 % | 98,5 % | 78,7 % | 71,0 % | 1,3 % | 2,5 % | 3,8 % | 94,0 % |
| « classic » (l'ancien) | sans | 100 % | 72,4 % | 39,9 % | 1,0 % | 48,1 % | 4,8 % | 10,1 % | 3,5 % | 74,8 % |
| « classic » (l'ancien) | variés | 98,3 % | 83,4 % | 78,4 % | 17,7 % | 57,5 % | 2,1 % | 4,5 % | 4,5 % | 89,0 % |

Lecture : voir des réticules à l'entraînement ne coûte rien quand il n'y en a pas dans l'image (mêmes chiffres à la marge près), et efface l'effet du réticule
actuel du jeu (à tort 1,3 % au lieu de 4,1 % ; au-delà de 40 m 3,8 % au lieu de 14,9 %). Derrière un muret, réticule « small » : à tort 1,7 % au lieu de 6,3 % ;
derrière un angle de mur : 1,8 % au lieu de 3,3 %. L'ancien gros réticule reste un problème de loin même pour ce modèle (17,7 % au-delà de 40 m) : ses traits de
9 px recouvrent une tête de 2 à 3 px, il n'y a plus rien à voir. Non essayé : retirer le réticule de l'image avant le réseau (côté Mac), qui laisserait le
modèle « sans » tel quel.

**Décision (Antoine, 2026-11-18) : le modèle « réticules variés »** (`pose_n5_reticules`). Il est branché dans la chaîne deux machines (dépôt aimlabs-pipeline,
`sniper_b.py --head-model`, sur le Mac `yolo11n-pose-jeu5_256.mlpackage`) : il remplace le modèle de silhouette tant que le tir « uniquement sur la tête » est activé.

**Forme et taille de la zone visée** (même banc, réticule « small », personnage entier, `pose_n5_reticules` ; tire quand le réticule est sur la tête / tire à tort) :

| `shrink` | rectangle (`shape=box`) | ellipse (`shape=ellipse`) |
|---|---|---|
| 1,0 | 90,8 % / 6,9 % | 85,3 % / 2,6 % |
| 0,9 | 84,6 % / 2,9 % | **74,2 % / 0,7 %** (retenu) |
| 0,8 | 71,0 % / 1,3 % | 57,9 % / 0,2 % |
| 0,7 | 55,1 % / 0,4 % | – |
| 0,6 | 40,0 % / 0,0 % | – |

La zone « tête » du jeu est une capsule, arrondie en haut et en bas : les coins du rectangle sont hors de la tête. L'ellipse à 0,9 tire plus que le rectangle à 0,8 et se
trompe deux fois moins. Sur la chaîne réelle (stand de tir du jeu, 72 traversées visées dans la tête, 5 à 25 m) : ancienne règle 35 tirs à la tête, rectangle 0,8 : 58,
ellipse 0,9 : 63 ; aucun tir dans le corps, aucun tir sur les 48 traversées visées au-dessus ou au-dessous de la tête (détail : `docs/PIPELINE.md` du dépôt aimlabs-pipeline,
« Modèle de tête »). `tools/head_model_check.py` de ce dépôt-là contrôle que le modèle converti pour le Mac voit les mêmes têtes que celui du banc.

Pistes non essayées pour un meilleur modèle : plus de passes (40 ici ; le score de validation des points montait encore, 0,451 à la 35e, 0,458 à la 40e), plus d'images des cas
faibles (au-delà de 40 m, derrière un muret), une taille au-dessus (s : 0,65 ms sur le Mac au lieu de 0,37). Les tailles s / m / l / x n'ont été jugées que telles que livrées.
Au-delà de 40 m la tête fait 2 à 3 px dans l'image de 256 px donnée au réseau : aucun entraînement ne rend ce qui n'est pas dans l'image.

Le fichier des images préparées porte maintenant les variantes dans son nom quand elles ne sont pas toutes demandées (`….small.libre.npy`) : `--variants libre` réécrivait
sinon le fichier des trois variantes.

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
