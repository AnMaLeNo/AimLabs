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

Pour pointer le jeu sur un autre dossier : `--algos=/chemin` (ou le bouton « Dossier » du menu).
