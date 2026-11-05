#!/usr/bin/env python3
"""fetch_coco_negatives.py — télécharge des photos COCO train2017 SANS personne (fonds + négatifs difficiles).

Choix à partir des annotations COCO : aucune annotation « person » (crowd compris), côté court ≥ 360 px. On en tire
`--n` au hasard, dont une part (`--confusers`) parmi les photos contenant des objets dressés qui ressemblent à une
silhouette (bouche d'incendie, panneau, feu, parcmètre, plante, parapluie, banc, chaise…) : ce sont eux qui donnent
des fausses détections. Le filtre par détecteur (prepare_backgrounds.py) se fait ensuite, comme pour val2017.

  python fetch_coco_negatives.py --annotations <instances_train2017.json> --out <dir> [--n 10000 --confusers 0.4]
"""
import argparse
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

CONFUSERS = {"fire hydrant", "stop sign", "parking meter", "traffic light", "potted plant", "umbrella", "bench", "chair",
             "vase", "bottle", "tie", "suitcase", "backpack", "handbag", "clock", "tv", "refrigerator", "toilet", "bird",
             "giraffe", "horse", "cow", "sheep", "dog", "teddy bear"}


def fetch(job):
    url, path = job
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    for _ in range(3):
        try:
            data = urlopen(url, timeout=30).read()
            with open(path, "wb") as f:
                f.write(data)
            return True
        except Exception:
            pass
    return False


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--confusers", type=float, default=0.4, help="part des photos choisies parmi celles avec un objet « confondant »")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--threads", type=int, default=16)
    a = ap.parse_args()
    coco = json.load(open(a.annotations, encoding="utf-8"))
    cat = {c["id"]: c["name"] for c in coco["categories"]}
    with_person, conf_imgs = set(), set()
    for x in coco["annotations"]:
        name = cat[x["category_id"]]
        if name == "person":
            with_person.add(x["image_id"])
        elif name in CONFUSERS:
            conf_imgs.add(x["image_id"])
    ok = [im for im in coco["images"] if im["id"] not in with_person and min(im["width"], im["height"]) >= 360]
    conf = [im for im in ok if im["id"] in conf_imgs]
    rest = [im for im in ok if im["id"] not in conf_imgs]
    rng = random.Random(a.seed)
    rng.shuffle(conf)
    rng.shuffle(rest)
    n_conf = min(int(a.n * a.confusers), len(conf))
    pick = conf[:n_conf] + rest[:a.n - n_conf]
    print(f"{len(coco['images'])} images, {len(ok)} sans personne ({len(conf)} avec objet confondant) → {len(pick)} choisies", flush=True)
    os.makedirs(a.out, exist_ok=True)
    jobs = [(f"http://images.cocodataset.org/train2017/{im['file_name']}", os.path.join(a.out, im["file_name"])) for im in pick]
    done = 0
    with ThreadPoolExecutor(a.threads) as ex:
        for i, good in enumerate(ex.map(fetch, jobs)):
            done += int(good)
            if (i + 1) % 1000 == 0:
                print(f"  {i + 1}/{len(jobs)}", flush=True)
    json.dump([im["file_name"] for im in pick], open(os.path.join(a.out, "_picked.json"), "w"))
    print(f"{done}/{len(jobs)} téléchargées → {a.out}")


if __name__ == "__main__":
    main()
