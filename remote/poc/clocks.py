"""Horloges communes aux outils de la chaîne « Pi » (PC, Mac, Pi).

`wall_ns()` : horloge murale (ns depuis 1970) PRÉCISE. Sous Windows, `time.time_ns()` n'a que la résolution du minuteur système
(~15,6 ms avec Python <= 3.12) : on appelle GetSystemTimePreciseAsFileTime (< 1 µs). Ailleurs, `time.time_ns()` suffit.
`mono_ns()` : horloge monotone (QPC / CLOCK_MONOTONIC), pour les durées.
"""
import sys
import time

mono_ns = time.perf_counter_ns

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _fn = ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime
    _fn.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    _ft = wintypes.FILETIME()
    _EPOCH_DELTA_100NS = 116444736000000000        # 1601-01-01 -> 1970-01-01

    def wall_ns() -> int:
        _fn(ctypes.byref(_ft))
        return ((_ft.dwHighDateTime << 32 | _ft.dwLowDateTime) - _EPOCH_DELTA_100NS) * 100
else:
    wall_ns = time.time_ns
