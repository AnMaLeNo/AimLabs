//! aimlabs-selftest: attach, read frames for a few seconds, measure the per-frame cost of the
//! SDK path (wait → crop of the reticle square → mark) and the capture→command latency the game
//! reports. Usage: aimlabs-selftest [--shm aimlabs] [--seconds 5] [--crop 240] [--fire]
use aimlabs::{Bridge, protocol};
use std::time::{Duration, Instant};

fn main() {
    let mut shm = "aimlabs".to_string();
    let mut seconds = 5.0f64;
    let mut crop = 240u32;
    let mut fire = false;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--shm" => { shm = args[i + 1].clone(); i += 1; }
            "--seconds" => { seconds = args[i + 1].parse().unwrap(); i += 1; }
            "--crop" => { crop = args[i + 1].parse().unwrap(); i += 1; }
            "--fire" => fire = true,
            a => { eprintln!("unknown arg {a}"); std::process::exit(2); }
        }
        i += 1;
    }
    let mut c = match Bridge::attach(&shm, Duration::from_secs(10)) {
        Ok(c) => c,
        Err(e) => { eprintln!("{e}"); std::process::exit(1); }
    };
    let st = c.state();
    println!("attached: segment {} B, mode {:#x}, fps {:.0}, game time {} us", c.mapping_len(), c.mode_flags(), st.fps, st.time_usec);
    let mut buf = Vec::new();
    let (mut n, mut torn, mut t_wait, mut t_crop) = (0u64, 0u64, 0.0f64, 0.0f64);
    let mut lat_us: Vec<u64> = Vec::new();
    let t_start = Instant::now();
    while t_start.elapsed().as_secs_f64() < seconds {
        let t0 = Instant::now();
        let (t1, t2, ok, id, cap, centre, w, h) = {
            let Some(f) = c.wait_frame(Duration::from_secs(2)) else { eprintln!("no frame for 2 s"); break; };
            let t1 = Instant::now();
            let (x0, y0, side) = f.centre_square(crop);
            f.crop_into(x0, y0, side, side, 3, &mut buf);
            let ok = f.still_valid();
            let t2 = Instant::now();
            (t1, t2, ok, f.frame_id(), f.capture_usec(), f.pixel(f.width() / 2, f.height() / 2), f.width(), f.height())
        };
        if !ok { torn += 1; }
        c.mark(id, 0.0, 0.0);
        if fire { c.fire(id); }
        n += 1;
        t_wait += (t1 - t0).as_secs_f64();
        t_crop += (t2 - t1).as_secs_f64();
        let st = c.state();
        lat_us.push(st.time_usec.saturating_sub(cap));
        if n == 1 { println!("first frame {}x{} id {} centre RGBA {:?}", w, h, id, centre); }
    }
    lat_us.sort();
    let st = c.state();
    println!("frames {} ({:.0}/s), torn {}, wait {:.3} ms, crop {}px {:.3} ms (per frame)", n, n as f64 / seconds, torn,
        t_wait / n.max(1) as f64 * 1e3, crop, t_crop / n.max(1) as f64 * 1e3);
    if !lat_us.is_empty() {
        println!("capture → state read: median {} us, p90 {} us", lat_us[lat_us.len() / 2], lat_us[lat_us.len() * 9 / 10]);
    }
    println!("game: cmds applied {}, dropped {}, last latency {} us, avg {} us, shots {}, caps slot pressed {}",
        st.cmds_applied, st.cmds_dropped, st.last_latency_usec, st.avg_latency_usec, st.shots, Bridge::key_pressed(&st, protocol::SLOT_CAPSLOCK));
    println!("SELFTEST OK");
}
