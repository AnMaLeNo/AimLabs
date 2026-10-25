"""Tests du flux de touches (remote/pi/fire_server.py <-> sdk/python/aimlabs/keys.py), SANS Pi ni clavier :

    python keys_test.py            # le vrai FireServer, avec un faux relais, sur le port 7791 de cette machine

Vérifie : abonnement signé, évènements dans l'ordre, filtrage (seules les touches demandées sortent), perte de paquets (évènements recomposés, touche « coincée »
corrigée), mauvais secret ignoré, touches avalées (rendues au PC dès que B disparaît), bindings bascule / maintien / impulsion, liste blanche annoncée (CAPS).
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "remote", "pi"))
import fire_server as FS  # noqa: E402
from aimlabs import keys as K  # noqa: E402

fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAUX ") + what, flush=True)
    if not ok:
        fails.append(what)


class FakeRelay:
    def __init__(self):
        self.on_key, self.swallow = None, frozenset()

    def inject(self, *a):
        pass

    def keepalive(self):
        pass


def wait(cond, t=1.0):
    t_end = time.monotonic() + t
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def main():
    secret = os.urandom(16)
    conf = os.path.join(tempfile.gettempdir(), "keys_test_fire.json")
    with open(conf, "w") as f:
        json.dump({"port": 7791, "key_hex": secret.hex(), "usages": [4, 10, 58], "rt_prio": 0}, f)
    relay = FakeRelay()
    srv = FS.FireServer(relay, object(), conf)
    srv.start()
    press = lambda name, down=True: relay.on_key(K.usage_of(name), down)

    print("noms de touches")
    check(K.usage_of("g") == 10 and K.name_of(0xE2) == "LALT" and K.usage_of("F8") == 0x41, "G = 10, LALT = 0xE2, F8 = 0x41")
    check(K.key_matches("ALT", "RALT") and not K.key_matches("LALT", "RALT") and K.key_matches("LALT", "ALT"), "ALT = LALT ou RALT ; LALT seul")

    print("abonnement et évènements")
    ev = []
    pk = K.PiKeys("127.0.0.1", 7791, key=secret, watch=["CAPSLOCK", "ALT", "F8"], swallow=["F8"])
    pk.add_listener(lambda n, d: ev.append((n, d)))
    check(pk.start(2.0), "premier paquet reçu après l'abonnement")
    check(wait(lambda: relay.swallow == frozenset({K.usage_of("F8")})), "touche avalée annoncée au relais : F8")
    press("CAPSLOCK")
    press("CAPSLOCK", False)
    press("LALT")
    press("G")                                           # non demandée : ne doit pas sortir
    press("G", False)
    press("LALT", False)
    check(wait(lambda: len(ev) >= 4), "4 évènements reçus")
    check(ev == [("CAPSLOCK", True), ("CAPSLOCK", False), ("LALT", True), ("LALT", False)], f"dans l'ordre, sans la touche non demandée : {ev}")
    check(pk.held == frozenset(), "plus aucune touche enfoncée")

    print("bindings")
    b = K.Bindings(pk)
    b.add("auto", {"key": "CAPSLOCK", "mode": "toggle"})
    b.add("burst", {"key": "ALT", "mode": "hold"})
    b.add("mark", {"key": "F8", "mode": "press"})
    press("CAPSLOCK")
    press("CAPSLOCK", False)
    check(wait(lambda: b.get("auto")), "bascule : un appui active")
    press("CAPSLOCK")
    press("CAPSLOCK", False)
    check(wait(lambda: not b.get("auto")), "bascule : un second appui désactive")
    press("RALT")
    check(wait(lambda: b.get("burst")), "maintien : vrai tant que RALT est enfoncée (ALT = LALT ou RALT)")
    press("LALT")
    press("RALT", False)
    time.sleep(0.05)
    check(b.get("burst"), "maintien : encore vrai (LALT toujours enfoncée)")
    press("LALT", False)
    check(wait(lambda: not b.get("burst")), "maintien : faux au relâchement")
    press("F8")
    press("F8", False)
    check(wait(lambda: b.pressed("mark")) and not b.pressed("mark"), "impulsion : vraie une seule fois par appui")

    print("pertes de paquets")
    n0 = pk.counters["packets"]
    sock_send = srv._send
    srv._send = lambda *a, **k: None                      # le réseau « disparaît » : ces évènements ne parviendront jamais à B
    for _ in range(5):                                    # 10 évènements : l'historique du paquet n'en garde que 6, les plus anciens sont perdus pour de bon
        press("CAPSLOCK")
        press("CAPSLOCK", False)
        time.sleep(0.02)
    press("LALT")
    time.sleep(0.05)
    srv._send = sock_send
    check(wait(lambda: b.get("burst")), "touche pressée pendant la panne : retrouvée au battement suivant (liste des touches enfoncées)")
    check(pk.counters["gaps"] >= 1, f"trou d'évènements détecté (les bascules perdues ne sont pas rejouées, l'état des touches maintenues est exact) {pk.counters}")
    press("LALT", False)
    press("CAPSLOCK", False)
    check(wait(lambda: not b.get("burst")), "relâchement retrouvé")

    print("sécurité")
    bad = K.PiKeys("127.0.0.1", 7791, key=os.urandom(16), watch=["G"])
    bad.start(0.6)
    check(bad.counters["packets"] == 0 and len(srv.subs) == 1, "mauvais secret : ni abonnement ni réponse")
    bad.close()
    caps = pk.allowed_output_keys()
    check(caps == sorted({"A", "G", "F1"}), f"liste blanche annoncée : {caps}")
    pk.close()
    FS.SUB_TTL_S = 0.0
    check(wait(lambda: relay.swallow == frozenset(), 3.0), "abonné disparu : la touche avalée est rendue au PC")

    print("\n" + ("TOUT EST BON" if not fails else f"{len(fails)} ÉCHEC(S) : " + " | ".join(fails)), flush=True)
    os._exit(1 if fails else 0)


if __name__ == "__main__":
    main()
