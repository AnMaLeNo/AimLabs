"""Touches du clavier physique, vues depuis la machine B (celle de l'algo) : client du flux de touches du Raspberry Pi + « bindings » prêts à l'emploi.

Le clavier du joueur est branché sur le Pi (qui le relaie au PC A en Bluetooth) : il est donc le seul à savoir quelles touches sont enfoncées. Il les envoie à B
(protocole : remote/pi/fire_server.py, « Flux des touches ») ; ce module en fait deux objets simples :

    from aimlabs.keys import PiKeys, Bindings

    keys = PiKeys("192.168.1.101")          # secret : ~/.aimlabs/pi_fire.key ou variable AIMLABS_PI_KEY
    keys.start()
    b = Bindings(keys)
    b.add("auto", {"key": "CAPSLOCK", "mode": "toggle"})       # bascule : chaque appui inverse
    b.add("burst", {"key": "LALT", "mode": "hold"})            # maintien : vrai tant que la touche est enfoncée
    b.add("mark", {"key": "F8", "mode": "press"})              # impulsion : b.pressed("mark") est vrai une fois par appui
    ...
    if b.get("auto") or b.get("burst"):    # lecture : un simple attribut, aucun appel réseau, ~0,1 µs
        ...

Ces valeurs (`{"key", "mode"}`) sont exactement celles que produit un champ « key_in » de la fenêtre de réglage (aimlabs.setup) : le joueur choisit ses touches et
ses modes dans la fenêtre, l'algo les passe à `Bindings.add`. Rien de tout cela n'est sur le chemin chaud : les évènements arrivent dans un fil de fond (quelques par seconde).

Noms de touches (`KEY_NAMES`) : lettres « A »-« Z », chiffres « 0 »-« 9 », « F1 »-« F12 », « SPACE », « ENTER », « ESC », « TAB », « CAPSLOCK », flèches « UP » « DOWN » « LEFT » « RIGHT »,
modificateurs « LCTRL » « RCTRL » « LSHIFT » « RSHIFT » « LALT » « RALT » « LGUI » « RGUI » ; les noms génériques « CTRL », « SHIFT », « ALT », « GUI » désignent les deux côtés.
"""
import hashlib
import hmac
import os
import socket
import struct
import threading
import time
from typing import Callable, Dict, FrozenSet, Iterable, Optional

# ----------------------------------------------------------------------------------------------- noms de touches (usages HID, page « clavier »)
KEY_NAMES: Dict[int, str] = {}
for _i in range(26):
    KEY_NAMES[0x04 + _i] = chr(ord("A") + _i)
for _i, _c in enumerate("1234567890"):
    KEY_NAMES[0x1E + _i] = _c
KEY_NAMES.update({0x28: "ENTER", 0x29: "ESC", 0x2A: "BACKSPACE", 0x2B: "TAB", 0x2C: "SPACE", 0x2D: "MINUS", 0x2E: "EQUAL", 0x2F: "LBRACKET", 0x30: "RBRACKET",
                  0x31: "BACKSLASH", 0x33: "SEMICOLON", 0x34: "APOSTROPHE", 0x35: "GRAVE", 0x36: "COMMA", 0x37: "PERIOD", 0x38: "SLASH", 0x39: "CAPSLOCK"})
for _i in range(12):
    KEY_NAMES[0x3A + _i] = "F%d" % (_i + 1)
KEY_NAMES.update({0x46: "PRINTSCREEN", 0x47: "SCROLLLOCK", 0x48: "PAUSE", 0x49: "INSERT", 0x4A: "HOME", 0x4B: "PAGEUP", 0x4C: "DELETE", 0x4D: "END", 0x4E: "PAGEDOWN",
                  0x4F: "RIGHT", 0x50: "LEFT", 0x51: "DOWN", 0x52: "UP", 0x53: "NUMLOCK", 0x65: "MENU",
                  0xE0: "LCTRL", 0xE1: "LSHIFT", 0xE2: "LALT", 0xE3: "LGUI", 0xE4: "RCTRL", 0xE5: "RSHIFT", 0xE6: "RALT", 0xE7: "RGUI"})
for _i in range(1, 10):
    KEY_NAMES[0x58 + _i] = "KP%d" % _i
KEY_NAMES[0x62] = "KP0"
USAGES: Dict[str, int] = {n: u for u, n in KEY_NAMES.items()}
FAMILY = {"CTRL": ("LCTRL", "RCTRL"), "SHIFT": ("LSHIFT", "RSHIFT"), "ALT": ("LALT", "RALT"), "GUI": ("LGUI", "RGUI")}
MODIFIERS = frozenset(n for u, n in KEY_NAMES.items() if u >= 0xE0)


def usage_of(name: str) -> Optional[int]:
    return USAGES.get(str(name).upper())


def name_of(usage: int) -> str:
    return KEY_NAMES.get(usage, "0x%02X" % usage)


def key_matches(binding_key: str, event_name: str) -> bool:
    """Une touche liée à « ALT » réagit à LALT et RALT ; liée à « LALT », seulement à LALT (un évènement générique « ALT » venu du jeu réagit aux deux)."""
    b, e = str(binding_key).upper(), str(event_name).upper()
    return b == e or e in FAMILY.get(b, ()) or b in FAMILY.get(e, ())


# ----------------------------------------------------------------------------------------------- client du Pi
T_PING, T_PONG, T_FIRE, T_CANCEL, T_ACK, T_KSUB, T_KEYS, T_CAPS = 1, 2, 3, 4, 5, 6, 7, 9
MAGIC = 0xA7
HDR = struct.Struct("<BBI")
P_KEYS = struct.Struct("<QIBB")
P_EV = struct.Struct("<IBB")
MAC_LEN = 8


def load_key(key=None) -> bytes:
    if key:
        return key if isinstance(key, bytes) else bytes.fromhex(key)
    h = os.environ.get("AIMLABS_PI_KEY")
    if not h:
        with open(os.path.expanduser("~/.aimlabs/pi_fire.key")) as f:
            h = f.read().strip()
    return bytes.fromhex(h)


class PiKeys:
    """Abonnement au flux de touches du Pi. Thread-safe ; `held` et les écouteurs sont mis à jour par un fil de fond.

    watch   : touches à suivre (noms) ; `all_keys=True` : toutes (sert à « assigner une touche en l'appuyant », jamais pendant le jeu : confidentialité) ;
    swallow : touches que le Pi ne transmet PAS au PC tant que cet abonnement vit (ex. la touche de bascule du tir auto ne doit pas écrire dans le jeu ; 8 au plus)."""

    def __init__(self, host: str, port: int = 7780, key=None, watch: Iterable[str] = (), swallow: Iterable[str] = (), all_keys: bool = False, swallow_all: bool = False):
        self.addr = (host, port)
        self.key = load_key(key)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.25)
        self._watch, self._swallow, self._all, self._swallow_all = self._usages(watch), self._usages(swallow), all_keys, swallow_all
        self._held: set = set()                  # usages enfoncés (parmi ceux suivis)
        self._last_seq = 0
        self._listeners = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._first = threading.Event()
        self._caps = None
        self._caps_ev = threading.Event()
        self.counters = {"packets": 0, "events": 0, "gaps": 0, "stale": 0, "resync": 0, "bad_mac": 0}
        self._seq = 0

    @staticmethod
    def _usages(names: Iterable[str]) -> set:
        out = set()
        for n in names:
            n = str(n).upper()
            for m in FAMILY.get(n, (n,)):
                u = usage_of(m)
                if u is not None:
                    out.add(u)
        return out

    # ------------------------------------------------------------------ cycle de vie
    def start(self, wait: float = 2.0) -> bool:
        threading.Thread(target=self._rx, daemon=True, name="pikeys-rx").start()
        threading.Thread(target=self._sub_loop, daemon=True, name="pikeys-sub").start()
        return self._first.wait(wait)

    def close(self) -> None:
        """Se désabonne : les touches bloquées sont rendues au PC tout de suite (sinon le Pi attend 5 s sans nouvelles)."""
        self._stop.set()
        self._wake.set()
        for _ in range(3):
            self._send(T_KSUB, bytes([0, 0, 0]))
            time.sleep(0.002)
        try:
            self.sock.close()
        except OSError:
            pass

    def set(self, watch: Optional[Iterable[str]] = None, swallow: Optional[Iterable[str]] = None, all_keys: Optional[bool] = None,
            swallow_all: Optional[bool] = None) -> None:
        """Change ce qui est suivi / avalé (pris en compte tout de suite). `swallow_all` : plus aucune touche ne part vers le PC (assignation d'une touche ; fin automatique 5 s après la disparition de B)."""
        with self._lock:
            if watch is not None:
                self._watch = self._usages(watch)
            if swallow is not None:
                self._swallow = self._usages(swallow)
            if all_keys is not None:
                self._all = all_keys
            if swallow_all is not None:
                self._swallow_all = swallow_all
        self._wake.set()

    # ------------------------------------------------------------------ lecture
    @property
    def held(self) -> FrozenSet[str]:
        return frozenset(name_of(u) for u in list(self._held))

    def add_listener(self, fn: Callable[[str, bool], None]) -> None:
        """`fn(nom, enfoncée)` est appelée, dans le fil de réception, pour chaque appui / relâchement (dans l'ordre, sans doublon, évènements perdus recomposés)."""
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def caps(self, timeout: float = 1.0) -> Optional[dict]:
        """Ce que le Pi accepte : {"usages": touches de tir autorisées (usages HID), "keys_all": bool}. None sans réponse."""
        self._caps_ev.clear()
        self._send(T_CAPS, b"")
        return self._caps if self._caps_ev.wait(timeout) else None

    def allowed_output_keys(self, timeout: float = 1.0) -> Optional[list]:
        c = self.caps(timeout)
        return None if c is None else sorted(name_of(u) for u in c["usages"])

    # ------------------------------------------------------------------ paquets
    def _pack(self, typ: int, payload: bytes) -> bytes:
        self._seq += 1
        body = HDR.pack(MAGIC, typ, self._seq & 0xFFFFFFFF) + payload
        return body + hmac.new(self.key, body, hashlib.sha256).digest()[:MAC_LEN]

    def _send(self, typ: int, payload: bytes) -> None:
        try:
            self.sock.sendto(self._pack(typ, payload), self.addr)
        except OSError:
            pass

    def _sub_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                w, s, a, sa = sorted(self._watch), sorted(self._swallow), self._all, self._swallow_all
            self._send(T_KSUB, bytes([(1 if a else 0) | (2 if sa else 0), len(w)]) + bytes(w) + bytes([len(s)]) + bytes(s))
            self._wake.wait(1.0)
            self._wake.clear()

    def _rx(self) -> None:
        while not self._stop.is_set():
            try:
                d, _ = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(d) < HDR.size + MAC_LEN:
                continue
            body, tag = d[:-MAC_LEN], d[-MAC_LEN:]
            if not hmac.compare_digest(tag, hmac.new(self.key, body, hashlib.sha256).digest()[:MAC_LEN]):
                self.counters["bad_mac"] += 1
                continue
            magic, typ, _seq = HDR.unpack_from(body)
            if magic != MAGIC:
                continue
            p = body[HDR.size:]
            if typ == T_CAPS:
                import json
                self._caps = json.loads(bytes(p))
                self._caps_ev.set()
            elif typ == T_KEYS and len(p) >= P_KEYS.size:
                try:
                    self._on_keys(p)
                except Exception:   # noqa: BLE001  un paquet mal formé ne doit jamais tuer le fil
                    pass

    def _on_keys(self, p: bytes) -> None:
        _t, seq, n_held, n_ev = P_KEYS.unpack_from(p, 0)
        held = set(p[P_KEYS.size:P_KEYS.size + n_held])
        off = P_KEYS.size + n_held
        events = [P_EV.unpack_from(p, off + i * P_EV.size) for i in range(n_ev)]
        self.counters["packets"] += 1
        out = []
        with self._lock:
            first = not self._first.is_set()
            if seq < self._last_seq and self._last_seq - seq < 1 << 30:     # paquet périmé (UDP déséquencé) : un état plus récent est déjà appliqué
                self.counters["stale"] += 1
                return
            if first:                                                      # premier paquet : on prend l'état tel quel, sans rejouer d'historique
                self._last_seq = seq
                self._held = set(held)
            else:
                new = [e for e in events if e[0] > self._last_seq]
                if new and new[0][0] > self._last_seq + 1:
                    self.counters["gaps"] += 1                              # des évènements ont disparu avant ceux-ci : la liste des touches enfoncées corrigera
                for _s, u, down in new:
                    if down and u not in self._held:
                        self._held.add(u)
                        out.append((u, True))
                    elif not down and u in self._held:
                        self._held.discard(u)
                        out.append((u, False))
                self.counters["events"] += len(new)
                if seq >= self._last_seq:
                    self._last_seq = seq
                    mine = self._watch if not self._all else None             # touches pour lesquelles le Pi a la vérité
                    for u in held - self._held:
                        self._held.add(u)
                        out.append((u, True))
                        self.counters["resync"] += 1
                    for u in list(self._held - held):
                        self._held.discard(u)
                        if mine is None or u in mine:                               # (une touche qu'on ne suit plus disparaît sans évènement)
                            out.append((u, False))
                            self.counters["resync"] += 1
        self._first.set()
        for u, down in out:
            name = name_of(u)
            for fn in list(self._listeners):
                try:
                    fn(name, down)
                except Exception:   # noqa: BLE001
                    pass


# ----------------------------------------------------------------------------------------------- bindings (bascule / maintien / impulsion)
MODES = ("toggle", "hold", "press")


class Bindings:
    """Transforme des évènements de touches en états booléens, selon les réglages du joueur. Lecture : `get(id)` (état), `pressed(id)` (impulsion consommée)."""

    def __init__(self, source=None):
        self._b: Dict[str, dict] = {}
        self._src = source
        if source is not None:
            source.add_listener(self.feed)

    def add(self, ident: str, value: Optional[dict], initial: bool = False) -> None:
        """`value` = {"key": nom, "mode": "toggle"|"hold"|"press"} (ou None : non assigné, l'état reste faux)."""
        value = value or {}
        key, mode = value.get("key"), value.get("mode", "toggle")
        if mode not in MODES:
            raise ValueError("mode inconnu %r (attendu : %s)" % (mode, ", ".join(MODES)))
        self._b[ident] = {"key": str(key).upper() if key else None, "mode": mode, "state": bool(initial), "down": set(), "pulse": False}

    def keys(self, modes: Optional[Iterable[str]] = None) -> list:
        """Noms des touches liées (à passer à PiKeys(watch=...))."""
        return sorted({b["key"] for b in self._b.values() if b["key"] and (modes is None or b["mode"] in modes)})

    def feed(self, name: str, down: bool) -> None:
        for b in self._b.values():
            if b["key"] is None or not key_matches(b["key"], name):
                continue
            if down:
                b["down"].add(name)
                if b["mode"] == "toggle":
                    b["state"] = not b["state"]
                elif b["mode"] == "press":
                    b["pulse"] = True
            else:
                b["down"].discard(name)
            if b["mode"] == "hold":
                b["state"] = bool(b["down"])

    def get(self, ident: str) -> bool:
        return self._b[ident]["state"]

    def pressed(self, ident: str) -> bool:
        b = self._b[ident]
        if b["pulse"]:
            b["pulse"] = False
            return True
        return False

    def set_state(self, ident: str, state: bool) -> None:
        self._b[ident]["state"] = bool(state)


class ManualSource:
    """Source d'évènements alimentée à la main (ex. les touches que le JEU publie en mode « remote » : `feed_down(frozenset_de_noms)` à chaque image)."""

    def __init__(self):
        self._listeners = []
        self._down: FrozenSet[str] = frozenset()

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def feed_down(self, down: Iterable[str]) -> None:
        down = frozenset(str(n).upper() for n in down)
        if down == self._down:
            return
        for n in sorted(down - self._down):
            for fn in self._listeners:
                fn(n, True)
        for n in sorted(self._down - down):
            for fn in self._listeners:
                fn(n, False)
        self._down = down
