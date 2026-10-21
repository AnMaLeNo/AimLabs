//! sniper-rs — port Rust de algos/yolo_sniper.py, pensé pour le plancher de latence :
//! crop zéro copie (SDK Rust) → ONNX Runtime (CUDA ou TensorRT) sur un carré 256×256 → décodage
//! YOLO11-seg (boîtes + coefficients de masque) → vitesse sur les bords libres → tir anticipé,
//! une balle, seulement si sûr. Chaque étape est chronométrée (`--stats`).
//!
//! Usage : sniper-rs [--model algos/yolo11n-seg_256x256.onnx] [--crop 0.333] [--band 0.5]
//!         [--conf 0.5] [--cooldown 1.0] [--lead-scale 1.0] [--ep cuda|tensorrt|cpu] [--debug]
//! CAPSLOCK on/off, ALT maintenu ; AIMLABS_AUTOSTART=1 → armé d'office (bench).
use aimlabs::{protocol, Bridge};
use ndarray::Array4;
use ort::session::{builder::GraphOptimizationLevel, Session};
use ort::value::Tensor;
use std::time::{Duration, Instant};

struct Args { model: String, crop: f32, band: f32, conf: f32, cooldown: f32, lead_scale: f32, ep: String, debug: bool, track: usize }

fn parse() -> Args {
    let mut a = Args { model: String::new(), crop: 0.3333, band: 0.5, conf: 0.5, cooldown: 1.0, lead_scale: 1.0, ep: "cuda".into(), debug: false, track: 2 };
    let v: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < v.len() {
        let next = |i: &mut usize| { *i += 1; v[*i].clone() };
        match v[i].as_str() {
            "--model" => a.model = next(&mut i),
            "--crop" => a.crop = next(&mut i).parse().unwrap(),
            "--band" => a.band = next(&mut i).parse().unwrap(),
            "--conf" => a.conf = next(&mut i).parse().unwrap(),
            "--cooldown" => a.cooldown = next(&mut i).parse().unwrap(),
            "--lead-scale" => a.lead_scale = next(&mut i).parse().unwrap(),
            "--track" => a.track = next(&mut i).parse().unwrap(),
            "--ep" => a.ep = next(&mut i),
            "--debug" => a.debug = true,
            x => { eprintln!("option inconnue {x}"); std::process::exit(2); }
        }
        i += 1;
    }
    if a.model.is_empty() {
        let here = std::env::current_exe().ok().and_then(|p| p.ancestors().find(|d| d.join("algos").is_dir()).map(|d| d.to_path_buf()));
        a.model = here.map(|d| d.join("algos").join("yolo11n-seg_256x256.onnx").to_string_lossy().into_owned())
            .unwrap_or_else(|| "algos/yolo11n-seg_256x256.onnx".into());
    }
    a
}

#[derive(Clone, Copy, Debug)]
struct Det { x1: f32, y1: f32, x2: f32, y2: f32, conf: f32, idx: usize }

/// Décodage de output0 (1, 4 + 80 + 32, N) : boîtes « person » (classe 0) ≥ conf, NMS maison.
fn decode(out: &[f32], n: usize, conf: f32) -> Vec<Det> {
    let mut dets: Vec<Det> = Vec::new();
    for i in 0..n {
        let c = out[4 * n + i]; // classe 0 = person
        if c < conf { continue; }
        let (cx, cy, w, h) = (out[i], out[n + i], out[2 * n + i], out[3 * n + i]);
        dets.push(Det { x1: cx - w / 2.0, y1: cy - h / 2.0, x2: cx + w / 2.0, y2: cy + h / 2.0, conf: c, idx: i });
    }
    dets.sort_by(|a, b| b.conf.partial_cmp(&a.conf).unwrap());
    let mut keep: Vec<Det> = Vec::new();
    'outer: for d in dets {
        for k in &keep {
            let ix = (d.x2.min(k.x2) - d.x1.max(k.x1)).max(0.0);
            let iy = (d.y2.min(k.y2) - d.y1.max(k.y1)).max(0.0);
            let inter = ix * iy;
            let uni = (d.x2 - d.x1) * (d.y2 - d.y1) + (k.x2 - k.x1) * (k.y2 - k.y1) - inter;
            if uni > 0.0 && inter / uni > 0.45 { continue 'outer; }
        }
        keep.push(d);
    }
    keep
}

/// Probabilités du masque de la détection `d` sur la ligne `y` (pixels du crop) → étendue ≥ 0,5.
fn mask_extent(out: &[f32], n: usize, protos: &[f32], mh: usize, mw: usize, d: &Det, y: f32, side: f32) -> Option<(f32, f32)> {
    let py = ((y * mh as f32 / side) as usize).min(mh - 1);
    let (mut lo, mut hi) = (usize::MAX, 0usize);
    for px in 0..mw {
        let mut logit = 0.0f32;
        for k in 0..32 {
            logit += out[(84 + k) * n + d.idx] * protos[k * mh * mw + py * mw + px];
        }
        if logit > 0.0 { // sigmoid > 0.5
            let x = px as f32 * side / mw as f32;
            if x < d.x1 - 2.0 || x > d.x2 + 2.0 { continue; }
            lo = lo.min(px);
            hi = hi.max(px);
        }
    }
    if lo == usize::MAX { None } else { Some((lo as f32 * side / mw as f32, (hi + 1) as f32 * side / mw as f32)) }
}

struct Track { hist: Vec<(f64, f32, f32, f32, f32)>, side: f32 }
impl Track {
    fn push(&mut self, t: f64, d: &Det) {
        self.hist.push((t, d.x1, d.x2, d.y1, d.y2));
        self.hist.retain(|p| t - p.0 <= 0.12);
    }
    fn fit(pts: &[(f64, f32)]) -> Option<(f32, f32)> {
        if pts.len() < 2 { return None; }
        let t_last = pts[pts.len() - 1].0;
        let n = pts.len() as f64;
        let (mut st, mut sv, mut stt, mut stv) = (0.0, 0.0, 0.0, 0.0);
        for &(t, v) in pts { let t = t - t_last; st += t; sv += v as f64; stt += t * t; stv += t * v as f64; }
        let den = n * stt - st * st;
        if den.abs() < 1e-9 { return None; }
        let a = (n * stv - st * sv) / den;
        let b = (sv - a * st) / n;
        let res = (pts.iter().map(|&(t, v)| { let e = a * (t - t_last) + b - v as f64; e * e }).sum::<f64>() / n).sqrt();
        Some((a as f32, res as f32))
    }
    /// (vx, vy, résidu) mesurés sur les bords non collés à la bordure du crop.
    fn velocity(&self) -> Option<(f32, f32, f32)> {
        let m = 2.0;
        let left: Vec<_> = self.hist.iter().filter(|p| p.1 > m).map(|p| (p.0, p.1)).collect();
        let right: Vec<_> = self.hist.iter().filter(|p| p.2 < self.side - m).map(|p| (p.0, p.2)).collect();
        let top: Vec<_> = self.hist.iter().filter(|p| p.3 > m).map(|p| (p.0, p.3)).collect();
        let bottom: Vec<_> = self.hist.iter().filter(|p| p.4 < self.side - m).map(|p| (p.0, p.4)).collect();
        let fx: Vec<_> = [Self::fit(&left), Self::fit(&right)].into_iter().flatten().collect();
        let fy: Vec<_> = [Self::fit(&top), Self::fit(&bottom)].into_iter().flatten().collect();
        let (vx, rx) = *fx.iter().min_by(|a, b| a.1.partial_cmp(&b.1).unwrap())?;
        let (vy, ry) = fy.iter().min_by(|a, b| a.1.partial_cmp(&b.1).unwrap()).copied().unwrap_or((0.0, 0.0));
        Some((vx, vy, rx.max(ry)))
    }
}

fn main() -> ort::Result<()> {
    let a = parse();
    let mut builder = Session::builder()?.with_optimization_level(GraphOptimizationLevel::Level3)?.with_intra_threads(2)?;
    let mut ep_name = "cpu";
    #[cfg(feature = "tensorrt")]
    if a.ep == "tensorrt" {
        use ort::execution_providers::{ExecutionProvider, TensorRTExecutionProvider};
        let ep = TensorRTExecutionProvider::default().with_fp16(true).with_engine_cache(true).with_engine_cache_path(std::env::temp_dir().join("aimlabs_trt").to_string_lossy());
        if ep.is_available()? { builder = builder.with_execution_providers([ep.build()])?; ep_name = "tensorrt"; }
    }
    #[cfg(feature = "cuda")]
    if a.ep == "cuda" || (a.ep == "tensorrt" && ep_name == "cpu") {
        use ort::execution_providers::{CUDAExecutionProvider, ExecutionProvider};
        let ep = CUDAExecutionProvider::default();
        if ep.is_available()? { builder = builder.with_execution_providers([ep.build()])?; ep_name = "cuda"; }
    }
    let mut session = builder.commit_from_file(&a.model)?;
    let in_side = 256usize;
    println!("sniper-rs : modèle {} · EP {ep_name} · crop {:.3}×h · bande {} · conf {}", a.model, a.crop, a.band, a.conf);

    let mut c = Bridge::attach("aimlabs", Duration::from_secs(20)).map_err(|e| ort::Error::new(e.to_string()))?;
    let mut enabled = std::env::var("AIMLABS_AUTOSTART").ok().as_deref() == Some("1");
    let (mut caps_last, mut locked, mut last_fire) = (false, false, Instant::now() - Duration::from_secs(10));
    let mut crop_buf: Vec<u8> = Vec::new();
    let mut input = Array4::<f32>::zeros((1, 3, in_side, in_side));
    let mut track: Option<Track> = None;
    let (mut n, mut t_crop, mut t_inf, mut t_post, mut n_fire, mut n_pre) = (0u64, 0.0f64, 0.0f64, 0.0f64, 0u64, 0u64);
    let mut t_stats = Instant::now();
    // warm-up
    { let t = Tensor::from_array(input.clone())?; let _ = session.run(ort::inputs![t])?; }
    println!("sniper-rs connecté. CAPSLOCK: on/off, ALT maintenu: actif");
    loop {
        let (frame_id, capture_usec, side_px, t0) = {
            let Some(f) = c.wait_frame(Duration::from_secs(2)) else {
                if !c.game_alive() { println!("jeu fermé"); return Ok(()); }
                continue;
            };
            let t0 = Instant::now();
            let side = ((f.height() as f32 * a.crop).round() as u32).max(32).min(f.height()).min(f.width());
            let (x0, y0, side) = f.centre_square(side);
            f.crop_into(x0, y0, side, side, 3, &mut crop_buf);
            (f.frame_id(), f.capture_usec(), side, t0)
        };
        let st = c.state();
        let caps = Bridge::key_pressed(&st, protocol::SLOT_CAPSLOCK);
        if caps && !caps_last { enabled = !enabled; println!("sniper {}", if enabled { "ON" } else { "OFF" }); }
        caps_last = caps;
        let active = enabled || Bridge::key_pressed(&st, protocol::SLOT_ALT);
        if t_stats.elapsed().as_secs_f64() > 5.0 {
            if active && n > 0 {
                println!("[stats] {:.0} frames/s · crop {:.2} ms · inférence {:.2} ms · post {:.2} ms · tirs {n_fire} (anticipés {n_pre})",
                    n as f64 / t_stats.elapsed().as_secs_f64(), t_crop / n as f64 * 1e3, t_inf / n as f64 * 1e3, t_post / n as f64 * 1e3);
            }
            n = 0; t_crop = 0.0; t_inf = 0.0; t_post = 0.0; n_fire = 0; n_pre = 0; t_stats = Instant::now();
        }
        if !active { track = None; continue; }
        // crop (side_px) → 256×256 NCHW f32 (plus proche voisin si side ≠ 256 ; égal à 240→256 : léger agrandissement)
        let sp = side_px as usize;
        for y in 0..in_side {
            let sy = y * sp / in_side;
            for x in 0..in_side {
                let sx = x * sp / in_side;
                let o = (sy * sp + sx) * 3;
                input[[0, 0, y, x]] = crop_buf[o] as f32 / 255.0;
                input[[0, 1, y, x]] = crop_buf[o + 1] as f32 / 255.0;
                input[[0, 2, y, x]] = crop_buf[o + 2] as f32 / 255.0;
            }
        }
        let t1 = Instant::now();
        let outputs = session.run(ort::inputs![Tensor::from_array(input.clone())?])?;
        let t2 = Instant::now();
        let (shape0, out0) = outputs[0].try_extract_tensor::<f32>()?;
        let (shape1, protos) = outputs[1].try_extract_tensor::<f32>()?;
        let nb = shape0[2] as usize;
        let (mh, mw) = (shape1[2] as usize, shape1[3] as usize);
        let scale = sp as f32 / in_side as f32; // sortie réseau → pixels du crop
        let mut dets = decode(out0, nb, a.conf.min(0.25));
        for d in &mut dets { d.x1 *= scale; d.x2 *= scale; d.y1 *= scale; d.y2 *= scale; }
        let side = sp as f32;
        let (cx, cy) = (side / 2.0, side / 2.0);
        n += 1; t_crop += (t1 - t0).as_secs_f64(); t_inf += (t2 - t1).as_secs_f64();
        let Some(b) = dets.iter().min_by(|p, q| {
            let dp = ((p.x1 + p.x2) / 2.0 - cx).abs() + ((p.y1 + p.y2) / 2.0 - cy).abs();
            let dq = ((q.x1 + q.x2) / 2.0 - cx).abs() + ((q.y1 + q.y2) / 2.0 - cy).abs();
            dp.partial_cmp(&dq).unwrap()
        }).copied() else { track = None; locked = false; t_post += t2.elapsed().as_secs_f64(); continue; };
        let tr = track.get_or_insert_with(|| Track { hist: Vec::new(), side });
        tr.push(capture_usec as f64 / 1e6, &b);
        // marque : l'image montre la cible sous le réticule (masque)
        let ext_now = mask_extent(out0, nb, protos, mh, mw, &Det { x1: b.x1 / scale, y1: b.y1 / scale, x2: b.x2 / scale, y2: b.y2 / scale, ..b }, cy / scale, in_side as f32)
            .map(|(l, r)| (l * scale, r * scale));
        let under_now = b.x1 <= cx && cx <= b.x2 && b.y1 <= cy && cy <= b.y2 && ext_now.map_or(false, |(l, r)| l <= cx && cx <= r);
        if under_now { c.mark(frame_id, 0.0, 0.0); } else { locked = false; }
        if !(locked || last_fire.elapsed().as_secs_f32() < a.cooldown) {
            let latency = st.time_usec.saturating_sub(capture_usec) as f32 / 1e6 + 1.0 / st.fps.max(1.0);
            let (mut dx, mut dy) = (0.0f32, 0.0f32);
            if a.lead_scale > 0.0 {
                if let Some((vx, vy, res)) = tr.velocity() {
                    if res < 0.3 * (b.x2 - b.x1) + 2.0 { dx = vx * latency * a.lead_scale; dy = vy * latency * a.lead_scale; }
                }
            }
            let (px1, py1, px2, py2) = (b.x1 + dx, b.y1 + dy, b.x2 + dx, b.y2 + dy);
            let in_band = (cx - (px1 + px2) / 2.0).abs() <= (px2 - px1) / 2.0 * a.band && (cy - (py1 + py2) / 2.0).abs() <= (py2 - py1) / 2.0 * 0.9;
            let mut sure = in_band && b.conf >= a.conf && tr.hist.len() >= a.track;
            if sure {
                let ext = mask_extent(out0, nb, protos, mh, mw, &Det { x1: b.x1 / scale, y1: b.y1 / scale, x2: b.x2 / scale, y2: b.y2 / scale, ..b }, (cy - dy) / scale, in_side as f32);
                sure = match ext {
                    Some((l, r)) => { let (l, r) = (l * scale, r * scale); ((cx - dx) - (l + r) / 2.0).abs() <= ((r - l) / 2.0 * a.band).max(1.0) }
                    None => false,
                };
            }
            if sure && c.fire(frame_id) {
                n_fire += 1; if !under_now { n_pre += 1; }
                last_fire = Instant::now(); locked = true;
                if a.debug {
                    println!("[tir] frame {frame_id} {}conf {:.2} avance ({dx:+.0},{dy:+.0}) px latence {:.1} ms inf {:.2} ms",
                        if under_now { "" } else { "anticipé " }, b.conf, latency * 1e3, (t2 - t1).as_secs_f64() * 1e3);
                }
            }
        }
        t_post += t2.elapsed().as_secs_f64();
    }
}
