"""Ligne de commande de la fenêtre de réglages (utilisable depuis n'importe quel langage) :

    python -m aimlabs.setup schema.json [--out valeurs.json] [--pi HOTE[:PORT]]     fenêtre ; code de sortie 0 = validé, 1 = annulé, 2 = schéma invalide
    python -m aimlabs.setup schema.json --print                                    valeurs enregistrées (ou par défaut), sans fenêtre
    python -m aimlabs.setup schema.json --check                                    vérifie seulement le schéma
    python -m aimlabs.setup schema.json --snap fenetre.png                         rend la fenêtre dans un PNG (documentation, tests)

`--pi` : le Pi qui relaie le clavier du joueur (assignation des touches en les appuyant, liste blanche des touches de sortie).
Les valeurs validées sont aussi enregistrées dans ~/.aimlabs/<app>.json ; sur la sortie standard (ou dans --out) : un objet JSON {id du champ: valeur}.
"""
import argparse
import json
import sys

from . import SchemaError, load_values, run, validate_schema, values_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aimlabs.setup", description=__doc__.splitlines()[0])
    ap.add_argument("schema", help="fichier JSON du schéma")
    ap.add_argument("--out", default="", help="écrit les valeurs validées dans ce fichier (sinon : sortie standard)")
    ap.add_argument("--pi", default="", help="HOTE[:PORT] du Pi qui relaie le clavier")
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--snap", default="")
    a = ap.parse_args(argv)
    try:
        with open(a.schema, encoding="utf-8") as f:
            schema = validate_schema(json.load(f))
    except (OSError, ValueError, SchemaError) as e:
        print("schéma invalide :", e, file=sys.stderr)
        return 2
    if a.check:
        print("schéma valide ·", schema["app"], "· valeurs :", values_path(schema["app"]))
        return 0
    if a.show:
        print(json.dumps(load_values(schema), indent=1, ensure_ascii=False))
        return 0
    keys = None
    if a.pi:
        from ..keys import PiKeys
        host, _, port = a.pi.partition(":")
        keys = PiKeys(host, int(port or 7780))
        if not keys.start(3.0):
            print("le Pi ne répond pas : les touches se choisiront au clavier de cette machine", file=sys.stderr)
            keys.close()
            keys = None
    values = run(schema, keys=keys, snap=a.snap)
    if keys is not None:
        keys.close()
    if a.snap:
        print("écrit", a.snap)
        return 0
    if values is None:
        return 1
    out = json.dumps(values, indent=1, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
