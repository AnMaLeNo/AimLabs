"""Lecture / écriture des enregistrements de comportement du jeu (`game/scripts/behavior_log.gd`, fichiers .aimbeh).

    from aimbeh import load
    log = load("behavior_logs/silent_2026-11-12T22-00-00.aimbeh")
    log.header            # dict (mode, arme, sensibilité, carte…)
    log.motion            # tableau structuré : t (µs), dx, dy                      mouvement réel de la souris (pixels)
    log.button            # t, pressed, key                                         bouton / touche de tir
    log.frame             # t, yaw, pitch, fov, n_visible, near_id, off_x, off_y, half_w, half_h, dist, on_id, zone, ax, ay
    log.visible           # t, id, visible                                          une cible devient visible / ne l'est plus
    log.shot              # t, hit, id, zone, killed                                un tir est parti
    log.playing           # t, captured                                             en jeu / en pause

Les temps sont ceux de l'horloge du jeu (µs) au moment où il REÇOIT l'évènement : leur résolution est d'une image.
"""
import json
import struct
from types import SimpleNamespace

import numpy as np

MAGIC = b"AIMBEH1\n"
RECORDS = {
    1: ("motion", "<Qff", [("t", "u8"), ("dx", "f4"), ("dy", "f4")]),
    2: ("button", "<QBB", [("t", "u8"), ("pressed", "u1"), ("key", "u1")]),
    3: ("frame", "<QfffHhfffffhBff", [("t", "u8"), ("yaw", "f4"), ("pitch", "f4"), ("fov", "f4"), ("n_visible", "u2"), ("near_id", "i2"),
                                     ("off_x", "f4"), ("off_y", "f4"), ("half_w", "f4"), ("half_h", "f4"), ("dist", "f4"),
                                     ("on_id", "i2"), ("zone", "u1"), ("ax", "f4"), ("ay", "f4")]),
    4: ("visible", "<QhB", [("t", "u8"), ("id", "i2"), ("visible", "u1")]),
    5: ("shot", "<QBhBB", [("t", "u8"), ("hit", "u1"), ("id", "i2"), ("zone", "u1"), ("killed", "u1")]),
    6: ("playing", "<QB", [("t", "u8"), ("captured", "u1")]),
}
_BY_NAME = {name: (code, fmt, dtype) for code, (name, fmt, dtype) in RECORDS.items()}


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:len(MAGIC)] != MAGIC:
        raise ValueError(f"{path} : pas un enregistrement AIMBEH1")
    (n,) = struct.unpack_from("<I", data, len(MAGIC))
    pos = len(MAGIC) + 4
    header = json.loads(data[pos:pos + n].decode("utf-8"))
    pos += n
    rows = {name: [] for name, _, _ in RECORDS.values()}
    sizes = {code: struct.calcsize(fmt) for code, (_, fmt, _) in RECORDS.items()}
    truncated = False
    while pos < len(data):
        code = data[pos]
        if code not in RECORDS or pos + 1 + sizes[code] > len(data):
            truncated = True                    # fichier coupé (jeu tué pendant l'écriture) : on garde ce qui est lisible
            break
        name, fmt, _ = RECORDS[code]
        rows[name].append(struct.unpack_from(fmt, data, pos + 1))
        pos += 1 + sizes[code]
    out = SimpleNamespace(header=header, path=str(path), truncated=truncated)
    for name, _, dtype in RECORDS.values():
        setattr(out, name, np.array(rows[name], dtype=dtype) if rows[name] else np.zeros(0, dtype=dtype))
    return out


def save(path, header, records):
    """`records` : liste de (nom, tuple de valeurs), dans l'ordre du temps. Sert aux tests (enregistrements synthétiques)."""
    head = json.dumps(header).encode("utf-8")
    with open(path, "wb") as f:
        f.write(MAGIC + struct.pack("<I", len(head)) + head)
        for name, values in records:
            code, fmt, _ = _BY_NAME[name]
            f.write(bytes([code]) + struct.pack(fmt, *values))
