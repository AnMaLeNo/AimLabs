//! sniper-rs — port Rust de algos/yolo_sniper.py, pensé pour le plancher de latence :
//! crop zéro copie (SDK Rust) → ONNX Runtime (CUDA ou TensorRT) sur un carré 256×256 → décodage
//! YOLO11-seg (boîtes + coefficients de masque) → vitesse sur les bords libres → tir anticipé,
//! une balle, seulement si sûr. Chaque étape est chronométrée (`--stats`).
//!
//! Usage : sniper-rs [--model algos/yolo11n-seg_256x256.onnx] [--crop 0.333] [--band 0.5]
//!         [--conf 0.5] [--cooldown 1.0] [--lead-scale 1.0] [--ep tensorrt|cuda|cpu] [--no-graph]
//!         [--libs <dossier[;dossier…]>] [--latency-ms 0] [--debug]
//! CAPSLOCK on/off, ALT maintenu ; AIMLABS_AUTOSTART=1 → armé d'office (bench).
//!
//! GPU : entrée et sorties sont des tampons fixes liés à la session (IoBinding), alimentés par des
//! copies DMA depuis de la mémoire épinglée ; avec le CUDA graph (défaut) les ~350 kernels du réseau
//! sont rejoués en un seul lancement. Un EP demandé qui ne se charge pas est une erreur (pas de repli
//! silencieux sur le CPU).
mod cuda;
use aimlabs::{protocol, Bridge};
use cuda::{Cuda, Pinned};
use ort::io_binding::IoBinding;
use ort::memory::{AllocationDevice, Allocator, AllocatorType, MemoryInfo, MemoryType};
use ort::session::{builder::GraphOptimizationLevel, Session};
use ort::value::Tensor;
use std::ffi::c_void;
use std::time::{Duration, Instant};

struct Args { latency_ms: f32, model: String, crop: f32, band: f32, conf: f32, cooldown: f32, lead_scale: f32, ep: String, graph: bool, libs: String, debug: bool, track: usize }

fn parse() -> Args {
    let mut a = Args { latency_ms: 0.0, model: String::new(), crop: 0.3333, band: 0.5, conf: 0.5, cooldown: 1.0, lead_scale: 1.0, ep: "tensorrt".into(), graph: true, libs: String::new(), debug: false, track: 2 };
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
            "--latency-ms" => a.latency_ms = next(&mut i).parse().unwrap(),
            "--ep" => a.ep = next(&mut i),
            "--libs" => a.libs = next(&mut i),
            "--no-graph" => a.graph = false,
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

/// NMS sur des candidats déjà filtrés par confiance.
fn nms(mut dets: Vec<Det>) -> Vec<Det> {
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

fn det_of(cx: f32, cy: f32, w: f32, h: f32, conf: f32, idx: usize) -> Det {
    Det { x1: cx - w / 2.0, y1: cy - h / 2.0, x2: cx + w / 2.0, y2: cy + h / 2.0, conf, idx }
}

/// Sorties du réseau, sous l'une des deux formes que le modèle peut produire.
enum View<'a> {
    /// output0 (1, 4 + 80 + 32, N) + protos (1, 32, mh, mw), tels que YOLO11-seg les sort.
    Full { out0: &'a [f32], protos: &'a [f32], n: usize, mh: usize, mw: usize },
    /// Après décodage sur le GPU (tools/make_gpu_model.py --decode) : `det` (5, K) = cx,cy,w,h,conf des K
    /// meilleures « person » ; `logits` (K, rows*mw) = masque de ces K détections sur `rows` lignes de
    /// protos à partir de `row0` (centrées sur le réticule).
    Slim { det: &'a [f32], logits: &'a [f32], k: usize, row0: usize, rows: usize, mw: usize, mh: usize },
}

impl View<'_> {
    /// Boîtes « person » ≥ conf après NMS (coordonnées du réseau).
    fn decode(&self, conf: f32) -> Vec<Det> {
        let mut dets: Vec<Det> = Vec::new();
        match self {
            View::Full { out0, n, .. } => for i in 0..*n {
                let c = out0[4 * n + i]; // classe 0 = person
                if c >= conf { dets.push(det_of(out0[i], out0[n + i], out0[2 * n + i], out0[3 * n + i], c, i)); }
            },
            View::Slim { det, k, .. } => for i in 0..*k {
                let c = det[4 * k + i];
                if c >= conf { dets.push(det_of(det[i], det[k + i], det[2 * k + i], det[3 * k + i], c, i)); }
            },
        }
        nms(dets)
    }

    /// Probabilités du masque de la détection `d` sur la ligne `y` (pixels du réseau) → étendue ≥ 0,5.
    fn mask_extent(&self, d: &Det, y: f32, side: f32) -> Option<(f32, f32)> {
        let (mh, mw) = match self { View::Full { mh, mw, .. } | View::Slim { mh, mw, .. } => (*mh, *mw) };
        let py = ((y * mh as f32 / side) as usize).min(mh - 1);
        let (mut lo, mut hi) = (usize::MAX, 0usize);
        for px in 0..mw {
            let logit = match self {
                View::Full { out0, protos, n, .. } => {
                    let mut l = 0.0f32;
                    for k in 0..32 { l += out0[(84 + k) * n + d.idx] * protos[k * mh * mw + py * mw + px]; }
                    l
                }
                // ligne hors de la bande transférée (décalage vertical > ±rows/2 protos) : bord de la bande
                View::Slim { logits, row0, rows, .. } => logits[(d.idx * rows + py.clamp(*row0, row0 + rows - 1) - row0) * mw + px],
            };
            if logit > 0.0 { // sigmoid > 0.5
                let x = px as f32 * side / mw as f32;
                if x < d.x1 - 2.0 || x > d.x2 + 2.0 { continue; }
                lo = lo.min(px);
                hi = hi.max(px);
            }
        }
        if lo == usize::MAX { None } else { Some((lo as f32 * side / mw as f32, (hi + 1) as f32 * side / mw as f32)) }
    }
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


/// Forme du modèle, déduite de ses entrées / sorties (voir tools/make_gpu_model.py).
#[derive(Clone, Copy)]
struct Shape { u8_input: bool, slim: bool }

fn model_shape(session: &Session) -> Shape {
    Shape {
        u8_input: session.inputs[0].input_type.tensor_type() == Some(ort::tensor::TensorElementType::Uint8),
        slim: session.outputs[0].name == "det",
    }
}

/// Inférence sur le GPU : tampons fixes liés à la session (exigence du CUDA graph).
struct Gpu {
    cuda: Cuda, io: IoBinding, p_in: *mut c_void, p_out: [*const c_void; 2],
    h_in: Pinned, h_out: [Pinned; 2], dims: [Vec<usize>; 2],
    _keep: (Allocator, ort::value::DynValue),
}

fn setup_gpu(session: &Session, in_side: usize, shape: Shape) -> Result<Gpu, String> {
    let e = |x: ort::Error| x.to_string();
    let cuda = Cuda::load()?;
    let dev = MemoryInfo::new(AllocationDevice::CUDA, 0, AllocatorType::Device, MemoryType::Default).map_err(e)?;
    let alloc = Allocator::new(session, dev).map_err(e)?;
    let dims = |i: usize| -> Vec<usize> { session.outputs[i].output_type.tensor_shape().map(|s| s.iter().map(|&d| d as usize).collect()).unwrap_or_default() };
    let (d0, d1) = (dims(0), dims(1));
    let ok = if shape.slim { d0.len() == 3 && d1.len() == 2 } else { d0.len() == 3 && d1.len() == 4 };
    if !ok { return Err(format!("sorties inattendues {d0:?} {d1:?} (modèle YOLO11-seg 256×256, brut ou décodé, attendu)")); }
    let (in_bytes, p_in, t_in): (usize, *mut c_void, ort::value::DynValue) = if shape.u8_input {
        let mut t = Tensor::<u8>::new(&alloc, [1usize, in_side, in_side, 3]).map_err(e)?;
        (3 * in_side * in_side, t.data_ptr_mut(), t.into_dyn())
    } else {
        let mut t = Tensor::<f32>::new(&alloc, [1usize, 3, in_side, in_side]).map_err(e)?;
        (4 * 3 * in_side * in_side, t.data_ptr_mut(), t.into_dyn())
    };
    let mut t_o0 = Tensor::<f32>::new(&alloc, d0.clone()).map_err(e)?;
    let mut t_o1 = Tensor::<f32>::new(&alloc, d1.clone()).map_err(e)?;
    let (p_o0, p_o1) = (t_o0.data_ptr_mut() as *const c_void, t_o1.data_ptr_mut() as *const c_void);
    let mut io = session.create_binding().map_err(e)?;
    io.bind_input(session.inputs[0].name.clone(), &t_in).map_err(e)?;
    io.bind_output(session.outputs[0].name.clone(), t_o0).map_err(e)?;
    io.bind_output(session.outputs[1].name.clone(), t_o1).map_err(e)?;
    Ok(Gpu {
        h_in: cuda.pinned(in_bytes)?,
        h_out: [cuda.pinned(4 * d0.iter().product::<usize>())?, cuda.pinned(4 * d1.iter().product::<usize>())?],
        dims: [d0, d1], cuda, io, p_in, p_out: [p_o0, p_o1], _keep: (alloc, t_in),
    })
}

/// Une inférence. GPU : copie H2D → `run_binding` (CUDA graph rejoué) → copies D2H. CPU : `run` classique
/// (modèle brut, entrée f32). Renvoie les durées [envoi, exécution, récupération] en secondes ; les sorties
/// sont dans `g.h_out` (GPU) ou `cpu_o` (CPU).
fn infer(session: &mut Session, gpu: &mut Option<Gpu>, cpu_in: &[f32], cpu_o: &mut (Vec<f32>, Vec<f32>), in_side: usize) -> Result<[f64; 3], String> {
    let e = |x: ort::Error| x.to_string();
    if let Some(g) = gpu.as_mut() {
        let t0 = Instant::now();
        g.cuda.upload(g.p_in, &g.h_in)?;
        let t1 = Instant::now();
        drop(session.run_binding(&g.io).map_err(e)?);
        let t2 = Instant::now();
        for i in 0..2 { g.cuda.download(&mut g.h_out[i], g.p_out[i])?; }
        Ok([(t1 - t0).as_secs_f64(), (t2 - t1).as_secs_f64(), t2.elapsed().as_secs_f64()])
    } else {
        let t0 = Instant::now();
        let input = Tensor::from_array(([1usize, 3, in_side, in_side], cpu_in.to_vec().into_boxed_slice())).map_err(e)?;
        let outputs = session.run(ort::inputs![input]).map_err(e)?;
        let t1 = Instant::now();
        let (_, o0) = outputs[0].try_extract_tensor::<f32>().map_err(e)?;
        let (_, o1) = outputs[1].try_extract_tensor::<f32>().map_err(e)?;
        cpu_o.0.clear(); cpu_o.0.extend_from_slice(o0);
        cpu_o.1.clear(); cpu_o.1.extend_from_slice(o1);
        Ok([0.0, (t1 - t0).as_secs_f64(), t1.elapsed().as_secs_f64()])
    }
}

fn main() {
    if let Err(e) = run() { eprintln!("sniper-rs : {e}"); std::process::exit(1); }
}

fn run() -> Result<(), String> {
    let a = parse();
    let e = |x: ort::Error| x.to_string();
    if !a.libs.is_empty() {
        // DLL CUDA / cuDNN / TensorRT : à mettre dans le PATH avant qu'ONNX Runtime ne charge ses fournisseurs
        let sep = if cfg!(windows) { ";" } else { ":" };
        std::env::set_var("PATH", format!("{}{sep}{}", a.libs, std::env::var("PATH").unwrap_or_default()));
    }
    let mut builder = Session::builder().map_err(e)?.with_optimization_level(GraphOptimizationLevel::Level3).map_err(e)?.with_intra_threads(1).map_err(e)?;
    match a.ep.as_str() {
        "cpu" => {}
        #[cfg(feature = "tensorrt")]
        "tensorrt" => {
            use ort::execution_providers::TensorRTExecutionProvider;
            let cache = std::env::temp_dir().join("aimlabs_trt");
            if !cache.exists() { println!("TensorRT : premier lancement, construction du moteur FP16 (~5 min, mis en cache dans {})", cache.display()); }
            let ep = TensorRTExecutionProvider::default().with_fp16(true).with_engine_cache(true).with_engine_cache_path(cache.to_string_lossy())
                .with_timing_cache(true).with_timing_cache_path(cache.to_string_lossy()).with_cuda_graph(a.graph);
            builder = builder.with_execution_providers([ep.build().error_on_failure()]).map_err(|x| format!("EP TensorRT indisponible : {x}"))?;
        }
        #[cfg(feature = "cuda")]
        "cuda" => {
            use ort::execution_providers::{cuda::CuDNNConvAlgorithmSearch, CUDAExecutionProvider};
            let ep = CUDAExecutionProvider::default().with_cuda_graph(a.graph).with_conv_algorithm_search(CuDNNConvAlgorithmSearch::Heuristic);
            builder = builder.with_execution_providers([ep.build().error_on_failure()]).map_err(|x| format!("EP CUDA indisponible : {x}"))?;
        }
        other => return Err(format!("--ep {other} : inconnu ou non compilé (features cuda / tensorrt)")),
    }
    let t_load = Instant::now();
    let mut session = builder.commit_from_file(&a.model).map_err(e)?;
    let ep_name = a.ep.as_str();
    let in_side = 256usize;
    let shape = model_shape(&session);
    if a.ep == "cpu" && (shape.u8_input || shape.slim) { return Err("--ep cpu : seul le modèle brut (entrée f32, sorties output0/output1) est géré".into()); }
    let mut gpu = if a.ep == "cpu" { None } else { Some(setup_gpu(&session, in_side, shape)?) };
    println!("sniper-rs : modèle {}{}{} · EP {ep_name}{} (prêt en {:.1} s) · crop {:.3}×h · bande {} · conf {}", a.model, if shape.u8_input { " [entrée u8]" } else { "" }, if shape.slim { " [décodage GPU]" } else { "" }, if gpu.is_some() && a.graph { " + CUDA graph" } else { "" }, t_load.elapsed().as_secs_f64(), a.crop, a.band, a.conf);

    let mut c = Bridge::attach("aimlabs", Duration::from_secs(20)).map_err(|x| x.to_string())?;
    let mut enabled = std::env::var("AIMLABS_AUTOSTART").ok().as_deref() == Some("1");
    let (mut caps_last, mut locked, mut last_fire) = (false, false, Instant::now() - Duration::from_secs(10));
    let mut crop_buf: Vec<u8> = Vec::new();
    let plane = in_side * in_side;
    let mut cpu_in = vec![0f32; if gpu.is_none() { 3 * plane } else { 0 }];
    let mut cpu_o: (Vec<f32>, Vec<f32>) = (Vec::new(), Vec::new());
    let cpu_dims = { let d = |i: usize, k: usize| session.outputs[i].output_type.tensor_shape().map_or(0, |s| s.get(k).map_or(0, |&v| v as usize)); (d(0, 2), d(1, 2), d(1, 3)) };
    let mut xmap: Vec<usize> = Vec::new();
    let mut track: Option<Track> = None;
    let (mut n, mut t_crop, mut t_inf, mut t_post, mut n_fire, mut n_pre) = (0u64, 0.0f64, 0.0f64, 0.0f64, 0u64, 0u64);
    let mut t_split = [0.0f64; 3];
    let mut t_stats = Instant::now();
    let t_start = Instant::now();
    // warm-up (capture du CUDA graph, caches)
    for _ in 0..3 { infer(&mut session, &mut gpu, &cpu_in, &mut cpu_o, in_side)?; }
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
                let k = n as f64 * 1e-3;
                println!("[stats] {:.0} frames/s · crop+préparation {:.2} ms · inférence {:.2} ms (envoi {:.2} · exéc {:.2} · retour {:.2}) · post {:.2} ms · tirs {n_fire} (anticipés {n_pre})",
                    n as f64 / t_stats.elapsed().as_secs_f64(), t_crop / k, t_inf / k, t_split[0] / k, t_split[1] / k, t_split[2] / k, t_post / k);
            }
            n = 0; t_crop = 0.0; t_inf = 0.0; t_post = 0.0; t_split = [0.0; 3]; n_fire = 0; n_pre = 0; t_stats = Instant::now();
        }
        if !active { track = None; continue; }
        // crop (side_px) → 256×256 NCHW f32 (plus proche voisin si side ≠ 256 ; égal à 240→256 : léger agrandissement)
        let sp = side_px as usize;
        if xmap.len() != in_side || xmap[in_side - 1] != (in_side - 1) * sp / in_side * 3 { xmap = (0..in_side).map(|x| (x * sp / in_side) * 3).collect(); }
        match gpu.as_mut() {
            Some(g) if shape.u8_input => {
                // NHWC uint8 tel quel : cast, /255 et transposition se font dans le graphe
                let dst = g.h_in.as_u8_mut();
                for y in 0..in_side {
                    let row = &crop_buf[(y * sp / in_side) * sp * 3..][..sp * 3];
                    let o = y * in_side * 3;
                    for (x, &sx) in xmap.iter().enumerate() { dst[o + x * 3..o + x * 3 + 3].copy_from_slice(&row[sx..sx + 3]); }
                }
            }
            other => {
                let dst: &mut [f32] = match other { Some(g) => g.h_in.as_f32_mut(), None => &mut cpu_in[..] };
                let (pr, rest) = dst.split_at_mut(plane);
                let (pg, pb) = rest.split_at_mut(plane);
                for y in 0..in_side {
                    let row = &crop_buf[(y * sp / in_side) * sp * 3..][..sp * 3];
                    let o = y * in_side;
                    for (x, &sx) in xmap.iter().enumerate() {
                        pr[o + x] = row[sx] as f32 * (1.0 / 255.0);
                        pg[o + x] = row[sx + 1] as f32 * (1.0 / 255.0);
                        pb[o + x] = row[sx + 2] as f32 * (1.0 / 255.0);
                    }
                }
            }
        }
        let t1 = Instant::now();
        let split = infer(&mut session, &mut gpu, &cpu_in, &mut cpu_o, in_side)?;
        let t2 = Instant::now();
        for i in 0..3 { t_split[i] += split[i]; }
        let view = match gpu.as_ref() {
            Some(g) if shape.slim => {
                let rows = g.dims[1][1] / 64;
                View::Slim { det: g.h_out[0].as_f32(), logits: g.h_out[1].as_f32(), k: g.dims[0][2], row0: 32 - rows / 2, rows, mw: 64, mh: 64 }
            }
            Some(g) => View::Full { out0: g.h_out[0].as_f32(), protos: g.h_out[1].as_f32(), n: g.dims[0][2], mh: g.dims[1][2], mw: g.dims[1][3] },
            None => View::Full { out0: &cpu_o.0, protos: &cpu_o.1, n: cpu_dims.0, mh: cpu_dims.1, mw: cpu_dims.2 },
        };
        let scale = sp as f32 / in_side as f32; // sortie réseau → pixels du crop
        let mut dets = view.decode(a.conf.min(0.25));
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
        // profil d'information « réel » : le jeu ne donne ni instant de capture ni horloge → horloge locale
        let t_track = if capture_usec != 0 { capture_usec as f64 / 1e6 } else { t0.duration_since(t_start).as_secs_f64() };
        tr.push(t_track, &b);
        // marque : l'image montre la cible sous le réticule (masque)
        let ext_now = view.mask_extent(&Det { x1: b.x1 / scale, y1: b.y1 / scale, x2: b.x2 / scale, y2: b.y2 / scale, ..b }, cy / scale, in_side as f32)
            .map(|(l, r)| (l * scale, r * scale));
        let under_now = b.x1 <= cx && cx <= b.x2 && b.y1 <= cy && cy <= b.y2 && ext_now.map_or(false, |(l, r)| l <= cx && cx <= r);
        if under_now { c.mark(frame_id, 0.0, 0.0); } else { locked = false; }
        if !(locked || last_fire.elapsed().as_secs_f32() < a.cooldown) {
            let latency = if capture_usec != 0 && st.time_usec != 0 {
                st.time_usec.saturating_sub(capture_usec) as f32 / 1e6 + 1.0 / st.fps.max(1.0)
            } else {
                t0.elapsed().as_secs_f32() + a.latency_ms / 1e3 // sans horloge du jeu : temps écoulé + latence réglable
            };
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
                let ext = view.mask_extent(&Det { x1: b.x1 / scale, y1: b.y1 / scale, x2: b.x2 / scale, y2: b.y2 / scale, ..b }, (cy - dy) / scale, in_side as f32);
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
