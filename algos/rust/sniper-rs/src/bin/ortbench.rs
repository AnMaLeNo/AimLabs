//! ortbench — micro-benchmark de l'inférence seule (entrée synthétique), pour isoler d'où viennent
//! les ms de sniper-rs. Usage : ortbench [--model m.onnx] [--n 300] <variante>...
//! Variantes : cpu-tensor (comme sniper-rs), heuristic, tf32, iobind, graph, trt
use ort::execution_providers::{CUDAExecutionProvider, TensorRTExecutionProvider};
use ort::execution_providers::cuda::CuDNNConvAlgorithmSearch;
use ort::memory::{AllocationDevice, Allocator, AllocatorType, MemoryInfo, MemoryType};
use ort::session::{builder::GraphOptimizationLevel, Session};
use ort::value::Tensor;
use std::time::Instant;

fn stats(name: &str, mut v: Vec<f64>) {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mean = v.iter().sum::<f64>() / v.len() as f64;
    println!("{name:<28} mean {mean:6.2} ms · p50 {:6.2} · p90 {:6.2} · p99 {:6.2} · min {:6.2}", v[v.len() / 2], v[v.len() * 9 / 10], v[v.len() * 99 / 100], v[0]);
}

fn main() -> ort::Result<()> {
    let mut model = "algos/yolo11n-seg_256x256.onnx".to_string();
    let mut n = 300usize;
    let mut variants: Vec<String> = Vec::new();
    let a: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < a.len() {
        match a[i].as_str() {
            "--model" => { model = a[i + 1].clone(); i += 1; }
            "--n" => { n = a[i + 1].parse().unwrap(); i += 1; }
            v => variants.push(v.to_string()),
        }
        i += 1;
    }
    let input = ndarray::Array4::<f32>::from_shape_fn((1, 3, 256, 256), |(_, c, y, x)| ((c * 7 + y * 3 + x) % 255) as f32 / 255.0);

    for v in &variants {
        let mut b = Session::builder()?.with_optimization_level(GraphOptimizationLevel::Level3)?.with_intra_threads(2)?;
        let mut cuda = CUDAExecutionProvider::default();
        match v.as_str() {
            "heuristic" => cuda = cuda.with_conv_algorithm_search(CuDNNConvAlgorithmSearch::Heuristic),
            "tf32" => cuda = cuda.with_tf32(true).with_conv_algorithm_search(CuDNNConvAlgorithmSearch::Heuristic),
            "graph" => cuda = cuda.with_cuda_graph(true).with_conv_algorithm_search(CuDNNConvAlgorithmSearch::Heuristic),
            _ => {}
        }
        if v.starts_with("trt") {
            let cache = std::env::temp_dir().join("aimlabs_trt_bench");
            let trt = TensorRTExecutionProvider::default().with_fp16(true).with_engine_cache(true)
                .with_engine_cache_path(cache.to_string_lossy()).with_timing_cache(true).with_timing_cache_path(cache.to_string_lossy())
                .with_cuda_graph(v == "trtgraph");
            b = b.with_execution_providers([trt.build().error_on_failure()])?;
        } else if v != "cpu" { b = b.with_execution_providers([cuda.build().error_on_failure()])?; }
        let t_build = Instant::now();
        let mut s = b.commit_from_file(&model)?;
        println!("[{v}] session prête en {:.1} s", t_build.elapsed().as_secs_f64());
        match v.as_str() {
            "iobind" | "graph" | "trtgraph" | "trtbind" => {
                let dev = MemoryInfo::new(AllocationDevice::CUDA, 0, AllocatorType::Device, MemoryType::Default)?;
                let alloc = Allocator::new(&s, dev.clone())?;
                let t_in = Tensor::<f32>::new(&alloc, [1usize, 3, 256, 256])?;
                let mut io = s.create_binding()?;
                io.bind_input("images", &t_in)?;
                io.bind_output_to_device("output0", &dev)?;
                io.bind_output_to_device("output1", &dev)?;
                for _ in 0..30 { let _ = s.run_binding(&io)?; }
                let mut t = Vec::new();
                for _ in 0..n {
                    let t0 = Instant::now();
                    let o = s.run_binding(&io)?;
                    drop(o);
                    t.push(t0.elapsed().as_secs_f64() * 1e3);
                }
                stats(&format!("{v} (device in/out, no copy)"), t);
            }
            _ => {
                for _ in 0..30 { let _ = s.run(ort::inputs![Tensor::from_array(input.clone())?])?; }
                let mut t = Vec::new();
                let mut t_in = Vec::new();
                for _ in 0..n {
                    let t0 = Instant::now();
                    let tin = Tensor::from_array(input.clone())?;
                    let t1 = Instant::now();
                    let o = s.run(ort::inputs![tin])?;
                    let _ = o[0].try_extract_tensor::<f32>()?;
                    let _ = o[1].try_extract_tensor::<f32>()?;
                    t.push(t0.elapsed().as_secs_f64() * 1e3);
                    t_in.push((t1 - t0).as_secs_f64() * 1e3);
                }
                stats(v, t);
                stats(&format!("  (dont clone+from_array)"), t_in);
            }
        }
    }
    Ok(())
}
