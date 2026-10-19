#!/usr/bin/env python3
"""Download the Kingdom Archives (kingdomarchives.com) models used by AimLabs into
game/assets/ka/ and convert their textures to WebP.

  python3 tools/fetch_assets.py            # everything (agents, FP arms, weapons)
  python3 tools/fetch_assets.py --only jett,jett-fp,classic

Needs Pillow (pip install pillow). The raw downloads are cached in KA_CACHE
(default: <repo>/.ka_cache) so re-running only re-converts what is missing.
Then open the project in Godot once (or run `godot --headless --path game --import`).

The models are Riot Games' property, mirrored by kingdomarchives.com under Riot's
"Legal Jibber Jabber" policy. They are not redistributed in this repository.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.request

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install pillow")

SITE = "https://kingdomarchives.com/"
UA = {"User-Agent": "Mozilla/5.0 (AimLabs asset fetcher)"}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "game", "assets", "ka")
CACHE = os.environ.get("KA_CACHE", os.path.join(REPO, ".ka_cache"))
TEX_KEYS = ("DF", "ORM", "EM", "NM", "MRAE", "MRS", "AEM", "Text")
MAX_TEX = 2048
WEBP_QUALITY = 92


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=120).read()


def cached(url_path: str) -> str:
    """Download SITE/url_path into the cache (once) and return the local path."""
    local = os.path.join(CACHE, url_path)
    if not (os.path.exists(local) and os.path.getsize(local) > 0):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        data = http_get(SITE + url_path)
        with open(local, "wb") as f:
            f.write(data)
    return local


def grab_json(page: str, var: str):
    m = re.search(var + r" = JSON\.parse\('(.*?)'\);", page, re.S)
    if not m:
        return None
    raw = m.group(1).encode().decode("unicode_escape").replace("\\/", "/")
    return json.loads(raw)


def grab_str(page: str, var: str) -> str:
    m = re.search(var + r"""\s*=\s*['"]([^'"]*)['"]""", page)
    return m.group(1) if m else ""


def norm(p: str) -> str:
    """'../uploads/models/x' -> 'uploads/models/x'"""
    return p.replace("../", "")


def rel(p: str) -> str:
    """'uploads/models/agents/jett/textures/df.png' -> 'agents/jett/textures/df'"""
    p = norm(p)
    p = p[len("uploads/models/"):] if p.startswith("uploads/models/") else p
    return os.path.splitext(p)[0]


def list_slugs() -> list:
    page = http_get(SITE + "models").decode()
    return sorted(set(re.findall(r'href="/modelviewer/([^"]+)"', page)))


def scrape(slug: str, query: str = "") -> dict:
    page = http_get(SITE + "modelviewer/" + slug + query).decode()
    return {
        "slug": slug,
        "type": grab_str(page, "MODEL_TYPE"),
        "name": grab_str(page, "MODEL_NAME") or re.sub(r"<title>([^<|]*).*", r"\1", re.search(r"<title>[^<]*", page).group(0)).strip(),
        "model": grab_json(page, "MODEL_JSON"),
        "held": grab_json(page, "MODEL_HELD_JSON"),
        "weapon_anims": grab_json(page, "WEAPONS_ANIMS_JSON"),
    }


def convert_texture(src: str, dst: str) -> None:
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    im = Image.open(src)
    if max(im.size) > MAX_TEX:
        s = MAX_TEX / max(im.size)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    im.save(dst, "WEBP", quality=WEBP_QUALITY, method=4)


def copy_file(src: str, dst: str) -> None:
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())


def process_model(mj: dict, jobs: list) -> dict:
    """Queue downloads/conversions for one MODEL_JSON block, return its trimmed manifest."""
    out = {}
    mo = mj.get("Model")
    if mo:
        url = norm(mo["Path"]) + mo["Name"]
        out["glb"] = rel(url) + ".glb"
        jobs.append(("copy", url, os.path.join(OUT, out["glb"])))
    mats = []
    for mat in mj.get("Materials", []) or []:
        m = {"name": mat["Name"], "type": mat.get("Type", "PBR")}
        for k in TEX_KEYS:
            if mat.get(k):
                m[k] = rel(mat[k]) + ".webp"
                jobs.append(("tex", norm(mat[k]), os.path.join(OUT, m[k])))
        for k in ("Metallic", "Roughness", "Emissive", "Color", "EmissiveIntensity", "Offset"):
            if mat.get(k) is not None:
                m[k] = mat[k]
        mats.append(m)
    out["materials"] = mats
    anims = []
    for a in mj.get("Animations", []) or []:
        an = {k: a[k] for k in ("Name", "Translatable", "SequenceLength", "SequenceLengthGN", "WeaponAnimName", "IsHeldDefault", "HeldItem") if k in a}
        if a.get("Type") == "External" and a.get("Path") and a.get("File"):
            url = norm(a["Path"]) + a["File"] + ".glb"
            an["glb"] = rel(url) + ".glb"
            jobs.append(("copy", url, os.path.join(OUT, an["glb"])))
        anims.append(an)
    out["animations"] = anims
    st = mj.get("Settings", {}) or {}
    out["settings"] = {k: st[k] for k in ("Type", "FOV", "FireRate", "FireRateAlt", "ReloadLength", "EquipLength", "AutomaticWeapon", "CameraParent", "FP", "CameraPosition", "OrbitTarget") if k in st}
    if mj.get("Constraints"):
        out["constraints"] = mj["Constraints"]
    if mj.get("FPExclusiveObjects"):
        out["fp_exclusive"] = mj["FPExclusiveObjects"]
    return out


def run_job(job):
    kind, url, dst = job
    try:
        src = cached(url)
        (convert_texture if kind == "tex" else copy_file)(src, dst)
        return None
    except Exception as e:  # noqa: BLE001
        return "%s: %s" % (url, e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated slugs (default: all agents, FP arms and weapons)")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()

    print("listing models…", flush=True)
    slugs = list_slugs()
    if args.only:
        slugs = [s for s in slugs if s in args.only.split(",")]
    manifest = {"agents": {}, "agents_fp": {}, "weapons": {}}
    jobs = []
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for d in ex.map(scrape, slugs):
            slug, typ = d["slug"], d["type"]
            if typ == "Agents_CS":
                entry = process_model(d["model"], jobs)
                entry["name"] = d["name"]
                manifest["agents"][slug] = entry
            elif typ == "Weapons" and slug.endswith("-fp"):
                entry = process_model(d["model"], jobs)
                entry["name"] = d["name"]
                entry["agent"] = slug[:-3]
                if d["held"]:
                    entry["held"] = process_model(d["held"], jobs)
                if d["weapon_anims"]:
                    entry["weapon_animations"] = process_model(d["weapon_anims"], jobs)["animations"]
                manifest["agents_fp"][slug] = entry
            elif typ == "Weapons":
                entry = process_model(d["model"], jobs)
                entry["name"] = d["name"]
                manifest["weapons"][slug] = entry
            print("  %-16s %s" % (slug, typ), flush=True)

    # First-person animations of every weapon are served by the FP agent pages
    # (/modelviewer/<agent>-fp?held=<weapon>); they are shared by all agents.
    fp_pages = sorted(manifest["agents_fp"])
    if fp_pages and manifest["weapons"]:
        base = fp_pages[0]
        print("first-person weapon animations (via %s)…" % base, flush=True)
        wslugs = sorted(manifest["weapons"])
        def scrape_held(w: str):
            try:
                return scrape(base, "?held=" + w)
            except Exception as e:  # noqa: BLE001  (some weapons have no FP page: 404)
                return {"held": None, "weapon_anims": None, "error": str(e)}

        with cf.ThreadPoolExecutor(args.jobs) as ex:
            for wslug, d in zip(wslugs, ex.map(scrape_held, wslugs)):
                held, wa = d["held"], d["weapon_anims"]
                if not held or not wa:
                    print("  %-16s no first-person data (%s)" % (wslug, d.get("error", "missing")), flush=True)
                    continue
                fp = process_model(held, jobs)
                fp["keybinds"] = held.get("Keybinds", [])
                fp["fp_animations"] = process_model(wa, jobs)["animations"]
                manifest["weapons"][wslug]["fp"] = fp
                print("  %-16s %d FP anims" % (wslug, len(fp["fp_animations"])), flush=True)

    uniq = {j[2]: j for j in jobs}.values()
    print("%d files to fetch/convert…" % len(uniq), flush=True)
    errors = []
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for i, err in enumerate(ex.map(run_job, list(uniq)), 1):
            if err:
                errors.append(err)
                print("  ERROR", err, flush=True)
            elif i % 50 == 0:
                print("  %d/%d" % (i, len(uniq)), flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    total = sum(os.path.getsize(os.path.join(r, fn)) for r, _, fs in os.walk(OUT) for fn in fs)
    print("done: %d agents, %d FP, %d weapons, %.0f MB in %s%s" % (
        len(manifest["agents"]), len(manifest["agents_fp"]), len(manifest["weapons"]), total / 1e6, OUT,
        "" if not errors else " (%d errors)" % len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
