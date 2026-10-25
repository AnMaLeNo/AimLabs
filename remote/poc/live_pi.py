"""Session de jeu complète en mode SILENT, tir par le Raspberry Pi : le jeu ne publie RIEN (pas de pont), le PC A capture l'écran et envoie les images à B (Mac),
B décide et envoie l'ordre de tir au Pi, qui appuie sur la touche de tir du jeu par Bluetooth. Rend les touches sur N passages et la précision de tir mesurée par le jeu.

    python live_pi.py --speed 36 --passes 30 [--pi 192.168.1.101] [--lead-ms 4.8] [--bias-ms 0] [--tag essai]

Ordre : B écoute (Mac) -> jeu (--mode=silent) -> la fenêtre du jeu prend le FOCUS CLAVIER (vérifié : sinon on s'arrête, pour ne jamais taper la touche de tir dans une autre
application) -> agent_a --no-bridge -> fin du jeu -> nettoyage. Secret du Pi : ~/.aimlabs/pi_fire.key (PC et Mac).
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_bench as A  # noqa: E402
import shot_timing  # noqa: E402
from live_bench import GODOT, kill_local  # noqa: E402

TITLE = "AimLabs (DEBUG)"
u32 = ctypes.windll.user32


def fg_title():
    h = u32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(h, buf, 256)
    return buf.value


def focus_game(timeout=45.0):
    """Attend la fenêtre du jeu et lui donne le focus clavier. Rend True seulement si elle est bien au premier plan."""
    t_end = time.time() + timeout
    while time.time() < t_end:
        h = u32.FindWindowW(None, TITLE)
        if h:
            u32.keybd_event(0x12, 0, 0, 0)               # Alt : autorise SetForegroundWindow depuis un processus de fond
            u32.keybd_event(0x12, 0, 2, 0)
            u32.ShowWindow(h, 9)                          # SW_RESTORE
            u32.SetForegroundWindow(h)
            time.sleep(0.4)
            if fg_title() == TITLE:
                return True
        time.sleep(0.5)
    return False


def pi_stats(pi_ssh, rx=0):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-i", A.KEY, pi_ssh, f"sudo btkbd-send fire {rx}"], capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def rx_summary(entries):
    """Ordres programmés reçus par le Pi : avance sur l'échéance À L'ARRIVÉE (ms ; < 0 = arrivé en retard) = la mesure du réseau B -> Pi dans les conditions réelles.
    On regroupe en « rafales » (un passage de cible = une rafale d'ordres mis à jour image par image)."""
    if not entries:
        return {}
    leads = [e[4] / 1e6 for e in entries]
    bursts, cur = [], [entries[0]]
    for e in entries[1:]:
        if (e[0] - cur[-1][0]) / 1e6 > 400:
            bursts.append(cur)
            cur = []
        cur.append(e)
    bursts.append(cur)
    last = [b[-1][4] / 1e6 for b in bursts]
    q = lambda v, p: sorted(v)[min(int(len(v) * p), len(v) - 1)]
    return {"ordres": len(entries), "tard_%": round(100 * sum(1 for x in leads if x < 0) / len(leads), 1), "avance_p10": round(q(leads, 0.1), 1),
            "avance_p50": round(q(leads, 0.5), 1), "avance_p90": round(q(leads, 0.9), 1), "rafales": len(bursts),
            "dernier_ordre_de_la_rafale_tard_%": round(100 * sum(1 for x in last if x < 0) / len(last), 1), "dernier_avance_p50": round(q(last, 0.5), 1)}


def run(speed, passes, pi, lead_ms, bias_ms, settle, tag, extra_b=""):
    kill_local()
    log = "~/aimlabs-remote/live_pi_b.log"
    cfg = dict(dir="~/aimlabs-remote/repo", args=f"--fire-via pi:{pi} --pi-lead-ms {lead_ms} --bias-ms {bias_ms} {extra_b}")
    b_pid = A.start_b(cfg, log)
    out_json = os.path.join(A.ROOT, "bench_results", f"livepi_{tag}.json")
    game_log = os.path.join(A.ROOT, "bench_results", f"livepi_{tag}.game.log")
    agent_log = os.path.join(A.ROOT, "bench_results", f"livepi_{tag}.agent.log")
    pb = pi_stats(f"claude@{pi}", 4000)
    pi_before = pb.get("fire", {}).get("stats", {})
    rx_before = pb.get("rx", [])
    t_rx_last = rx_before[-1][0] if rx_before else 0
    gl = open(game_log, "w", encoding="utf-8", errors="replace")
    game = subprocess.Popen([GODOT, "--path", os.path.join(A.ROOT, "game"), "--always-on-top", "--", "--mode=silent", "--map=range", "--fire-key=G", "--bench-sniper",
                             "--bench-criterion=hit", f"--bench-settle={settle}", "--bench-distance-start=5", "--bench-distance-max=5", "--bench-heights=chest",
                             f"--bench-fixed-speed={speed}", f"--bench-passes={passes}", f"--bench-out={out_json}"], stdout=gl, stderr=subprocess.STDOUT)
    agent = None
    ok = focus_game()
    if not ok:
        print(f"ARRÊT : la fenêtre du jeu n'a pas le focus clavier (premier plan : « {fg_title()} »). Aucun tir envoyé.", flush=True)
    else:
        al = open(agent_log, "w", encoding="utf-8", errors="replace")
        agent = subprocess.Popen([sys.executable, os.path.join(A.ROOT, "remote", "poc", "agent_a.py"), "--no-bridge", "--connect", A.DATA_HOST + ":7777", "--transport", "tcp",
                                  "--jpeg", "60", "--crop", "384", "--resize", "256", "--window", "AimLabs (DEBUG)"], stdout=al, stderr=subprocess.STDOUT)
        try:
            game.wait(timeout=settle + 60 + passes * 6)
        except subprocess.TimeoutExpired:
            pass
    time.sleep(1.5)
    if agent:
        agent.terminate()
    text = A.ssh(f"cat {log}")
    A.ssh(f"for p in $(pgrep -P {b_pid}); do kill $p; done; kill {b_pid}; sleep 0.5; pkill -f '[s]niper_b.py'")
    kill_local()
    gl.close()
    pa = pi_stats(f"claude@{pi}", 4000)
    pi_after = pa.get("fire", {}).get("stats", {})
    rx_new = [e for e in pa.get("rx", []) if e[0] > t_rx_last]
    g = open(game_log, encoding="utf-8", errors="replace").read()
    m = re.search(r"→ (\d+)/(\d+) réussites", g)
    hits = int(m.group(1)) if m else None
    return dict(ok=ok, hits=hits, passes=passes, out_json=out_json, b_log=text, pi_delta={k: pi_after.get(k, 0) - pi_before.get(k, 0) for k in pi_after}, rx=rx_summary(rx_new))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--speed", type=float, default=36.0)
    ap.add_argument("--passes", type=int, default=30)
    ap.add_argument("--pi", default="192.168.1.101", help="adresse du Pi vue depuis le Mac ET le PC")
    ap.add_argument("--lead-ms", type=float, default=4.8)
    ap.add_argument("--bias-ms", type=float, default=0.0)
    ap.add_argument("--settle", type=float, default=30.0, help="délai avant le premier passage (le temps de démarrer l'agent)")
    ap.add_argument("--tag", default=time.strftime("%H%M%S"))
    ap.add_argument("--b-args", default="", help="options supplémentaires de sniper_b")
    a = ap.parse_args()
    r = run(a.speed, a.passes, a.pi, a.lead_ms, a.bias_ms, a.settle, a.tag, a.b_args)
    if not r["ok"]:
        return 1
    print(f"vitesse {a.speed:g} m/s : {r['hits']}/{a.passes} touches ; serveur de tir du Pi : {r['pi_delta']}")
    print("  ordres reçus par le Pi (avance sur l'échéance à l'arrivée, ms) :", r["rx"])
    shot_timing.report(r["out_json"])
    for line in [l for l in r["b_log"].splitlines() if l.startswith("[pi]")][-3:]:
        print(" ", line[:230])
    return 0


if __name__ == "__main__":
    sys.exit(main())
