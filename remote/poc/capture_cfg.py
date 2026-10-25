"""Configuration du cadrage de la capture (sans dépendance Windows : utilisée par A, par B et par l'interface de réglage)."""


DEFAULT_CFG = {"mode": "screen", "title": "", "process": "", "monitor": 0, "dx": 0, "dy": 0, "crop": 384, "resize": 256}


def sanitize(cfg: dict) -> dict:
    """Configuration valide, bornée (elle vient du réseau : jamais de valeur absurde, de type inattendu ou de chaîne géante)."""
    c = dict(DEFAULT_CFG)
    c.update({k: cfg[k] for k in DEFAULT_CFG if k in cfg})
    c["mode"] = "window" if c["mode"] == "window" else "screen"
    c["title"], c["process"] = str(c["title"])[:200], str(c["process"])[:100]
    num = lambda v, lo, hi, d: max(lo, min(hi, int(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else d
    c["monitor"] = num(c["monitor"], 0, 15, 0)
    c["dx"], c["dy"] = num(c["dx"], -8000, 8000, 0), num(c["dy"], -8000, 8000, 0)
    c["crop"], c["resize"] = num(c["crop"], 32, 2048, 384), num(c["resize"], 0, 1024, 256)    # resize 0 = pas de réduction
    if 0 < c["resize"] < 32:
        c["resize"] = 32
    return c


def region_in(ref, cfg) -> tuple:
    """Carré de `crop` px centré sur la référence (x, y, w, h), décalé de (dx, dy), ramené à l'intérieur de la référence."""
    rx, ry, rw, rh = ref
    side = min(cfg["crop"], rw, rh)
    x = round(rx + rw / 2 + cfg["dx"] - side / 2)
    y = round(ry + rh / 2 + cfg["dy"] - side / 2)
    return (max(rx, min(x, rx + rw - side)), max(ry, min(y, ry + rh - side)), side, side)
