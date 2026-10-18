"""Auto-aim : vise la cible la plus proche du réticule. CAPSLOCK = on/off, ALT maintenu = vise tant qu'appuyé, V = tir auto on/off."""

import time

from aimlabs import AimClient
from aimlabs.vision import skin_target

# Touches lues dans l'état partagé (noms Godot : "A".."Z", "0".."9", "SPACE", "SHIFT", "CTRL",
# "ALT", "CAPSLOCK", "F1".."F12", "KP_0".., souris "LEFT"/"RIGHT"/"MIDDLE"/"XBUTTON1"...).
TOGGLE_KEY = "CAPSLOCK"   # bascule l'aide à la visée
HOLD_KEY = "ALT"          # aide active seulement tant que la touche est maintenue
AUTOFIRE_KEY = "V"        # bascule le tir automatique quand la cible est centrée

GAIN = 0.5                # pixels de souris par pixel d'erreur
MAX_STEP_PX = 60.0        # borne par frame, évite les à-coups sur un faux positif
FIRE_RADIUS = 5.0         # px autour du centre pour déclencher le tir auto
FIRE_INTERVAL = 0.12      # s


def main() -> None:
    enabled = False
    autofire = False
    with AimClient() as c:
        print(f"auto_aim connecté, mode {c.mode}. {TOGGLE_KEY}: on/off, {HOLD_KEY} maintenu: vise, "
              f"{AUTOFIRE_KEY}: tir auto", flush=True)
        last_fire = 0.0
        while True:
            f = c.frame(timeout=2.0)
            if f is None:
                if not c.game_alive():
                    print("jeu fermé", flush=True)
                    return
                continue
            k = c.keys()
            if TOGGLE_KEY in k.just_pressed:
                enabled = not enabled
                print("auto-aim", "ON" if enabled else "OFF", flush=True)
            if AUTOFIRE_KEY in k.just_pressed:
                autofire = not autofire
                print("tir auto", "ON" if autofire else "OFF", flush=True)
            if not (enabled or HOLD_KEY in k):
                continue
            t = skin_target(f.image)
            if t is None:
                continue
            cx, cy, _n = t
            ex, ey = cx - f.width / 2, cy - f.height / 2
            dx = max(-MAX_STEP_PX, min(MAX_STEP_PX, ex * GAIN))
            dy = max(-MAX_STEP_PX, min(MAX_STEP_PX, ey * GAIN))
            c.move(dx, dy, frame_id=f.id)
            if autofire and abs(ex) < FIRE_RADIUS and abs(ey) < FIRE_RADIUS and time.monotonic() - last_fire > FIRE_INTERVAL:
                c.fire(frame_id=f.id)
                last_fire = time.monotonic()


if __name__ == "__main__":
    main()
