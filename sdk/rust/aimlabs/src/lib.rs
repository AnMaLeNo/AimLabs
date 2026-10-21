//! AimLabs AimBridge SDK in Rust — attach to the game's shared-memory segment and read frames
//! **without copying** (a `FrameView` borrows the pixels in place; `crop()` copies only the
//! region you ask for), read the player state and keyboard, push `move`/`fire`/`mark` commands.
//!
//! ```no_run
//! let mut c = aimlabs::Bridge::attach("aimlabs", std::time::Duration::from_secs(10))?;
//! loop {
//!     if let Some(f) = c.wait_frame(std::time::Duration::from_secs(2)) {
//!         let (cx, cy) = (f.width() / 2, f.height() / 2);
//!         let px = f.pixel(cx, cy);                  // RGBA under the reticle
//!         c.mark(f.frame_id(), 0.0, 0.0);            // « target under the reticle » claim
//!         c.fire(f.frame_id());
//!     }
//! }
//! # Ok::<(), aimlabs::Error>(())
//! ```
//! Segment names: Linux `/dev/shm/<name>`, macOS POSIX shm `/<name>`, Windows file mapping `<name>`.

pub mod protocol;
mod shm;

use protocol::*;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::time::{Duration, Instant};

#[derive(Debug)]
pub enum Error {
    NotFound(String),
    Io(std::io::Error),
    BadMagic(u32),
    BadVersion(u32),
    Timeout,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::NotFound(n) => write!(f, "no AimBridge segment '{n}' (is the game running with the bridge enabled?)"),
            Error::Io(e) => write!(f, "shared memory: {e}"),
            Error::BadMagic(m) => write!(f, "bad magic 0x{m:08x}"),
            Error::BadVersion(v) => write!(f, "protocol version {v}, expected {VERSION}"),
            Error::Timeout => write!(f, "timeout"),
        }
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self { Error::Io(e) }
}

/// Client clock in microseconds (monotonic, process start = 0).
pub fn now_usec() -> u64 {
    thread_local! { static T0: Instant = Instant::now(); }
    T0.with(|t| t.elapsed().as_micros() as u64)
}

pub struct Bridge {
    map: shm::Mapping,
    base: *mut u8,
    frames_offset: usize,
    frame_slots: u32,
    frame_slot_size: usize,
    state_offset: usize,
    commands_offset: usize,
    command_capacity: u64,
    last_frame_id: u64,
    heartbeat: u64,
}

unsafe impl Send for Bridge {}

/// A frame borrowed from the shared memory (valid until the next `wait_frame`; checked by a
/// seqlock, so the pixels are a consistent snapshot of one published frame).
pub struct FrameView<'a> {
    pub header: FrameHeader,
    pixels: &'a [u8],
}

impl<'a> FrameView<'a> {
    pub fn width(&self) -> u32 { self.header.width }
    pub fn height(&self) -> u32 { self.header.height }
    pub fn frame_id(&self) -> u64 { self.header.frame_id }
    pub fn capture_usec(&self) -> u64 { self.header.capture_usec }
    /// RGBA8 rows of `stride` bytes.
    pub fn pixels(&self) -> &'a [u8] { self.pixels }
    pub fn pixel(&self, x: u32, y: u32) -> [u8; 4] {
        let o = (y * self.header.stride + x * 4) as usize;
        [self.pixels[o], self.pixels[o + 1], self.pixels[o + 2], self.pixels[o + 3]]
    }
    /// Copy a `w`×`h` RGB(A) region whose top-left corner is (x0, y0) into `out`
    /// (row-major, `channels` = 3 or 4). Only the crop is copied — e.g. the square around the
    /// reticle for a detector — never the full frame.
    pub fn crop_into(&self, x0: u32, y0: u32, w: u32, h: u32, channels: usize, out: &mut Vec<u8>) {
        out.clear();
        out.reserve((w * h) as usize * channels);
        let stride = self.header.stride as usize;
        for y in y0..y0 + h {
            let row = &self.pixels[y as usize * stride + x0 as usize * 4..][..w as usize * 4];
            if channels == 4 {
                out.extend_from_slice(row);
            } else {
                for p in row.chunks_exact(4) {
                    out.extend_from_slice(&p[..3]);
                }
            }
        }
    }
    /// After reading pixels: true if the game did not rewrite this slot meanwhile (seqlock).
    pub fn still_valid(&self) -> bool {
        let slot_ptr = self.pixels.as_ptr() as usize - 64;
        let seq = unsafe { &*(slot_ptr as *const AtomicU32) };
        std::sync::atomic::fence(Ordering::Acquire);
        seq.load(Ordering::Acquire) == self.header.seq
    }
    /// Square of side `side` centred on the reticle, clamped to the frame; returns (x0, y0, side).
    pub fn centre_square(&self, side: u32) -> (u32, u32, u32) {
        let side = side.min(self.header.width).min(self.header.height);
        ((self.header.width - side) / 2, (self.header.height - side) / 2, side)
    }
}

impl Bridge {
    /// Attach to the segment `name`, retrying until `timeout` (the game may not be up yet).
    pub fn attach(name: &str, timeout: Duration) -> Result<Bridge, Error> {
        let t0 = Instant::now();
        loop {
            match shm::Mapping::open(name) {
                Ok(map) => {
                    let base = map.ptr();
                    let h: Header = unsafe { std::ptr::read_volatile(base as *const Header) };
                    if h.magic == 0 {
                        // segment created, header not written yet (game starting) → retry
                        drop(map);
                        if t0.elapsed() >= timeout { return Err(Error::BadMagic(0)); }
                        std::thread::sleep(Duration::from_millis(50));
                        continue;
                    }
                    if h.magic != MAGIC {
                        return Err(Error::BadMagic(h.magic));
                    }
                    if h.version != VERSION {
                        return Err(Error::BadVersion(h.version));
                    }
                    return Ok(Bridge {
                        frames_offset: h.frames_offset as usize,
                        frame_slots: h.frame_slots,
                        frame_slot_size: h.frame_slot_size as usize,
                        state_offset: h.state_offset as usize,
                        commands_offset: h.commands_offset as usize,
                        command_capacity: h.command_capacity as u64,
                        last_frame_id: 0,
                        heartbeat: 0,
                        base,
                        map,
                    });
                }
                Err(e) => {
                    if t0.elapsed() >= timeout {
                        return Err(match e {
                            Error::NotFound(_) => Error::NotFound(name.to_string()),
                            e => e,
                        });
                    }
                    std::thread::sleep(Duration::from_millis(20));
                }
            }
        }
    }

    fn header(&self) -> &Header { unsafe { &*(self.base as *const Header) } }
    fn atomic_u64(&self, off: usize) -> &AtomicU64 { unsafe { &*(self.base.add(off) as *const AtomicU64) } }
    fn atomic_u32(&self, off: usize) -> &AtomicU32 { unsafe { &*(self.base.add(off) as *const AtomicU32) } }

    pub fn mode_flags(&self) -> u32 { self.atomic_u32(std::mem::offset_of!(Header, mode_flags)).load(Ordering::Acquire) }
    pub fn latest_frame_id(&self) -> u64 { self.atomic_u64(std::mem::offset_of!(Header, latest_frame_id)).load(Ordering::Acquire) }
    pub fn game_heartbeat_usec(&self) -> u64 { self.atomic_u64(std::mem::offset_of!(Header, game_heartbeat_usec)).load(Ordering::Acquire) }
    pub fn game_pid(&self) -> u64 { self.header().game_pid }
    /// True while the game keeps writing its heartbeat (checked against the state clock).
    pub fn game_alive(&self) -> bool {
        let st = self.state();
        st.time_usec.saturating_sub(self.game_heartbeat_usec()) < 2_000_000
    }

    /// Consistent snapshot of the state block (seqlock).
    pub fn state(&self) -> State {
        let seq = self.atomic_u32(self.state_offset);
        loop {
            let s1 = seq.load(Ordering::Acquire);
            if s1 & 1 == 1 { std::hint::spin_loop(); continue; }
            let st: State = unsafe { std::ptr::read_volatile(self.base.add(self.state_offset) as *const State) };
            std::sync::atomic::fence(Ordering::Acquire);
            if seq.load(Ordering::Acquire) == s1 { return st; }
        }
    }

    /// Is the key in `slot` (see protocol::SLOT_*, or a Latin-1 keycode < 256) pressed?
    pub fn key_pressed(st: &State, slot: usize) -> bool {
        slot < KEY_SLOTS && (st.key_bits[slot / 64] >> (slot % 64)) & 1 == 1
    }

    /// Wait for a frame newer than the last one returned; `None` on timeout. Zero-copy: the view
    /// borrows the slot. If the game overwrites the slot while you read it, `FrameView::still_valid()`
    /// tells you — read your crop, then check.
    pub fn wait_frame(&mut self, timeout: Duration) -> Option<FrameView<'_>> {
        let t0 = Instant::now();
        loop {
            let id = self.latest_frame_id();
            if id != 0 && id != self.last_frame_id {
                let slot = self.atomic_u32(std::mem::offset_of!(Header, latest_slot)).load(Ordering::Acquire) as usize;
                if slot < self.frame_slots as usize {
                    let off = self.frames_offset + slot * self.frame_slot_size;
                    let seq = self.atomic_u32(off);
                    let s1 = seq.load(Ordering::Acquire);
                    if s1 & 1 == 0 {
                        let header: FrameHeader = unsafe { std::ptr::read_volatile(self.base.add(off) as *const FrameHeader) };
                        if header.frame_id == id {
                            self.last_frame_id = id;
                            self.heartbeat += 1;
                            self.atomic_u64(std::mem::offset_of!(Header, client_heartbeat)).store(self.heartbeat, Ordering::Release);
                            let n = (header.stride * header.height) as usize;
                            let pixels = unsafe { std::slice::from_raw_parts(self.base.add(off + 64), n) };
                            return Some(FrameView { header, pixels });
                        }
                    }
                }
            }
            if t0.elapsed() >= timeout { return None; }
            std::thread::sleep(Duration::from_micros(200));
        }
    }


    fn push(&self, cmd: Command) -> bool {
        let ring = self.atomic_u64(self.commands_offset);        // head (client)
        let tail = self.atomic_u64(self.commands_offset + 8).load(Ordering::Acquire);
        let head = ring.load(Ordering::Relaxed);
        if head - tail >= self.command_capacity { return false; }
        let slot = (head % self.command_capacity) as usize;
        let off = self.commands_offset + 64 + slot * std::mem::size_of::<Command>();
        unsafe { std::ptr::write_volatile(self.base.add(off) as *mut Command, cmd) };
        ring.store(head + 1, Ordering::Release);
        true
    }
    /// Turn the view by (dx, dy) mouse pixels (x right, y down).
    pub fn move_by(&self, dx: f32, dy: f32, frame_id: u64) -> bool {
        self.push(Command { kind: CMD_MOVE, flags: 0, dx, dy, frame_id, client_usec: now_usec() })
    }
    /// Request one shot (applied next game frame, subject to the weapon's fire rate).
    pub fn fire(&self, frame_id: u64) -> bool {
        self.push(Command { kind: CMD_FIRE, flags: 0, dx: 0.0, dy: 0.0, frame_id, client_usec: now_usec() })
    }
    /// « I believe the target is under the reticle on this frame » (benchmarks; no game effect).
    pub fn mark(&self, frame_id: u64, x: f32, y: f32) -> bool {
        self.push(Command { kind: CMD_MARK, flags: 0, dx: x, dy: y, frame_id, client_usec: now_usec() })
    }
    pub fn mapping_len(&self) -> usize { self.map.len() }
}
