#include "shared_memory.h"

#include <cstring>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <cerrno>
#endif

namespace aimbridge {

#ifdef _WIN32

bool SharedMemory::create(const std::string &name, size_t size, std::string &error) {
	close();
	HANDLE h = CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
			(DWORD)((uint64_t)size >> 32), (DWORD)((uint64_t)size & 0xffffffffu), name.c_str());
	if (!h) {
		error = "CreateFileMapping failed, error " + std::to_string(GetLastError());
		return false;
	}
	void *p = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, size);
	if (!p) {
		error = "MapViewOfFile failed, error " + std::to_string(GetLastError());
		CloseHandle(h);
		return false;
	}
	std::memset(p, 0, size);
	_handle = h;
	_data = static_cast<uint8_t *>(p);
	_size = size;
	_name = name;
	return true;
}

void SharedMemory::close() {
	if (_data) {
		UnmapViewOfFile(_data);
		_data = nullptr;
	}
	if (_handle) {
		CloseHandle(_handle);
		_handle = nullptr;
	}
	_size = 0;
}

#else

bool SharedMemory::create(const std::string &name, size_t size, std::string &error) {
	close();
	std::string path = "/" + name;
	shm_unlink(path.c_str()); // replace any stale segment from a previous run
	int fd = shm_open(path.c_str(), O_CREAT | O_RDWR, 0666);
	if (fd < 0) {
		error = std::string("shm_open failed: ") + std::strerror(errno);
		return false;
	}
	if (ftruncate(fd, (off_t)size) != 0) {
		error = std::string("ftruncate failed: ") + std::strerror(errno);
		::close(fd);
		shm_unlink(path.c_str());
		return false;
	}
	void *p = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (p == MAP_FAILED) {
		error = std::string("mmap failed: ") + std::strerror(errno);
		::close(fd);
		shm_unlink(path.c_str());
		return false;
	}
	std::memset(p, 0, size);
	_fd = fd;
	_data = static_cast<uint8_t *>(p);
	_size = size;
	_name = name;
	return true;
}

void SharedMemory::close() {
	if (_data) {
		munmap(_data, _size);
		_data = nullptr;
	}
	if (_fd >= 0) {
		::close(_fd);
		_fd = -1;
		shm_unlink(("/" + _name).c_str());
	}
	_size = 0;
}

#endif

} // namespace aimbridge
