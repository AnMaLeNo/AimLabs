"""Sur QUELS cœurs tourne chaque processus du Mac ? Échantillonne `sudo powermetrics --samplers cpu_power` (autorisé sans mot de passe pour
cette seule commande) pendant un flux rejoué, pour plusieurs configurations de B, et résume par cœur (E = efficacité, S = « Super »).

    python pm_check.py remote/recordings/live_36ms_30pass.bin --configs off,view-gui,view-gui-def
"""
import argparse
import os
import re
import statistics as st
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_bench as A  # noqa: E402


def sample(seconds):
    """Lance powermetrics sur le Mac pendant `seconds` s (un échantillon par seconde) et rend les mesures par échantillon."""
    out = A.ssh(f"sudo -n /usr/bin/powermetrics --samplers cpu_power -i 1000 -n {seconds}", timeout=seconds + 60)
    blocks = out.split("*** Sampled system activity")[1:]
    rows = []
    for b in blocks:
        r = {"res": {}, "freq": {}}
        for m in re.finditer(r"CPU (\d+) active residency:\s+([\d.]+)%", b):
            r["res"][int(m.group(1))] = float(m.group(2))
        for m in re.finditer(r"CPU (\d+) frequency: (\d+) MHz", b):
            r["freq"][int(m.group(1))] = float(m.group(2))
        for key, pat in (("cpu_mw", r"CPU Power: (\d+) mW"), ("gpu_mw", r"GPU Power: (\d+) mW"), ("ane_mw", r"ANE Power: (\d+) mW")):
            m = re.search(pat, b)
            r[key] = float(m.group(1)) if m else 0.0
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rec")
    ap.add_argument("--configs", default="off,view-gui,view-gui-def")
    ap.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    print(f"{'config':14s} " + " ".join(f"E{i}" .rjust(5) for i in range(6)) + "  |  " + " ".join(f"S{i}".rjust(5) for i in range(4)) + "   CPU mW  GPU mW  ANE mW")
    for name in a.configs.split(","):
        pid = A.start_b(dict(A.CONFIGS[name]), "~/aimlabs-remote/pm_b.log")
        replay = subprocess.Popen([sys.executable, os.path.join(A.ROOT, "remote", "poc", "replay_agent.py"), a.rec, "--connect", A.DATA_HOST + ":7777",
                                   "--seconds", str(a.seconds + 12)], stdout=subprocess.DEVNULL)
        time.sleep(6)
        rows = sample(a.seconds)[2:]
        replay.wait(timeout=90)
        A.ssh(f"for p in $(pgrep -P {pid}); do kill $p; done; kill {pid}; sleep 0.5; pkill -f '[s]niper_b.py'")
        res = [st.mean(r["res"].get(i, 0.0) for r in rows) for i in range(10)]
        print(f"{name:14s} " + " ".join(f"{x:5.1f}" for x in res[:6]) + "  |  " + " ".join(f"{x:5.1f}" for x in res[6:]) +
              f"   {st.mean(r['cpu_mw'] for r in rows):6.0f}  {st.mean(r['gpu_mw'] for r in rows):6.0f}  {st.mean(r['ane_mw'] for r in rows):6.0f}", flush=True)


if __name__ == "__main__":
    main()
