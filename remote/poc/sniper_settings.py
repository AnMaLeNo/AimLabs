"""Réglages de sniper_b.py : le schéma décrit à la fenêtre générique (aimlabs.setup) ce qui est réglable, puis les touches choisies deviennent les commandes de l'algo.

  Page « Capture »  (si l'agent le propose)   cadrage de la capture du PC A, aperçu en direct (sniper_setup.CapturePage)
  Page « Touches »                            ENTRÉES : bascule / maintien du tir automatique ; SORTIE : touche de tir injectée par le Pi

    python sniper_b.py ... --setup                  ouvre la fenêtre (valeurs précédentes), Entrée valide et enregistre ~/.aimlabs/sniper_b.json, l'algo tourne ensuite
    python sniper_b.py ... --settings [FICHIER]     applique les valeurs enregistrées, sans fenêtre
    (sans l'un ni l'autre : valeurs par défaut = comportement historique : CAPSLOCK bascule, ALT maintenu, touche de tir G)

Source des touches : le Pi (clavier physique ; seulement avec --fire-via pi:…) ou, sinon, les touches que le JEU publie en mode « remote » (comportement d'origine).
Rien de ce module n'est dans la boucle de décision : `Controls.bind.get(...)` est une lecture d'attribut ; les évènements de touches arrivent par un fil de fond (quelques par seconde).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk", "python"))
import aimlabs.setup as S  # noqa: E402
from aimlabs import keys as K  # noqa: E402

APP = "sniper_b"


def schema(capture: bool, pi: bool) -> dict:
    keys_fields = [
        {"type": "label", "text": "Les touches d'ENTRÉE commandent l'algorithme depuis ton clavier ; la touche de SORTIE est celle que l'algorithme fait appuyer (par le Raspberry Pi). "
                                  "Pour assigner : clique sur la case, puis appuie sur la touche voulue sur ton clavier."},
        {"id": "auto_toggle", "type": "key_in", "label": "Tir automatique : bascule", "modes": ["toggle"],
         "help": "Un appui active le tir automatique, le suivant le désactive.", "default": {"key": "CAPSLOCK", "mode": "toggle"}},
        {"id": "auto_hold", "type": "key_in", "label": "Tir automatique : maintenu", "modes": ["hold"],
         "help": "Le tir automatique est actif tant que cette touche est enfoncée (et seulement alors, sauf si la bascule est active).", "default": {"key": "ALT", "mode": "hold"}},
    ]
    if pi:
        keys_fields.append({"id": "fire_key", "type": "key_out", "label": "Touche de tir", "default": "G",
                            "help": "La touche que l'algorithme fait appuyer. Elle doit être la touche de tir du jeu (option --fire-key, ou bouton « Touche de tir » du menu Échap)."})
    pages = []
    if capture:
        pages.append({"id": "capture", "title": "Capture", "type": "capture"})
    pages.append({"id": "touches", "title": "Touches", "fields": keys_fields})
    return {"setup": 1, "app": APP, "title": "sniper_b : réglages", "pages": pages}


class Controls:
    """Ce que l'algo lit : `bind.get("auto")` / `bind.get("hold")`, `msrc` (touches du jeu à alimenter à chaque image, ou None), `fire_usage` (touche HID de tir ou None)."""

    def __init__(self, bind, msrc, pikeys, fire_usage):
        self.bind, self.msrc, self.pikeys, self.fire_usage = bind, msrc, pikeys, fire_usage

    def close(self):
        if self.pikeys is not None:
            self.pikeys.close()


def phase(c, a, side_net, pi_host=None, pi_port=7780) -> Controls:
    """Réglages (fenêtre ou fichier ou drapeaux), application du cadrage à l'agent, mise en place des touches. À appeler une fois connecté à l'agent, avant la boucle."""
    import sniper_setup as SC

    pikeys = None
    if pi_host and a.keys != "game":
        pikeys = K.PiKeys(pi_host, pi_port)
        if pikeys.start(3.0):
            print(f"sniper_b : flux des touches du Pi {pi_host} établi", flush=True)
        else:
            print(f"sniper_b : ATTENTION, le Pi {pi_host} ne répond pas au flux des touches (btkbd à jour ? secret ?) : touches du jeu à la place", flush=True)
            pikeys.close()
            pikeys = None
    has_ctrl = c.has_ctrl
    sch = S.validate_schema(schema(has_ctrl, pi_host is not None))
    use_saved = a.setup or a.settings is not None
    path = (a.settings or S.values_path(APP)) if use_saved else None
    values = S.load_values(sch, path) if use_saved else S.defaults(sch)

    # --- cadrage de la capture : drapeaux > fichier (--capture-config / valeurs enregistrées) > état de l'agent
    cfg, touched = None, False
    if has_ctrl:
        flags = {"mode": a.capture_mode, "title": a.capture_window, "process": a.capture_process, "monitor": a.capture_monitor, "crop": a.capture_crop}
        if a.capture_window and not a.capture_mode:
            flags["mode"] = "window"
        if a.capture_offset:
            flags["dx"], flags["dy"] = (int(v) for v in a.capture_offset.split(","))
        saved = SC.sanitize(values["capture"]) if use_saved and isinstance(values.get("capture"), dict) else None
        if a.capture_config:
            saved = SC.load_cfg(a.capture_config) or saved
            if SC.load_cfg(a.capture_config) is None:
                print(f"sniper_b : ATTENTION, {a.capture_config} illisible : cadrage de l'agent conservé", flush=True)
        cfg = SC.initial_config(c, saved, flags)
        cfg["resize"] = side_net
        touched = bool(saved) or any(v is not None for v in flags.values()) or (c.hello.get("capture") or {}).get("config", {}).get("resize") != side_net

    if a.setup:
        plugins = {"capture": SC.CapturePage(c, cfg, side_net)} if has_ctrl else {}
        vals = S.run(sch, values=values, keys=pikeys, plugins=plugins)
        if vals is None:
            print("sniper_b : réglages annulés : valeurs précédentes conservées", flush=True)
            if has_ctrl:
                c.ctrl("set", config=cfg)                 # la page a recadré l'agent en direct : on lui rend le cadrage d'avant
        else:
            values = vals
            print(f"sniper_b : réglages validés et enregistrés dans {S.values_path(APP)}", flush=True)
            if has_ctrl:
                cfg, touched = SC.sanitize(vals["capture"]), False        # déjà appliqué par la page
    if has_ctrl and touched:
        r = c.ctrl("set", config=cfg)
        info = r[0] if r else {}
        print(f"sniper_b : capture {cfg['mode']}" + (f" « {cfg['title']} »" if cfg["mode"] == "window" else "") + f", carré {cfg['crop']} px → {side_net}, "
              f"décalage {cfg['dx']:+d},{cfg['dy']:+d}" + ("" if info.get("found", True) else f" — ATTENTION : {info.get('error')}"), flush=True)
    elif not has_ctrl and (a.capture_config or a.capture_window or a.capture_mode):
        print("sniper_b : cet agent n'a pas de canal de réglage de la capture : cadrage ignoré", flush=True)

    # --- touches : sources, liaisons
    msrc = None if pikeys is not None else K.ManualSource()
    bind = K.Bindings(pikeys if pikeys is not None else msrc)
    autostart = os.environ.get("AIMLABS_AUTOSTART") == "1"
    bind.add("auto", values["auto_toggle"], initial=autostart)
    bind.add("hold", values["auto_hold"])
    swallow = sorted({v["key"] for v in (values["auto_toggle"], values["auto_hold"]) if v.get("swallow") and v.get("key")})
    if pikeys is not None:
        pikeys.set(watch=bind.keys(), swallow=swallow, all_keys=False, swallow_all=False)
    fire_usage = K.usage_of(values["fire_key"]) if pi_host and values.get("fire_key") else None
    src = "Pi" if pikeys is not None else "jeu (mode remote)"
    desc = lambda v: f"{v['key']} ({'bascule' if v['mode'] == 'toggle' else 'maintien'})" if v.get("key") else "non assignée"
    print(f"sniper_b : tir automatique : {desc(values['auto_toggle'])} / {desc(values['auto_hold'])} ; touches lues sur : {src}"
          + (f" ; bloquées côté PC : {', '.join(swallow)}" if swallow else "") + (f" ; touche de tir : {values['fire_key']}" if fire_usage else ""), flush=True)
    return Controls(bind, msrc, pikeys, fire_usage)
