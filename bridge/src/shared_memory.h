// Minimal cross-platform named shared memory (create / unlink only; the client
// side attaches with Python's multiprocessing.shared_memory or any mmap).
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace aimbridge {

class SharedMemory {
public:
	SharedMemory() = default;
	~SharedMemory() { close(); }
	SharedMemory(const SharedMemory &) = delete;
	SharedMemory &operator=(const SharedMemory &) = delete;

	// Creates (or replaces) a segment named `name` of `size` bytes, zero-filled.
	bool create(const std::string &name, size_t size, std::string &error);
	void close();

	uint8_t *data() const { return _data; }
	size_t size() const { return _size; }
	bool is_open() const { return _data != nullptr; }

private:
	std::string _name;
	uint8_t *_data = nullptr;
	size_t _size = 0;
#ifdef _WIN32
	void *_handle = nullptr;
#else
	int _fd = -1;
#endif
};

} // namespace aimbridge
