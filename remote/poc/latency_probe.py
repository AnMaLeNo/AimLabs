"""PC B : sonde de latence du transport, sans inférence. Pour chaque image reçue elle renvoie tout de suite
une commande `mark` : le « rtt capture→cmd reçue » affiché par l'agent du PC A est alors le coût pur de
(encodage + réseau aller + décodage + réseau retour), à comparer à celui d'un vrai algo.

    python latency_probe.py <ip du PC A>:7777 [secondes]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk", "python"))
from aimlabs.remote import RemoteClient  # noqa: E402

addr = sys.argv[1]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
n, t0 = 0, time.monotonic()
with RemoteClient(addr) as c:
    print("sonde connectée, mode", c.mode, flush=True)
    while time.monotonic() - t0 < seconds:
        f = c.frame(timeout=1.0)
        if f is None:
            continue
        c.mark(frame_id=f.id)
        n += 1
print(f"{n} images traitées en {time.monotonic() - t0:.1f} s ({n / (time.monotonic() - t0):.0f}/s)", flush=True)
