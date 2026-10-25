"""Sonde clavier Windows : horodate (QPC, ns) chaque appui / relâchement de la touche Pause, vus par un crochet bas niveau
(WH_KEYBOARD_LL, tous les claviers, fenêtre au premier plan ou non). Sert à mesurer la latence et la gigue du chemin
« Pi -> Bluetooth HID -> Windows » (voir remote/pi/) avec une vérité terrain côté PC.

    python kbd_probe_win.py --seconds 30 --out probe.jsonl [--vk 0x13]

Chaque ligne : {"t_ns": <perf_counter_ns>, "wall_ns": <time.time_ns>, "vk": .., "down": true/false, "flags": ..}
`wall_ns` (horloge murale précise) permet de rapprocher les horloges du PC et du Pi (voir clock_sync.py).
"""
import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clocks import wall_ns  # noqa: E402

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default="probe.jsonl")
    ap.add_argument("--vk", type=lambda s: int(s, 0), default=0x13, help="touche surveillée (0x13 = Pause) ; 0 = toutes")
    a = ap.parse_args()
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    u32.SetWindowsHookExW.restype = wintypes.HHOOK
    u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u32.CallNextHookEx.restype = ctypes.c_long
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    f = open(a.out, "w")
    n = [0]

    def proc(code, wparam, lparam):
        if code >= 0 and wparam in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            t, w = time.perf_counter_ns(), wall_ns()
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if a.vk == 0 or kb.vkCode == a.vk:
                f.write(json.dumps({"t_ns": t, "wall_ns": w, "vk": kb.vkCode, "down": wparam in (WM_KEYDOWN, WM_SYSKEYDOWN), "flags": kb.flags}) + "\n")
                n[0] += 1
        return u32.CallNextHookEx(None, code, wparam, lparam)

    cb = HOOKPROC(proc)                                     # référence gardée vivante
    hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, k32.GetModuleHandleW(None), 0)
    if not hook:
        sys.exit("SetWindowsHookEx a échoué")
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x80)     # HIGH_PRIORITY_CLASS
    print(f"sonde prête ({a.seconds:.0f} s), touche vk=0x{a.vk:02x}", flush=True)
    msg = wintypes.MSG()
    tid = k32.GetCurrentThreadId()

    def stopper():                                           # arrête la boucle de messages au bout du temps demandé
        time.sleep(a.seconds)
        u32.PostThreadMessageW(tid, 0x0012, 0, 0)            # WM_QUIT

    import threading
    threading.Thread(target=stopper, daemon=True).start()
    # GetMessage BLOQUANT : le crochet est servi dès l'arrivée de l'évènement (un PeekMessage + sleep ajouterait jusqu'à 15 ms)
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))
    u32.UnhookWindowsHookEx(hook)
    f.close()
    print(f"{n[0]} évènements -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
