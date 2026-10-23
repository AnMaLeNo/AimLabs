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
  Mesuré sur RTX 3080 (Operator, poitrine/bassin/tête, 5 et 10 m, 64 traversées) : 39 touchés, 10 ratés,
  15 sans tir (refus = sûreté), 0 balle en trop ; ≤ 8 m/s : 20/23 (les 3 ratés = trou de hitbox au bassin
  à 10 m, corrigé depuis) ; les ratés restants sont à ≥ 16 m/s, balle 0,1–0,4 m en retard. `yolo_trigger.py`
  sous les mêmes règles : 24 touchés, 14 ratés, 7 sans tir, 3 balles en trop (48 traversées). Deux
  leçons : (1) le crop **n'accélère pas** l'inférence (9–10 ms/frame contre 5–6 ms plein cadre 384×640 :
  le coût est le lancement GPU + les sorties seg, pas les pixels) — son intérêt est la pleine
  résolution ; (2) la vitesse doit être mesurée sur les **bords libres** de la boîte, pas son centre :
  tronquée par le crop à l'entrée, la boîte avance à mi-vitesse et le tir part en retard.

Pour pointer le jeu sur un autre dossier : `--algos=/chemin` (ou le bouton « Dossier » du menu).

## sniper-rs (port Rust, `algos/rust/sniper-rs`)

```
cargo build --release --features cuda,tensorrt          # toolchain stable-x86_64-pc-windows-gnu suffit
ORT_DYLIB_PATH=<onnxruntime GPU>/onnxruntime.dll AIMLABS_AUTOSTART=1 \
  sniper-rs --ep tensorrt --libs "<dossier TensorRT>;<dossier CUDA 12 + cuDNN 9>"
```

* `--ep tensorrt` (défaut) | `cuda` | `cpu`, `--no-graph` pour couper le CUDA graph. Entrée et sorties
  sont des tampons fixes liés à la session (IoBinding) ; avec le CUDA graph les ~350 kernels sont
  rejoués en un lancement. **Un EP demandé qui ne se charge pas est une erreur** : ONNX Runtime
  retombe sinon en silence sur le CPU (c'est ce qui a faussé les premières mesures « CUDA »).
* DLL à fournir via `--libs` (ou le PATH) : `cudart64_12`, `cublas(Lt)64_12`, `cudnn64_9` + ses
  moteurs, et pour TensorRT `nvinfer_10` (les dossiers `torch/lib` et `tensorrt_libs` d'un env Python
  les contiennent). Le **premier** lancement TensorRT construit le moteur FP16 (~5 min, mis en cache
  dans `%TEMP%/aimlabs_trt`) ; les suivants chargent en < 1 s.
* `ortbench` (`src/bin/ortbench.rs`) mesure l'inférence seule : `ortbench cpu-tensor iobind graph trt trtgraph`.
* Mesures RTX 3080, 256×256, jeu ouvert (`--stats` intégré) : CPU 9,9 ms · ORT CUDA 6,7 ms (lancement
  des kernels, l'IoBinding seul n'y change rien) · CUDA graph 2,5 ms · **TensorRT EP + CUDA graph
  0,8 ms isolé, 1,7 ms en jeu** (envoi 0,12 · exéc 1,4 · retour 0,15) · TensorRT natif Python sans
  graphe 2,1 ms, avec graphe 1,0 ms. Bench sniper 5 m torse : limite 3,75 m/s (CPU : 1,88 m/s).

### Modèles dérivés (`algos/rust/sniper-rs/tools/make_gpu_model.py`)

`python make_gpu_model.py in.onnx out.onnx [--u8] [--decode]` greffe du pré/post-traitement sur le graphe ;
`sniper-rs --model out.onnx` détecte la forme (`[entrée u8]`, `[décodage GPU]`) tout seul.
`--u8` : entrée `uint8` NHWC (cast, /255, transposition dans le graphe) ; `--decode` : `det` (5, K=32) +
`mask_logits` (K, 16 lignes × 64) au lieu des 1,15 Mo de sorties brutes. Équivalence vérifiée sur images réelles
(écart max 7e-4 en entrée, 0 en décodage ; K=8 ou 16 perd des personnes dans une foule, 32 non).
Gain mesuré en jeu (~515 frames/s) : envoi 0,12 → 0,05 ms, retour 0,14 → 0,06 ms, mais l'exécution GPU gagne
0,1 ms de nœuds en plus : **inférence 1,71 → 1,65 ms, préparation du crop 0,19 → 0,15 ms** (≈ 0,1 ms au total),
sans effet mesurable sur la vitesse limite du bench. Les modèles dérivés sont ignorés par git.

### Latence capture → tir côté jeu (`--bridge-trace`)

`godot --path game -- --map=range --bridge-trace` imprime toutes les 2 s : lecture GPU (`readback`),
capture → frame visible dans la mémoire partagée (`published`), capture → commande appliquée, et commande → tir
réel (`cmd->fire`). Mesures RTX 3080 à ~520 fps : lecture GPU 3,6 ms (`texture_get_data_async` rend les données
~2 frames plus tard : structurel), copie mémoire partagée 0,2 ms, capture → commande 5,5 ms. Le nœud du pont
tournait **après** le joueur dans la frame : le drapeau de tir n'était lu qu'à la frame suivante (`cmd->fire`
1,65 ms) ; `bridge.process_priority = -100` le ramène à 0,05 ms. Essayés sans gain : lecture synchrone sur le
thread de rendu (lecture 1,6 ms mais jeu à 290 fps), `get_image()` (110 fps), attente active côté SDK (5,53 vs 5,60 ms).
