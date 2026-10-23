//! Accès minimal au runtime CUDA (cudart, chargé dynamiquement — même DLL que celle qu'ONNX Runtime
//! a déjà chargée) : mémoire hôte épinglée et copies hôte ↔ GPU, pour alimenter les tampons liés à la
//! session (IoBinding + CUDA graph, qui exigent des adresses fixes).
use libloading::Library;
use std::ffi::c_void;

const H2D: i32 = 1;
const D2H: i32 = 2;

pub struct Cuda {
    host_alloc: unsafe extern "C" fn(*mut *mut c_void, usize, u32) -> i32,
    memcpy: unsafe extern "C" fn(*mut c_void, *const c_void, usize, i32) -> i32,
    sync: unsafe extern "C" fn() -> i32,
    _lib: Library,
}

/// Tampon d'octets en mémoire épinglée (copies DMA directes, sans passage par un tampon intermédiaire).
pub struct Pinned { ptr: *mut u8, bytes: usize }
impl Pinned {
    pub fn as_f32(&self) -> &[f32] { unsafe { std::slice::from_raw_parts(self.ptr as *const f32, self.bytes / 4) } }
    pub fn as_f32_mut(&mut self) -> &mut [f32] { unsafe { std::slice::from_raw_parts_mut(self.ptr as *mut f32, self.bytes / 4) } }
    pub fn as_u8_mut(&mut self) -> &mut [u8] { unsafe { std::slice::from_raw_parts_mut(self.ptr, self.bytes) } }
}

impl Cuda {
    pub fn load() -> Result<Self, String> {
        let names: &[&str] = if cfg!(windows) { &["cudart64_12.dll", "cudart64_13.dll"] } else { &["libcudart.so.12", "libcudart.so"] };
        let lib = names.iter().find_map(|n| unsafe { Library::new(n).ok() })
            .ok_or_else(|| format!("runtime CUDA introuvable ({}) : ajoute le dossier de cudart au PATH (voir --libs)", names.join(" / ")))?;
        unsafe {
            let host_alloc = *lib.get::<unsafe extern "C" fn(*mut *mut c_void, usize, u32) -> i32>(b"cudaHostAlloc\0").map_err(|e| e.to_string())?;
            let memcpy = *lib.get::<unsafe extern "C" fn(*mut c_void, *const c_void, usize, i32) -> i32>(b"cudaMemcpy\0").map_err(|e| e.to_string())?;
            let sync = *lib.get::<unsafe extern "C" fn() -> i32>(b"cudaDeviceSynchronize\0").map_err(|e| e.to_string())?;
            Ok(Self { host_alloc, memcpy, sync, _lib: lib })
        }
    }

    pub fn pinned(&self, bytes: usize) -> Result<Pinned, String> {
        let mut p: *mut c_void = std::ptr::null_mut();
        let r = unsafe { (self.host_alloc)(&mut p, bytes, 0) };
        if r != 0 || p.is_null() { return Err(format!("cudaHostAlloc a échoué ({r})")); }
        Ok(Pinned { ptr: p as *mut u8, bytes })
    }

    /// Hôte → GPU, puis synchronisation du périphérique : la session ORT tourne sur son propre flux,
    /// la copie doit être terminée avant qu'elle ne lise le tampon.
    pub fn upload(&self, dev: *mut c_void, host: &Pinned) -> Result<(), String> {
        let r = unsafe { (self.memcpy)(dev, host.ptr as *const c_void, host.bytes, H2D) };
        if r != 0 { return Err(format!("cudaMemcpy H2D a échoué ({r})")); }
        let r = unsafe { (self.sync)() };
        if r != 0 { return Err(format!("cudaDeviceSynchronize a échoué ({r})")); }
        Ok(())
    }

    /// GPU → hôte (ORT a déjà synchronisé son flux à la fin de `run_binding`).
    pub fn download(&self, host: &mut Pinned, dev: *const c_void) -> Result<(), String> {
        let r = unsafe { (self.memcpy)(host.ptr as *mut c_void, dev, host.bytes, D2H) };
        if r != 0 { return Err(format!("cudaMemcpy D2H a échoué ({r})")); }
        Ok(())
    }
}
