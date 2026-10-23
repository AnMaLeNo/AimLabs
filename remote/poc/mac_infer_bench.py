"""Machine B (Mac) : mesure de l'inférence YOLO11n-seg 256×256 selon le moteur.

    python mac_infer_bench.py --pt algos/yolo11n-seg.pt [--n 300] [--sustain 0]

 * exporte (une fois) le modèle en Core ML FP16 ;
 * Core ML selon l'unité de calcul : ALL / CPU_AND_NE (Neural Engine) / CPU_AND_GPU / CPU_ONLY ;
 * PyTorch MPS (GPU Metal) en FP16 et FP32 ;
 * --sustain S : répète la meilleure configuration pendant S secondes et affiche la latence par tranche de 15 s
   (un MacBook Air n'a pas de ventilateur : il faut voir si la latence dérive).
"""
import argparse
import os
import sys
import time

import numpy as np


def stats(name, ts):
    t = np.array(ts) * 1e3
    print(f"{name:34} moy {t.mean():6.2f} ms · p50 {np.median(t):6.2f} · p90 {np.percentile(t, 90):6.2f} · p99 {np.percentile(t, 99):6.2f} · min {t.min():6.2f}", flush=True)
    return float(np.median(t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", default="algos/yolo11n-seg.pt")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--sustain", type=float, default=0.0)
    ap.add_argument("--imgsz", type=int, default=256)
    a = ap.parse_args()

    import coremltools as ct
    from PIL import Image

    pkg = os.path.splitext(a.pt)[0] + f"_{a.imgsz}.mlpackage"
    if not os.path.exists(pkg):
        from ultralytics import YOLO
        out = YOLO(a.pt).export(format="coreml", imgsz=a.imgsz, half=True, nms=False)
        os.replace(out, pkg)
    m0 = ct.models.MLModel(pkg)
    spec = m0.get_spec()
    print("entrées :", [(i.name, i.type.WhichOneof("Type")) for i in spec.description.input])
    print("sorties :", [(o.name, list(o.type.multiArrayType.shape)) for o in spec.description.output])
    in_name = spec.description.input[0].name
    is_image = spec.description.input[0].type.WhichOneof("Type") == "imageType"
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, (a.imgsz, a.imgsz, 3), dtype=np.uint8)
    feed_img = {in_name: Image.fromarray(arr)}
    feed_arr = {in_name: (arr.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]}
    feed = feed_img if is_image else feed_arr

    results = {}
    for cu_name in ("ALL", "CPU_AND_NE", "CPU_AND_GPU", "CPU_ONLY"):
        cu = getattr(ct.ComputeUnit, cu_name)
        t0 = time.perf_counter()
        m = ct.models.MLModel(pkg, compute_units=cu)
        load = time.perf_counter() - t0
        for _ in range(30):
            m.predict(feed)
        ts = []
        for _ in range(a.n):
            t = time.perf_counter()
            m.predict(feed)
            ts.append(time.perf_counter() - t)
        results[cu_name] = stats(f"CoreML {cu_name} (charge {load:.1f}s)", ts)

    try:
        import torch
        from ultralytics import YOLO
        net = YOLO(a.pt).model.eval()
        for dt_name, dt in (("fp16", torch.float16), ("fp32", torch.float32)):
            d = net.to("mps").to(dt)
            x = torch.rand(1, 3, a.imgsz, a.imgsz, device="mps", dtype=dt)
            with torch.no_grad():
                for _ in range(30):
                    d(x)
                torch.mps.synchronize()
                ts = []
                for _ in range(a.n):
                    t = time.perf_counter()
                    d(x)
                    torch.mps.synchronize()
                    ts.append(time.perf_counter() - t)
            results[f"MPS_{dt_name}"] = stats(f"PyTorch MPS {dt_name}", ts)
    except Exception as e:  # noqa: BLE001
        print("MPS indisponible :", e)

    if a.sustain > 0:
        best = min((k for k in results if k in ("ALL", "CPU_AND_NE", "CPU_AND_GPU", "CPU_ONLY")), key=results.get)
        print(f"\nlongue durée sur CoreML {best} ({a.sustain:.0f} s)", flush=True)
        m = ct.models.MLModel(pkg, compute_units=getattr(ct.ComputeUnit, best))
        t_end, t_slice, ts = time.perf_counter() + a.sustain, time.perf_counter(), []
        while time.perf_counter() < t_end:
            t = time.perf_counter()
            m.predict(feed)
            ts.append(time.perf_counter() - t)
            if time.perf_counter() - t_slice >= 15:
                stats(f"  +{time.perf_counter() - (t_end - a.sustain):4.0f} s", ts)
                ts, t_slice = [], time.perf_counter()


if __name__ == "__main__":
    sys.exit(main())
