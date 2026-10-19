"""Bot jouet : masque de pixels « peau », vise le blob le plus proche du réticule et tire. Toujours actif."""

import time

from aimlabs import AimClient
from aimlabs.vision import skin_target

GAIN = 0.6          # pixels de souris par pixel d'erreur (dépend de la sensibilité)
FIRE_RADIUS = 6.0   # px


def main() -> None:
    with AimClient() as c:
        print("connected, mode", c.mode, flush=True)
        last_fire = 0.0
        while True:
            f = c.frame(timeout=2.0)
            if f is None:
                if not c.game_alive():
                    print("game gone", flush=True)
                    return
                continue
            t = skin_target(f.image)
            if t is None:
                continue
            cx, cy, n = t
            ex, ey = cx - f.width / 2, cy - f.height / 2
            c.move(ex * GAIN, ey * GAIN, frame_id=f.id)
            if abs(ex) < FIRE_RADIUS and abs(ey) < FIRE_RADIUS and time.monotonic() - last_fire > 0.12:
                c.fire(frame_id=f.id)
                last_fire = time.monotonic()
                s = c.state()
                print(f"frame #{f.id}: blob {n}px -> fire; shots={s['shots']} hits={s['hits']} kills={s['kills']} "
                      f"latency={s['avg_latency_usec'] / 1000:.1f} ms", flush=True)


if __name__ == "__main__":
    main()
