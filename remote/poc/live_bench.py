"""Banc EN DIRECT de la chaîne distante : jeu (benchmark sniper) sur ce PC + agent_a + sniper_b sur le Mac, dans le bon ordre, avec
nettoyage. Rend le nombre de touches sur N traversées à une vitesse donnée, et les statistiques de B.

    python live_bench.py --speed 36 --passes 30 --configs off,view-gui          # configurations de ab_bench.py
    python live_bench.py --speed 48 --passes 30 --configs off --repeat 2

Ordre : B écoute (Mac) -> jeu (attend le relais) -> agent_a (s'attache au pont, le benchmark démarre) -> fin du jeu -> nettoyage.
Variables : GODOT (exécutable), MAC / MAC_KEY (voir ab_bench.py).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_bench as A  # noqa: E402

GODOT = os.environ.get("GODOT", r"C:\Users\antoi\Godot\Godot_v4.7.2-stable_win64_console.exe")


def kill_local():
    for exe in ("Godot_v4.7.2-stable_win64_console.exe", "Godot_v4.7.2-stable_win64.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True)


def run_live(name, speed, passes, tag):
    cfg = A.CONFIGS[name]
    kill_local()
    log = "~/aimlabs-remote/live_b.log"
    pid = A.start_b(cfg, log)
    out_json = os.path.join(A.ROOT, "bench_results", f"live_{tag}.json")
    game_log = os.path.join(A.ROOT, "bench_results", f"live_{tag}.game.log")
    agent_log = os.path.join(A.ROOT, "bench_results", f"live_{tag}.agent.log")
    gl = open(game_log, "w", encoding="utf-8", errors="replace")
    game = subprocess.Popen([GODOT, "--path", os.path.join(A.ROOT, "game"), "--always-on-top", "--", "--map=range", "--mode=remote", "--bench-sniper",
                             "--bench-wait-client", "--bench-criterion=hit", "--bench-distance-start=5", "--bench-distance-max=5", "--bench-heights=chest",
                             f"--bench-fixed-speed={speed}", f"--bench-passes={passes}", f"--bench-out={out_json}"], stdout=gl, stderr=subprocess.STDOUT)
    time.sleep(12)
    al = open(agent_log, "w", encoding="utf-8", errors="replace")
    agent = subprocess.Popen([sys.executable, os.path.join(A.ROOT, "remote", "poc", "agent_a.py"), "--connect", A.DATA_HOST + ":7777", "--transport", "tcp",
                              "--jpeg", "60", "--crop", "384", "--resize", "256"], stdout=al, stderr=subprocess.STDOUT)
    t0 = time.time()
    try:
        game.wait(timeout=60 + passes * 6)
    except subprocess.TimeoutExpired:
        pass
    wall = time.time() - t0
    time.sleep(2)
    agent.terminate()
    kill_local()
    gl.close()
    al.close()
    text = ssh_log = A.ssh(f"cat {log}")
    A.ssh(f"for p in $(pgrep -P {pid}); do kill $p; done; kill {pid}; sleep 0.5; pkill -f '[s]niper_b.py'")
    g = open(game_log, encoding="utf-8", errors="replace").read()
    m = re.search(r"→ (\d+)/(\d+) réussites", g)
    hits = int(m.group(1)) if m else None
    rows = A.parse_stats(text)
    return dict(config=name, speed=speed, passes=passes, hits=hits, wall=wall, rows=rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--speed", type=float, default=36.0)
    ap.add_argument("--passes", type=int, default=30)
    ap.add_argument("--configs", default="off")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    results = []
    for rep in range(a.repeat):
        for name in a.configs.split(","):
            r = run_live(name, a.speed, a.passes, f"{name}_v{int(a.speed)}_r{rep}_{time.strftime('%H%M%S')}")
            rows = [x for x in r["rows"] if x["fps"] > 50]
            inf = sum(x["inf"] for x in rows) / max(len(rows), 1)
            proc = sum(x["proc"] for x in rows) / max(len(rows), 1)
            print(f"{name:14s} vitesse {a.speed:g} m/s : {r['hits']}/{a.passes} touches ; B : inférence {inf:.3f} ms, réception→commande {proc:.3f} ms ({len(rows)} fenêtres)", flush=True)
            results.append(r)
    path = os.path.join(A.ROOT, "bench_results", f"live_{time.strftime('%Y%m%d_%H%M%S')}.json")
    json.dump(results, open(path, "w"), indent=1)
    print("->", path)


if __name__ == "__main__":
    main()
