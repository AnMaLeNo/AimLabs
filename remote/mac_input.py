#!/usr/bin/env python3
"""Souris du Mac par CoreGraphics (ctypes, aucune installation) : à lancer DANS la session graphique, via remote/mac_job.sh.

    python3 mac_input.py where                       position du pointeur
    python3 mac_input.py click X Y [--keep]          clic gauche en (X, Y) (points d'écran, origine en haut à gauche) ; le pointeur est RAMENÉ où il était (sauf --keep)
    python3 mac_input.py drag X1 Y1 X2 Y2 [--steps N] [--keep]    glisser-déposer

Pourquoi : `tell application "System Events" to click at {x, y}` échoue (erreur -25204, constatée sur macOS 26.5) alors que les touches (`key code`) et la lecture de la
position des fenêtres passent. Les évènements CoreGraphics ne demandent que l'autorisation « Accessibilité » déjà donnée au Terminal du « runner ».
Ce que ça dérange : le pointeur réel bouge pendant ~0,3 s (puis revient) et le clic atterrit sur ce qui est sous lui : à n'utiliser que quand l'utilisateur n'est pas en train de
travailler sur le Mac, et seulement après avoir mis la fenêtre visée au premier plan à une position connue (AIMLABS_SETUP_POS, voir sdk/python/aimlabs/setup/ui.py).
"""
import ctypes
import ctypes.util
import sys
import time

cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


cg.CGEventCreate.restype = ctypes.c_void_p
cg.CGEventCreate.argtypes = [ctypes.c_void_p]
cg.CGEventGetLocation.restype = CGPoint
cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
cf.CFRelease.argtypes = [ctypes.c_void_p]
MOVED, DOWN, UP, DRAG = 5, 1, 2, 6


def where():
    ev = cg.CGEventCreate(None)
    p = cg.CGEventGetLocation(ev)
    cf.CFRelease(ev)
    return p.x, p.y


def post(kind, x, y):
    ev = cg.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), 0)
    cg.CGEventPost(0, ev)                 # kCGHIDEventTap
    cf.CFRelease(ev)


def move(x, y):
    post(MOVED, x, y)
    time.sleep(0.03)


def click(x, y):
    move(x, y)
    post(DOWN, x, y)
    time.sleep(0.05)
    post(UP, x, y)
    time.sleep(0.05)


def drag(x1, y1, x2, y2, steps=12):
    move(x1, y1)
    post(DOWN, x1, y1)
    for i in range(1, steps + 1):
        post(DRAG, x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        time.sleep(0.02)
    post(UP, x2, y2)
    time.sleep(0.05)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, args, keep = argv[0], [a for a in argv[1:] if not a.startswith("--")], "--keep" in argv
    steps = 12
    if "--steps" in argv:
        steps = int(argv[argv.index("--steps") + 1])
        args = [a for a in args if a != str(steps)]
    x0, y0 = where()
    if cmd == "where":
        print("%.0f %.0f" % (x0, y0))
        return
    if cmd == "click":
        click(float(args[0]), float(args[1]))
    elif cmd == "drag":
        drag(*(float(a) for a in args[:4]), steps=steps)
    else:
        sys.exit("commande inconnue : " + cmd)
    if not keep:
        move(x0, y0)


if __name__ == "__main__":
    main(sys.argv[1:])
