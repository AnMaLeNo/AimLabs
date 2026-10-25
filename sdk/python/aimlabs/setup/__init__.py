"""Fenêtre de réglages générique d'AimLabs : le programme du joueur DÉCRIT ce qui est réglable (un « schéma » JSON), la fenêtre affiche les réglages, le joueur les change,
et le programme récupère les valeurs. Aucune connaissance du programme n'est codée dans la fenêtre : n'importe quel algorithme, dans n'importe quel langage, peut s'en servir.

La fenêtre ne tourne QUE pendant le réglage (avant de lancer l'algorithme) : une fois validée elle se ferme, rien ne reste dans le chemin chaud. Elle est distincte de la fenêtre
de debug (`sniper_b.py --view`), qui montre l'algorithme pendant qu'il tourne.

Trois façons de l'utiliser (même schéma, mêmes valeurs) :

  1. Python :   from aimlabs.setup import run
                values = run(schema)            # bloque jusqu'à Valider / Annuler ; None si annulé

  2. Ligne de commande (tout langage) :
                python -m aimlabs.setup schema.json --out valeurs.json       # code de sortie 0 = validé, 1 = annulé
                python -m aimlabs.setup schema.json --print                  # affiche les valeurs enregistrées (ou par défaut) sans fenêtre

  3. Sans rien lancer : relire `~/.aimlabs/<app>.json` (voir `values_path`), écrit à chaque validation.

Schéma (version 1) :

  {"setup": 1, "app": "mon_algo", "title": "Mon algo",
   "pages": [{"id": "touches", "title": "Touches", "fields": [ <champ>, ... ]}, ...]}

Champs (`id` unique dans tout le schéma ; `label`, `help` facultatifs ; `default` facultatif) :

  bool      {"type": "bool"}                                        -> true / false
  int/float {"type": "int", "min": 0, "max": 100, "step": 1, "unit": "ms"}   -> nombre
  text      {"type": "text", "max_len": 60}                         -> chaîne
  choice    {"type": "choice", "options": ["a", "b"] | [{"value": 1, "label": "Un"}]}
  key_in    {"type": "key_in", "modes": ["toggle", "hold", "press"], "swallow": false}   -> {"key": "CAPSLOCK" | null, "mode": "toggle", "swallow": false}
            TOUCHE EN ENTRÉE : une touche du clavier du joueur qui commande le programme. `toggle` : bascule à chaque appui ; `hold` : actif tant qu'elle est enfoncée ;
            `press` : une impulsion par appui. Le joueur l'assigne en APPUYANT la touche (le clavier est branché sur le Pi, qui la transmet) ; `aimlabs.keys.Bindings` en fait des états.
            `swallow` : le joueur peut demander que la touche NE soit PAS transmise au PC pendant que le programme tourne (le programme la donne alors à `PiKeys(swallow=...)`).
  key_out   {"type": "key_out", "allowed": ["G", "F1"]}              -> "G"
            TOUCHE EN SORTIE : une touche que le programme fera appuyer (injectée par le Pi), ex. la touche de tir. `allowed` : liste imposée ; sinon la liste blanche du Pi.
  label     {"type": "label", "text": "..."}                         -> texte d'explication (aucune valeur)

Pages « actives » : `{"id": "capture", "title": "Capture", "type": "capture"}` demande au programme hôte d'en fournir le code (`plugins={"capture": page}`) : c'est ainsi que
sniper_b.py ajoute le cadrage de la capture (aperçu en direct) à côté des champs ordinaires. Sans hôte pour la fournir, la page affiche « non disponible ».

Valeurs : un dictionnaire PLAT {id du champ: valeur} (+ une entrée par page active, ex. "capture"), enregistré dans `~/.aimlabs/<app>.json` à chaque validation.
"""
import json
import os
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
FIELD_TYPES = ("bool", "int", "float", "text", "choice", "key_in", "key_out", "label")
KEY_MODES = ("toggle", "hold", "press")


class SchemaError(ValueError):
    pass


def values_path(app: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".aimlabs", "%s.json" % app)


def fields_of(schema: dict) -> List[dict]:
    return [f for p in schema.get("pages", []) for f in p.get("fields", [])]


def validate_schema(schema: dict) -> dict:
    """Vérifie le schéma (messages d'erreur précis : il est écrit par des joueurs) et le retourne avec les valeurs par défaut complétées."""
    if not isinstance(schema, dict):
        raise SchemaError("le schéma doit être un objet JSON")
    if schema.get("setup", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise SchemaError("version de schéma %r non prise en charge (attendu : %d)" % (schema.get("setup"), SCHEMA_VERSION))
    app = schema.get("app")
    if not isinstance(app, str) or not app or not all(c.isalnum() or c in "_-" for c in app):
        raise SchemaError("« app » : un identifiant (lettres, chiffres, _ et -) est obligatoire : il nomme le fichier de valeurs")
    pages = schema.get("pages")
    if not isinstance(pages, list) or not pages:
        raise SchemaError("« pages » : au moins une page")
    seen = set()
    for pi, page in enumerate(pages):
        where = "page %d" % (pi + 1)
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            raise SchemaError("%s : « id » obligatoire" % where)
        if page.get("type") not in (None, "form") and "fields" in page:
            raise SchemaError("%s : une page « %s » n'a pas de champs" % (where, page["type"]))
        for f in page.get("fields", []):
            fid, typ = f.get("id"), f.get("type")
            if typ not in FIELD_TYPES:
                raise SchemaError("champ %r : type %r inconnu (attendu : %s)" % (fid, typ, ", ".join(FIELD_TYPES)))
            if typ != "label":
                if not isinstance(fid, str) or not fid:
                    raise SchemaError("%s : un champ sans « id »" % where)
                if fid in seen or fid in {p["id"] for p in pages if p.get("type") not in (None, "form")}:
                    raise SchemaError("champ %r : « id » déjà utilisé" % fid)
                seen.add(fid)
            if typ in ("int", "float") and not all(isinstance(f.get(k, 0), (int, float)) for k in ("min", "max", "step")):
                raise SchemaError("champ %r : min / max / step doivent être des nombres" % fid)
            if typ == "choice" and not f.get("options"):
                raise SchemaError("champ %r : « options » obligatoire" % fid)
            if typ == "key_in":
                modes = f.get("modes", list(KEY_MODES))
                if not modes or any(m not in KEY_MODES for m in modes):
                    raise SchemaError("champ %r : modes parmi %s" % (fid, ", ".join(KEY_MODES)))
    return schema


def default_for(f: dict) -> Any:
    if "default" in f:
        return f["default"]
    t = f["type"]
    if t == "bool":
        return False
    if t in ("int", "float"):
        return f.get("min", 0)
    if t == "text":
        return ""
    if t == "choice":
        o = f["options"][0]
        return o["value"] if isinstance(o, dict) else o
    if t == "key_in":
        return {"key": None, "mode": f.get("modes", list(KEY_MODES))[0], "swallow": bool(f.get("swallow", False))}
    if t == "key_out":
        return None
    return None


def defaults(schema: dict) -> Dict[str, Any]:
    return {f["id"]: coerce(f, default_for(f)) for f in fields_of(schema) if f["type"] != "label"}


def coerce(f: dict, v: Any) -> Any:
    """Ramène une valeur (fichier enregistré, saisie) à un type valide pour le champ ; la valeur par défaut si elle est inutilisable."""
    t = f["type"]
    try:
        if t == "bool":
            return bool(v)
        if t in ("int", "float"):
            x = float(v)
            if "min" in f:
                x = max(f["min"], x)
            if "max" in f:
                x = min(f["max"], x)
            return int(round(x)) if t == "int" else x
        if t == "text":
            return str(v)[:int(f.get("max_len", 200))]
        if t == "choice":
            vals = [o["value"] if isinstance(o, dict) else o for o in f["options"]]
            return v if v in vals else default_for(f)
        if t == "key_in":
            modes = f.get("modes", list(KEY_MODES))
            key = v.get("key") if isinstance(v, dict) else None
            mode = v.get("mode") if isinstance(v, dict) and v.get("mode") in modes else modes[0]
            swallow = v.get("swallow", f.get("swallow", False)) if isinstance(v, dict) else f.get("swallow", False)
            return {"key": str(key).upper() if key else None, "mode": mode, "swallow": bool(swallow)}
        if t == "key_out":
            return str(v).upper() if v else None
    except (TypeError, ValueError):
        pass
    return default_for(f)


def load_values(schema: dict, path: Optional[str] = None, strict_defaults: bool = False) -> Dict[str, Any]:
    """Valeurs enregistrées (si le fichier existe) complétées par les valeurs par défaut du schéma. Les pages actives gardent leur entrée telle quelle."""
    vals = defaults(schema)
    path = path or values_path(schema["app"])
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if strict_defaults:
        return vals
    by_id = {f["id"]: f for f in fields_of(schema) if f["type"] != "label"}
    for k, v in saved.items():
        if k in by_id:
            vals[k] = coerce(by_id[k], v)
        elif isinstance(v, (dict, list)):                      # entrée d'une page active
            vals[k] = v
    return vals


def save_values(schema: dict, values: Dict[str, Any], path: Optional[str] = None) -> str:
    path = path or values_path(schema["app"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def run(schema: dict, values: Optional[dict] = None, keys=None, plugins: Optional[dict] = None, save: bool = True, snap: str = "") -> Optional[Dict[str, Any]]:
    """Ouvre la fenêtre de réglages et BLOQUE jusqu'à ce que le joueur valide (retourne les valeurs, enregistrées dans ~/.aimlabs/<app>.json) ou annule (retourne None).

    keys    : un `aimlabs.keys.PiKeys` (flux des touches du clavier branché sur le Pi) : permet d'assigner une touche en l'appuyant, et de connaître la liste blanche des touches de sortie.
              Sans lui, les touches se choisissent au clavier de la machine qui affiche la fenêtre.
    plugins : {type de page: objet page} pour les pages actives (voir ui.Page).
    snap    : chemin d'un PNG : pas de fenêtre, la fenêtre est rendue dans ce fichier (tests, documentation)."""
    from .ui import SetupWindow
    schema = validate_schema(schema)
    win = SetupWindow(schema, load_values(schema) if values is None else values, keys=keys, plugins=plugins or {})
    if snap:
        win.snapshot(snap)
        return None
    result = win.run()
    if result is not None and save:
        save_values(schema, result)
    return result
