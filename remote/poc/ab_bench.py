"""Banc A/B de B (Mac) : mêmes images rejouées (replay_agent.py), plusieurs configurations de sniper_b entrelacées sur plusieurs
tours (la dérive thermique ou le bruit de la machine ne favorise donc aucune), statistiques de B + CPU consommé par chaque processus.

    python ab_bench.py remote/recordings/live_36ms_30pass.bin --rounds 3 --seconds 45 --configs old,off,view-headless
    python ab_bench.py REC --configs view-gui                       # fenêtre réelle : lancée via Terminal (session graphique)

Configurations : `old` (sniper_b du dernier commit, dans ~/aimlabs-remote/old sur le Mac : `git archive HEAD remote/poc sdk/python`),
`off` (code courant, sans --view), `view-headless` (processus de vue qui rend sans fenêtre), `view-gui` (fenêtre réelle),
`view-gui60` (idem à 60 images/s). Sortie : tableau + JSON dans bench_results/.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time

MAC = os.environ.get("MAC", "antoine@192.168.1.7")
KEY = os.environ.get("MAC_KEY", os.path.expanduser("~/.ssh/id_ed25519_claude"))
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
VENV = "~/aimlabs-remote/venv/bin/python"
MODEL = "~/aimlabs-remote/repo/algos/yolo11n-seg_256.mlpackage"
CONFIGS = {
    "old": dict(dir="~/aimlabs-remote/old", args=""),
    "off": dict(dir="~/aimlabs-remote/repo", args=""),
    "view-headless": dict(dir="~/aimlabs-remote/repo", args="--view --view-headless --view-linger 0"),
    "view-gui": dict(dir="~/aimlabs-remote/repo", args="--view --view-linger 0", gui=True),
    "view-gui60": dict(dir="~/aimlabs-remote/repo", args="--view --view-fps 60 --view-linger 0", gui=True),
    "view-gui15": dict(dir="~/aimlabs-remote/repo", args="--view --view-fps 15 --view-linger 0", gui=True),
    "view-gui-s2": dict(dir="~/aimlabs-remote/repo", args="--view --view-scale 2 --view-linger 0", gui=True),
}


def ssh(cmd, timeout=60):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-i", KEY, MAC, cmd], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return r.stdout


def cpu_seconds(pid):
    """Temps CPU cumulé d'un processus du Mac (ps cputime, résolution 10 ms)."""
    out = ssh(f"ps -o cputime= -p {pid}").strip()
    if not out:
        return None
    parts = out.replace(",", ".").split(":")
    t = 0.0
    for p_ in parts:
        t = t * 60 + float(p_)
    return t


def children(pid):
    return [int(x) for x in ssh(f"pgrep -P {pid}").split()]


def start_b(cfg, log):
    args = cfg["args"]
    script = f"{cfg['dir']}/remote/poc/sniper_b.py"
    cmd = (f"cd {cfg['dir']} && exec env AIMLABS_AUTOSTART=1 {VENV} remote/poc/sniper_b.py --connect listen:7777 --model {MODEL} --stats 5 {args} > {log} 2>&1 < /dev/null")
    ssh("pkill -f '[s]niper_b.py'; sleep 1")
    if cfg.get("gui"):
        ssh(f"printf '#!/bin/bash\\n{cmd}\\n' > ~/aimlabs-remote/ab.command; chmod +x ~/aimlabs-remote/ab.command; rm -f {log}; open -g -a Terminal ~/aimlabs-remote/ab.command")
    else:
        ssh(f"rm -f {log}; (nohup bash -c '{cmd}' > /dev/null 2>&1 &)")
    for _ in range(60):
        if "en attente de l'agent" in ssh(f"cat {log} 2>/dev/null"):
            break
        time.sleep(0.5)
    time.sleep(1.0 if not cfg.get("gui") else 2.5)
    pid = int(ssh("pgrep -f '[r]emote/poc/sniper_b.py' | head -1").split()[0])
    return pid


def parse_stats(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"\[stats\] (\d+) images/s · inférence ([\d.]+) ms · post ([\d.]+) ms · réception→commande ([\d.]+) ms(?: \(p99 ([\d.]+), max ([\d.]+)\))? · tirs (\d+) \(programmés (\d+)\)(?: · vue (\d+) µs)?", line)
        if m:
            g = m.groups()
            rows.append(dict(fps=float(g[0]), inf=float(g[1]), post=float(g[2]), proc=float(g[3]), p99=float(g[4]) if g[4] else None,
                             mx=float(g[5]) if g[5] else None, fires=int(g[6]), sched=int(g[7]), view_us=float(g[8]) if g[8] else 0.0))
    return rows


def run_one(name, cfg, rec, seconds, tag):
    log = "~/aimlabs-remote/ab_b.log"
    pid = start_b(cfg, log)
    kids = children(pid)
    replay_log = os.path.join(ROOT, "bench_results", f"ab_replay_{tag}.log")
    os.makedirs(os.path.dirname(replay_log), exist_ok=True)
    c0 = {p: cpu_seconds(p) for p in [pid] + kids}
    t0 = time.time()
    f = open(replay_log, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "remote", "poc", "replay_agent.py"), rec, "--connect", MAC.split("@")[1] + ":7777",
                             "--seconds", str(seconds)], stdout=f, stderr=subprocess.STDOUT)
    time.sleep(max(seconds - 3.0, 1.0))                    # CPU relevé avant la fin du rejeu : ensuite B quitte (« jeu fermé ») et ses fils aussi
    c1 = {p: cpu_seconds(p) for p in [pid] + kids}
    wall = time.time() - t0
    proc.wait(timeout=60)
    f.close()
    time.sleep(1.0)
    text = ssh(f"cat {log}")
    view_pid = None
    m = re.search(r"processus (\d+)", text)
    if m:
        view_pid = int(m.group(1))
    cpu = {}
    for p in c0:
        if c0[p] is not None and c1[p] is not None:
            role = "B" if p == pid else ("vue" if p == view_pid else "chauffeur")
            cpu[role] = max(cpu.get(role, 0.0), (c1[p] - c0[p]) / wall * 100)   # « chauffeur » : le resource_tracker (~0 %) ne masque pas le processus qui tourne
    ssh(f"for p in $(pgrep -P {pid}); do kill $p; done; kill {pid}; sleep 0.5; pkill -f '[s]niper_b.py'")
    rows = parse_stats(text)[1:]                      # la première fenêtre contient le démarrage à froid
    rtt = [float(x) for x in re.findall(r"p99 ([\d.]+)", open(replay_log, encoding="utf-8").read())]
    return dict(config=name, rows=rows, cpu=cpu, wall=wall, rtt_p99=rtt)


def summarize(results):
    print()
    print(f"{'config':15s} {'n':>3s} {'inférence':>10s} {'récep→cmd':>10s} {'p99':>6s} {'max':>6s} {'vue µs':>7s} {'tirs':>5s} {'prog':>5s}   CPU Mac (% d'un cœur) : B / vue / chauffeur")
    out = {}
    for name in dict.fromkeys(r["config"] for r in results):
        rs = [r for r in results if r["config"] == name]
        rows = [x for r in rs for x in r["rows"]]
        if not rows:
            continue
        p99s = [x["p99"] for x in rows if x["p99"] is not None]
        mxs = [x["mx"] for x in rows if x["mx"] is not None]
        cpu = {k: statistics.mean(r["cpu"].get(k, 0.0) for r in rs) for k in ("B", "vue", "chauffeur")}
        s = dict(n=len(rows), inf=statistics.mean(x["inf"] for x in rows), proc=statistics.mean(x["proc"] for x in rows),
                 p99=statistics.mean(p99s) if p99s else None, mx=statistics.median(mxs) if mxs else None,
                 view_us=statistics.mean(x["view_us"] for x in rows), fires=sum(x["fires"] for x in rows) / len(rs),
                 sched=sum(x["sched"] for x in rows) / len(rs), cpu=cpu)
        out[name] = s
        print(f"{name:15s} {s['n']:3d} {s['inf']:8.3f}ms {s['proc']:8.3f}ms {s['p99'] if s['p99'] else float('nan'):6.2f} {s['mx'] if s['mx'] else float('nan'):6.2f} "
              f"{s['view_us']:7.1f} {s['fires']:5.1f} {s['sched']:5.1f}   {cpu['B']:5.1f} / {cpu['vue']:5.1f} / {cpu['chauffeur']:5.1f}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rec")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--configs", default="old,off,view-headless")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    names = a.configs.split(",")
    results = []
    for rnd in range(a.rounds):
        for name in names:
            tag = f"{name}_r{rnd}"
            print(f"[{time.strftime('%H:%M:%S')}] tour {rnd + 1}/{a.rounds} : {name}", flush=True)
            r = run_one(name, CONFIGS[name], a.rec, a.seconds, tag)
            results.append(r)
            last = r["rows"][-1] if r["rows"] else {}
            print(f"    {len(r['rows'])} fenêtres ; dernière : {last}", flush=True)
    out = summarize(results)
    path = a.out or os.path.join(ROOT, "bench_results", f"ab_{time.strftime('%Y%m%d_%H%M%S')}.json")
    json.dump(dict(configs=names, rounds=a.rounds, seconds=a.seconds, summary=out, runs=results), open(path, "w"), indent=1)
    print("->", path)


if __name__ == "__main__":
    main()
