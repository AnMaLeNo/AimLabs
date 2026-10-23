//! Platform shared-memory mapping (read/write, created by the game).
use crate::Error;

pub struct Mapping {
    ptr: *mut u8,
    len: usize,
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
}

impl Mapping {
    pub fn ptr(&self) -> *mut u8 { self.ptr }
    pub fn len(&self) -> usize { self.len }
}

#[cfg(unix)]
impl Mapping {
    pub fn open(name: &str) -> Result<Mapping, Error> {
        use std::os::unix::io::AsRawFd;
        #[cfg(target_os = "linux")]
        let path = format!("/dev/shm/{name}");
        #[cfg(not(target_os = "linux"))]
        let path = format!("/{name}");
        let file = {
            #[cfg(target_os = "linux")]
            { std::fs::OpenOptions::new().read(true).write(true).open(&path) }
            #[cfg(not(target_os = "linux"))]
            {
                let c = std::ffi::CString::new(path.clone()).unwrap();
                let fd = unsafe { libc::shm_open(c.as_ptr(), libc::O_RDWR, 0) };
                if fd < 0 { Err(std::io::Error::last_os_error()) } else { Ok(unsafe { <std::fs::File as std::os::unix::io::FromRawFd>::from_raw_fd(fd) }) }
            }
        };
        let file = match file {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(Error::NotFound(name.to_string())),
            Err(e) => return Err(Error::Io(e)),
        };
        let len = file.metadata()?.len() as usize;
        if len < 256 { return Err(Error::NotFound(name.to_string())); }
        let ptr = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, file.as_raw_fd(), 0) };
        if ptr == libc::MAP_FAILED { return Err(Error::Io(std::io::Error::last_os_error())); }
        Ok(Mapping { ptr: ptr as *mut u8, len })
    }
}
#[cfg(unix)]
impl Drop for Mapping {
    fn drop(&mut self) { unsafe { libc::munmap(self.ptr as *mut _, self.len); } }
}

#[cfg(windows)]
impl Mapping {
    pub fn open(name: &str) -> Result<Mapping, Error> {
        use windows_sys::Win32::System::Memory::*;
        let wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
        let handle = unsafe { OpenFileMappingW(FILE_MAP_ALL_ACCESS, 0, wide.as_ptr()) };
        if handle.is_null() {
            let e = std::io::Error::last_os_error();
            return Err(if e.raw_os_error() == Some(2) { Error::NotFound(name.to_string()) } else { Error::Io(e) });
        }
        let ptr = unsafe { MapViewOfFile(handle, FILE_MAP_ALL_ACCESS, 0, 0, 0) }.Value as *mut u8;
        if ptr.is_null() { return Err(Error::Io(std::io::Error::last_os_error())); }
        // total size from the header (the mapping size is not queryable portably)
        let total = unsafe { std::ptr::read_volatile(ptr.add(16) as *const u64) } as usize;
        Ok(Mapping { ptr, len: total, handle })
    }
}
#[cfg(windows)]
impl Drop for Mapping {
    fn drop(&mut self) {
        use windows_sys::Win32::System::Memory::*;
        use windows_sys::Win32::Foundation::CloseHandle;
        unsafe {
            UnmapViewOfFile(MEMORY_MAPPED_VIEW_ADDRESS { Value: self.ptr as *mut _ });
            CloseHandle(self.handle);
        }
    }
}
