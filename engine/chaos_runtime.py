#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  CHAOS CALCULATOR  ::  motor de caos clasico  ::  edicion CRT-80
================================================================
Motor/interfaz principal dentro del paquete modular Chaos Calculator.

    python chaos_calculator.py        # en una terminal con entorno grafico

Novedades de esta version:
  - interfaz CRT retro-80s estilo terminal ficticia de laboratorio/hacker
  - barras de progreso tipo monitor fosforo, scanlines y cabeceras de registro
  - menu de OPCIONES: numero de figuras/CIs en Poincare, paso de animacion,
    resolucion de grilla, estilo de matplotlib, etiquetas en LaTeX (mathtext)
  - simbolos matematicos en los graficos via LaTeX (mathtext + sympy.latex)
  - gestor de audio SID via sidplayfp/sidplay; playlist reducida a dos temas C64
  - cierre limpio de reproductores externos al salir del .py
  - configuracion modular limpia, con banco de modulos y panel de detalle
  - animacion de prueba en el logo principal durante el arranque
  - audio SID real mediante sidplayfp; no se genera audio WAV de respaldo

Las figuras se muestran en matplotlib (cierra la ventana para continuar) y se
guardan en los formatos configurados en ./figuras_caos/<sistema>/.

Dependencias: numpy, scipy, sympy, matplotlib. Audio: sidplayfp.exe en media/ para reproducir archivos SID.
"""

import os
import sys
import time
import threading
import subprocess
import shutil
import atexit
import signal
import json
import tempfile
import glob
from dataclasses import dataclass

try:
    import numpy as np
    import sympy as sp
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    from matplotlib.animation import FuncAnimation, PillowWriter
    from scipy.integrate import solve_ivp
except ImportError as exc:                                   # pragma: no cover
    print("Falta una dependencia:", exc)
    print("Instala con:  pip install numpy scipy sympy matplotlib")
    sys.exit(1)


# ======================================================================
#  CONSOLA DE DIAGNOSTICO / LOG EN VIVO
# ======================================================================
# Proyecto compacto: raiz / engine / media / data
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, os.pardir))
MEDIA_DIR = os.path.join(PROJECT_ROOT, "media")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
OUTPUT_DIR = os.path.join(DATA_DIR, "figuras_caos")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LOG_FILE = os.path.join(LOG_DIR, "chaos_debug.log")
AUDIO_PID_FILE = os.path.join(LOG_DIR, "audio.pid")
_DEBUG_MONITOR_PROC = None
_ACTIVE_MUSIC = None
_AUDIO_CHILD_MODE = len(sys.argv) > 1 and sys.argv[1] == "--audio-child"
_GIF_CHILD_MODE = len(sys.argv) > 1 and sys.argv[1] == "--encode-gif"


def _now_stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def debug_log(message):
    """Registra procesos internos para depuracion sin ensuciar la interfaz CRT."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{_now_stamp()}] {message}\n")
    except Exception:
        pass


def _gif_encode_child_main():
    """Codificador GIF aislado.

    Se usa para evitar el bloqueo final de PillowWriter dentro de la interfaz
    principal: la terminal vuelve al menu y el GIF queda escribiendose en
    segundo plano desde fotogramas PNG ya renderizados.
    """
    try:
        if len(sys.argv) < 5:
            return 2
        frame_dir = os.path.abspath(sys.argv[2])
        out_path = os.path.abspath(sys.argv[3])
        fps = max(1, int(float(sys.argv[4])))
        from PIL import Image
        frames = sorted(glob.glob(os.path.join(frame_dir, "frame_*.png")))
        if not frames:
            debug_log(f"gif encode child: no frames in {frame_dir}")
            return 3
        duration = int(round(1000 / fps))
        debug_log(f"gif encode child start out={out_path} frames={len(frames)} fps={fps}")
        images = []
        for fp in frames:
            im = Image.open(fp).convert("P", palette=Image.Palette.ADAPTIVE, colors=128)
            images.append(im.copy())
            im.close()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        images[0].save(out_path, save_all=True, append_images=images[1:],
                       duration=duration, loop=0, optimize=False, disposal=2)
        for im in images:
            try:
                im.close()
            except Exception:
                pass
        try:
            shutil.rmtree(frame_dir, ignore_errors=True)
        except Exception:
            pass
        debug_log(f"gif encode child done out={out_path}")
        return 0
    except Exception as exc:
        debug_log(f"gif encode child error: {exc}")
        return 1


def _monitor_log_main(path):
    """Modo auxiliar: terminal secundaria que muestra el log en vivo."""
    print("=" * 78)
    print(" CHAOS CALCULATOR :: DEBUG MONITOR")
    print(" Copia/pega este registro si aparece un bug.")
    print(" Log:", path)
    print(" Cierra esta ventana para ocultar el monitor; el programa principal sigue.")
    print("=" * 78)
    last_size = 0
    while True:
        try:
            if os.path.exists(path):
                size = os.path.getsize(path)
                if size < last_size:
                    last_size = 0
                if size > last_size:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        chunk = f.read()
                    if chunk:
                        print(chunk, end="", flush=True)
                    last_size = size
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("[monitor error]", exc, flush=True)
        time.sleep(0.35)


def launch_debug_monitor():
    """Abre una segunda terminal con el log en vivo, cuando el SO lo permite."""
    global _DEBUG_MONITOR_PROC
    if os.environ.get("CHAOS_NO_MONITOR", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    if _DEBUG_MONITOR_PROC is not None:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"[{_now_stamp()}] DEBUG MONITOR READY\n")
        script = os.path.abspath(__file__)
        args = [sys.executable, script, "--monitor-log", LOG_FILE]
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            _DEBUG_MONITOR_PROC = subprocess.Popen(args, creationflags=flags)
        else:
            term_cmds = [
                ["x-terminal-emulator", "-e"], ["gnome-terminal", "--"],
                ["konsole", "-e"], ["xterm", "-e"],
            ]
            for prefix in term_cmds:
                exe = shutil.which(prefix[0])
                if exe:
                    _DEBUG_MONITOR_PROC = subprocess.Popen(prefix + args)
                    break
        debug_log("debug monitor spawned")
    except Exception as exc:
        debug_log(f"debug monitor unavailable: {exc}")


def _kill_pid_tree(pid):
    """Termina un PID y sus hijos sin requerir dependencias externas.

    En Windows se usa taskkill /T /F; en POSIX se intenta grupo de proceso
    y luego PID directo. Se usa para limpiar sidplayfp al salir.
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=3)
            return True
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        try:
            if os.name != "nt":
                os.kill(pid, signal.SIGKILL)
                return True
        except Exception:
            pass
    return False



def _audio_parent_alive(pid):
    """True si el proceso padre sigue vivo.

    El modo --audio-child usa esto para cerrar sidplayfp si el
    proceso principal de ChaosCalculator desaparece incluso sin ejecutar atexit.
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            WAIT_TIMEOUT = 0x00000102
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Fallback sin dependencias: tasklist devuelve el PID si existe.
            try:
                r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, text=True, timeout=1.5)
                return str(pid) in (r.stdout or "")
            except Exception:
                return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _audio_child_popen(args):
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  stdin=subprocess.DEVNULL)
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _audio_child_main():
    """Proceso guardián de audio.

    Se lanza como: python ChaosCalculator.py --audio-child <parent_pid> <loop> <json_args>
    Mantiene vivo el reproductor externo y lo mata si el proceso padre muere.
    """
    proc = None
    try:
        parent_pid = int(sys.argv[2])
        loop = str(sys.argv[3]).strip().lower() in ("1", "true", "on", "yes", "s")
        args = json.loads(sys.argv[4])
        if not isinstance(args, list) or not args:
            return 2
        while _audio_parent_alive(parent_pid):
            proc = _audio_child_popen(args)
            try:
                _write_audio_pid_file(proc.pid)
            except Exception:
                pass
            while proc.poll() is None:
                if not _audio_parent_alive(parent_pid):
                    _kill_pid_tree(proc.pid)
                    return 0
                time.sleep(0.25)
            if not loop:
                break
            time.sleep(0.15)
        if proc is not None and proc.poll() is None:
            _kill_pid_tree(proc.pid)
        return 0
    except KeyboardInterrupt:
        if proc is not None:
            _kill_pid_tree(getattr(proc, "pid", 0))
        return 0
    except Exception:
        if proc is not None:
            _kill_pid_tree(getattr(proc, "pid", 0))
        return 1

def _read_audio_pid_file():
    try:
        with open(AUDIO_PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _write_audio_pid_file(pid):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(AUDIO_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except Exception:
        pass


def _remove_audio_pid_file():
    try:
        if os.path.exists(AUDIO_PID_FILE):
            os.remove(AUDIO_PID_FILE)
    except Exception:
        pass


def cleanup_stale_audio_process():
    """Limpia un reproductor externo de una sesion anterior, si quedo vivo."""
    pid = _read_audio_pid_file()
    if pid:
        debug_log(f"cleanup stale audio pid={pid}")
        _kill_pid_tree(pid)
        _remove_audio_pid_file()


def _cleanup_runtime():
    """Handler de salida: detiene reproductores externos y deja log limpio."""
    global _ACTIVE_MUSIC
    try:
        debug_log("main process terminating")
        if _ACTIVE_MUSIC is not None:
            _ACTIVE_MUSIC.stop(force=True)
        else:
            cleanup_stale_audio_process()
    except Exception:
        pass


def _signal_shutdown(signum, frame):
    _cleanup_runtime()
    raise KeyboardInterrupt


if not _AUDIO_CHILD_MODE:
    atexit.register(_cleanup_runtime)
    for _sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, _sig_name):
            try:
                signal.signal(getattr(signal, _sig_name), _signal_shutdown)
            except Exception:
                pass


# ======================================================================
#  CONFIGURACION GLOBAL (editable desde el menu de opciones)
# ======================================================================
CONFIG = {
    "poincare_ic": 6,
    "poincare_crossings": 80,
    "dt": 0.01,
    "ftle_T": 200.0,
    "grid_res": 220,
    "anim_dt": 0.01,
    "anim_frames": 90,
    "anim_fps": 20,
    "mpl_style": "neon8bit",
    "use_latex": True,
    "save_formats": ["svg", "png", "pdf"],
    "ode_T": 80.0,
    "ode_dt_out": 0.03,
    "lyap_dt_out": 0.2,
    "poincare_var": 1,
    "poincare_plot_x": 0,
    "poincare_plot_y": 2,
    "audio_autostart": True,
    "audio_loop": True,
    "audio_track_index": 0,
    "debug_console": False,
    "random_ic_attempts": 250,
    "random_ic_scale": 0.20,
    "ode_rtol": 1e-9,
    "ode_atol": 1e-11,
    "fast_highdim_lyapunov": True,
    "ask_before_save": True,
    "show_after_save": True,
    "logo_animation": True,
    "quality_profile": "normal",
    "randomize_preset_z0": True,
    "random_ic_sanity_check": True,
}

CUSTOM_STYLES = ["neon8bit", "crt_green", "amber_terminal", "blue_matrix"]
STYLES = CUSTOM_STYLES + [
    "dark_background", "default", "classic", "ggplot", "bmh", "fast", "grayscale",
    "Solarize_Light2", "seaborn-v0_8-darkgrid", "seaborn-v0_8-poster",
    "tableau-colorblind10",
]

CONFIG_GROUPS = [
    ("1", "HAMILTONIAN ANALYSIS", "Poincare, FTLE/SALI, potencial y barridos de energia", [
        ("poincare_ic", "Figuras (CIs) en Poincare", "int"),
        ("poincare_crossings", "Cruces por figura", "int"),
        ("dt", "Paso de integracion dt", "float"),
        ("ftle_T", "Tiempo de FTLE / SALI", "float"),
        ("grid_res", "Resolucion de grilla (potencial)", "int"),
    ]),
    ("2", "ODE GENERAL ENGINE", "Sistemas z'=f(z), tolerancias y condiciones iniciales", [
        ("ode_T", "Tiempo de integracion ODE", "float"),
        ("ode_dt_out", "Paso de salida ODE", "float"),
        ("lyap_dt_out", "Paso salida Lyapunov", "float"),
        ("ode_rtol", "Tolerancia relativa ODE", "float"),
        ("ode_atol", "Tolerancia absoluta ODE", "float"),
        ("fast_highdim_lyapunov", "Lyapunov rapido alta dimension", "bool"),
        ("random_ic_attempts", "Intentos z0 aleatoria", "int"),
        ("random_ic_scale", "Escala z0 aleatoria", "float"),
        ("randomize_preset_z0", "Randomizar z0 de presets al cargar", "bool"),
        ("random_ic_sanity_check", "Test corto de estabilidad z0", "bool"),
    ]),
    ("3", "ANIMATION CONTROL", "Duracion numerica, cuadros, FPS y muestreo visual", [
        ("anim_dt", "Paso de tiempo de animacion", "float"),
        ("anim_frames", "Cuadros de animacion", "int"),
        ("anim_fps", "FPS de animacion", "int"),
    ]),
    ("4", "PLOT STYLE / EXPORT", "Estetica Matplotlib, formatos y confirmaciones de salida", [
        ("mpl_style", "Tema Matplotlib global", "choice"),
        ("save_formats", "Formatos de guardado", "formats"),
        ("ask_before_save", "Preguntar antes de guardar", "bool"),
        ("use_latex", "Etiquetas en LaTeX (mathtext)", "bool"),
    ]),
    ("5", "AUDIO / PLAYLIST", "SID, tracker/audio externo y control de loop", [
        ("audio_track_index", "Pista de audio / playlist", "track"),
        ("audio_autostart", "Musica al iniciar", "bool"),
        ("audio_loop", "Loop de pista", "bool"),
    ]),
    ("6", "PERFORMANCE PRESETS", "Perfiles inteligentes de coste computacional", [
        ("quality_profile", "Perfil de calidad/coste", "quality"),
    ]),
    ("7", "INTERFACE / CRT", "Animacion del logo y detalles visuales de la terminal", [
        ("logo_animation", "Animacion de logo al iniciar", "bool"),
    ]),
]

CONFIG_SPEC = [entry for *_head, entries in CONFIG_GROUPS for entry in entries]


def _jsonable_config_value(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable_config_value(x) for x in v]
    return str(v)


def save_runtime_config():
    """Memoria persistente de la calculadora en data/config.json."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {k: _jsonable_config_value(v) for k, v in CONFIG.items()}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        debug_log(f"config saved {CONFIG_FILE}")
    except Exception as exc:
        debug_log(f"config save failed: {exc}")


def load_runtime_config():
    """Carga la configuracion persistente sin romper claves nuevas."""
    try:
        if not os.path.exists(CONFIG_FILE):
            return False
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            for k, v in payload.items():
                if k in CONFIG:
                    CONFIG[k] = v
            debug_log(f"config loaded {CONFIG_FILE}")
            return True
    except Exception as exc:
        debug_log(f"config load failed: {exc}")
    return False


QUALITY_PRESETS = {
    "safe": {"label": "SAFE / RAPIDO", "warning": "bajo coste; ideal para probar sistemas nuevos",
             "values": {"grid_res": 120, "poincare_ic": 3, "poincare_crossings": 40, "ftle_T": 60.0,
                        "ode_T": 35.0, "ode_dt_out": 0.06, "lyap_dt_out": 0.5,
                        "anim_frames": 55, "anim_fps": 15, "random_ic_attempts": 100}},
    "normal": {"label": "NORMAL / EQUILIBRADO", "warning": "calidad razonable para uso diario",
               "values": {"grid_res": 220, "poincare_ic": 6, "poincare_crossings": 80, "ftle_T": 200.0,
                          "ode_T": 80.0, "ode_dt_out": 0.03, "lyap_dt_out": 0.2,
                          "anim_frames": 90, "anim_fps": 20, "random_ic_attempts": 250}},
    "high": {"label": "HIGH / PUBLICACION", "warning": "mayor coste; recomendado para figuras finales",
             "values": {"grid_res": 320, "poincare_ic": 10, "poincare_crossings": 160, "ftle_T": 300.0,
                        "ode_T": 120.0, "ode_dt_out": 0.02, "lyap_dt_out": 0.12,
                        "anim_frames": 140, "anim_fps": 24, "random_ic_attempts": 400}},
    "ultra": {"label": "ULTRA / COSTO ALTO", "warning": "coste alto: puede tardar bastante y trabar equipos modestos",
              "values": {"grid_res": 520, "poincare_ic": 16, "poincare_crossings": 280, "ftle_T": 500.0,
                         "ode_T": 200.0, "ode_dt_out": 0.01, "lyap_dt_out": 0.06,
                         "anim_frames": 220, "anim_fps": 30, "random_ic_attempts": 650}},
}


def apply_quality_profile(name):
    key = str(name).strip().lower()
    if key not in QUALITY_PRESETS:
        return False
    for k, v in QUALITY_PRESETS[key]["values"].items():
        if k in CONFIG:
            CONFIG[k] = v
    CONFIG["quality_profile"] = key
    save_runtime_config()
    debug_log(f"quality profile applied {key}")
    return True


# ======================================================================
#  ESTILO DE TERMINAL (ANSI 8-bit)
# ======================================================================
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"; REV = "\033[7m"
    BLACK = "\033[38;5;16m"
    BRIGHT = "\033[38;5;46m"; GREEN = "\033[38;5;40m"; DGREEN = "\033[38;5;28m"
    PHOS = "\033[38;5;118m"; CRT = "\033[38;5;42m"; GRID = "\033[38;5;22m"
    CYAN = "\033[38;5;48m"; MAG = "\033[38;5;46m"; YEL = "\033[38;5;118m"
    AMBER = "\033[38;5;214m"; GOLD = "\033[38;5;220m"
    WHITE = "\033[38;5;194m"; TEAL = "\033[38;5;35m"; GREY = "\033[38;5;238m"
    RED = "\033[38;5;203m"; ORANGE = "\033[38;5;70m"; PURPLE = "\033[38;5;29m"


def _supports_ansi():
    if os.name == "nt":
        os.system("")
    return True


_ANSI = _supports_ansi()


def col(s, c):
    return f"{c}{s}{C.RESET}" if _ANSI else s


def hl(text, fg=16, bg=46):
    return f"\033[48;5;{bg}m\033[38;5;{fg}m{text}{C.RESET}" if _ANSI else text


def clear():
    if os.name == "nt":
        os.system("cls")
    elif os.environ.get("TERM"):
        os.system("clear")
    else:
        print("\n" * 3)


def typewriter(s, c=C.PHOS, delay=0.0035):
    for ch in s:
        sys.stdout.write(col(ch, c)); sys.stdout.flush(); time.sleep(delay)
    sys.stdout.write("\n")


def _term_width(default=88):
    """Ancho visual CRT fijo.

    En Windows Terminal el ancho dinamico generaba bordes derechos demasiado
    desplazados en ventanas grandes. La interfaz conserva la piel Unicode,
    pero usa un ancho estable. Se puede forzar con CHAOS_UI_WIDTH.
    """
    try:
        env = os.environ.get("CHAOS_UI_WIDTH")
        if env:
            return max(72, min(int(env), 100))
        return max(72, min(int(default), 100))
    except Exception:
        return default


def _crt_rule(w=None, left="╔", mid="═", right="╗", c=C.GREEN):
    w = _term_width() if w is None else w
    return col(left + mid * (w - 2) + right, c)


def _crt_row(text="", w=None, c=C.DGREEN):
    w = _term_width() if w is None else w
    # Regla: borde izquierdo + (w-2) celdas + borde derecho = w columnas.
    # Antes usaba w-4, lo que dejaba el borde derecho dos columnas desplazado
    # respecto de las reglas superior/inferior en Windows Terminal.
    return col("║", C.GREEN) + col(text[:w - 2].ljust(w - 2), c) + col("║", C.GREEN)


_BLK = [
    " ██████╗██╗  ██╗ █████╗  ██████╗ ███████╗",
    "██╔════╝██║  ██║██╔══██╗██╔═══██╗██╔════╝",
    "██║     ███████║███████║██║   ██║███████╗",
    "██║     ██╔══██║██╔══██║██║   ██║╚════██║",
    "╚██████╗██║  ██║██║  ██║╚██████╔╝███████║",
    " ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
]


def _logo_rows(frame=None):
    """Variantes del logo para una animacion CRT mas agresiva.

    La animacion conserva el ancho visual de cada fila, pero simula:
    - ensamble por bloques,
    - interferencia horizontal,
    - duplicacion fantasma,
    - pulso de fosforo saturado.
    """
    if frame is None:
        return list(_BLK)
    f = int(frame) % 24
    rows = list(_BLK)
    blank = " " * len(rows[0])
    # Construccion desde ruido/bloques hacia logo completo.
    if f < 4:
        cut = int(len(rows[0]) * (f + 1) / 4)
        out = []
        for i, r in enumerate(rows):
            noise = ("▓▒░"[(i + f) % 3] * max(0, cut - i * 2))[:cut]
            out.append((noise + blank)[:len(r)])
        return out
    if f in (4, 5):
        return [r.replace("█", "▓").replace("═", "─") for r in rows]
    if f in (6, 10, 15):
        k = (f // 2) % len(rows)
        rows[k] = ("  " + rows[k][:-2])
    if f in (7, 11, 16):
        k = (f // 3) % len(rows)
        rows[k] = rows[k].replace("█", "▒").replace("╗", "╕").replace("╝", "╛")
    if f in (8, 12, 18):
        rows[1] = rows[1].replace("╔", "┌").replace("╗", "┐")
        rows[4] = rows[4].replace("╚", "└").replace("╝", "┘")
    if f in (13, 14):
        rows = [" " + r[:-1] if i % 2 == 0 else r for i, r in enumerate(rows)]
    if f in (19, 20):
        rows = [r.replace("█", "█") for r in rows]
        rows[0] = rows[0].replace(" ", "░", 8)
        rows[5] = rows[5].replace(" ", "░", 6)
    if f == 21:
        rows = [r.replace("█", "▓") for r in rows]
    return rows


def _logo_boot_status(frame, total):
    phases = [
        "PHOSPHOR WARMUP", "SYNCING CRT BEAM", "ASSEMBLING 8-BIT GLYPHS",
        "DECRYPTING PHASE SPACE", "LINKING ODE REGISTER", "PATCHING POINCARE BUS",
        "OPENING MUSIC CHIP", "ACCESS GRANTED"
    ]
    phase = phases[min(len(phases) - 1, int(frame * len(phases) / max(1, total)))]
    w = 38
    frac = min(1.0, (frame + 1) / max(1, total))
    fill = int(frac * w)
    bar = col("█" * fill, C.PHOS) + col("░" * (w - fill), C.GRID)
    return col(f"    {phase:<28}", C.AMBER) + col(" [", C.GRID) + bar + col("]", C.GRID)



def banner(music=None, logo_frame=None):
    clear()
    w = _term_width(88)
    clock = time.strftime("%H:%M:%S")
    print()
    print(" " + _crt_rule(w, c=C.GREEN))
    print(" " + _crt_row(" INDEX 03        *** CHAOS CALCULATOR READY ***        " + clock, w, C.PHOS))
    print(" " + _crt_row(" COMMAND: RUN CLASSICAL CHAOS ENGINE / CRT-80 PHOSPHOR MODE", w, C.CRT))
    print(" " + col("╠" + "═" * (w - 2) + "╣", C.GREEN))
    print(" " + _crt_row(" REGISTER CONTROL     VOICE: DYNAMICAL SYSTEM     MODE: SIMULATION", w, C.DGREEN))
    print(" " + col("╚" + "═" * (w - 2) + "╝", C.GREEN))
    print()
    lf = 22 if logo_frame is None else int(logo_frame)
    for i, row in enumerate(_logo_rows(logo_frame)):
        glow = C.BRIGHT if (lf + i) % 3 else C.PHOS
        print("    " + col(row, glow))
    print("    " + col("CALCULATOR", C.PHOS) + col("  // nonlinear dynamics workstation", C.DGREEN))
    print("    " + col("ACCESS:", C.GRID) + " " + col("GRANTED", C.BRIGHT)
          + col("   DEVICE:", C.GRID) + " " + col("BUBBLE MEMORY", C.PHOS)
          + col("   KIND:", C.GRID) + " " + col("PROGRAM", C.PHOS))
    print("    " + col("NOTE:", C.GRID) + " "
          + col("fictional CRT/cracktro skin; no network or intrusion routines.", C.DGREEN))
    if music is not None:
        est = "N/A" if not music.available() else ("ON" if music.on else "OFF")
        mode = music.mode_label() if hasattr(music, "mode_label") else "SYNTH WAV"
        print("    " + col("SOUND CHIP:", C.GRID) + " " + col(est, C.BRIGHT)
              + col("   TRACK:", C.GRID) + " " + col(mode[:48], C.PHOS))
    print()


def animate_logo_startup(music=None, frames=28, delay=0.035):
    """Animacion de arranque CRT mas marcada.

    Sigue siendo solo terminal/ANSI, sin dependencias nuevas: redibuja el logo
    como si el monitor estuviera sincronizando el haz y reconstruyendo el
    programa desde memoria de burbuja.
    """
    if not CONFIG.get("logo_animation", True):
        banner(music)
        return
    total = max(12, int(frames))
    for f in range(total):
        banner(music, logo_frame=f)
        print(_logo_boot_status(f, total))
        if f % 5 == 2:
            print("    " + col("///// SIGNAL INTERFERENCE /////", C.GRID))
        elif f > total - 5:
            print("    " + col("*** CHAOS CALCULATOR ONLINE ***", C.BRIGHT))
        else:
            print("    " + col("CRT VECTOR FONT CACHE :: LOADING", C.GRID))
        time.sleep(delay)
    # Pulso final: pantalla saturada brevemente antes del menu real.
    for f in range(3):
        banner(music, logo_frame=20 + f)
        print("    " + hl("  ACCESS GRANTED  ::  NONLINEAR WORKSTATION READY  ", fg=16, bg=46))
        time.sleep(0.055)
    # Redibuja el logo estable. Sin esto, el ultimo frame animado puede quedar
    # como logo persistente en los menus siguientes.
    banner(music, logo_frame=None)


def boot():
    pasos = [
        ("MEMCHK", "64K BUBBLE MEMORY OK"),
        ("LOAD", "VERLET.SYS / YOSHIDA4.SYS"),
        ("PATCH", "POINCARE-SECTION VECTOR TABLE"),
        ("TRACE", "FTLE + SALI ANALYZER"),
        ("LINK", "MATPLOTLIB CRT DRIVER"),
        ("OPEN", "DYNAMICAL REGISTER BANK"),
    ]
    width = 34
    for i, (tag, msg) in enumerate(pasos):
        f = (i + 1) / len(pasos); fill = int(f * width)
        bar = col("▓" * fill, C.PHOS) + col("░" * (width - fill), C.GRID)
        sys.stdout.write("\r   " + col(f"{tag:<6}", C.AMBER) + col("▕", C.GRID)
                         + bar + col("▏", C.GRID) + " " + col(msg.ljust(34), C.CRT))
        sys.stdout.flush(); time.sleep(0.12)
    print("\n   " + col("BOOT COMPLETE :: PRESSURE OF PHOSPHOR NOMINAL", C.DGREEN) + "\n")


def render_bar(frac, label="", width=34):
    frac = max(0.0, min(1.0, frac)); fill = int(frac * width)
    cursor = "▓" if frac < 1.0 else "█"
    s = ("\r  " + col(f"{label[:13].upper():<13}", C.AMBER) + col(" ", C.GRID)
         + col("[", C.GRID) + col("█" * fill, C.PHOS)
         + col(cursor if fill < width else "", C.BRIGHT)
         + col("·" * max(0, width - fill - (1 if fill < width else 0)), C.GRID)
         + col("]", C.GRID) + col(f" {int(frac * 100):03d}%", C.PHOS))
    sys.stdout.write(s); sys.stdout.flush()
    if frac >= 1.0:
        sys.stdout.write("\n")


class Bar:
    def __init__(self, total, label=""):
        self.total = max(1, total); self.label = label; self.n = 0
        render_bar(0.0, label)

    def step(self, k=1):
        self.n += k; render_bar(self.n / self.total, self.label)

    def done(self):
        render_bar(1.0, self.label)


def _term_cell(text, width):
    """Texto truncado/padded para celdas CRT. Usa ASCII visible dentro de marcos Unicode."""
    raw = str(text).replace("\t", " ")
    return raw[:width].ljust(width)


def _safe_highlight(text, width, fg=16, bg=118):
    """Highlight sin tocar el borde derecho; evita residuos visuales en Windows Terminal."""
    inner_w = max(0, width - 1)
    return hl(_term_cell(text, inner_w), fg=fg, bg=bg) + col(" ", C.GREEN)


def draw_menu(title, items, footer=None):
    labels = [f"[{k}] {t}" for k, t in items]
    w = max(68, len(title) + 14, max(len(l) for l in labels) + 14)
    code_w = 6
    label_w = w - code_w - 3
    print("  " + col("┌" + "─" * (w - 2) + "┐", C.GREEN))
    title_text = f" {title} // PAGE READY "
    print("  " + col("│", C.GREEN) + col(_term_cell(title_text, w - 2), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┬" + "─" * label_w + "┤", C.GREEN))
    print("  " + col("│", C.GREEN) + col(_term_cell(" CMD", code_w), C.PHOS) + col("│", C.GREEN)
          + col(_term_cell(" EXECUTIVE PROGRAM", label_w), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┼" + "─" * label_w + "┤", C.GREEN))
    for (k, t) in items:
        key = _term_cell(f" {k.upper():<5}", code_w)
        label = _term_cell(" " + t.upper(), label_w)
        print("  " + col("│", C.GREEN) + col(key, C.AMBER) + col("│", C.GREEN)
              + col(label, C.WHITE) + col("│", C.GREEN))
    print("  " + col("└" + "─" * code_w + "┴" + "─" * label_w + "┘", C.GREEN))
    if footer:
        print("  " + col("STATUS: ", C.GRID) + col(footer, C.DGREEN))

def _prompt_prefix():
    return col("\n  C:\\CHAOS\\CALC", C.GREEN) + col("> ", C.PHOS)


def read_choice(valid):
    while True:
        c = input(_prompt_prefix() + col("RUN CODE: ", C.WHITE)).strip().lower()
        if c in valid:
            return c
        print(col("  ACCESS DENIED: UNKNOWN PROGRAM CODE.", C.RED))


def ask(prompt_text, default=None):
    sfx = col(f" [{default}]", C.GREY) if default is not None else ""
    v = input(_prompt_prefix() + col(prompt_text, C.WHITE) + sfx + " ").strip()
    return v if v else (default if default is not None else "")


def pause():
    input(col("\n  [RETURN] CONTINUE", C.GRID))


def ask_yes_no(prompt_text, default=True):
    """Pregunta CRT simple. Devuelve True/False.

    En ejecuciones no interactivas conserva el valor por defecto para evitar
    bloqueos en tests o automatizaciones.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return bool(default)
    d = "s" if default else "n"
    raw = ask(prompt_text + " (s/n):", d)
    return str(raw).strip().lower() not in ("n", "no", "0", "false", "off")


def confirm_save_output(kind, stem):
    if not CONFIG.get("ask_before_save", True):
        return True
    return ask_yes_no(f"guardar {kind} '{stem}'", True)


def confirm_show_output(kind, stem):
    if not CONFIG.get("show_after_save", True):
        return False
    return ask_yes_no(f"mostrar {kind} '{stem}' al finalizar", True)


def output_plan(kind, stem, show=True):
    """Pregunta solo si se guarda. La visualizacion se muestra sin preguntar.

    En animaciones esto evita el bloqueo/confusion de una segunda pregunta antes
    de abrir la ventana. El guardado, si se pide, muestra su propia barra.
    """
    do_save = confirm_save_output(kind, stem)
    do_show = bool(show)
    debug_log(f"output plan kind={kind} stem={stem} save={do_save} show={do_show}")
    return do_save, do_show


def _spawn_background_gif_encoder(frame_dir, out_path, fps):
    """Lanza el codificador GIF externo y vuelve de inmediato al menu."""
    env = os.environ.copy()
    env["CHAOS_AUDIO_AUTOSTART"] = "0"
    args = [sys.executable, os.path.abspath(__file__), "--encode-gif", frame_dir, out_path, str(int(fps))]
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, creationflags=creationflags,
                            startupinfo=startupinfo)
    debug_log(f"gif encoder spawned pid={proc.pid} out={out_path}")
    return proc


def _render_animation_frames(fig, update_func, frame_count, label="render gif"):
    """Renderiza fotogramas PNG temporales con una barra completa.

    Esto reemplaza Animation.save(PillowWriter) para evitar el periodo muerto
    que se producia despues del 100% mientras Pillow cerraba el GIF.
    """
    tmp = tempfile.mkdtemp(prefix="chaos_gif_frames_")
    total = max(1, int(frame_count))
    bar = Bar(total, label)
    for i in range(total):
        try:
            update_func(i)
            fig.canvas.draw()
            fig.savefig(os.path.join(tmp, f"frame_{i:05d}.png"), dpi=110)
        except Exception as exc:
            debug_log(f"render gif frame error i={i}: {exc}")
            raise
        bar.step()
    bar.done()
    return tmp


def queue_gif_save(fig, update_func, frame_count, out_path, fps, label="render gif"):
    """Renderiza frames y deja la codificacion GIF en segundo plano."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    info("renderizando fotogramas para GIF. Al terminar, la codificacion queda en segundo plano.")
    frame_dir = _render_animation_frames(fig, update_func, frame_count, label=label)
    _spawn_background_gif_encoder(frame_dir, os.path.abspath(out_path), fps)
    info("codificacion GIF enviada a segundo plano; puedes seguir usando el menu.")
    print(col("  OUT ", C.AMBER) + col("// ", C.GRID) + col(os.path.abspath(out_path), C.PHOS))
    return os.path.abspath(out_path)


def info(msg):
    print(col("  SYS ", C.AMBER) + col("// ", C.GRID) + col(msg, C.CRT))


def ok(msg):
    print(col("  [ACCESS GRANTED] ", C.BRIGHT) + col(msg, C.WHITE))


def _flash(msg, c=C.YEL, t=1.0):
    print(col("\n  " + msg, c)); time.sleep(t)


# ---- navegacion por teclado (flechas / enter / esc / espacio) ----
def _has_termios():
    try:
        import termios  # noqa: F401
        return True
    except Exception:
        return False


def _interactive():
    try:
        return bool(sys.stdin.isatty()) and (os.name == "nt" or _has_termios())
    except Exception:
        return False


def read_key():
    """Devuelve 'UP','DOWN','LEFT','RIGHT','ENTER','ESC','SPACE' o el caracter."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            return {b"H": "UP", b"P": "DOWN", b"K": "LEFT",
                    b"M": "RIGHT"}.get(msvcrt.getch(), "")
        if ch in (b"\r", b"\n"):
            return "ENTER"
        if ch == b"\x1b":
            return "ESC"
        if ch == b" ":
            return "SPACE"
        try:
            return ch.decode("utf-8", "ignore").lower()
        except Exception:
            return ""
    import termios, tty, select
    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            r, _, _ = select.select([sys.stdin], [], [], 0.0008)
            if not r:
                return "ESC"
            if sys.stdin.read(1) == "[":
                return {"A": "UP", "B": "DOWN", "C": "RIGHT",
                        "D": "LEFT"}.get(sys.stdin.read(1), "")
            return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == " ":
            return "SPACE"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _panel(title, items, idx, footer=None):
    w = max(68, max(len(f"[{k}] {t}") for k, t in items) + 14)
    code_w = 6
    label_w = w - code_w - 3
    print("  " + col("┌" + "─" * (w - 2) + "┐", C.GREEN))
    title_text = f" {title} // PAGE READY "
    print("  " + col("│", C.GREEN) + col(_term_cell(title_text, w - 2), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┬" + "─" * label_w + "┤", C.GREEN))
    print("  " + col("│", C.GREEN) + col(_term_cell(" CODE", code_w), C.PHOS) + col("│", C.GREEN)
          + col(_term_cell(" REGISTER / PROGRAM", label_w), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┼" + "─" * label_w + "┤", C.GREEN))
    for i, (k, t) in enumerate(items):
        code = _term_cell(f" {k.upper():<5}", code_w)
        label = _term_cell(" " + t.upper(), label_w)
        if i == idx:
            print("  " + col("│", C.GREEN) + _safe_highlight(code, code_w) + col("│", C.GREEN)
                  + _safe_highlight(label, label_w) + col("│", C.GREEN))
        else:
            print("  " + col("│", C.GREEN) + col(code, C.AMBER) + col("│", C.GREEN)
                  + col(label, C.DGREEN) + col("│", C.GREEN))
    print("  " + col("└" + "─" * code_w + "┴" + "─" * label_w + "┘", C.GREEN))
    if footer:
        print("  " + col("SUBJECT: ", C.GRID) + col(footer, C.DGREEN))
    print("  " + col(" ↑/↓ SELECT   ENTER RUN   ESC BACK   M MUSIC   N NEXT   B PREV", C.GREY))

def _navigate_typed(title, items, music, header_fn, footer):
    clear(); banner(music)
    if header_fn:
        header_fn()
    draw_menu(title, items, footer)
    return read_choice({k for k, _ in items} | {"m", "n", "b"})


def navigate(title, items, music=None, header_fn=None, footer=None):
    """Menu navegable con flechas. Devuelve la clave elegida o 'ESC'."""
    if not _interactive():
        return _navigate_typed(title, items, music, header_fn, footer)
    keys = [k for k, _ in items]; idx = 0
    while True:
        clear(); banner(music)
        if header_fn:
            header_fn()
        _panel(title, items, idx, footer)
        k = read_key()
        if k == "UP":
            idx = (idx - 1) % len(items)
        elif k == "DOWN":
            idx = (idx + 1) % len(items)
        elif k in ("ENTER", "SPACE", "RIGHT"):
            return keys[idx]
        elif k in ("ESC", "LEFT"):
            return "ESC"
        elif k in keys or k in ("m", "n", "b"):
            return k


# ======================================================================
#  ESTILO DE FIGURAS  +  ETIQUETAS LaTeX
# ======================================================================
def _base_rc(face="#020804", fg="#39ff14", grid="#0b3d1e", edge="#0e7a2f"):
    plt.rcParams.update({
        "figure.facecolor": face, "axes.facecolor": face,
        "savefig.facecolor": face, "savefig.edgecolor": face,
        "text.color": "#d8ffe4", "axes.labelcolor": fg,
        "axes.titlecolor": fg, "xtick.color": fg, "ytick.color": fg,
        "axes.edgecolor": edge, "grid.color": grid, "grid.linestyle": ":",
        "legend.edgecolor": edge, "legend.facecolor": face,
        "font.family": "monospace", "figure.dpi": 120, "savefig.dpi": 170,
        "font.size": 10, "lines.linewidth": 1.55, "svg.fonttype": "none",
        "axes.grid": True,
    })


def _neon8bit_rc():
    _base_rc(face="#020804", fg="#39ff14", grid="#0b3d1e", edge="#0e7a2f")


def _crt_green_rc():
    _base_rc(face="#000000", fg="#00ff66", grid="#063d1e", edge="#00aa44")


def _amber_terminal_rc():
    _base_rc(face="#070300", fg="#ffbf3d", grid="#4a2a00", edge="#bf7f00")


def _blue_matrix_rc():
    _base_rc(face="#020612", fg="#22d3ee", grid="#063449", edge="#0e7490")


def _available_styles():
    found = list(dict.fromkeys(CUSTOM_STYLES + sorted(getattr(plt.style, "available", []))))
    return found


def apply_style():
    plt.rcdefaults()
    s = CONFIG["mpl_style"]
    if s == "neon8bit":
        _neon8bit_rc()
    elif s == "crt_green":
        _crt_green_rc()
    elif s == "amber_terminal":
        _amber_terminal_rc()
    elif s == "blue_matrix":
        _blue_matrix_rc()
    else:
        try:
            plt.style.use(s)
        except Exception:
            _neon8bit_rc()
    plt.rcParams["mathtext.default"] = "regular"
    plt.rcParams["svg.fonttype"] = "none"
    _apply_global_palette()


NEON = ["#22d3ee", "#f472b6", "#a3e635", "#fbbf24", "#818cf8", "#fb7185"]


def _palette_for_style(style_name):
    """Paleta global usada por todas las figuras/animaciones propias.

    Matplotlib cambia rcParams con style.use(), pero muchas curvas del programa
    usan colores explicitos para conservar legibilidad. Esta funcion hace que
    esos colores tambien cambien cuando cambia el tema global.
    """
    name = str(style_name).lower()
    if name in ("crt_green", "neon8bit"):
        return ["#39ff14", "#00ff99", "#b6ff00", "#00cc66", "#ccff66", "#66ffcc"]
    if name == "amber_terminal":
        return ["#ffbf3d", "#ff8c00", "#ffd166", "#ff6b35", "#f4d35e", "#ee964b"]
    if name == "blue_matrix":
        return ["#22d3ee", "#38bdf8", "#818cf8", "#67e8f9", "#a5b4fc", "#0ea5e9"]
    if "dark" in name:
        return ["#22d3ee", "#f472b6", "#a3e635", "#fbbf24", "#818cf8", "#fb7185"]
    if "grayscale" in name:
        return ["#111111", "#333333", "#555555", "#777777", "#999999", "#bbbbbb"]
    return ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]


def _apply_global_palette():
    global NEON
    NEON[:] = _palette_for_style(CONFIG.get("mpl_style", "neon8bit"))


def lab(plain, tex):
    return f"${tex}$" if CONFIG["use_latex"] else plain


# ======================================================================
#  MOTOR
# ======================================================================
class HamiltonianSystem:
    def __init__(self, q, p, H, name="sistema"):
        self.name = name
        self.desc = "Hamiltonian system"
        self.kind = "hamiltonian"
        self.q, self.p = list(q), list(p)
        self.n = len(self.q)
        self.dim = 2 * self.n
        self.state_symbols = self.q + self.p
        self.state_labels = [str(x) for x in self.state_symbols]
        self.default_z0 = None
        self.t_final = None
        self.z_syms = self.q + self.p
        self.H_expr = sp.simplify(sp.sympify(H))
        dHdq = [sp.diff(self.H_expr, qi) for qi in self.q]
        dHdp = [sp.diff(self.H_expr, pi) for pi in self.p]
        f_expr = sp.Matrix(dHdp + [-d for d in dHdq])
        self.f_expr = f_expr
        self._f = self._vec(f_expr)
        self._H = self._scal(self.H_expr)
        self._J = self._mat(f_expr.jacobian(self.z_syms))
        mixed = sp.Matrix([[sp.diff(self.H_expr, qi, pi) for pi in self.p]
                           for qi in self.q])
        self.separable = bool(mixed.is_zero_matrix)
        if self.separable:
            V = self.H_expr.subs({pi: 0 for pi in self.p})
            self.V_expr = sp.simplify(V)
            self.T_expr = sp.simplify(self.H_expr - V)
            self._gV = self._vec(sp.Matrix([sp.diff(V, qi) for qi in self.q]))
            self._gT = self._vec(sp.Matrix([sp.diff(self.T_expr, pi)
                                            for pi in self.p]))

    def _scal(self, e):
        f = sp.lambdify(self.z_syms, e, "numpy")
        return lambda z: float(f(*z))

    def _vec(self, m):
        f = sp.lambdify(self.z_syms, m, "numpy"); k = m.shape[0]
        def w(z):
            o = np.asarray(f(*z), float).reshape(-1)
            return o if o.size == k else np.broadcast_to(o, (k,)).astype(float)
        return w

    def _mat(self, m):
        f = sp.lambdify(self.z_syms, m, "numpy"); r, c = m.shape
        return lambda z: np.asarray(f(*z), float).reshape(r, c)

    def flow(self, z): return self._f(np.asarray(z, float))
    def jacobian(self, z): return self._J(np.asarray(z, float))

    def energy(self, z):
        z = np.asarray(z, float)
        return self._H(z) if z.ndim == 1 else np.array([self._H(zi) for zi in z])

    def grad_V(self, q):
        return self._gV(np.concatenate([np.asarray(q, float), np.zeros(self.n)]))

    def grad_T(self, p):
        return self._gT(np.concatenate([np.zeros(self.n), np.asarray(p, float)]))

    def split(self, z):
        z = np.asarray(z, float); return z[:self.n].copy(), z[self.n:].copy()

    def join(self, q, p):
        return np.concatenate([np.asarray(q, float), np.asarray(p, float)])


class ODESystem:
    """Sistema dinámico autónomo arbitrario z' = f(z).

    A diferencia de HamiltonianSystem, no presupone estructura simpléctica,
    energía conservada ni pares coordenada-momento. Sirve para Lorenz, Rössler,
    N-cuerpos y sistemas definidos manualmente.
    """
    def __init__(self, variables, f_exprs, params=None, name="sistema", desc="ODE", z0=None,
                 t_final=80.0, energy_expr=None, rhs_callable=None, jac_callable=None,
                 ic_bounds=None, validators=None, visual_groups=None, metadata=None):
        self.name = name
        self.desc = desc
        self.kind = "ode"
        self.state_symbols = list(variables)
        self.state_labels = [str(v) for v in self.state_symbols]
        self.n = len(self.state_symbols)
        self.dim = self.n
        self.default_z0 = None if z0 is None else np.asarray(z0, float)
        self.active_z0 = None
        self.t_final = float(t_final)
        self.params = params or {}
        self.ic_bounds = ic_bounds
        self.validators = validators or []
        self.visual_groups = visual_groups or []
        self.metadata = metadata or {}
        self.energy_expr = energy_expr
        self._rhs_callable = rhs_callable
        self._jac_callable = jac_callable
        if rhs_callable is not None:
            self.f_expr = sp.Matrix([sp.Symbol(f"f_{i}") for i in range(self.n)])
            self._energy = energy_expr if callable(energy_expr) else None
            return
        local = {str(v): v for v in self.state_symbols}
        local.update({k: sp.sympify(v) for k, v in self.params.items()})
        exprs = []
        for e in f_exprs:
            ex = sp.sympify(e, locals=local)
            if self.params:
                ex = ex.subs({sp.Symbol(k): sp.sympify(v) for k, v in self.params.items()})
            exprs.append(sp.simplify(ex))
        self.f_expr = sp.Matrix(exprs)
        self._f = sp.lambdify(self.state_symbols, self.f_expr, "numpy")
        self._J_expr = self.f_expr.jacobian(self.state_symbols)
        self._J = sp.lambdify(self.state_symbols, self._J_expr, "numpy")
        if energy_expr is not None and not callable(energy_expr):
            E = sp.sympify(energy_expr, locals=local)
            if self.params:
                E = E.subs({sp.Symbol(k): sp.sympify(v) for k, v in self.params.items()})
            self.energy_expr = sp.simplify(E)
            self._energy = sp.lambdify(self.state_symbols, self.energy_expr, "numpy")
        elif callable(energy_expr):
            self._energy = energy_expr
        else:
            self._energy = None

    def flow(self, z):
        z = np.asarray(z, float)
        if self._rhs_callable is not None:
            return np.asarray(self._rhs_callable(z), float).reshape(-1)
        out = np.asarray(self._f(*z), float).reshape(-1)
        return out if out.size == self.n else np.broadcast_to(out, (self.n,)).astype(float)

    def jacobian(self, z):
        z = np.asarray(z, float)
        if self._jac_callable is not None:
            return np.asarray(self._jac_callable(z), float)
        if hasattr(self, "_J"):
            return np.asarray(self._J(*z), float).reshape(self.n, self.n)
        # Diferencias finitas robustas para sistemas definidos por callable.
        eps = 1e-6 * (1.0 + np.linalg.norm(z))
        J = np.empty((self.n, self.n), float)
        for j in range(self.n):
            dz = np.zeros(self.n); dz[j] = eps
            J[:, j] = (self.flow(z + dz) - self.flow(z - dz)) / (2 * eps)
        return J

    def energy(self, z):
        if self._energy is None:
            return np.nan
        z = np.asarray(z, float)
        if z.ndim == 1:
            if callable(self._energy) and self._rhs_callable is not None:
                return float(self._energy(z))
            return float(self._energy(*z))
        return np.array([self.energy(zi) for zi in z])


@dataclass
class Trajectory:
    t: np.ndarray
    z: np.ndarray
    system: HamiltonianSystem


class Integrator:
    _X1 = 1.0 / (2.0 - 2.0 ** (1.0 / 3.0))
    _X0 = -2.0 ** (1.0 / 3.0) / (2.0 - 2.0 ** (1.0 / 3.0))

    def __init__(self, system): self.sys = system

    def _verlet(self, q, p, dt):
        gV, gT = self.sys.grad_V, self.sys.grad_T
        ph = p - 0.5 * dt * gV(q)
        qn = q + dt * gT(ph)
        return qn, ph - 0.5 * dt * gV(qn)

    def _yoshida(self, q, p, dt):
        for c in (self._X1, self._X0, self._X1):
            q, p = self._verlet(q, p, c * dt)
        return q, p

    def integrate(self, z0, t_final, dt, method="verlet"):
        z0 = np.asarray(z0, float)
        if method in ("verlet", "yoshida4") and getattr(self.sys, "kind", "hamiltonian") == "hamiltonian" and hasattr(self.sys, "split"):
            step = self._verlet if method == "verlet" else self._yoshida
            n = int(round(t_final / dt))
            q, p = self.sys.split(z0)
            t = np.empty(n + 1); Z = np.empty((n + 1, z0.size))
            t[0], Z[0] = 0.0, z0
            for k in range(n):
                q, p = step(q, p, dt)
                t[k + 1] = (k + 1) * dt; Z[k + 1] = self.sys.join(q, p)
            return Trajectory(t, Z, self.sys)
        n = max(2, int(round(t_final / max(dt, 1e-12))))
        te = np.linspace(0, t_final, n + 1)
        sol = solve_ivp(lambda _t, z: self.sys.flow(z), (0, t_final), z0,
                        method="DOP853", t_eval=te,
                        rtol=float(CONFIG.get("ode_rtol", 1e-9)),
                        atol=float(CONFIG.get("ode_atol", 1e-11)))
        if not sol.success:
            raise RuntimeError(sol.message)
        return Trajectory(sol.t, sol.y.T, self.sys)


class PoincareSection:
    def __init__(self, system, sect=1, plot=0, direction=+1):
        self.sys = system; self.s = sect; self.pq = plot; self.dir = direction

    def _henon(self, z):
        s = self.s; h = -z[s]
        g = lambda zz: self.sys.flow(zz) / self.sys.flow(zz)[s]
        k1 = g(z); k2 = g(z + .5 * h * k1); k3 = g(z + .5 * h * k2); k4 = g(z + h * k3)
        return z + (h / 6.) * (k1 + 2 * k2 + 2 * k3 + k4)

    def compute(self, z0, n_crossings=80, dt=0.01, t_max=900.0, progress=None):
        it = Integrator(self.sys); n, s, pq = self.sys.n, self.s, self.pq
        q, p = self.sys.split(np.asarray(z0, float))
        zp = self.sys.join(q, p); pts, t = [], 0.0
        while len(pts) < n_crossings and t < t_max:
            q, p = it._verlet(q, p, dt); z = self.sys.join(q, p); t += dt
            up = (zp[s] < 0 <= z[s]) and self.dir == 1
            dn = (zp[s] > 0 >= z[s]) and self.dir == -1
            if (up or dn) and abs(z[n + s]) > 1e-6:
                zc = self._henon(zp); pts.append((zc[pq], zc[n + pq]))
                if progress and len(pts) % 24 == 0:
                    progress(len(pts) / n_crossings)
            zp = z
        if progress:
            progress(1.0)
        return np.array(pts)


class LyapunovAnalyzer:
    def __init__(self, system): self.sys = system

    def run(self, z0, t_final, dt_out=0.5, seed=0):
        z0 = np.asarray(z0, float); n2 = z0.size
        W0, _ = np.linalg.qr(np.random.default_rng(seed).standard_normal((n2, 2)))
        y0 = np.concatenate([z0, W0.flatten()])
        def rhs(_t, y):
            z = y[:n2]; W = y[n2:].reshape(n2, 2)
            return np.concatenate([self.sys.flow(z),
                                   (self.sys.jacobian(z) @ W).flatten()])
        te = np.arange(0, t_final + dt_out / 2, dt_out)
        sol = solve_ivp(rhs, (0, t_final), y0, method="DOP853",
                        t_eval=te, rtol=1e-9, atol=1e-12)
        t = sol.t; ftle = np.full_like(t, np.nan); sali = np.empty_like(t)
        for j in range(len(t)):
            W = sol.y[n2:, j].reshape(n2, 2)
            n1 = np.linalg.norm(W[:, 0])
            if t[j] > 0:
                ftle[j] = np.log(n1) / t[j]
            u1 = W[:, 0] / n1; u2 = W[:, 1] / np.linalg.norm(W[:, 1])
            sali[j] = min(np.linalg.norm(u1 + u2), np.linalg.norm(u1 - u2))
        return t, ftle, sali


def section_ic(system, E, x, px, sect=1, plot=0):
    q = np.zeros(system.n); p = np.zeros(system.n)
    q[plot] = x; p[plot] = px
    rad = 2.0 * (E - system.energy(system.join(q, p)))
    if rad < 0:
        return None
    p[sect] = np.sqrt(rad); return system.join(q, p)


def _section_ics(system, E, n_ic):
    out = []; side = max(2, int(np.ceil(np.sqrt(n_ic))) + 2); a = np.sqrt(max(E, 1e-6))
    for x in np.linspace(-0.85 * a, 0.85 * a, side):
        for px in np.linspace(-0.7 * np.sqrt(2 * E), 0.7 * np.sqrt(2 * E), side):
            z = section_ic(system, E, x, px)
            if z is not None:
                out.append(z)
            if len(out) >= n_ic:
                return out
    return out


def fractal_dimension(points, n_sizes=20, fmin=0.004, fmax=0.5):
    pts = np.asarray(points, float)
    if pts.ndim != 2 or len(pts) < 100:
        return np.nan
    M = len(pts); mn, mx = pts.min(0), pts.max(0)
    span = float(np.max(mx - mn))
    if span <= 0:
        return np.nan
    eps = span * np.logspace(np.log10(fmax), np.log10(fmin), n_sizes)
    cnt = np.array([len({tuple(r) for r in np.floor((pts - mn) / e).astype(np.int64)})
                    for e in eps], float)
    m = (cnt > 3) & (cnt < 0.5 * M)
    if m.sum() < 4:
        m = cnt > 0
    return float(np.polyfit(np.log(1 / eps[m]), np.log(cnt[m]), 1)[0])


class EnergyScanner:
    def __init__(self, system): self.sys = system

    def scan(self, energies, n_ic=8, T=55.0, sali_floor=1e-6, seed=0, progress=None):
        rng = np.random.default_rng(seed); fr = []; tot = len(energies) * n_ic; done = 0
        for E in energies:
            ch = vd = 0
            for _ in range(n_ic):
                z = None
                for _t in range(30):
                    z = section_ic(self.sys, E, rng.uniform(-np.sqrt(E), np.sqrt(E)),
                                   rng.uniform(-np.sqrt(2 * E), np.sqrt(2 * E)))
                    if z is not None:
                        break
                done += 1
                if progress:
                    progress(done / tot)
                if z is None:
                    continue
                vd += 1
                _, _, s = LyapunovAnalyzer(self.sys).run(z, T, dt_out=1.0)
                ch += int(np.nanmin(s) < sali_floor)
            fr.append(ch / vd if vd else np.nan)
        return np.array(energies, float), np.array(fr, float)


def animate_trajectory_3d(system, z0, coords=("q0", "q1", "t"),
                          out="trayectoria_3d.gif", title="", show=True):
    apply_style()
    out = os.path.abspath(out)
    do_save, do_show = output_plan("animacion GIF", os.path.basename(out), show)
    info("renderizando trayectoria numerica antes de exportar/mostrar...")
    traj = Integrator(system).integrate(z0, 25.0, CONFIG["anim_dt"], "verlet")
    n = system.n; lut = {"t": traj.t}
    for i in range(n):
        lut[f"q{i}"], lut[f"p{i}"] = traj.z[:, i], traj.z[:, n + i]
    A, B, Cc = lut[coords[0]], lut[coords[1]], lut[coords[2]]
    nf = CONFIG["anim_frames"]; st = max(1, len(A) // nf)
    A, B, Cc = A[::st], B[::st], Cc[::st]
    fig = plt.figure(figsize=(6.2, 5.2)); ax = fig.add_subplot(111, projection="3d")
    ax.set_facecolor(plt.rcParams.get("axes.facecolor", "#0b0f14"))
    ax.set_xlabel(coords[0]); ax.set_ylabel(coords[1]); ax.set_zlabel(coords[2])
    pad = lambda a: (a.min() - .05 * np.ptp(a), a.max() + .05 * np.ptp(a))
    ax.set_xlim(*pad(A)); ax.set_ylim(*pad(B)); ax.set_zlim(*pad(Cc))
    if title:
        ax.set_title(title)
    ln, = ax.plot([], [], [], lw=1.4, color=NEON[0])
    hd, = ax.plot([], [], [], "s", color=NEON[1], ms=5)

    def upd(k):
        ln.set_data(A[:k], B[:k]); ln.set_3d_properties(Cc[:k])
        hd.set_data(A[k:k + 1], B[k:k + 1]); hd.set_3d_properties(Cc[k:k + 1])
        ax.view_init(elev=22, azim=0.6 * k); return ln, hd

    info("animacion preparada en memoria.")
    render_bar(1.0, "render anim")
    saved = []
    if do_save:
        debug_log(f"hamiltonian animation background save queue {out} frames={len(A)}")
        saved = [queue_gif_save(fig, upd, len(A), out, CONFIG["anim_fps"], label="render gif")]
    else:
        debug_log(f"hamiltonian animation save skipped {out}")
        info("animacion no guardada por decision del usuario.")
    anim = FuncAnimation(fig, upd, frames=len(A),
                         interval=1000 / CONFIG["anim_fps"], blit=False)
    if do_show:
        info("mostrando animacion. Cierra la ventana para volver a la terminal.")
        plt.show()
    plt.close(fig)
    return saved


# ======================================================================
#  ANALISIS -> figura(s) + SVG, con barra de progreso y LaTeX
# ======================================================================
def _selected_formats():
    allowed = {"svg", "png", "pdf", "eps", "ps"}
    formats = CONFIG.get("save_formats", ["svg"])
    if isinstance(formats, str):
        formats = [x.strip().lower() for x in formats.replace(";", ",").split(",") if x.strip()]
    if "all" in formats:
        formats = ["svg", "png", "pdf", "eps"]
    clean = []
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
        if fmt in allowed and fmt not in clean:
            clean.append(fmt)
    return clean or ["svg"]


def _save_show(fig, outdir, stem, show):
    os.makedirs(outdir, exist_ok=True)
    paths = []
    do_save = confirm_save_output("figura", stem)
    if show:
        info("mostrando figura. Cierra la ventana para volver a la terminal.")
        plt.show()
    if do_save:
        formats = _selected_formats()
        bar = Bar(len(formats), "save fig")
        for fmt in formats:
            path = os.path.join(outdir, f"{stem}.{fmt}")
            debug_log(f"save figure start {path}")
            fig.savefig(path)
            paths.append(path)
            bar.step()
            debug_log(f"save figure done {path}")
        bar.done()
    else:
        debug_log(f"save figure skipped stem={stem}")
        info("salida no guardada por decision del usuario.")
    plt.close(fig)
    return paths


def _fmt_paths(paths):
    if not paths:
        return "no guardado"
    if isinstance(paths, (list, tuple)):
        return ", ".join(os.path.basename(p) for p in paths) if paths else "no guardado"
    return os.path.basename(str(paths))




def _default_z0(sysm):
    if getattr(sysm, "active_z0", None) is not None:
        return np.asarray(sysm.active_z0, float)
    if getattr(sysm, "default_z0", None) is not None:
        return np.asarray(sysm.default_z0, float)
    return np.ones(getattr(sysm, "dim", getattr(sysm, "n", 1)), float) * 0.1


def _base_z0(sysm):
    if getattr(sysm, "default_z0", None) is not None:
        return np.asarray(sysm.default_z0, float)
    return np.ones(getattr(sysm, "dim", getattr(sysm, "n", 1)), float) * 0.1


def _labels(sysm):
    return getattr(sysm, "state_labels", [f"z{i}" for i in range(getattr(sysm, "dim", len(_default_z0(sysm))))])


def validate_initial_condition(sysm, z, strict=True):
    """Filtro general de condiciones iniciales.

    No intenta probar existencia global. Solo descarta condiciones evidentemente
    mal definidas para el campo vectorial actual: dimension incorrecta, NaN/Inf,
    singularidades numericas, colisiones declaradas por validadores del sistema,
    o RHS absurdamente grande.
    """
    try:
        z = np.asarray(z, float).reshape(-1)
    except Exception as exc:
        return False, f"vector no numerico: {exc}"
    dim = int(getattr(sysm, "dim", getattr(sysm, "n", len(z))))
    if z.size != dim:
        return False, f"dimension incorrecta: {z.size} != {dim}"
    if not np.all(np.isfinite(z)):
        return False, "z0 contiene NaN/Inf"
    try:
        f = np.asarray(sysm.flow(z), float).reshape(-1)
        if f.size != dim or not np.all(np.isfinite(f)):
            return False, "f(z0) no es finito"
        if np.linalg.norm(f) > float(getattr(sysm, "max_flow_norm", 1e8)):
            return False, "|f(z0)| demasiado grande; posible singularidad"
    except Exception as exc:
        return False, f"f(z0) fallo: {exc}"
    for validator in getattr(sysm, "validators", []) or []:
        try:
            res = validator(z)
            if isinstance(res, tuple):
                okv, msg = bool(res[0]), str(res[1])
            else:
                okv, msg = bool(res), getattr(validator, "__name__", "validator")
            if not okv:
                return False, msg
        except Exception as exc:
            return False, f"validator fallo: {exc}"
    return True, "OK"


def _short_sanity_check(sysm, z, T=None):
    """Integra muy poco tiempo para rechazar z0 que explotan de inmediato.

    No es una demostracion de estabilidad global. Solo evita condiciones
    iniciales que pasan el test local f(z0), pero generan NaN/Inf, colision o
    drift numerico absurdo en los primeros pasos. Se puede desactivar desde
    SYSTEM CONFIG -> ODE GENERAL ENGINE.
    """
    if not CONFIG.get("random_ic_sanity_check", True):
        return True, "OK"
    try:
        dim = int(getattr(sysm, "dim", getattr(sysm, "n", len(z))))
        base_T = float(getattr(sysm, "t_final", CONFIG.get("ode_T", 80.0)) or CONFIG.get("ode_T", 80.0))
        T = float(T if T is not None else max(0.08, min(1.25, 0.025 * base_T)))
        steps = 8 if dim <= 6 else 5
        te = np.linspace(0.0, T, steps)
        sol = solve_ivp(lambda _t, zz: sysm.flow(zz), (0.0, T), np.asarray(z, float),
                        method="DOP853", t_eval=te,
                        rtol=max(float(CONFIG.get("ode_rtol", 1e-9)), 1e-7),
                        atol=max(float(CONFIG.get("ode_atol", 1e-11)), 1e-9))
        if not sol.success or sol.y.size == 0:
            return False, "sanity integration failed"
        Z = sol.y.T
        if not np.all(np.isfinite(Z)):
            return False, "sanity integration produced NaN/Inf"
        if np.nanmax(np.linalg.norm(Z, axis=1)) > float(getattr(sysm, "max_state_norm", 1e7)):
            return False, "sanity integration diverged"
        # Reaplica validadores en el ultimo punto, util para N-cuerpos.
        okv, msg = validate_initial_condition(sysm, Z[-1], strict=False)
        if not okv:
            return False, "sanity final state: " + msg
        return True, "OK"
    except Exception as exc:
        return False, f"sanity integration error: {exc}"


def _bounded_jitter(base, bounds, rng, scale):
    b = np.asarray(bounds, float)
    lo, hi = b[:, 0], b[:, 1]
    span = np.maximum(hi - lo, 1e-12)
    z = np.asarray(base, float) + rng.normal(0.0, scale * span)
    return np.minimum(np.maximum(z, lo), hi)


def _random_nbody_planar_z0(sysm, rng, scale):
    """Genera z0 razonables para N-cuerpos planares.

    Preserva dos restricciones fisicas numericamente convenientes:
    centro de masa cerca del origen y momento total cero. Esto evita que la
    animacion se vaya de la pantalla por una traslacion trivial y reduce
    singularidades por condiciones iniciales absurdas.
    """
    meta = getattr(sysm, "metadata", {}) or {}
    N = int(meta.get("n_bodies", max(1, getattr(sysm, "dim", 4)//4)))
    masses = np.asarray(meta.get("masses", [1.0] * N), float)
    base = _base_z0(sysm)
    if base.size != 4 * N:
        base = np.zeros(4 * N, float)
        theta = np.linspace(0, 2*np.pi, N, endpoint=False)
        base[:2*N] = np.column_stack([np.cos(theta), np.sin(theta)]).reshape(-1)
    pos0 = base[:2*N].reshape(N, 2)
    vel0 = base[2*N:].reshape(N, 2)
    # 80%: orbita cercana al preset; 20%: configuracion nueva en anillo.
    if rng.random() < 0.80:
        size = max(0.15, float(np.std(pos0)) if np.std(pos0) > 0 else 1.0)
        vsize = max(0.15, float(np.std(vel0)) if np.std(vel0) > 0 else 0.5)
        pos = pos0 + rng.normal(0.0, scale * 1.4 * size, size=(N, 2))
        vel = vel0 + rng.normal(0.0, scale * 1.2 * vsize, size=(N, 2))
    else:
        theta = np.sort(rng.uniform(0, 2*np.pi, N))
        radii = rng.uniform(0.45, 1.35, N)
        pos = np.column_stack([radii*np.cos(theta), radii*np.sin(theta)])
        vel = rng.normal(0.0, 0.65, size=(N, 2))
    M = float(np.sum(masses))
    pos -= np.sum(masses[:, None] * pos, axis=0) / M
    vel -= np.sum(masses[:, None] * vel, axis=0) / M
    z = np.concatenate([pos.reshape(-1), vel.reshape(-1)])
    bounds = getattr(sysm, "ic_bounds", None)
    if bounds is not None:
        b = np.asarray(bounds, float)
        z = np.minimum(np.maximum(z, b[:, 0]), b[:, 1])
    return z


def _random_hamiltonian_z0(sysm, rng, scale=0.20):
    """Genera z0 en una region de energia admisible para sistemas Hamiltonianos 2D."""
    cfg = getattr(sysm, "_cfg", {}) or {}
    energies = list(cfg.get("energies", []))
    if not energies:
        lo = float(cfg.get("E_lo", 0.5)); hi = float(cfg.get("E_hi", max(1.0, lo)))
        energies = [lo, hi]
    lo, hi = float(min(energies)), float(max(energies))
    lo = max(1e-9, lo)
    # Para el motor Hamiltoniano actual los graficos Poincare estan pensados
    # para dos grados de libertad. Usamos la misma seccion y=q2=0, p2>0.
    for _ in range(200):
        E = float(rng.uniform(lo, hi))
        a = np.sqrt(max(E, 1e-9))
        x = rng.uniform(-0.9*a, 0.9*a)
        px = rng.uniform(-0.9*np.sqrt(2*E), 0.9*np.sqrt(2*E))
        z = section_ic(sysm, E, x, px)
        if z is not None:
            okv, msg = validate_initial_condition(sysm, z)
            if okv:
                return z
    # Fallback: perturbacion de base si existe.
    base = _base_z0(sysm)
    return base + rng.normal(0.0, scale * (1.0 + np.abs(base)), size=base.size)


def intelligent_random_initial_condition(sysm, attempts=None, scale=None, seed=None, sanity=True):
    """Elige una condicion inicial aleatoria que tenga sentido para el sistema.

    Estrategia general:
    1. Si el sistema declara una familia especial, usa un generador estructurado
       del motor; por ahora: N-cuerpos planar y Hamiltonianos 2DOF.
    2. Si tiene `ic_bounds`, mezcla muestreo uniforme en la caja con
       perturbaciones alrededor del z0 base.
    3. Si no tiene cotas, perturba el z0 base con una escala relativa.
    4. Todo candidato pasa por `validate_initial_condition` y por una
       integracion corta opcional.

    Devuelve (z0, mensaje). Usa `np.random.default_rng`, el generador moderno
    recomendado en NumPy para muestreo reproducible cuando se entrega semilla.
    """
    attempts = int(attempts if attempts is not None else CONFIG.get("random_ic_attempts", 250))
    scale = float(scale if scale is not None else CONFIG.get("random_ic_scale", 0.20))
    rng = np.random.default_rng(seed)
    base = _base_z0(sysm)
    bounds = getattr(sysm, "ic_bounds", None)
    family = (getattr(sysm, "metadata", {}) or {}).get("family", getattr(sysm, "preset", ""))
    last_msg = "sin candidatos"
    for k in range(max(1, attempts)):
        try:
            if getattr(sysm, "kind", "hamiltonian") == "hamiltonian":
                z = _random_hamiltonian_z0(sysm, rng, scale=scale)
            elif family == "nbody_planar":
                z = _random_nbody_planar_z0(sysm, rng, scale=scale)
            elif bounds is not None:
                b = np.asarray(bounds, float)
                lo, hi = b[:, 0], b[:, 1]
                if rng.random() < 0.65 and base.size == len(lo):
                    z = _bounded_jitter(base, b, rng, scale)
                else:
                    z = rng.uniform(lo, hi)
            else:
                amp = scale * (1.0 + np.abs(base))
                z = base + rng.normal(0.0, amp, size=base.size)
        except Exception as exc:
            last_msg = f"generator error: {exc}"
            continue
        okv, msg = validate_initial_condition(sysm, z)
        if not okv:
            last_msg = msg
            continue
        if sanity:
            ok_s, msg_s = _short_sanity_check(sysm, z)
            if not ok_s:
                last_msg = msg_s
                continue
        return np.asarray(z, float), f"SMART RANDOM OK after {k+1} try"
    raise RuntimeError(f"no se encontro z0 valida tras {attempts} intentos: {last_msg}")


def random_valid_initial_condition(sysm, attempts=None, scale=None):
    # Alias publico usado por la interfaz antigua: ahora llama al generador
    # inteligente del motor.
    return intelligent_random_initial_condition(sysm, attempts=attempts, scale=scale, sanity=True)


def auto_randomize_preset_initial_condition(sysm, cfg=None):
    """Asigna z0 aleatoria al cargar un preset, si la memoria lo permite."""
    if not CONFIG.get("randomize_preset_z0", True):
        return sysm
    cfg = cfg or getattr(sysm, "_cfg", {}) or {}
    if cfg.get("randomize_z0") is False:
        return sysm
    # Evita randomizar sistemas creados manualmente salvo que el usuario lo pida.
    if cfg.get("manual", False):
        return sysm
    try:
        z, msg = intelligent_random_initial_condition(sysm, attempts=max(30, int(CONFIG.get("random_ic_attempts", 250))//2))
        sysm.active_z0 = z
        sysm.randomized_z0 = True
        sysm.ic_randomization_info = msg
        debug_log(f"auto randomized z0 system={getattr(sysm, 'name', '?')} {msg} z0={z[:min(6, len(z))].tolist()}")
    except Exception as exc:
        sysm.randomized_z0 = False
        sysm.ic_randomization_info = f"fallback default: {exc}"
        debug_log(f"auto random z0 failed system={getattr(sysm, 'name', '?')}: {exc}")
    return sysm


def _format_state_line(labels, z, max_items=12):
    chunks = [f"{labels[i]}={z[i]:+.5g}" for i in range(min(len(z), max_items))]
    if len(z) > max_items:
        chunks.append(f"... +{len(z)-max_items}")
    return ", ".join(chunks)


def set_initial_condition_menu(sysm, music=None):
    labels = _labels(sysm)
    while True:
        z = _default_z0(sysm)
        items = [
            ("1", "USE DEFAULT Z0"),
            ("2", "MANUAL Z0 VECTOR"),
            ("3", "SMART RANDOM VALID Z0"),
            ("4", "INSPECT CURRENT Z0"),
            ("0", "BACK"),
        ]
        def header():
            print(col("  INITIAL STATE REGISTER", C.CYAN))
            print(col("    " + _format_state_line(labels, z, 10), C.GREY))
            okv, msg = validate_initial_condition(sysm, z)
            print(col(f"    validator: {msg}", C.PHOS if okv else C.RED))
        ch = navigate("INITIAL CONDITIONS", items, music, header_fn=header,
                      footer="manual or random valid state for next analysis/animation")
        if ch in ("0", "ESC"):
            return
        if ch == "1":
            sysm.active_z0 = None
            ok("z0 restaurada al preset/base")
            time.sleep(0.7)
        elif ch == "2":
            clear(); banner(music); header(); print()
            print(col("  Ingresa el vector completo separado por comas.", C.GREY))
            raw = ask("z0 =", ",".join(f"{x:.12g}" for x in z))
            try:
                cand = _parse_vector(raw, expected=len(labels))
                okv, msg = validate_initial_condition(sysm, cand)
                if okv:
                    sysm.active_z0 = np.asarray(cand, float)
                    ok("z0 manual aceptada")
                else:
                    print(col(f"  z0 rechazada: {msg}", C.RED))
                pause()
            except Exception as exc:
                print(col(f"  error: {exc}", C.RED)); pause()
        elif ch == "3":
            clear(); banner(music); print(col("  SMART RANDOM INITIAL STATE", C.BOLD + C.CYAN))
            try:
                cand, msg = random_valid_initial_condition(sysm)
                sysm.active_z0 = cand
                ok("z0 aleatoria inteligente generada")
                print(col("  " + _format_state_line(labels, cand, 12), C.WHITE))
                print(col(f"  validator: {msg}", C.PHOS))
            except Exception as exc:
                print(col(f"  random z0 fallo: {exc}", C.RED))
            pause()
        elif ch == "4":
            clear(); banner(music); header(); print()
            print(col("  VECTOR COMPLETO:", C.PHOS))
            for i, (lab_i, val) in enumerate(zip(labels, z)):
                print(col(f"    [{i:02d}] {lab_i:<8} = {val:+.12g}", C.WHITE))
            pause()


def integrate_generic(sysm, z0=None, T=None, dt=None):
    z0 = _default_z0(sysm) if z0 is None else np.asarray(z0, float)
    okv, msg = validate_initial_condition(sysm, z0)
    if not okv:
        raise ValueError(f"condicion inicial no valida: {msg}")
    T = float(T if T is not None else getattr(sysm, "t_final", CONFIG.get("ode_T", 80.0)))
    dt = float(dt if dt is not None else CONFIG.get("ode_dt_out", 0.03))
    debug_log(f"integrate_generic start system={sysm.name} dim={len(z0)} T={T} dt_out={dt} z0={z0.tolist()[:6]}")
    traj = Integrator(sysm).integrate(z0, T, dt, method="DOP853")
    debug_log(f"integrate_generic done system={sysm.name} samples={len(traj.t)}")
    return traj


def poincare_generic(sysm, z0=None, section_var=None, plot_x=None, plot_y=None, direction=+1,
                     T=None, dt=None):
    traj = integrate_generic(sysm, z0, T, dt)
    Z, t = traj.z, traj.t
    dim = Z.shape[1]
    section_var = int(section_var if section_var is not None else CONFIG.get("poincare_var", 1)) % dim
    plot_x = int(plot_x if plot_x is not None else CONFIG.get("poincare_plot_x", 0)) % dim
    plot_y = int(plot_y if plot_y is not None else CONFIG.get("poincare_plot_y", min(2, dim - 1))) % dim
    vals = Z[:, section_var]
    pts = []
    for k in range(1, len(vals)):
        up = vals[k-1] < 0 <= vals[k]
        dn = vals[k-1] > 0 >= vals[k]
        if (direction > 0 and up) or (direction < 0 and dn) or (direction == 0 and (up or dn)):
            den = vals[k] - vals[k-1]
            a = 0.0 if abs(den) < 1e-14 else -vals[k-1] / den
            zc = Z[k-1] + a * (Z[k] - Z[k-1])
            pts.append((zc[plot_x], zc[plot_y]))
    return np.asarray(pts), traj


def _energy_stats(sysm, traj):
    try:
        E = sysm.energy(traj.z)
        if np.all(np.isfinite(E)):
            E0 = float(E[0]); drift = float(np.max(np.abs(E - E0)))
            return E, E0, drift
    except Exception:
        pass
    return None, np.nan, np.nan


def fig_generic_timeseries(sysm, outdir, show=True):
    apply_style()
    traj = integrate_generic(sysm)
    labels = _labels(sysm)
    fig, ax = plt.subplots(figsize=(9.0, 4.8))
    max_lines = min(traj.z.shape[1], 8)
    for i in range(max_lines):
        ax.plot(traj.t, traj.z[:, i], lw=1.0, label=labels[i])
    ax.set_xlabel("t")
    ax.set_ylabel("state variables")
    ax.set_title(f"{sysm.name}: time series")
    ax.legend(ncol=2, fontsize=8)
    E, E0, drift = _energy_stats(sysm, traj)
    if np.isfinite(drift):
        ax.text(0.02, 0.96, f"E0={E0:+.6g}\nmax |ΔE|={drift:.2e}", transform=ax.transAxes,
                va="top", ha="left", fontsize=8,
                bbox=dict(boxstyle="round", facecolor="#020804", edgecolor="#39ff14", alpha=0.75))
    fig.tight_layout()
    return _save_show(fig, outdir, "ode_series", show)


def fig_generic_phase(sysm, outdir, show=True):
    apply_style()
    traj = integrate_generic(sysm)
    labels = _labels(sysm)
    dim = traj.z.shape[1]
    fig = plt.figure(figsize=(7.2, 5.8))
    if dim >= 3:
        ax = fig.add_subplot(111, projection="3d")
        ax.plot(traj.z[:, 0], traj.z[:, 1], traj.z[:, 2], lw=0.8, color=NEON[0])
        ax.scatter(traj.z[0, 0], traj.z[0, 1], traj.z[0, 2], s=40, marker="s", color=NEON[2], label="start")
        ax.scatter(traj.z[-1, 0], traj.z[-1, 1], traj.z[-1, 2], s=40, marker="o", color=NEON[1], label="end")
        ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1]); ax.set_zlabel(labels[2])
    else:
        ax = fig.add_subplot(111)
        x = traj.z[:, 0]; y = traj.z[:, 1] if dim > 1 else traj.t
        ax.plot(x, y, lw=0.8, color=NEON[0])
        ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1] if dim > 1 else "t")
    ax.set_title(f"{sysm.name}: phase projection")
    try:
        ax.legend()
    except Exception:
        pass
    fig.tight_layout()
    return _save_show(fig, outdir, "ode_phase", show)


def fig_generic_poincare(sysm, outdir, show=True):
    apply_style()
    pts, traj = poincare_generic(sysm)
    labels = _labels(sysm)
    dim = traj.z.shape[1]
    sv = int(CONFIG.get("poincare_var", 1)) % dim
    px = int(CONFIG.get("poincare_plot_x", 0)) % dim
    py = int(CONFIG.get("poincare_plot_y", min(2, dim - 1))) % dim
    fig, ax = plt.subplots(figsize=(6.2, 5.2))
    if len(pts):
        ax.scatter(pts[:, 0], pts[:, 1], s=4, marker="s", color=NEON[1], linewidths=0)
    ax.set_xlabel(labels[px]); ax.set_ylabel(labels[py])
    ax.set_title(f"{sysm.name}: Poincare {labels[sv]}=0")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    return _save_show(fig, outdir, "ode_poincare", show)


def fast_lyapunov_two_traj(sysm, z0=None, T=None, segment_dt=0.08, delta0=1e-8, seed=0):
    """Estimador rapido de Lyapunov por dos trayectorias.

    Es menos fino que integrar las ecuaciones variacionales, pero evita bloqueos
    en sistemas de dimension alta mientras se esta testeando el programa.
    """
    z = _default_z0(sysm) if z0 is None else np.asarray(z0, float)
    rng = np.random.default_rng(seed)
    d = rng.standard_normal(z.size)
    d = delta0 * d / (np.linalg.norm(d) + 1e-30)
    zp = z + d
    T = float(T if T is not None else min(getattr(sysm, "t_final", CONFIG.get("ode_T", 80.0)), 8.0))
    nseg = max(4, int(np.ceil(T / segment_dt)))
    tvals = [0.0]
    lvals = [np.nan]
    s = 0.0
    debug_log(f"fast Lyapunov start system={sysm.name} dim={z.size} T={T} segment={segment_dt}")
    for k in range(1, nseg + 1):
        h = min(segment_dt, T - (k - 1) * segment_dt)
        if h <= 0:
            break
        tb = integrate_generic(sysm, z0=z, T=h, dt=max(h, 1e-4))
        tp = integrate_generic(sysm, z0=zp, T=h, dt=max(h, 1e-4))
        z = tb.z[-1]
        zp = tp.z[-1]
        d = zp - z
        nd = float(np.linalg.norm(d))
        if not np.isfinite(nd) or nd <= 0:
            break
        s += np.log(nd / delta0)
        zp = z + delta0 * d / nd
        tt = k * segment_dt
        tvals.append(tt)
        lvals.append(s / max(tt, 1e-12))
    debug_log(f"fast Lyapunov done samples={len(tvals)} final={lvals[-1] if lvals else np.nan}")
    return np.asarray(tvals), np.asarray(lvals)


def fig_generic_lyapunov(sysm, outdir, show=True):
    apply_style()
    z0 = _default_z0(sysm)
    T = float(getattr(sysm, "t_final", CONFIG.get("ode_T", 80.0)))
    dt = float(CONFIG.get("lyap_dt_out", 0.2))
    use_fast = bool(CONFIG.get("fast_highdim_lyapunov", True)) and len(z0) > 8
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    if use_fast:
        bar = Bar(1, "lyap fast")
        t, f = fast_lyapunov_two_traj(sysm, z0=z0, T=min(T, 8.0), segment_dt=max(0.04, dt))
        bar.done()
        a1.plot(t, f, color=NEON[0])
        a1.axhline(0, color="#5fd1b0", lw=0.7, ls=":")
        a1.set_xlabel("t"); a1.set_ylabel(r"$\lambda_1(t)$"); a1.set_title("FTLE rapido")
        a2.axis("off")
        a2.text(0.03, 0.92,
                "SALI desactivado en modo rapido\npara sistemas de alta dimension.\n\n"
                "Usa este panel como diagnostico preliminar;\n"
                "baja three_body_dt o desactiva\nfast_highdim_lyapunov para un calculo mas pesado.",
                transform=a2.transAxes, va="top", ha="left", fontsize=9,
                bbox=dict(boxstyle="round", facecolor="#020804", edgecolor="#39ff14", alpha=0.82))
        sali_min = np.nan
    else:
        bar = Bar(1, "lyap ode")
        debug_log(f"variational Lyapunov start system={sysm.name} dim={len(z0)} T={T} dt_out={dt}")
        t, f, sali = LyapunovAnalyzer(sysm).run(z0, T, dt_out=dt)
        debug_log(f"variational Lyapunov done final={f[-1]} sali_min={np.nanmin(sali)}")
        bar.done()
        a1.plot(t, f, color=NEON[0])
        a1.axhline(0, color="#5fd1b0", lw=0.7, ls=":")
        a1.set_xlabel("t"); a1.set_ylabel(r"$\lambda_1(t)$"); a1.set_title("FTLE")
        a2.semilogy(t, sali, color=NEON[1])
        a2.set_xlabel("t"); a2.set_ylabel("SALI"); a2.set_title("SALI")
        sali_min = float(np.nanmin(sali))
    fig.suptitle(f"{sysm.name}: variational indicators")
    fig.tight_layout()
    paths = _save_show(fig, outdir, "ode_ftle_sali", show)
    return paths, (float(f[-1]), sali_min)



def _group_coordinates_from_traj(traj, group):
    """Devuelve coordenadas 3D para un grupo visual genérico.

    group puede traer indices=(ix,iy,iz) o indices=(ix,iy) con z_mode="time"/"zero".
    Esto permite visualizar N-cuerpos, osciladores acoplados u otras ODEs sin
    crear animadores específicos para cada preset.
    """
    idx = list(group.get("indices", []))
    Z = traj.z
    if len(idx) >= 3:
        return Z[:, idx[0]], Z[:, idx[1]], Z[:, idx[2]]
    if len(idx) == 2:
        zmode = group.get("z_mode", "time")
        Cc = traj.t if zmode == "time" else np.zeros_like(traj.t)
        return Z[:, idx[0]], Z[:, idx[1]], Cc
    if len(idx) == 1:
        return Z[:, idx[0]], traj.t, np.zeros_like(traj.t)
    return Z[:, 0], traj.t, np.zeros_like(traj.t)


def _min_group_distance(traj, groups, k):
    pts = []
    for g in groups:
        A, B, Cc = _group_coordinates_from_traj(traj, g)
        pts.append(np.array([A[k], B[k], Cc[k]], float))
    if len(pts) < 2:
        return np.nan
    dmin = np.inf
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            dmin = min(dmin, float(np.linalg.norm(pts[i] - pts[j])))
    return dmin


def fig_generic_phase(sysm, outdir, show=True):
    apply_style()
    traj = integrate_generic(sysm)
    labels = _labels(sysm)
    groups = getattr(sysm, "visual_groups", []) or []
    dim = traj.z.shape[1]
    fig = plt.figure(figsize=(7.4, 5.9))
    ax = fig.add_subplot(111, projection="3d") if dim >= 3 or groups else fig.add_subplot(111)
    if groups:
        for i, g in enumerate(groups):
            A, B, Cc = _group_coordinates_from_traj(traj, g)
            ax.plot(A, B, Cc, lw=0.9, color=NEON[i % len(NEON)], label=g.get("label", f"body {i+1}"))
            ax.scatter(A[0], B[0], Cc[0], s=28, marker="s", color=NEON[i % len(NEON)])
            ax.scatter(A[-1], B[-1], Cc[-1], s=28, marker="o", color=NEON[i % len(NEON)])
        ax.set_xlabel(getattr(sysm, "projection_labels", ("x", "y", "t"))[0])
        ax.set_ylabel(getattr(sysm, "projection_labels", ("x", "y", "t"))[1])
        ax.set_zlabel(getattr(sysm, "projection_labels", ("x", "y", "t"))[2])
    elif dim >= 3:
        ax.plot(traj.z[:, 0], traj.z[:, 1], traj.z[:, 2], lw=0.8, color=NEON[0])
        ax.scatter(traj.z[0, 0], traj.z[0, 1], traj.z[0, 2], s=40, marker="s", color=NEON[2], label="start")
        ax.scatter(traj.z[-1, 0], traj.z[-1, 1], traj.z[-1, 2], s=40, marker="o", color=NEON[1], label="end")
        ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1]); ax.set_zlabel(labels[2])
    else:
        x = traj.z[:, 0]; y = traj.z[:, 1] if dim > 1 else traj.t
        ax.plot(x, y, lw=0.8, color=NEON[0])
        ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1] if dim > 1 else "t")
    ax.set_title(f"{sysm.name}: phase projection")
    try:
        ax.legend(fontsize=8)
    except Exception:
        pass
    fig.tight_layout()
    return _save_show(fig, outdir, "ode_phase", show)


def animate_generic(sysm, outdir, show=True):
    apply_style()
    debug_log(f"animation start system={sysm.name}")
    out = os.path.join(outdir, "ode_animation.gif")
    os.makedirs(outdir, exist_ok=True)
    do_save, do_show = output_plan("animacion GIF", os.path.basename(out), show)
    info("renderizando trayectoria ODE antes de exportar/mostrar...")
    traj = integrate_generic(sysm)
    labels = _labels(sysm)
    groups = getattr(sysm, "visual_groups", []) or []
    nf = int(CONFIG.get("anim_frames", 90))
    st = max(1, len(traj.t) // max(2, nf))
    idxs = np.arange(0, len(traj.t), st)[:nf]
    E, E0, drift = _energy_stats(sysm, traj)

    fig = plt.figure(figsize=(7.0, 6.0))
    ax = fig.add_subplot(111, projection="3d")
    ax.set_facecolor(plt.rcParams.get("axes.facecolor", "#0b0f14"))

    if groups:
        coords = [_group_coordinates_from_traj(traj, g) for g in groups]
        allA = np.concatenate([c[0] for c in coords]); allB = np.concatenate([c[1] for c in coords]); allC = np.concatenate([c[2] for c in coords])
        pad = lambda a: (float(np.min(a) - 0.08 * max(np.ptp(a), 1e-9)), float(np.max(a) + 0.08 * max(np.ptp(a), 1e-9)))
        ax.set_xlim(*pad(allA)); ax.set_ylim(*pad(allB)); ax.set_zlim(*pad(allC))
        plabs = getattr(sysm, "projection_labels", ("x", "y", "t"))
        ax.set_xlabel(plabs[0]); ax.set_ylabel(plabs[1]); ax.set_zlabel(plabs[2])
        ax.set_title(f"{sysm.name}: generic multi-object 3D trace")
        trails = [ax.plot([], [], [], lw=1.0, color=NEON[i % len(NEON)], label=g.get("label", f"obj {i+1}"))[0]
                  for i, g in enumerate(groups)]
        heads = [ax.plot([], [], [], "o", ms=6, color=NEON[i % len(NEON)])[0] for i in range(len(groups))]
        ax.legend(fontsize=8, loc="upper right")
        txt = ax.text2D(0.02, 0.98, "", transform=ax.transAxes, va="top", ha="left", fontsize=8,
                        bbox=dict(boxstyle="round", facecolor="#020804", edgecolor="#39ff14", alpha=0.80))
        trace_len = int(getattr(sysm, "animation_trace", 350))
        def upd(frame_i):
            k = int(idxs[frame_i]); lo = max(0, k - trace_len)
            for i, (A, B, Cc) in enumerate(coords):
                trails[i].set_data(A[lo:k+1], B[lo:k+1]); trails[i].set_3d_properties(Cc[lo:k+1])
                heads[i].set_data([A[k]], [B[k]]); heads[i].set_3d_properties([Cc[k]])
            ed = "" if not np.isfinite(E0) else f"\nE={E[k]:+.6f}  drift={drift:.1e}"
            dmin = _min_group_distance(traj, groups, k)
            dm = "" if not np.isfinite(dmin) else f"\nd_min={dmin:.5g}"
            ztxt = _format_state_line(labels, traj.z[k], 5)
            txt.set_text(f"t={traj.t[k]:.4f}\n|z|={np.linalg.norm(traj.z[k]):.4g}{dm}{ed}\n{ztxt}")
            ax.view_init(elev=24, azim=0.55 * frame_i)
            return trails + heads + [txt]
    else:
        dim = traj.z.shape[1]
        A = traj.z[:, 0]
        B = traj.z[:, 1] if dim > 1 else traj.t
        Cc = traj.z[:, 2] if dim > 2 else traj.t
        pad = lambda a: (float(np.min(a) - .05*np.ptp(a)), float(np.max(a) + .05*np.ptp(a)))
        ax.set_xlim(*pad(A)); ax.set_ylim(*pad(B)); ax.set_zlim(*pad(Cc))
        ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1] if dim > 1 else "t"); ax.set_zlabel(labels[2] if dim > 2 else "t")
        ax.set_title(f"{sysm.name}: generic 3D orbit trace")
        ln, = ax.plot([], [], [], lw=1.0, color=NEON[0])
        hd, = ax.plot([], [], [], "s", color=NEON[1], ms=5)
        txt = ax.text2D(0.02, 0.98, "", transform=ax.transAxes, va="top", fontsize=8,
                        bbox=dict(boxstyle="round", facecolor="#020804", edgecolor="#39ff14", alpha=0.80))
        def upd(frame_i):
            k = int(idxs[frame_i])
            ln.set_data(A[:k+1], B[:k+1]); ln.set_3d_properties(Cc[:k+1])
            hd.set_data([A[k]], [B[k]]); hd.set_3d_properties([Cc[k]])
            ed = "" if not np.isfinite(E0) else f"\nE={E[k]:+.6f}  drift={drift:.1e}"
            txt.set_text(f"t={traj.t[k]:.3f}\nstate norm={np.linalg.norm(traj.z[k]):.4g}{ed}\n" + _format_state_line(labels, traj.z[k], 5))
            ax.view_init(elev=22, azim=0.7 * frame_i)
            return ln, hd, txt

    info("animacion preparada en memoria.")
    render_bar(1.0, "render anim")
    saved = []
    if do_save:
        debug_log(f"animation background save queue {out} frames={len(idxs)}")
        saved = [queue_gif_save(fig, upd, len(idxs), out, int(CONFIG.get("anim_fps", 20)), label="render gif")]
    else:
        debug_log(f"animation save skipped {out}")
        info("animacion no guardada por decision del usuario.")
    anim = FuncAnimation(fig, upd, frames=len(idxs), interval=1000 / CONFIG.get("anim_fps", 20), blit=False)
    if do_show:
        info("mostrando animacion. Cierra la ventana para volver a la terminal.")
        plt.show()
    plt.close(fig)
    return saved

def fig_potential(sysm, energies, outdir, show=True):
    apply_style()
    res = int(CONFIG["grid_res"]); a = 1.3 * np.sqrt(max(energies))
    g = np.linspace(-a, a, res); X, Y = np.meshgrid(g, g); Z = np.empty_like(X)
    bar = Bar(res, "potencial")
    for i in range(res):
        for j in range(res):
            Z[i, j] = sysm.energy(np.array([X[i, j], Y[i, j], 0., 0.]))
        bar.step()
    bar.done()
    fig, ax = plt.subplots(figsize=(5.8, 4.8))
    cs = ax.contourf(X, Y, Z, levels=24, cmap="magma")
    ax.contour(X, Y, Z, levels=list(energies), colors="#22d3ee", linewidths=0.9)
    ax.set_aspect("equal", "box")
    ax.set_xlabel(lab(str(sysm.q[0]), sp.latex(sysm.q[0])))
    ax.set_ylabel(lab(str(sysm.q[1]), sp.latex(sysm.q[1])))
    ax.set_title(lab(f"{sysm.name}: potencial",
                     rf"{sysm.name}:\ V({sp.latex(sysm.q[0])},{sp.latex(sysm.q[1])})"))
    fig.colorbar(cs, ax=ax, label=lab("V", "V"))
    fig.tight_layout()
    return _save_show(fig, outdir, "potencial", show)


def fig_poincare(sysm, energies, outdir, show=True):
    apply_style()
    ic, cr, dt = CONFIG["poincare_ic"], CONFIG["poincare_crossings"], CONFIG["dt"]
    poin = PoincareSection(sysm, 1, 0, +1)
    fig, axes = plt.subplots(1, len(energies),
                             figsize=(4.3 * len(energies), 4.1), squeeze=False)
    bar = Bar(len(energies) * ic, "poincare")
    px_lab = lab("p_" + str(sysm.q[0]), sp.latex(sysm.p[0]))
    for ax, E in zip(axes.flat, energies):
        for j, z0 in enumerate(_section_ics(sysm, E, ic)):
            pts = poin.compute(z0, cr, dt, t_max=900)
            if len(pts):
                ax.scatter(pts[:, 0], pts[:, 1], s=2.2, alpha=0.7, marker="s",
                           color=NEON[j % len(NEON)], linewidths=0)
            bar.step()
        ax.set_title(lab(f"E = {E:g}", rf"E = {E:g}"))
        ax.set_xlabel(lab(str(sysm.q[0]), sp.latex(sysm.q[0]))); ax.set_ylabel(px_lab)
        ax.grid(True, alpha=0.25, lw=0.35)
    bar.done()
    fig.suptitle(lab(f"{sysm.name}: secciones de Poincare",
                     rf"{sysm.name}:\ secciones\ de\ Poincare"), color="#7CFFC4")
    fig.tight_layout()
    return _save_show(fig, outdir, "poincare", show)


def fig_ftle_sali(sysm, E_lo, E_hi, outdir, show=True):
    apply_style()
    T = CONFIG["ftle_T"]
    z_lo = section_ic(sysm, E_lo, 0.25 * np.sqrt(E_lo), 0.0)
    z_hi = section_ic(sysm, E_hi, -0.30 * np.sqrt(E_hi), 0.18 * np.sqrt(2 * E_hi))
    L = LyapunovAnalyzer(sysm); bar = Bar(2, "ftle/sali")
    t1, f1, s1 = L.run(z_lo, T); bar.step()
    t2, f2, s2 = L.run(z_hi, T); bar.done()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.1))
    a1.plot(t1, f1, color=NEON[0], label=lab(f"E={E_lo:g}", rf"E={E_lo:g}"))
    a1.plot(t2, f2, color=NEON[1], label=lab(f"E={E_hi:g}", rf"E={E_hi:g}"))
    a1.axhline(0, color="#5fd1b0", lw=0.7, ls=":")
    a1.set_xlabel(lab("t", "t")); a1.set_ylabel(lab("lambda_1(t)", r"\lambda_1(t)"))
    a1.set_title(lab("FTLE corriente", r"FTLE\ corriente")); a1.grid(True, alpha=0.25)
    a1.legend()
    a2.semilogy(t1, s1, color=NEON[0], label=lab(f"E={E_lo:g}", rf"E={E_lo:g}"))
    a2.semilogy(t2, s2, color=NEON[1], label=lab(f"E={E_hi:g}", rf"E={E_hi:g}"))
    a2.set_xlabel(lab("t", "t")); a2.set_ylabel("SALI")
    a2.set_title("SALI"); a2.grid(True, which="both", alpha=0.25); a2.legend()
    fig.tight_layout()
    path = _save_show(fig, outdir, "ftle_sali", show)
    return path, (f1[-1], f2[-1], np.nanmin(s1), np.nanmin(s2))


def fig_fractal(sysm, E_lo, E_hi, outdir, show=True):
    apply_style()
    dt = CONFIG["dt"]; poin = PoincareSection(sysm, 1, 0, +1)
    z_lo = section_ic(sysm, E_lo, 0.25 * np.sqrt(E_lo), 0.0)
    z_hi = section_ic(sysm, E_hi, -0.30 * np.sqrt(E_hi), 0.18 * np.sqrt(2 * E_hi))
    info("orbita regular...")
    p_lo = poin.compute(z_lo, 700, dt, t_max=5000,
                        progress=lambda f: render_bar(f, "fractal reg"))
    info("orbita caotica...")
    p_hi = poin.compute(z_hi, 1600, dt, t_max=9000,
                        progress=lambda f: render_bar(f, "fractal caos"))
    D_lo, D_hi = fractal_dimension(p_lo), fractal_dimension(p_hi)
    px_lab = lab("p_" + str(sysm.q[0]), sp.latex(sysm.p[0]))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4.2))
    if len(p_lo):
        a1.scatter(p_lo[:, 0], p_lo[:, 1], s=1.8, marker="s", color=NEON[0], linewidths=0)
    a1.set_title(lab(f"E={E_lo:g} (regular) D~{D_lo:.2f}",
                     rf"E={E_lo:g}\ (regular)\ D\approx{D_lo:.2f}"))
    a1.set_xlabel(lab(str(sysm.q[0]), sp.latex(sysm.q[0]))); a1.set_ylabel(px_lab)
    a1.grid(True, alpha=0.25)
    if len(p_hi):
        a2.scatter(p_hi[:, 0], p_hi[:, 1], s=1.2, marker="s", color=NEON[1], linewidths=0)
    a2.set_title(lab(f"E={E_hi:g} (caotica) D~{D_hi:.2f}",
                     rf"E={E_hi:g}\ (caotica)\ D\approx{D_hi:.2f}"))
    a2.set_xlabel(lab(str(sysm.q[0]), sp.latex(sysm.q[0]))); a2.set_ylabel(px_lab)
    a2.grid(True, alpha=0.25)
    fig.suptitle(lab(f"{sysm.name}: dimension de caja (regular~1, caos~2)",
                     rf"{sysm.name}:\ dimension\ de\ caja"), color="#7CFFC4")
    fig.tight_layout()
    path = _save_show(fig, outdir, "fractal", show)
    return path, (D_lo, D_hi)


def fig_energy_scan(sysm, energies, outdir, show=True):
    apply_style()
    grid = np.linspace(min(energies), max(energies), 8)
    E, fr = EnergyScanner(sysm).scan(grid, n_ic=8, T=55.0,
                                     progress=lambda f: render_bar(f, "barrido E"))
    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    ax.plot(E, fr, "s-", color=NEON[2], ms=6)
    ax.set_xlabel(lab("E", "E"))
    ax.set_ylabel(lab("fraccion caotica", r"fraccion\ caotica"))
    ax.set_ylim(-0.05, 1.05); ax.grid(True, alpha=0.25)
    ax.set_title(lab(f"{sysm.name}: transicion orden-caos vs E",
                     rf"{sysm.name}:\ orden\!-\!caos\ vs\ E"))
    fig.tight_layout()
    return _save_show(fig, outdir, "barrido_energia", show)


def make_anim(sysm, E_hi, coords, outdir, show=True):
    z_hi = section_ic(sysm, E_hi, -0.30 * np.sqrt(E_hi), 0.18 * np.sqrt(2 * E_hi))
    info("renderizando animacion 3D (se guarda como GIF)...")
    return animate_trajectory_3d(sysm, z_hi, coords=coords,
                                 out=os.path.join(outdir, "trayectoria_3d.gif"),
                                 title=f"{sysm.name}: trayectoria", show=show)


def veredicto(fl, fh, sl, sh):
    caos = (fh > 0.03) and (sh < 1e-3)
    reg = (fl < 0.03) and (sl > 1e-2)
    if caos and reg:
        return "DINAMICA MIXTA: regular a baja E, caotica a alta E (transicion orden-caos)."
    if caos:
        return "CAOTICO en buena parte del rango de energias."
    return "SIN CAOS detectado: dinamica regular / cuasi-integrable."


# ======================================================================
#  MUSICA CHIPTUNE 8-BIT
# ======================================================================
def make_chiptune(path, sr=44100):
    """Deprecated: v19 mantiene solo pistas SID; esta funcion no se usa."""
    return False
    # legacy code below kept inert for reference.
    """Genera un loop WAV propio, inspirado en tracker/cracktro 90s.

    No usa samples ni melodias externas: todo se sintetiza con osciladores,
    ruido y envolventes para evitar problemas de licencia.
    """
    NOTES = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
             "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}

    def freq(name):
        if name in (None, "-", "R"):
            return 0.0
        note = name[:-1]
        octv = int(name[-1])
        midi = 12 * (octv + 1) + NOTES[note]
        return 440.0 * (2.0 ** ((midi - 69) / 12.0))

    def env(n, attack=0.004, release=0.030, curve=1.7):
        e = np.ones(n, dtype=float)
        a = min(n, max(1, int(sr * attack)))
        r = min(n, max(1, int(sr * release)))
        e[:a] = np.linspace(0, 1, a) ** 0.7
        e[-r:] *= np.linspace(1, 0, r) ** curve
        return e

    def square(f, dur, duty=0.50, vol=0.25, vibrato=0.0):
        n = int(sr * dur)
        if f <= 0:
            return np.zeros(n)
        t = np.arange(n) / sr
        inst_f = f * (1.0 + vibrato * np.sin(2 * np.pi * 6.0 * t))
        phase = np.cumsum(inst_f) / sr
        y = np.where((phase % 1.0) < duty, 1.0, -1.0)
        return y * env(n) * vol

    def saw(f, dur, vol=0.20):
        n = int(sr * dur)
        if f <= 0:
            return np.zeros(n)
        t = np.arange(n) / sr
        y = 2.0 * ((f * t) % 1.0) - 1.0
        return y * env(n, release=0.022) * vol

    def arp(chord, dur, tick=1/96, duty=0.45, vol=0.22):
        n_tick = max(1, int(sr * tick))
        out = []
        total = int(sr * dur)
        i = 0
        while sum(len(x) for x in out) < total:
            f = freq(chord[i % len(chord)])
            out.append(square(f, n_tick / sr, duty=duty, vol=vol))
            i += 1
        return np.concatenate(out)[:total] * env(total, attack=0.002, release=0.018)

    def kick(dur=0.105, vol=0.55):
        n = int(sr * dur)
        t = np.arange(n) / sr
        f = 120 * np.exp(-22 * t) + 42
        phase = np.cumsum(f) / sr
        return np.sin(2 * np.pi * phase) * np.exp(-22 * t) * vol

    def snare(dur=0.105, vol=0.30):
        n = int(sr * dur)
        noise = np.random.default_rng(1337).uniform(-1, 1, n)
        tone = square(185, dur, duty=0.35, vol=0.12)
        return (0.75 * noise + 0.25 * tone) * np.exp(-18 * np.arange(n) / sr) * vol

    def hat(dur=0.052, vol=0.13):
        n = int(sr * dur)
        rng = np.random.default_rng(7331 + n)
        noise = rng.uniform(-1, 1, n)
        # diferencia discreta: ruido mas agudo, parecido a charles digital
        noise[1:] = noise[1:] - noise[:-1]
        return noise * np.exp(-55 * np.arange(n) / sr) * vol

    bpm = 150
    step = 60.0 / bpm / 4.0       # semicorchea tracker
    bars = 8
    steps = 16 * bars
    total_n = int(steps * step * sr)
    mix = np.zeros(total_n)

    bassline = ["A2", "A2", "C3", "A2", "E3", "A2", "G2", "E2",
                "F2", "F2", "A2", "F2", "C3", "F2", "E3", "C3"]
    chords = [["A4", "C5", "E5"], ["A4", "D5", "F5"],
              ["C5", "E5", "G5"], ["B4", "D5", "G5"]]
    lead = ["A5", "R", "C6", "E6", "G5", "E6", "C6", "R",
            "F5", "A5", "C6", "F6", "E6", "C6", "B5", "G5"]

    for i in range(steps):
        pos = int(i * step * sr)
        sl = slice(pos, min(total_n, pos + int(step * sr)))
        dur = (sl.stop - sl.start) / sr
        # bajo y acordes por patron
        mix[sl] += square(freq(bassline[i % 16]), dur, duty=0.38, vol=0.20)
        mix[sl] += arp(chords[(i // 16) % len(chords)], dur, tick=step / 3,
                       duty=0.42, vol=0.105)
        # lead con efecto de tracker: alterna square/saw y vibrato leve
        lf = freq(lead[(i + (i // 16) * 3) % 16])
        if lf > 0:
            if i % 4 in (0, 3):
                mix[sl] += square(lf, dur, duty=0.22, vol=0.16, vibrato=0.002)
            else:
                mix[sl] += saw(lf, dur, vol=0.11)
        # percusion digital simple
        if i % 8 == 0:
            k = kick(min(0.14, dur * 1.4)); mix[pos:pos + len(k)] += k[:max(0, total_n - pos)]
        if i % 16 in (4, 12):
            sn = snare(min(0.13, dur * 1.5)); mix[pos:pos + len(sn)] += sn[:max(0, total_n - pos)]
        if i % 2 == 1:
            hh = hat(min(0.065, dur)); mix[pos:pos + len(hh)] += hh[:max(0, total_n - pos)]

    # leve filtro/soft clipping para sonar menos aspero en laptop speakers
    mix = np.tanh(1.25 * mix)
    mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.82
    data = (mix * 32767).astype("<i2").tobytes()
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def _project_root():
    """Carpeta raiz del proyecto compacto."""
    return PROJECT_ROOT


def _resource_candidates(*parts):
    rel = os.path.join(*parts)
    bases = []
    if hasattr(sys, "_MEIPASS"):
        bases.append(getattr(sys, "_MEIPASS"))
    bases.extend([_project_root(), _script_dir(), os.getcwd()])
    out = []
    for base in bases:
        out.append(os.path.join(base, rel))
    return out


AUDIO_SID_EXT = {".sid"}
AUDIO_MODULE_EXT = set()
AUDIO_MEDIA_EXT = set()
AUDIO_EXT = AUDIO_SID_EXT


def _audio_folders():
    folders = [MEDIA_DIR]
    out, seen = [], set()
    for folder in folders:
        key = os.path.normcase(os.path.abspath(folder))
        if key not in seen and os.path.isdir(folder):
            seen.add(key); out.append(os.path.abspath(folder))
    return out


def scan_audio_tracks():
    """Devuelve una playlist ordenada de archivos soportados.

    Prioridad: CHAOS_TRACK/CHAOS_AUDIO/CHAOS_SID, luego media/music/assets.
    """
    tracks = []
    for raw in (os.environ.get("CHAOS_TRACK"), os.environ.get("CHAOS_AUDIO"), os.environ.get("CHAOS_SID")):
        path = _norm_path(raw)
        if path and os.path.exists(path) and os.path.splitext(path)[1].lower() in AUDIO_EXT:
            tracks.append(path)
    for folder in _audio_folders():
        try:
            for name in sorted(os.listdir(folder), key=str.lower):
                path = os.path.join(folder, name)
                if os.path.isfile(path) and os.path.splitext(name)[1].lower() in AUDIO_EXT:
                    tracks.append(os.path.abspath(path))
        except Exception:
            pass
    seen, clean = set(), []
    # Preferir SID empaquetado si existe.
    tracks.sort(key=lambda p: (0 if os.path.basename(p).lower().replace(" ", "_") == "i_feel_love.sid" else 1, os.path.basename(p).lower()))
    for path in tracks:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key); clean.append(os.path.abspath(path))
    return clean


def _norm_path(path):
    if not path:
        return None
    path = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    return os.path.abspath(path)


def _candidate_players(env_var, names):
    """Busca un ejecutable por variable de entorno, carpeta del script y PATH."""
    candidates = []
    env = _norm_path(os.environ.get(env_var))
    if env:
        candidates.append(env)
    for name in names:
        candidates.append(os.path.join(_script_dir(), name))
        for p in _resource_candidates("media", name):
            candidates.append(p)
        for p in _resource_candidates("assets", "tools", "sidplayfp", name):
            candidates.append(p)
        for p in _resource_candidates("tools", "sidplayfp", name):
            candidates.append(p)
        found = shutil.which(name)
        if found:
            candidates.append(found)
    seen = set()
    for exe in candidates:
        if not exe:
            continue
        if os.name != "nt" and str(exe).lower().endswith(".exe"):
            continue
        key = os.path.normcase(os.path.abspath(exe))
        if key in seen:
            continue
        seen.add(key)
        if os.path.exists(exe):
            return os.path.abspath(exe)
    return None


def _track_kind(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in AUDIO_SID_EXT:
        return "sid"
    if ext in AUDIO_MODULE_EXT:
        return "module"
    if ext in AUDIO_MEDIA_EXT:
        return "media"
    return "unknown"


def _find_audio_file():
    tracks = scan_audio_tracks()
    if not tracks:
        return None
    idx = int(CONFIG.get("audio_track_index", 0)) % len(tracks)
    CONFIG["audio_track_index"] = idx
    return tracks[idx]


def sid_metadata(path):
    try:
        with open(path, "rb") as f:
            data = f.read(124)
        if data[:4] not in (b"PSID", b"RSID"):
            return None
        def field(start):
            return data[start:start + 32].split(b"\0", 1)[0].decode("latin1", "replace")
        return {"format": data[:4].decode("ascii", "replace"),
                "version": int.from_bytes(data[4:6], "big"),
                "title": field(22), "author": field(54), "released": field(86)}
    except Exception:
        return None


def audio_metadata(path):
    kind = _track_kind(path)
    if kind == "sid":
        meta = sid_metadata(path) or {}
        return {"kind": kind,
                "title": meta.get("title") or os.path.basename(path),
                "author": meta.get("author", ""),
                "released": meta.get("released", ""),
                "format": meta.get("format", "SID")}
    return {"kind": kind, "title": os.path.basename(path or ""),
            "author": "", "released": "", "format": os.path.splitext(path or "")[1][1:].upper()}


class MusicPlayer:
    """Administrador de audio externo.

    v19 conserva deliberadamente solo musica SID/Commodore 64.
    Python no decodifica el SID: se lanza sidplayfp.exe desde media/ y se
    cierra mediante un proceso guardian cuando termina ChaosCalculator.

    Variables utiles:
      CHAOS_TRACK       ruta a una pista .sid.
      CHAOS_SID         compatibilidad: ruta a un .sid.
      CHAOS_SID_PLAYER  ruta a sidplayfp.exe / sidplayfp.
    """
    def __init__(self):
        self.on = False
        cleanup_stale_audio_process()
        self.playlist = scan_audio_tracks()
        if self.playlist:
            CONFIG["audio_track_index"] = int(CONFIG.get("audio_track_index", 0)) % len(self.playlist)
            self.track = self.playlist[CONFIG["audio_track_index"]]
        else:
            self.track = None
        self.track_kind = _track_kind(self.track)
        self.track_info = audio_metadata(self.track) if self.track else None
        self._stop = threading.Event(); self._thread = None
        self._proc = None
        self.backend = self._detect()

    def _detect_sid_player(self):
        return _candidate_players("CHAOS_SID_PLAYER",
                                  ("sidplayfp.exe", "sidplayfp", "sidplay2.exe",
                                   "sidplay2", "sidplay.exe", "sidplay"))

    def _detect(self):
        # v19: solo SID. Si no hay sidplayfp disponible, la musica se desactiva
        # sin generar WAV ni usar reproductores alternativos.
        if self.track and self.track_kind == "sid":
            exe = self._detect_sid_player()
            if exe:
                return ("sid", exe)
        return ("none", None)

    def refresh_playlist(self):
        self.playlist = scan_audio_tracks()
        if self.playlist:
            CONFIG["audio_track_index"] = int(CONFIG.get("audio_track_index", 0)) % len(self.playlist)
            self.track = self.playlist[CONFIG["audio_track_index"]]
        else:
            self.track = None
        self.track_kind = _track_kind(self.track)
        self.track_info = audio_metadata(self.track) if self.track else None
        self.backend = self._detect()

    def select_track(self, idx):
        was_on = self.on
        if was_on:
            self.stop()
        self.refresh_playlist()
        if self.playlist:
            CONFIG["audio_track_index"] = int(idx) % len(self.playlist)
            self.track = self.playlist[CONFIG["audio_track_index"]]
            self.track_kind = _track_kind(self.track)
            self.track_info = audio_metadata(self.track)
            self.backend = self._detect()
            debug_log(f"audio track selected index={CONFIG['audio_track_index']} file={self.track} backend={self.backend[0]}")
        if was_on:
            self.start()

    def next_track(self, step=1):
        self.refresh_playlist()
        if self.playlist:
            self.select_track(CONFIG.get("audio_track_index", 0) + step)
            debug_log(f"audio next_track step={step} -> {self.track}")
        return self.track

    def track_table(self):
        self.refresh_playlist()
        rows = []
        for i, path in enumerate(self.playlist):
            meta = audio_metadata(path)
            rows.append((i, meta.get("format", ""), meta.get("title", os.path.basename(path)), os.path.basename(path)))
        return rows

    def mode_label(self):
        if self.track and self.backend[0] == "sid":
            title = self.track_info.get("title", os.path.basename(self.track)) if self.track_info else os.path.basename(self.track)
            return f"SID:{title}"[:52]
        if self.track:
            return f"SID PLAYER MISSING: {os.path.basename(self.track)}"[:52]
        return "NO SID TRACK"

    def available(self):
        return self.backend[0] != "none"

    def toggle(self):
        if self.on:
            self.stop()
        else:
            self.start()
        return self.on

    def start(self):
        # Evita duplicar procesos de audio si el usuario presiona ENTER/M varias
        # veces durante calculos largos.
        if self.on:
            debug_log("audio start ignored: already running")
            return True
        if not self.available():
            debug_log("audio unavailable: sidplayfp missing or no SID tracks")
            return False
        kind, exe = self.backend
        debug_log(f"audio start backend={kind} track={self.track}")
        if kind == "sid":
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop_external,
                                            args=(kind, exe, self.track), daemon=True)
            self._thread.start()
            self.on = True
            return True
        return False

    def _args_for_external(self, kind, exe, track):
        # sidplayfp/sidplay aceptan el archivo SID como argumento simple.
        return [exe, track]

    def _popen_audio(self, args):
        """Lanza un guardián que mata el reproductor si muere ChaosCalculator.

        El PID persistente corresponde al guardián. taskkill /T /F sobre ese PID
        cierra también sidplayfp. Si el .py se cierra de forma
        abrupta, el guardián detecta que el PID padre desapareció y mata el audio.
        """
        guard_cmd = [
            sys.executable, os.path.abspath(__file__), "--audio-child",
            str(os.getpid()), "1" if CONFIG.get("audio_loop", True) else "0",
            json.dumps(list(args)),
        ]
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL)
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(guard_cmd, **kwargs)
        _write_audio_pid_file(proc.pid)
        debug_log(f"audio guard process pid={proc.pid}")
        return proc

    def ensure_alive(self):
        """Watchdog liviano: si el audio externo cae durante un calculo, lo reinicia."""
        if not self.on:
            return False
        kind = self.backend[0]
        if kind == "sid":
            if self._thread is None or not self._thread.is_alive():
                debug_log("audio watchdog: playback thread dead; restarting")
                self.on = False
                self._stop.clear()
                return self.start()
        return True

    def _loop_external(self, kind, exe, track):
        args = self._args_for_external(kind, exe, track)
        debug_log("external audio process: " + " ".join(map(str, args)))
        while not self._stop.is_set():
            try:
                self._proc = self._popen_audio(args)
            except Exception:
                self.on = False; return
            while self._proc.poll() is None:
                if self._stop.wait(0.2):
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
                    break
            if self._stop.is_set():
                break
            if not CONFIG.get("audio_loop", True):
                break
            # Si el tema termina, se relanza para simular loop de cracktro.
            time.sleep(0.2)
        self._proc = None
        if not self._stop.is_set():
            self.on = False
            debug_log("external audio loop ended")

    def _terminate_proc_tree(self, proc, timeout=1.2):
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return
        except Exception:
            return
        pid = getattr(proc, "pid", None)
        if pid:
            _kill_pid_tree(pid)
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self, force=False):
        debug_log("audio stop requested")
        self._stop.set()
        proc = self._proc
        if proc is not None:
            self._terminate_proc_tree(proc, timeout=0.8 if force else 1.2)
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0 if force else 1.5)
        pid = _read_audio_pid_file()
        if pid:
            _kill_pid_tree(pid)
            _remove_audio_pid_file()
        self.on = False
        self._proc = None


# ======================================================================
#  PRESETS
# ======================================================================

def _three_body_figure8_cfg():
    # Condiciones iniciales clasicas de la coreografia figura-8 para tres masas iguales.
    # Orden del estado: posiciones (x1,y1,x2,y2,x3,y3), luego velocidades.
    z0 = [-0.97000436, 0.24308753, 0.97000436, -0.24308753, 0.0, 0.0,
          0.4662036850, 0.4323657300, 0.4662036850, 0.4323657300,
          -0.93240737, -0.86473146]
    return dict(kind="ode", preset="nbody_planar", name="ThreeBody_Figure8",
                desc="ODE newtoniana planar N=3; masas iguales; coreografia figura-8",
                n_bodies=3, masses=[1.0, 1.0, 1.0], G=1.0, eps=1e-9,
                z0=z0, t_final=6.3259,
                ic_bounds=[[-1.4, 1.4], [-1.0, 1.0], [-1.4, 1.4], [-1.0, 1.0], [-1.4, 1.4], [-1.0, 1.0],
                           [-1.4, 1.4], [-1.4, 1.4], [-1.4, 1.4], [-1.4, 1.4], [-1.4, 1.4], [-1.4, 1.4]],
                min_pair_distance=0.08)


PRESETS = {
    "1": dict(kind="hamiltonian", name="Salasnich", desc="SU(2) Yang-Mills-Higgs homogeneo",
              H="0.5*(p_x**2+p_y**2)+x**2+y**2+0.5*x**2*y**2",
              coords="x y", moms="p_x p_y", energies=[2.5, 6.2, 10, 14],
              E_lo=2.5, E_hi=14, anim=("q0", "q1", "t")),
    "2": dict(kind="hamiltonian", name="Canfora", desc="bobina no abeliana (Georgi-Glashow, nu=0)",
              H="0.5*(p_G**2+p_W**2)+2*W**2*G**2+0.5*W**4+0.25*(G**2)**2",
              coords="G W", moms="p_G p_W", energies=[0.5, 1.0, 2.0],
              E_lo=0.5, E_hi=2.0, anim=("q0", "q1", "t")),
    "3": dict(kind="hamiltonian", name="Henon-Heiles", desc="benchmark hamiltoniano clasico de caos",
              H="0.5*(p_x**2+p_y**2)+0.5*(x**2+y**2)+x**2*y-y**3/3",
              coords="x y", moms="p_x p_y", energies=[0.06, 0.10, 0.15],
              E_lo=0.06, E_hi=0.15, anim=("q0", "q1", "t")),
    "4": _three_body_figure8_cfg(),
    "5": dict(kind="ode", name="Lorenz63", desc="atractor de Lorenz clasico",
              variables="x y z",
              equations=["sigma*(y-x)", "x*(rho-z)-y", "x*y-beta*z"],
              params={"sigma":10, "rho":28, "beta":sp.Rational(8,3)},
              z0=[1.0, 1.0, 1.0], t_final=45.0,
              ic_bounds=[[-20,20],[-30,30],[0,55]]),
    "6": dict(kind="ode", name="Rossler", desc="atractor de Roessler",
              variables="x y z",
              equations=["-y-z", "x+a*y", "b+z*(x-c)"],
              params={"a":0.2, "b":0.2, "c":5.7},
              z0=[1.0, 0.0, 0.0], t_final=120.0,
              ic_bounds=[[-15,15],[-15,15],[0,30]]),
    "7": dict(kind="ode", name="Duffing_Forced", desc="oscilador de Duffing forzado autonomizado",
              variables="x v phi",
              equations=["v", "-delta*v-alpha*x-beta*x**3+gamma*cos(phi)", "omega"],
              params={"delta":0.2, "alpha":-1.0, "beta":1.0, "gamma":0.30, "omega":1.2},
              z0=[0.1, 0.0, 0.0], t_final=120.0,
              ic_bounds=[[-2.5,2.5],[-2.5,2.5],[0,6.28318]]),
    "8": dict(kind="ode", name="Chua_Circuit", desc="circuito de Chua adimensional",
              variables="x y z",
              equations=["alpha*(y-x-m1*x-0.5*(m0-m1)*(Abs(x+1)-Abs(x-1)))", "x-y+z", "-beta*y"],
              params={"alpha":15.6, "beta":28.0, "m0":-1.143, "m1":-0.714},
              z0=[0.7, 0.0, 0.0], t_final=80.0,
              ic_bounds=[[-3,3],[-1,1],[-5,5]]),
    "9": dict(kind="ode", name="DoublePendulum", desc="pendulo doble caotico sin rozamiento",
              variables="th1 w1 th2 w2",
              equations=[
                  "w1",
                  "(-g*(2*m1+m2)*sin(th1)-m2*g*sin(th1-2*th2)-2*sin(th1-th2)*m2*(w2**2*L2+w1**2*L1*cos(th1-th2)))/(L1*(2*m1+m2-m2*cos(2*th1-2*th2)))",
                  "w2",
                  "(2*sin(th1-th2)*(w1**2*L1*(m1+m2)+g*(m1+m2)*cos(th1)+w2**2*L2*m2*cos(th1-th2)))/(L2*(2*m1+m2-m2*cos(2*th1-2*th2)))"
              ],
              params={"m1":1.0, "m2":1.0, "L1":1.0, "L2":1.0, "g":9.81},
              z0=[1.2, 0.0, 1.6, 0.0], t_final=40.0,
              ic_bounds=[[-3.1416,3.1416],[-8,8],[-3.1416,3.1416],[-8,8]]),
}


def _parse_params(text):
    out = {}
    for chunk in str(text or "").replace(";", ",").split(","):
        if not chunk.strip() or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = sp.sympify(v.strip())
    return out


def _parse_vector(text, expected=None):
    vals = [float(sp.N(sp.sympify(x.strip()))) for x in str(text).replace(";", ",").split(",") if x.strip()]
    if expected is not None and len(vals) != expected:
        raise ValueError(f"se esperaban {expected} valores, llegaron {len(vals)}")
    return vals


def _nbody_planar_accel(x, masses=None, G=1.0, eps=1e-9):
    """Aceleracion gravitacional planar para N cuerpos.

    Herramienta general del motor: sirve para N=3, pero no esta atada al
    analisis de tres cuerpos. x tiene forma (N,2).
    """
    x = np.asarray(x, float)
    N = x.shape[0]
    m = np.ones(N, float) if masses is None else np.asarray(masses, float)
    a = np.zeros_like(x)
    for i in range(N):
        for j in range(N):
            if i == j:
                continue
            r = x[j] - x[i]
            r2 = float(np.dot(r, r) + eps * eps)
            a[i] += G * m[j] * r / (r2 ** 1.5)
    return a


def _nbody_planar_rhs(z, n_bodies=3, masses=None, G=1.0, eps=1e-9):
    z = np.asarray(z, float)
    N = int(n_bodies)
    x = z[:2*N].reshape(N, 2)
    v = z[2*N:4*N].reshape(N, 2)
    a = _nbody_planar_accel(x, masses=masses, G=G, eps=eps)
    return np.concatenate([v.reshape(-1), a.reshape(-1)])


def _nbody_planar_energy(z, n_bodies=3, masses=None, G=1.0, eps=1e-9):
    z = np.asarray(z, float)
    N = int(n_bodies)
    m = np.ones(N, float) if masses is None else np.asarray(masses, float)
    x = z[:2*N].reshape(N, 2)
    v = z[2*N:4*N].reshape(N, 2)
    E = 0.5 * float(np.sum(m[:, None] * v * v))
    for i in range(N):
        for j in range(i + 1, N):
            r = float(np.sqrt(np.dot(x[j] - x[i], x[j] - x[i]) + eps * eps))
            E -= G * m[i] * m[j] / r
    return E


def _nbody_min_distance(z, n_bodies=3):
    z = np.asarray(z, float)
    N = int(n_bodies)
    x = z[:2*N].reshape(N, 2)
    if N < 2:
        return np.inf
    dmin = np.inf
    for i in range(N):
        for j in range(i + 1, N):
            dmin = min(dmin, float(np.linalg.norm(x[j] - x[i])))
    return dmin


def _make_nbody_min_distance_validator(n_bodies, minimum):
    def validator(z):
        d = _nbody_min_distance(z, n_bodies=n_bodies)
        return (d > minimum, f"distancia minima {d:.4g} <= {minimum}; colision/singularidad")
    validator.__name__ = "nbody_min_distance"
    return validator


def _build_nbody_planar(cfg):
    N = int(cfg.get("n_bodies", 3))
    masses = np.asarray(cfg.get("masses", [1.0] * N), float)
    G = float(cfg.get("G", 1.0))
    eps = float(cfg.get("eps", 1e-9))
    pos_names = [name for i in range(1, N + 1) for name in (f"x{i}", f"y{i}")]
    vel_names = [name for i in range(1, N + 1) for name in (f"vx{i}", f"vy{i}")]
    names = pos_names + vel_names
    syms = sp.symbols(" ".join(names), real=True)
    rhs = lambda z, N=N, masses=masses, G=G, eps=eps: _nbody_planar_rhs(z, N, masses, G, eps)
    energy = lambda z, N=N, masses=masses, G=G, eps=eps: _nbody_planar_energy(z, N, masses, G, eps)
    rhs_labels = vel_names + [f"a{i}{c}" for i in range(1, N + 1) for c in ("x", "y")]
    groups = [{"label": f"m{i+1}", "indices": (2*i, 2*i + 1), "z_mode": "time"} for i in range(N)]
    validators = [_make_nbody_min_distance_validator(N, float(cfg.get("min_pair_distance", 1e-3)))]
    sysm = ODESystem(syms, [], name=cfg["name"], desc=cfg["desc"], z0=cfg.get("z0"),
                     t_final=cfg.get("t_final", CONFIG["ode_T"]), energy_expr=energy,
                     rhs_callable=rhs, ic_bounds=cfg.get("ic_bounds"), validators=validators,
                     visual_groups=groups,
                     metadata={"family": "nbody_planar", "n_bodies": N, "masses": masses.tolist(), "G": G})
    sysm.preset = cfg.get("preset", "nbody_planar")
    sysm.f_expr = sp.Matrix([sp.Symbol(x) for x in rhs_labels])
    sysm.projection_labels = ("x", "y", "t")
    sysm.animation_trace = 400
    sysm.max_flow_norm = float(cfg.get("max_flow_norm", 1e7))
    return sysm


def build_system(cfg):
    kind = cfg.get("kind", "hamiltonian")
    if kind == "ode" and cfg.get("preset") == "nbody_planar":
        sysm = _build_nbody_planar(cfg)
        sysm._cfg = dict(cfg)
        return auto_randomize_preset_initial_condition(sysm, cfg)
    if kind == "ode":
        vars_ = [sp.symbols(v.strip(), real=True) for v in cfg["variables"].replace(",", " ").split() if v.strip()]
        sysm = ODESystem(vars_, cfg["equations"], params=cfg.get("params", {}), name=cfg.get("name", "ODE"),
                         desc=cfg.get("desc", "custom ODE"), z0=cfg.get("z0"), t_final=cfg.get("t_final", CONFIG["ode_T"]),
                         energy_expr=cfg.get("energy"), ic_bounds=cfg.get("ic_bounds"),
                         validators=cfg.get("validators", []), visual_groups=cfg.get("visual_groups", []),
                         metadata=cfg.get("metadata", {}))
        sysm.preset = cfg.get("preset", "custom_ode")
        sysm._cfg = dict(cfg)
        return auto_randomize_preset_initial_condition(sysm, cfg)
    cn, mn = cfg["coords"].split(), cfg["moms"].split()
    qs = [sp.symbols(c, real=True) for c in cn]
    ps = [sp.symbols(m, real=True) for m in mn]
    local = dict(zip(cn, qs)); local.update(dict(zip(mn, ps)))
    sysm = HamiltonianSystem(qs, ps, sp.sympify(cfg["H"], locals=local), name=cfg["name"])
    sysm.desc = cfg.get("desc", "Hamiltonian system")
    sysm._cfg = dict(cfg)
    return auto_randomize_preset_initial_condition(sysm, cfg)


def manual_hamiltonian():
    banner()
    print(col("  LOAD CUSTOM HAMILTONIAN / SYMPLECTIC REGISTER", C.BOLD + C.CYAN))
    print(col("  ej:  0.5*(p_x**2+p_y**2) + 0.5*(x**2+y**2) + x**2*y - y**3/3\n", C.GREY))
    coords = ask("coordenadas:", "x y"); moms = ask("momentos:", "p_x p_y")
    H = ask("H(q,p) =", ""); energ = ask("energias (coma):", "1.0, 2.0, 4.0")
    name = ask("nombre:", "manual_hamiltonian")
    try:
        cn, mn = coords.split(), moms.split()
        if not H.strip() or len(cn) != len(mn) or len(cn) != 2:
            raise ValueError("Se requieren 2 coordenadas, 2 momentos y un H no vacio.")
        energies = [float(t) for t in energ.replace(";", ",").split(",") if t.strip()]
        cfg = dict(kind="hamiltonian", name=name, desc="manual Hamiltonian", H=H, coords=coords, moms=moms,
                   energies=energies, E_lo=min(energies), E_hi=max(energies), anim=("q0", "q1", "t"),
                   manual=True, randomize_z0=False)
        build_system(cfg)
        return cfg
    except Exception as exc:
        print(col(f"\n  no se pudo crear: {exc}", C.RED)); pause(); return None


def manual_ode():
    banner()
    print(col("  LOAD CUSTOM ODE / GENERAL DYNAMICAL REGISTER", C.BOLD + C.CYAN))
    print(col("  Forma: z' = f(z). Ejemplo Lorenz:", C.GREY))
    print(col("  variables: x y z", C.GREY))
    print(col("  ecuaciones: sigma*(y-x); x*(rho-z)-y; x*y-beta*z", C.GREY))
    print(col("  parametros: sigma=10,rho=28,beta=8/3\n", C.GREY))
    variables = ask("variables:", "x y z")
    eqs_raw = ask("ecuaciones separadas por ;", "sigma*(y-x); x*(rho-z)-y; x*y-beta*z")
    params = _parse_params(ask("parametros k=v:", "sigma=10,rho=28,beta=8/3"))
    var_list = [v for v in variables.replace(",", " ").split() if v.strip()]
    z0 = _parse_vector(ask("condicion inicial z0:", "1,1,1"), expected=len(var_list))
    T = float(sp.N(sp.sympify(ask("tiempo total:", str(CONFIG.get("ode_T", 80.0))))))
    name = ask("nombre:", "Custom_ODE")
    try:
        eqs = [e.strip() for e in eqs_raw.split(";") if e.strip()]
        if len(eqs) != len(var_list):
            raise ValueError("numero de ecuaciones distinto al numero de variables")
        cfg = dict(kind="ode", name=name, desc="manual ODE", variables=" ".join(var_list),
                   equations=eqs, params=params, z0=z0, t_final=T, manual=True, randomize_z0=False)
        build_system(cfg)
        return cfg
    except Exception as exc:
        print(col(f"\n  no se pudo crear ODE: {exc}", C.RED)); pause(); return None


def manual_system():
    items = [
        ("1", "ODE GENERAL       z'=f(z)"),
        ("2", "HAMILTONIAN       H(q,p)"),
        ("0", "BACK"),
    ]
    ch = navigate("LOAD CUSTOM DYNAMICAL SYSTEM", items, footer="ODE general o Hamiltoniano especial")
    if ch == "1":
        return manual_ode()
    if ch == "2":
        return manual_hamiltonian()
    return None


# ======================================================================
#  MENU DE OPCIONES
# ======================================================================
def _display_value(value, typ=None):
    if typ == "bool":
        return "ON" if value else "OFF"
    if typ == "formats":
        return "+".join(value) if isinstance(value, list) else str(value)
    if typ == "quality":
        key = str(value).lower()
        return QUALITY_PRESETS.get(key, {}).get("label", str(value)).upper()
    if typ == "track":
        tracks = scan_audio_tracks()
        if not tracks:
            return "sin pistas SID"
        idx = int(value) % len(tracks)
        meta = audio_metadata(tracks[idx])
        return f"{idx}: {meta.get('title', os.path.basename(tracks[idx]))}"[:36]
    return str(value)


def _config_edit_banner(key, label_txt, current):
    w = 60
    print("  " + col("┌" + "─" * (w - 2) + "┐", C.GREEN))
    print("  " + col("│", C.GREEN) + _safe_highlight(" MODIFY REGISTER ".center(w - 2), w - 2, fg=16, bg=46) + col("│", C.GREEN))
    print("  " + col("├" + "─" * (w - 2) + "┤", C.GREEN))
    rows = [
        f" PARAMETER : {key}",
        f" FUNCTION  : {label_txt}",
        f" CURRENT   : {current}",
        " MODE      : WRITE / CONFIRM",
    ]
    for row in rows:
        # Cuadro verde vertical: fondo fosforo + letras negras, sin tocar bordes.
        print("  " + col("│", C.GREEN) + _safe_highlight(row, w - 2, fg=16, bg=46) + col("│", C.GREEN))
    print("  " + col("└" + "─" * (w - 2) + "┘", C.GREEN))
    print()


def _style_picker(cur, music=None):
    """Selector limpio para el tema Matplotlib global.

    No afecta solo animaciones: al confirmar se actualizan rcParams y la paleta
    explicita del programa. Las figuras nuevas, exportaciones y animaciones 3D
    usan este tema.
    """
    styles = _available_styles()
    page = 0
    per_page = 10
    while True:
        total_pages = max(1, (len(styles) - 1) // per_page + 1)
        start = page * per_page
        chunk = styles[start:start + per_page]
        rows = []
        for i, style in enumerate(chunk):
            idx = start + i
            tag = "* " if style == cur else "  "
            rows.append((str(idx), tag + style.upper()))
        rows.append(("0", "BACK / CANCEL"))
        detail = [
            "SELECTION: MATPLOTLIB GLOBAL THEME",
            f"ACTIVE   : {str(cur).upper()}",
            "SCOPE    : figures, exports, phase plots and animations",
        ]
        if _interactive():
            idx_sel = 0
            while True:
                clear(); banner(music)
                _clean_module_panel(f"MATPLOTLIB THEME BANK {page+1}/{total_pages}", rows, idx_sel,
                                    "ENTER aplica tema   N/P pagina   ESC vuelve", detail)
                k = read_key()
                if k == "UP":
                    idx_sel = (idx_sel - 1) % len(rows)
                elif k == "DOWN":
                    idx_sel = (idx_sel + 1) % len(rows)
                elif str(k).lower() == "n":
                    page = (page + 1) % total_pages; break
                elif str(k).lower() == "p":
                    page = (page - 1) % total_pages; break
                elif k in ("ESC", "LEFT"):
                    return cur
                elif k in ("ENTER", "SPACE", "RIGHT"):
                    code = rows[idx_sel][0]
                    if code == "0":
                        return cur
                    new_style = styles[int(code)]
                    CONFIG["mpl_style"] = new_style
                    apply_style()
                    return new_style
                elif str(k).isdigit() and 0 <= int(k) < len(styles):
                    new_style = styles[int(k)]
                    CONFIG["mpl_style"] = new_style
                    apply_style()
                    return new_style
        else:
            clear(); banner(music)
            _clean_module_panel(f"MATPLOTLIB THEME BANK {page+1}/{total_pages}", rows, 0,
                                "n/p cambia pagina; enter cancela", detail)
            v = ask("style id / n / p:", "")
            if not v:
                return cur
            if v.lower() == "n":
                page = (page + 1) % total_pages; continue
            if v.lower() == "p":
                page = (page - 1) % total_pages; continue
            if v.isdigit() and 0 <= int(v) < len(styles):
                new_style = styles[int(v)]
                CONFIG["mpl_style"] = new_style
                apply_style()
                return new_style
            print(col("  estilo no valido.", C.RED)); time.sleep(0.6)

def _quality_picker(cur, music=None):
    keys = list(QUALITY_PRESETS.keys())
    rows = [(str(i + 1), QUALITY_PRESETS[k]["label"]) for i, k in enumerate(keys)] + [("0", "BACK / CANCEL")]
    idx = 0
    while True:
        key = keys[idx] if idx < len(keys) else str(cur).lower()
        qp = QUALITY_PRESETS.get(key, QUALITY_PRESETS["normal"])
        detail = [f"ACTIVE : {str(cur).upper()}", f"TARGET : {qp['label']}", f"COST   : {qp['warning']}"]
        clear(); banner(music)
        _clean_module_panel("QUALITY / COMPUTE COST BANK", rows, idx, "ENTER aplica perfil   ESC vuelve", detail)
        if _interactive():
            k = read_key()
            if k == "UP": idx = (idx - 1) % len(rows)
            elif k == "DOWN": idx = (idx + 1) % len(rows)
            elif k in ("ESC", "LEFT"): return cur
            elif k in ("ENTER", "SPACE", "RIGHT"):
                if idx >= len(keys): return cur
                chosen = keys[idx]
                if chosen == "ultra":
                    print(col("\n  WARNING: ULTRA aumenta mucho el coste numerico.", C.RED))
                    if not ask_yes_no("confirmar ULTRA", False): continue
                apply_quality_profile(chosen); return chosen
            elif str(k).isdigit():
                j = int(k) - 1
                if 0 <= j < len(keys):
                    chosen = keys[j]
                    if chosen == "ultra":
                        print(col("\n  WARNING: ULTRA aumenta mucho el coste numerico.", C.RED))
                        if not ask_yes_no("confirmar ULTRA", False): continue
                    apply_quality_profile(chosen); return chosen
        else:
            v = ask("quality id:", "")
            if not v: return cur
            if v.isdigit() and 1 <= int(v) <= len(keys):
                chosen = keys[int(v) - 1]
                if chosen == "ultra" and not ask_yes_no("confirmar ULTRA", False): continue
                apply_quality_profile(chosen); return chosen


def _edit_value(key, label_txt, typ, music=None):
    cur = CONFIG[key]
    clear(); banner(music)
    _config_edit_banner(key, label_txt, _display_value(cur, typ))
    if typ == "bool":
        CONFIG[key] = not cur
        print(col(f"  {key} -> {_display_value(CONFIG[key], typ)}", C.PHOS))
        time.sleep(0.45)
        return
    if typ == "quality":
        CONFIG[key] = _quality_picker(cur, music)
        save_runtime_config()
        return
    if typ == "choice":
        CONFIG[key] = _style_picker(cur, music)
        save_runtime_config()
        return
    if typ == "formats":
        print(col("  formatos disponibles: svg, png, pdf, eps, ps, all", C.GREY))
        print(col("  ejemplo: svg,png,pdf   |   all", C.GREY))
        v = ask("formatos de salida:", ",".join(cur))
        toks = [x.strip().lower().lstrip(".") for x in v.replace(";", ",").split(",") if x.strip()]
        allowed = {"svg", "png", "pdf", "eps", "ps", "all"}
        clean = []
        for t in toks:
            if t in allowed and t not in clean:
                clean.append(t)
        if clean:
            CONFIG[key] = ["svg", "png", "pdf", "eps"] if "all" in clean else clean
        else:
            print(col("  valor no valido; se mantiene el anterior.", C.RED)); time.sleep(0.8)
        return
    if typ == "track":
        tracks = music.track_table() if music else [(i, audio_metadata(p).get("format", ""), audio_metadata(p).get("title", os.path.basename(p)), os.path.basename(p)) for i, p in enumerate(scan_audio_tracks())]
        if not tracks:
            print(col("  no hay pistas en media/ o music/. Se usara fallback sintetico.", C.RED)); time.sleep(1.0); return
        print(col("  PLAYLIST DETECTADA", C.PHOS))
        for i, fmt, title, fname in tracks:
            active = " <- ACTIVE" if i == int(CONFIG.get("audio_track_index", 0)) % len(tracks) else ""
            print("    " + col(f"[{i:02d}]", C.AMBER) + " " + col(f"{fmt:<5}", C.CRT) + " " + col(title[:38], C.WHITE) + col(active, C.GREEN))
        v = ask("track id / n next / p prev:", str(CONFIG.get("audio_track_index", 0)))
        if v.lower() in ("n", "next") and music:
            music.next_track(+1); return
        if v.lower() in ("p", "prev") and music:
            music.next_track(-1); return
        if v.isdigit():
            CONFIG[key] = int(v)
            if music:
                music.select_track(CONFIG[key])
        return
    v = ask(f"nuevo valor para {key}:", str(cur))
    try:
        CONFIG[key] = int(v) if typ == "int" else float(v)
    except ValueError:
        print(col("  valor no valido; se mantiene el anterior.", C.RED)); time.sleep(0.8)


def _group_snapshot(entries, max_items=2):
    parts = []
    for key, label_txt, typ in entries[:max_items]:
        try:
            parts.append(f"{label_txt.split()[0]}={_display_value(CONFIG[key], typ)}")
        except Exception:
            parts.append(f"{key}=...")
    if len(entries) > max_items:
        parts.append(f"+{len(entries)-max_items}")
    return "  ".join(parts)


def _clean_module_panel(title, rows, idx, footer=None, detail=None):
    """Panel CRT limpio para configuracion.

    rows: lista de (code, label). El valor detallado se muestra abajo, no en
    cada fila, para evitar el ruido visual de registros muy largos.
    """
    w = _term_width(88)
    code_w = 6
    label_w = w - code_w - 3
    print("  " + col("┌" + "─" * (w - 2) + "┐", C.GREEN))
    print("  " + col("│", C.GREEN) + col(_term_cell(f" {title} // PAGE READY", w - 2), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┬" + "─" * label_w + "┤", C.GREEN))
    print("  " + col("│", C.GREEN) + col(_term_cell(" CODE", code_w), C.PHOS) + col("│", C.GREEN)
          + col(_term_cell(" REGISTER CONTROL", label_w), C.PHOS) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┼" + "─" * label_w + "┤", C.GREEN))
    for i, (code, label) in enumerate(rows):
        ccell = _term_cell(f" {str(code).upper():<5}", code_w)
        lcell = _term_cell(" " + str(label).upper(), label_w)
        if i == idx:
            print("  " + col("│", C.GREEN) + _safe_highlight(ccell, code_w) + col("│", C.GREEN)
                  + _safe_highlight(lcell, label_w) + col("│", C.GREEN))
        else:
            print("  " + col("│", C.GREEN) + col(ccell, C.AMBER) + col("│", C.GREEN)
                  + col(lcell, C.DGREEN) + col("│", C.GREEN))
    print("  " + col("├" + "─" * code_w + "┴" + "─" * label_w + "┤", C.GREEN))
    if detail:
        for line in detail[:3]:
            print("  " + col("│", C.GREEN) + col(_term_cell(" " + line, w - 2), C.CRT) + col("│", C.GREEN))
        print("  " + col("└" + "─" * (w - 2) + "┘", C.GREEN))
    else:
        print("  " + col("└" + "─" * (w - 2) + "┘", C.GREEN))
    if footer:
        print("  " + col("SUBJECT: ", C.GRID) + col(footer, C.DGREEN))
    print("  " + col(" ↑/↓ SELECT   ENTER RUN   ESC BACK   M MUSIC   N NEXT   B PREV", C.GREY))


def _config_bank_choice(music):
    rows = [(code, title) for code, title, _desc, _entries in CONFIG_GROUPS] + [("0", "BACK")]
    valid = {str(code).lower() for code, _label in rows}
    if not _interactive():
        clear(); banner(music)
        detail = ["SELECTION: MODULE BANK", "CONTROL: choose one register family"]
        _clean_module_panel("SYSTEM CONFIG / MODULE BANK", rows, 0, "ENTER abre modulo   ESC vuelve", detail)
        return read_choice(valid | {"m", "n", "b"})
    idx = 0
    while True:
        clear(); banner(music)
        selected = rows[idx][0]
        group = next((g for g in CONFIG_GROUPS if g[0] == selected), None)
        if group:
            _code, title, desc, entries = group
            detail = [f"SELECTION: {title}", f"CONTROL: {desc}", f"REGISTERS: {len(entries)}"]
        else:
            detail = ["SELECTION: BACK", "CONTROL: return to main program"]
        _clean_module_panel("SYSTEM CONFIG / MODULE BANK", rows, idx, "ENTER abre modulo   ESC vuelve", detail)
        k = read_key()
        if k == "UP":
            idx = (idx - 1) % len(rows)
        elif k == "DOWN":
            idx = (idx + 1) % len(rows)
        elif k in ("ENTER", "SPACE", "RIGHT"):
            return str(rows[idx][0]).lower()
        elif k in ("ESC", "LEFT"):
            return "ESC"
        elif k in valid or k in ("m", "n", "b"):
            return k


def _config_param_choice(group_title, entries, music):
    rows = [(str(i), label_txt) for i, (_key, label_txt, _typ) in enumerate(entries, 1)] + [("0", "BACK")]
    valid = {str(code).lower() for code, _label in rows}
    if not _interactive():
        clear(); banner(music)
        detail = ["SELECTION: PARAMETER BANK", "CONTROL: choose register to edit"]
        _clean_module_panel(f"CONFIG / {group_title}", rows, 0, "ENTER edita registro   ESC vuelve", detail)
        return read_choice(valid | {"m", "n", "b"})
    idx = 0
    while True:
        clear(); banner(music)
        if idx < len(entries):
            key, label_txt, typ = entries[idx]
            detail = [
                f"REGISTER: {label_txt}",
                f"CURRENT : {_display_value(CONFIG[key], typ)}",
                f"TYPE    : {typ.upper()}",
            ]
        else:
            detail = ["REGISTER: BACK", "CURRENT : ---", "TYPE    : NAVIGATION"]
        _clean_module_panel(f"CONFIG / {group_title}", rows, idx, "ENTER edita registro   ESC vuelve", detail)
        k = read_key()
        if k == "UP":
            idx = (idx - 1) % len(rows)
        elif k == "DOWN":
            idx = (idx + 1) % len(rows)
        elif k in ("ENTER", "SPACE", "RIGHT"):
            return str(rows[idx][0]).lower()
        elif k in ("ESC", "LEFT"):
            return "ESC"
        elif k in valid or k in ("m", "n", "b"):
            return k


def _options_group_menu(group_title, entries, music):
    while True:
        ch = _config_param_choice(group_title, entries, music)
        if ch in ("m", "n", "b"):
            if ch == "m":
                music.toggle()
            elif ch == "n":
                music.next_track(+1)
            else:
                music.next_track(-1)
            continue
        if ch in ("0", "ESC"):
            return
        try:
            key, label_txt, typ = entries[int(ch) - 1]
        except Exception:
            continue
        _edit_value(key, label_txt, typ, music)
        save_runtime_config()
        apply_style()


def options_menu(music):
    while True:
        ch = _config_bank_choice(music)
        if ch in ("m", "n", "b"):
            if ch == "m":
                music.toggle()
            elif ch == "n":
                music.next_track(+1)
            else:
                music.next_track(-1)
            continue
        if ch in ("0", "ESC"):
            save_runtime_config(); apply_style(); return
        group = next((g for g in CONFIG_GROUPS if g[0] == ch), None)
        if group is None:
            continue
        _code, title, _desc, entries = group
        _options_group_menu(title, entries, music)


# ======================================================================
#  FLUJO DE LA APLICACION
# ======================================================================
def show_system(sysm):
    if getattr(sysm, "kind", "hamiltonian") == "hamiltonian":
        print(col("  Hamiltonian register:", C.CYAN))
        print(col(f"    H = {sysm.H_expr}", C.WHITE))
        print(col(f"    pdot = {sysm.f_expr[sysm.n:, 0].T}", C.GREY))
    else:
        labels = _labels(sysm)
        print(col("  ODE register:", C.CYAN))
        print(col(f"    dim = {getattr(sysm, 'dim', len(labels))}   T = {getattr(sysm, 't_final', CONFIG.get('ode_T'))}", C.WHITE))
        for i, expr in enumerate(list(getattr(sysm, 'f_expr', []))[:6]):
            print(col(f"    d{labels[i]}/dt = {expr}", C.GREY))
        if len(labels) > 6:
            print(col(f"    ... {len(labels)-6} ecuaciones mas", C.GREY))
        try:
            z = _default_z0(sysm)
            print(col("    z0 = " + _format_state_line(labels, z, 6), C.GREY))
            okv, msg = validate_initial_condition(sysm, z)
            print(col(f"    z0 check = {msg}", C.PHOS if okv else C.RED))
            if getattr(sysm, "randomized_z0", False):
                print(col("    z0 mode = SMART RANDOM PRESET", C.AMBER))
        except Exception:
            pass


def analysis_menu(cfg, music):
    debug_log(f"analysis menu open cfg={cfg.get('name')} kind={cfg.get('kind')}")
    sysm = build_system(cfg)
    outdir = os.path.abspath(os.path.join(OUTPUT_DIR, cfg["name"].replace(" ", "_")))
    os.makedirs(outdir, exist_ok=True)
    is_ham = getattr(sysm, "kind", "hamiltonian") == "hamiltonian"
    if is_ham:
        items = [
            ("1", "POTENTIAL MAP      V(q1,q2)"),
            ("2", "POINCARE GRID      ENERGY SWEEP"),
            ("3", "FTLE + SALI        REGULAR / CHAOTIC"),
            ("4", "FRACTAL DIM        BOX-COUNTING"),
            ("5", "ENERGY SCANNER     CHAOTIC FRACTION"),
            ("6", "3D TRACE           TRAJECTORY GIF"),
            ("7", "FULL SYSTEM RUN"),
            ("0", "BACK"),
        ]
    else:
        items = [
            ("1", "TIME SERIES       VARIABLES VS T"),
            ("2", "PHASE PROJECTION  2D/3D ORBIT"),
            ("3", "POINCARE SECTION  COORDINATE CROSSING"),
            ("4", "FTLE + SALI       VARIATIONAL INDICATORS"),
            ("5", "ANIMATION         GENERIC 3D + LIVE HUD"),
            ("6", "INITIAL STATE     DEFAULT / MANUAL / RANDOM"),
            ("7", "FULL ODE RUN"),
            ("0", "BACK"),
        ]

    def header():
        print(" " + col(f" ACTIVE FILE: {cfg['name'].upper()} -- {cfg['desc']} ", C.DGREEN))
        show_system(sysm); print()

    while True:
        ch = navigate("ANALYSIS", items, music, header_fn=header, footer=f"{'/'.join(_selected_formats()).upper()} -> {outdir}")
        if ch in ("0", "ESC"):
            return
        if ch == "m":
            music.toggle(); continue
        if ch == "n":
            music.next_track(+1); continue
        if ch == "b":
            music.next_track(-1); continue
        clear(); banner(music); print()
        try:
            debug_log(f"analysis action system={sysm.name} option={ch}")
            if is_ham:
                if ch == "1":
                    p = fig_potential(sysm, cfg["energies"], outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "2":
                    p = fig_poincare(sysm, cfg["energies"], outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "3":
                    p, (fl, fh, sl, sh) = fig_ftle_sali(sysm, cfg["E_lo"], cfg["E_hi"], outdir)
                    ok(f"guardado: {_fmt_paths(p)}")
                    print(col(f"  veredicto: {veredicto(fl, fh, sl, sh)}", C.YEL))
                elif ch == "4":
                    p, (dl, dh) = fig_fractal(sysm, cfg["E_lo"], cfg["E_hi"], outdir)
                    ok(f"guardado: {_fmt_paths(p)}")
                    print(col(f"  D(reg)~{dl:.2f}   D(caos)~{dh:.2f}", C.YEL))
                elif ch == "5":
                    p = fig_energy_scan(sysm, cfg["energies"], outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "6":
                    p = make_anim(sysm, cfg["E_hi"], cfg["anim"], outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "7":
                    full_analysis(cfg, sysm, outdir)
            else:
                if ch == "1":
                    p = fig_generic_timeseries(sysm, outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "2":
                    p = fig_generic_phase(sysm, outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "3":
                    p = fig_generic_poincare(sysm, outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "4":
                    p, (fl, sl) = fig_generic_lyapunov(sysm, outdir); ok(f"guardado: {_fmt_paths(p)}")
                    print(col(f"  FTLE final ~ {fl:+.4g}   SALI min ~ {sl:.2e}", C.YEL))
                elif ch == "5":
                    p = animate_generic(sysm, outdir); ok(f"guardado: {_fmt_paths(p)}")
                elif ch == "6":
                    set_initial_condition_menu(sysm, music)
                elif ch == "7":
                    full_analysis(cfg, sysm, outdir)
        except Exception as exc:
            print(col(f"  ERROR en el analisis: {exc}", C.RED))
            debug_log(f"analysis error system={sysm.name} option={ch}: {exc}")
        try:
            music.ensure_alive()
        except Exception as exc:
            debug_log(f"audio watchdog error after analysis: {exc}")
        pause()


def full_analysis(cfg, sysm, outdir):
    info("ANALISIS COMPLETO: cada salida preguntara si deseas guardarla/mostrarla.")
    if getattr(sysm, "kind", "hamiltonian") == "hamiltonian":
        fig_potential(sysm, cfg["energies"], outdir)
        fig_poincare(sysm, cfg["energies"], outdir)
        _, (fl, fh, sl, sh) = fig_ftle_sali(sysm, cfg["E_lo"], cfg["E_hi"], outdir)
        _, (dl, dh) = fig_fractal(sysm, cfg["E_lo"], cfg["E_hi"], outdir)
        fig_energy_scan(sysm, cfg["energies"], outdir)
        make_anim(sysm, cfg["E_hi"], cfg["anim"], outdir)
        print(col("\n  -- RESUMEN --------------------------", C.TEAL))
        print(col(f"  FTLE: bajo={fl:+.3f}  alto={fh:+.3f}", C.WHITE))
        print(col(f"  SALI(min): bajo={sl:.1e}  alto={sh:.1e}", C.WHITE))
        print(col(f"  dim fractal: reg~{dl:.2f}  caos~{dh:.2f}", C.WHITE))
        print(col(f"  VEREDICTO: {veredicto(fl, fh, sl, sh)}", C.YEL))
    else:
        fig_generic_timeseries(sysm, outdir)
        fig_generic_phase(sysm, outdir)
        fig_generic_poincare(sysm, outdir)
        _, (fl, sl) = fig_generic_lyapunov(sysm, outdir)
        animate_generic(sysm, outdir)
        print(col("\n  -- RESUMEN ODE ----------------------", C.TEAL))
        print(col(f"  FTLE final ~ {fl:+.4g}  SALI min ~ {sl:.2e}", C.WHITE))
        if getattr(sysm, "preset", "") == "nbody_planar":
            print(col("  preset: N-cuerpos planar; revisar energia, drift y d_min en la animacion.", C.WHITE))
    print(col(f"  FIGURAS en: {outdir}", C.GREEN))


def _preset_menu_items():
    rows = []
    for code in sorted(PRESETS.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
        cfg = PRESETS[code]
        name = str(cfg.get("name", f"PRESET {code}")).replace("_", " ")
        desc = str(cfg.get("desc", ""))[:36]
        rows.append((code, f"{name:<18} {desc}"))
    rows += [("C", "LOAD CUSTOM       ODE / HAMILTONIAN"),
             ("S", "SYSTEM CONFIG     OPCIONES / CRT / MEMORIA"),
             ("0", "LOGOUT")]
    return rows


def main_menu(music):
    while True:
        items = _preset_menu_items()
        ch = navigate("MENU PRINCIPAL", items, music, footer="SELECT PROGRAM FILE AND CONFIRM")
        debug_log(f"main menu input={ch}")
        if ch in ("0", "ESC"):
            music.stop(); save_runtime_config(); clear(); banner(music)
            print(col("\n  SESSION CLOSED. CARRIER LOST.\n", C.GREEN)); return
        if ch == "m":
            if not music.available():
                _flash("audio SID no disponible: revisa sidplayfp.exe en /media")
            else:
                music.toggle()
            continue
        if ch == "n":
            music.next_track(+1); save_runtime_config()
            continue
        if ch == "b":
            music.next_track(-1); save_runtime_config()
            continue
        if str(ch).lower() == "s":
            options_menu(music)
        elif str(ch).lower() == "c":
            cfg = manual_system()
            if cfg:
                apply_style(); analysis_menu(cfg, music); music.ensure_alive()
        elif ch in PRESETS:
            apply_style(); analysis_menu(PRESETS[ch], music); music.ensure_alive()


def main():
    # Desde v11 no se abre terminal paralela de diagnostico. El log queda en
    # data/logs/chaos_debug.log, pero la interfaz corre sola en una terminal.
    load_runtime_config()
    apply_style()
    debug_log("main start")
    music = MusicPlayer()
    global _ACTIVE_MUSIC
    _ACTIVE_MUSIC = music
    # La musica SID parte desde el inicio si sidplayfp esta disponible.
    if CONFIG.get("audio_autostart", True) and os.environ.get("CHAOS_AUDIO_AUTOSTART", "1").strip().lower() not in ("0", "false", "off", "no"):
        try:
            music.start()
        except Exception:
            debug_log("audio autostart failed")
    animate_logo_startup(music)
    boot()
    typewriter("  ACCESS GRANTED: CHAOS CALCULATOR WAITING FOR DYNAMICAL REGISTER.", C.PHOS)
    time.sleep(0.2)
    try:
        main_menu(music)
    except (KeyboardInterrupt, EOFError):
        print(col("\n  interrumpido.\n", C.MAG))
    finally:
        music.stop(force=True)


if __name__ == "__main__":
    if _GIF_CHILD_MODE:
        sys.exit(_gif_encode_child_main())
    if _AUDIO_CHILD_MODE:
        sys.exit(_audio_child_main())
    main()
