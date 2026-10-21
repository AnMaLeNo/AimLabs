# algos/

Un fichier `.py` = un algo lançable depuis le menu Échap du jeu (le jeu le lance avec
`python` / `python3`, `PYTHONPATH` pointant sur `sdk/python`, et `AIMLABS_SHM` réglé sur
le segment partagé en cours). La première ligne de la docstring sert de description.

- `auto_aim.py` — vise seul la cible la plus proche du réticule ; `CAPSLOCK` on/off,
  `ALT` maintenu = vise tant qu'appuyé, `V` tir auto on/off.
- `skin_bot.py` — bot jouet toujours actif (vise + tire).
- `yolo_aim.py` — détection de personnes par YOLO (Ultralytics, modèle COCO pré-entraîné, GPU si
  dispo) → vise la tête de la cible la plus proche du réticule, conversion pixels→souris exacte
  (FOV + sensibilité lus dans l'état), extrapolation du mouvement × latence ; `CAPSLOCK` on/off,
  `ALT` maintenu, `V` tir auto, `H` tête/corps. Dépendances : `pip install -r algos/requirements-yolo.txt`
  (sous Windows/Linux avec CUDA : installer d'abord torch depuis https://pytorch.org/get-started/locally/).
  Modèle/taille/device via `AIMLABS_YOLO_MODEL`, `AIMLABS_YOLO_IMGSZ`, `AIMLABS_YOLO_DEVICE`.
  Sur CUDA, un moteur TensorRT FP16 est exporté au premier lancement (`yolo11n_384x640.engine`
  à côté des poids, 1–4 min une fois, `pip install tensorrt` requis) : 5 ms/frame mesurés sur
  RTX 3080 contre 17–29 ms pour `model.predict()` (`--no-trt` pour comparer). Construire le moteur
  à l'avance, jeu fermé : `python algos/yolo_aim.py --export` (avec le jeu ouvert c'est bien plus
  long). Poids et moteurs sont rangés dans `algos/` quel que soit le dossier de lancement.
  Banc hors jeu : `python algos/yolo_aim.py --bench dossier_png` (ms/frame + taux de détection).
- `yolo_trigger.py` — tir auto YOLO sans bouger la souris : `mark()` (« cible sous le réticule »,
  critère du benchmark) puis tir sur la boîte avancée de vitesse × latence (`--no-lead`) ; `CAPSLOCK`
  on/off, `ALT` maintenu ; démarre armé si `AIMLABS_AUTOSTART=1` (posé par le jeu en `--bench-trigger`).
  Deux critères : `--mode box` (le réticule est dans la boîte « person », `--margin 0.6` = bande
  centrale) et `--mode seg` (le pixel du réticule est dans le **masque de segmentation** de la
  personne, modèle `yolo11n-seg.pt`, moteur TensorRT à deux sorties : exporter avec
  `python algos/yolo_trigger.py --export --mode seg`, jeu fermé). Mesuré sur RTX 3080 à la poitrine :
  box 38–44 % de marques fausses (la boîte englobe les bras en A-pose), seg 28–32 % et vitesse limite
  ×2 ; `--margin 0.5` supprime les fausses marques mais rate la tête et triple la latence. Benchmark :
  `godot --path game -- --map=range --bench-trigger --algo=yolo_trigger.py "--algo-args=--mode seg"`
  → `bench_results/*.json` + fiche SVG. Variables d'env équivalentes : `AIMLABS_TRIGGER_MODE`, `AIMLABS_TRIGGER_MARGIN`.
- `yolo_sniper.py` — tir auto **« une balle »** pour les armes à cadence lente (Operator) : si ça tire,
  ça doit toucher, le plus tôt possible. Le modèle n'analyse qu'un **carré centré sur le réticule**
  (`--crop 0.333` × hauteur de l'écran, pleine résolution, réseau carré `--imgsz 256` → moteur
  `yolo11n-seg_256x256.engine`, `--export` jeu fermé). La **vitesse de la cible en pixels** est mesurée
  par régression sur les dernières frames et sa position au départ de la balle (latence capture →
  maintenant + 1 frame) est prédite : le tir part quand la position *prédite* met le réticule dans la
  bande centrale du corps (`--band 0.5`) — parfois avant que l'image ne montre la cible dessus
  (pre-shot ; `--lead-scale 0` pour désactiver). Sûreté : suivi ≥ `--track 3` frames cohérent, confiance
  ≥ `--conf 0.5`, pixel du réticule dans le masque de segmentation (`--mode seg`, défaut) ; une balle
  puis silence `--cooldown 1` s et tant que la cible reste sous le réticule. Benchmark dédié :
  `godot --path game -- --map=range --bench-sniper --algo=yolo_sniper.py` (règle : touché avec ≤ 1 balle).

Pour pointer le jeu sur un autre dossier : `--algos=/chemin` (ou le bouton « Dossier » du menu).
