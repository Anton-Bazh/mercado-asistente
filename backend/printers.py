"""Gestión de impresoras vía CUPS (USB y red) — sin dependencias externas.

Se apoya en las herramientas estándar de CUPS ya presentes en el sistema
(`lpstat`, `lpinfo`, `lpadmin`, `lp`, `cancel`) invocadas por subprocess, más
un atajo de socket crudo para enviar ZPL a impresoras de red en el puerto 9100.

Enfoque principal: impresoras normales (PDF) por USB o red gestionadas por CUPS.
ZPL queda soportado (cola raw de CUPS o socket directo) como caso secundario.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import logutil
from config import STAMP_PATH

log = logutil.get_logger("cups")


class PrinterError(Exception):
    """Error al gestionar o usar una impresora."""


_CMD_TIMEOUT = 20

# Forzar locale C para que CUPS responda en inglés (parseo estable
# independiente del idioma del sistema, p. ej. es_MX).
_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _run(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            input=input_bytes,
            capture_output=True,
            timeout=_CMD_TIMEOUT,
            env=_ENV,
        )
    except FileNotFoundError as exc:
        raise PrinterError(f"Comando no disponible: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrinterError(f"Tiempo de espera agotado en: {' '.join(args)}") from exc


def _text(args: list[str]) -> str:
    """Ejecuta y devuelve stdout como texto (ignora código de salida)."""
    cp = _run(args)
    return (cp.stdout or b"").decode(errors="replace")


def cups_available() -> bool:
    """True si CUPS responde (cupsd activo)."""
    cp = _run(["lpstat", "-r"])
    out = (cp.stdout or b"").decode(errors="replace").lower()
    return cp.returncode == 0 and "is running" in out


# --- Listado -----------------------------------------------------------------
def _default_printer() -> str | None:
    out = _text(["lpstat", "-d"])
    m = re.search(r"system default destination:\s*(\S+)", out)
    return m.group(1) if m else None


def system_default() -> str | None:
    """Predeterminada del sistema (CUPS), informativa."""
    return _default_printer()


def _device_uris() -> dict[str, str]:
    """Mapa {impresora: device-uri} a partir de `lpstat -v`."""
    uris: dict[str, str] = {}
    for line in _text(["lpstat", "-v"]).splitlines():
        m = re.match(r"device for (.+?):\s*(\S+)", line.strip())
        if m:
            uris[m.group(1)] = m.group(2)
    return uris


def _classify(uri: str) -> str:
    """Clasifica la conexión a partir del device-uri."""
    u = (uri or "").lower()
    if u.startswith("usb:"):
        return "USB"
    if u.startswith(("ipp:", "ipps:", "socket:", "http:", "https:", "lpd:",
                     "dnssd:", "implicitclass:")):
        # implicitclass = cola driverless (típicamente IPP/Bonjour de red)
        return "Red"
    return "Otro"


def list_printers() -> list[dict]:
    """Lista las impresoras configuradas en CUPS con su estado y conexión."""
    if not cups_available():
        raise PrinterError("CUPS no está activo. Inicia el servicio: sudo systemctl start cups")

    default = _default_printer()
    uris = _device_uris()
    printers: list[dict] = []

    accepting = _accepting_map()
    out = _text(["lpstat", "-p"])
    # Cada impresora: "printer NAME is idle.  enabled since ..." (puede multilinea)
    current: dict | None = None
    for line in out.splitlines():
        # CUPS: "printer NAME is idle." | "printer NAME now printing JOB." |
        #       "printer NAME disabled since …" (con líneas de continuación).
        m = re.match(r"printer (\S+) (is idle|is processing|now printing|"
                     r"disabled|is stopped)", line.strip())
        if m:
            name, raw = m.group(1), m.group(2)
            state = ("printing" if raw in ("now printing", "is processing")
                     else "disabled" if raw in ("disabled", "is stopped")
                     else "idle")
            uri = uris.get(name, "")
            r = _cached_readiness(name, uri, accepting.get(name, True))
            current = {
                "name": name,
                "state": state,                 # idle / printing / disabled
                "system_default": name == default,
                "connection": _classify(uri),
                "uri": uri,
                "info": "",
                "shared": r["shared"],
                "ready": r["ready"],
                "ready_reason": r["reason"],
            }
            printers.append(current)
        elif current is not None and line.strip():
            current["info"] = (current["info"] + " " + line.strip()).strip()
    return printers


# --- Disponibilidad real (readiness): sondeo activo al dispositivo -----------
# En vez de fiarse de lo que CUPS "cree" (que miente: marca idle una impresora
# apagada), se sondea de verdad: ipptool pregunta el estado real a la cola
# (printer-state-message revela "offline"/"waiting for printer"), y lpinfo -v
# confirma qué USB están conectadas AHORA. Resultado cacheado y refrescado por
# un hilo en segundo plano para que la UI se actualice en tiempo real sin costo.

# state-reasons / mensajes que indican que NO puede imprimir ahora.
_BAD_REASONS = (
    "offline", "connecting-to-device", "timed-out", "unreachable", "shutdown",
    "com-failure", "jam", "media-empty", "media-needed", "cover-open",
    "door-open", "marker-supply-empty", "toner-empty", "ink-empty",
)
_BAD_MESSAGES = (
    "offline", "waiting for printer", "unable to", "not connected", "unreachable",
    "timed out", "no such", "powered off", "turned off", "cannot", "failed to",
)
_IPPTOOL_TEST = "get-printer-attributes.test"   # test estándar de CUPS

_readiness_cache: dict[str, dict] = {}
_lsusb_text: str = ""
_readiness_lock = threading.Lock()
_monitor_started = False


def _is_shared(uri: str) -> bool:
    """True si la cola depende de otro equipo (mDNS/Bonjour/implicitclass)."""
    u = (uri or "").lower()
    if u.startswith(("implicitclass:", "dnssd:")):
        return True
    if u.startswith(("ipp:", "ipps:")) and ".local" in u and ("%40" in u or "@" in u):
        return True
    return False


# Colas compartidas (dnssd): device-uri mDNS → URI ipp:// real, cacheada.
# Se invalida si el sondeo falla, para re-resolver al siguiente ciclo (el
# servidor pudo cambiar de IP o dejar de publicar la cola).
_shared_targets: dict[str, str] = {}


def _resolve_shared_uri(uri: str) -> str:
    """Resuelve el device-uri mDNS de una cola compartida a su URI ipp:// real.

    dnssd://Brother_DCP_T220_USB%20%40%20mtmdelta._ipp._tcp.local/cups?uuid=…
    → ipp://mtmdelta.local:631/printers/Brother_DCP_T220_USB (vía ippfind).
    """
    cached = _shared_targets.get(uri)
    if cached is not None:
        return cached
    m = re.match(r"^dnssd://([^/?]+)", uri or "", re.I)
    if not m:
        return ""
    service = unquote(m.group(1))
    if not service.endswith("."):
        service += "."
    try:
        cp = _run(["ippfind", service, "-T", "4"])
    except PrinterError:
        return ""
    lines = (cp.stdout or b"").decode(errors="replace").strip().splitlines()
    resolved = lines[0].strip() if cp.returncode == 0 and lines else ""
    if resolved:
        _shared_targets[uri] = resolved
    return resolved


def _probe_shared_queue(target: str) -> tuple[bool, str]:
    """Pregunta al servidor CUPS remoto si la cola compartida puede imprimir AHORA."""
    m = re.match(r"^ipps?://([^/:]+)(?::(\d+))?", target, re.I)
    if not m:
        return False, "Compartida: se comprueba al imprimir (depende de otro equipo)"
    host, port = m.group(1), int(m.group(2) or 631)
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError:
        return False, f"El servidor de impresión no responde ({host}:{port})."
    cp = _run(["ipptool", "-T", "5", "-tv", target, _IPPTOOL_TEST])
    out = (cp.stdout or b"").decode(errors="replace")

    def _attr(name: str) -> str:
        m2 = re.search(rf"^\s*{name} \([^)]*\) = ?(.*)$", out, re.M)
        return m2.group(1).strip().lower() if m2 else ""

    state = _attr("printer-state")
    if not state:
        return False, "El servidor no respondió por IPP (cola compartida)."
    if _attr("printer-is-accepting-jobs") == "false":
        return False, "El servidor no acepta trabajos para esta cola."
    reasons = _attr("printer-state-reasons")
    if state in ("stopped", "5") or any(k in reasons for k in _BAD_REASONS):
        return False, f"El servidor reporta la cola no disponible ({reasons or state})."
    return True, ""


def _accepting_map() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for line in _text(["lpstat", "-a"]).splitlines():
        m = re.match(r"(\S+)\s+(not accepting|accepting)", line.strip())
        if m:
            out[m.group(1)] = (m.group(2) == "accepting")
    return out


def _state_reasons(name: str) -> str:
    opts = _parse_lpoptions(_text(["lpoptions", "-p", _safe_name(name)]))
    return (opts.get("printer-state-reasons", "") or "").lower()


def _refresh_usb() -> None:
    """Cachea la lista de dispositivos USB conectados AHORA (lsusb)."""
    global _lsusb_text
    cp = _run(["lsusb"])
    _lsusb_text = (cp.stdout or b"").decode(errors="replace").lower()


def _usb_present(uri: str) -> bool:
    """¿Está físicamente conectada la impresora USB? (por fabricante, vía lsusb)."""
    u = (uri or "").lower()
    if not u.startswith("usb:"):
        return True
    if not _lsusb_text:
        return True   # lsusb no disponible: no bloquear por esto
    m = re.match(r"usb://([^/]+)/", u)
    vendor = (m.group(1).replace("%20", " ").strip() if m else "")
    key = vendor.split()[0] if vendor else ""   # p. ej. "brother"
    if not key:
        return True
    return key in _lsusb_text


def _ipp_state(name: str) -> tuple[str, str]:
    """Estado real de la cola vía ipptool: (mensaje, reasons) en minúsculas."""
    cp = _run(["ipptool", f"ipp://localhost/printers/{_safe_name(name)}", _IPPTOOL_TEST])
    out = (cp.stdout or b"").decode(errors="replace")
    msg = reasons = ""
    for line in out.splitlines():
        m = re.search(r"printer-state-message[^=]*=\s*(.*)", line)
        if m:
            msg = m.group(1).strip().lower()
        m = re.search(r"printer-state-reasons[^=]*=\s*(.*)", line)
        if m:
            reasons = m.group(1).strip().lower()
    return msg, reasons


def _compute_readiness(name: str, uri: str, accepting: bool) -> dict:
    """Sondeo activo: ¿esta impresora imprimirá si le mando algo AHORA?

    Señales reales, no lo que CUPS "cree":
      - USB → debe estar conectada (lsusb).
      - Red directa (IP) → sonda TCP.
      - Compartida (mDNS/otro equipo) → se resuelve el servidor por mDNS y se
        le pregunta por IPP el estado real de la cola.
      - En cualquier caso, el mensaje IPP delata «offline»/«waiting».
    """
    shared = _is_shared(uri)
    base = {"shared": shared}
    low = (uri or "").lower()
    p = _text(["lpstat", "-p", _safe_name(name)]).lower()
    if "disabled" in p:
        return {**base, "ready": False, "reason": "Deshabilitada en CUPS"}
    if not accepting:
        return {**base, "ready": False, "reason": "No acepta trabajos"}
    # Mensaje real del dispositivo (útil cuando hay un trabajo intentando salir).
    msg, reasons = _ipp_state(name)
    if next((k for k in _BAD_MESSAGES if k in msg), None):
        pretty = "apagada o sin conexión" if ("offline" in msg or "waiting" in msg) else msg
        return {**base, "ready": False, "reason": f"No responde: {pretty}"}
    if next((k for k in _BAD_REASONS if k in reasons), None):
        return {**base, "ready": False, "reason": f"La impresora reporta «{reasons}»"}
    # USB: presencia física.
    if low.startswith("usb:"):
        if not _usb_present(uri):
            return {**base, "ready": False, "reason": "Apagada o desconectada (USB)"}
        return {**base, "ready": True, "reason": ""}
    # Compartida (otro equipo): confirmar contra el servidor CUPS que la publica.
    if shared:
        target = _resolve_shared_uri(uri)
        if target:
            ok, why = _probe_shared_queue(target)
            if not ok:
                _shared_targets.pop(uri, None)   # re-resolver al siguiente ciclo
            return {**base, "ready": ok, "reason": why}
        return {**base, "ready": False,
                "reason": "Compartida: se comprueba al imprimir (depende de otro equipo)"}
    # Red directa por IP: sonda TCP real.
    ok, why = _probe_network(uri)
    if not ok:
        return {**base, "ready": False, "reason": why}
    return {**base, "ready": True, "reason": ""}


def refresh_readiness() -> None:
    """Recalcula la disponibilidad de todas las impresoras (para el monitor)."""
    if not cups_available():
        return
    _refresh_usb()
    uris = _device_uris()
    accepting = _accepting_map()
    fresh: dict[str, dict] = {}
    for name in uris:
        try:
            fresh[name] = _compute_readiness(name, uris[name], accepting.get(name, True))
        except PrinterError:
            fresh[name] = {"ready": False, "reason": "No se pudo consultar", "shared": False}
    with _readiness_lock:
        old = dict(_readiness_cache)
        _readiness_cache.clear()
        _readiness_cache.update(fresh)
    # Loguear solo transiciones (el monitor sondea cada pocos segundos).
    for name, r in fresh.items():
        prev = old.get(name)
        if prev is not None and prev.get("ready") == r["ready"]:
            continue
        if r["ready"]:
            if prev is not None:
                log.info("Impresora «%s» volvió a estar lista.", name)
        elif not r.get("shared"):
            log.warning("Impresora «%s» NO lista: %s", name, r.get("reason") or "?")


def _cached_readiness(name: str, uri: str, accepting: bool) -> dict:
    """Lee la disponibilidad del caché; si falta, la calcula al momento."""
    with _readiness_lock:
        r = _readiness_cache.get(name)
    if r is not None:
        return r
    r = _compute_readiness(name, uri, accepting)
    with _readiness_lock:
        _readiness_cache[name] = r
    return r


def printer_readiness(name: str) -> dict:
    """{ready, reason, shared} — sondeo activo al momento (sin caché)."""
    safe = _safe_name(name)
    if not _text(["lpstat", "-p", safe]).strip():
        return {"ready": False, "reason": "No existe o no está configurada", "shared": False}
    uri = _device_uris().get(safe, "")
    accepting = _accepting_map().get(safe, True)
    return _compute_readiness(safe, uri, accepting)


def start_readiness_monitor(interval: float = 8.0) -> None:
    """Arranca el hilo que sondea las impresoras en segundo plano."""
    global _monitor_started
    with _readiness_lock:
        if _monitor_started:
            return
        _monitor_started = True

    def _loop() -> None:
        while True:
            try:
                refresh_readiness()
            except Exception:
                log.exception("Fallo inesperado del monitor de impresoras "
                              "(el hilo sigue vivo).")
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="monitor-cups").start()
    log.debug("Monitor de impresoras arrancado (sondeo cada %.0f s).", interval)


def _probe_network(uri: str) -> tuple[bool, str]:
    """Sonda TCP corta para impresoras de red directas por IP."""
    low = (uri or "").lower()
    if not low or low.startswith("usb:") or _is_shared(uri):
        return True, ""
    m = re.match(r"^(socket|ipp|ipps|http|https)://([^/:]+)(?::(\d+))?", low)
    if not m:
        return True, ""
    scheme, host = m.group(1), m.group(2)
    port = int(m.group(3)) if m.group(3) else \
        (9100 if scheme == "socket" else 443 if scheme in ("ipps", "https") else 631)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True, ""
    except OSError:
        return False, f"No responde en la red ({host}:{port}). ¿Encendida y conectada?"


def preflight(name: str) -> tuple[bool, str]:
    """Puerta estricta ANTES de pedir la etiqueta a ML. (ok, motivo_legible)."""
    if not cups_available():
        return False, "CUPS no está activo (sudo systemctl start cups)."
    if not _text(["lpstat", "-p", _safe_name(name)]).strip():
        return False, "La impresora no existe o no está configurada."
    _refresh_usb()                       # presencia USB al momento
    r = printer_readiness(name)          # sondeo fresco (USB/red/ipp)
    return (True, "") if r["ready"] else (False, r["reason"])


# --- Vigilancia de trabajos (para no perder etiquetas en un lote) ------------
def _job_ids(args: list[str]) -> set[str]:
    ids: set[str] = set()
    for line in _text(args).splitlines():
        m = re.match(r"(\S+)\s", line.strip())
        if m:
            ids.add(m.group(1))
    return ids


def active_job_ids(name: str) -> set[str]:
    return _job_ids(["lpstat", "-o", _safe_name(name)])


def completed_job_ids(name: str) -> set[str]:
    return _job_ids(["lpstat", "-W", "completed", "-o", _safe_name(name)])


def printer_busy(name: str) -> bool:
    return bool(active_job_ids(name))


def wait_until_idle(name: str, timeout: float = 120.0) -> bool:
    """Espera a que la cola de la impresora se drene (backpressure)."""
    end = time.time() + timeout
    while time.time() < end:
        if not printer_busy(name):
            return True
        p = _text(["lpstat", "-p", _safe_name(name)]).lower()
        if "disabled" in p:
            return False
        time.sleep(1.0)
    return not printer_busy(name)


def wait_for_job(name: str, job_id: str, timeout: float = 240.0) -> tuple[str, str]:
    """Vigila un trabajo hasta que complete o falle.

    Devuelve ('completed'|'failed'|'timeout', motivo).
    """
    end = time.time() + timeout
    seen_active = False
    gone_checks = 0
    while time.time() < end:
        if job_id in completed_job_ids(name):
            return "completed", ""
        p = _text(["lpstat", "-p", _safe_name(name)]).lower()
        reasons = _state_reasons(name)
        jam = next((k for k in _BAD_REASONS if k in reasons), None)
        if "disabled" in p or jam:
            detail = f"La impresora se detuvo: {reasons}" if reasons and reasons != "none" \
                else "La impresora se detuvo o se atascó."
            return "failed", detail
        active = active_job_ids(name)
        if job_id in active:
            seen_active = True
            gone_checks = 0
        elif seen_active:
            # Estuvo activo y ya no: confirmar completado con un par de reintentos
            gone_checks += 1
            if gone_checks >= 2:
                return ("completed", "") if job_id in completed_job_ids(name) \
                    else ("failed", "El trabajo salió de la cola sin completarse.")
        time.sleep(1.0)
    return "timeout", "La impresora tardó demasiado (posible atasco o sin papel)."


def cancel_printer_jobs(name: str) -> None:
    _run(["cancel", "-a", _safe_name(name)])


def discover_devices() -> list[dict]:
    """Dispositivos detectables para dar de alta (`lpinfo -v`).

    Filtra a conexiones útiles: USB directas y destinos de red (ipp/dnssd/socket).
    """
    if not cups_available():
        raise PrinterError("CUPS no está activo.")
    devices: list[dict] = []
    for line in _text(["lpinfo", "-v"]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        kind, uri = parts  # kind: direct/network/serial/...
        low = uri.lower()
        if low.startswith("usb:"):
            devices.append({"connection": "USB", "uri": uri})
        elif low.startswith(("ipp:", "ipps:", "dnssd:", "socket:")) and "://" in low:
            # dnssd y ipp suelen traer modelo; lo dejamos tal cual
            devices.append({"connection": "Red", "uri": uri})
    return devices


# --- Alta / baja -------------------------------------------------------------
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_name(name: str) -> str:
    n = _NAME_RE.sub("_", name.strip()).strip("_")
    if not n:
        raise PrinterError("Nombre de impresora inválido.")
    return n[:60]


def add_printer(name: str, uri: str, everywhere: bool = True) -> dict:
    """Da de alta una impresora en CUPS por su device-uri (USB o red).

    everywhere=True usa el modelo driverless IPP Everywhere (recomendado para
    impresoras modernas, sin instalar PPD).
    """
    if not cups_available():
        raise PrinterError("CUPS no está activo.")
    if "://" not in uri:
        raise PrinterError("URI de dispositivo inválida.")
    safe = _safe_name(name)
    args = ["lpadmin", "-p", safe, "-E", "-v", uri]
    if everywhere:
        args += ["-m", "everywhere"]
    cp = _run(args)
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace").strip()
        # Reintento sin -m everywhere por si el modelo no aplica
        if everywhere:
            cp2 = _run(["lpadmin", "-p", safe, "-E", "-v", uri])
            if cp2.returncode == 0:
                log.info("Impresora «%s» dada de alta (%s) sin IPP Everywhere.", safe, uri)
                return {"name": safe, "uri": uri}
            err = (cp2.stderr or b"").decode(errors="replace").strip() or err
        log.error("No se pudo dar de alta «%s» (%s): %s", safe, uri, err or "lpadmin")
        raise PrinterError(f"No se pudo dar de alta la impresora: {err or 'error de lpadmin'}")
    log.info("Impresora «%s» dada de alta (%s).", safe, uri)
    return {"name": safe, "uri": uri}


def add_network_printer(name: str, ip: str, protocol: str = "ipp") -> dict:
    """Da de alta una impresora de red por IP.

    protocol: 'ipp' (driverless, recomendado para normales) o 'socket' (raw 9100).
    """
    ip = ip.strip()
    if not re.match(r"^[\w.\-]+(:\d+)?$", ip):
        raise PrinterError("Dirección IP/host inválida.")
    if protocol == "socket":
        host = ip if ":" in ip else f"{ip}:9100"
        return add_printer(name, f"socket://{host}", everywhere=False)
    # IPP Everywhere driverless
    uri = f"ipp://{ip}/ipp/print" if ":" not in ip else f"ipp://{ip}/ipp/print"
    return add_printer(name, uri, everywhere=True)


def remove_printer(name: str) -> None:
    if not cups_available():
        raise PrinterError("CUPS no está activo.")
    cp = _run(["lpadmin", "-x", _safe_name(name)])
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace").strip()
        raise PrinterError(f"No se pudo eliminar: {err or name}")
    log.info("Impresora «%s» eliminada de CUPS.", name)


def set_default(name: str) -> None:
    if not cups_available():
        raise PrinterError("CUPS no está activo.")
    cp = _run(["lpadmin", "-d", _safe_name(name)])
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace").strip()
        raise PrinterError(f"No se pudo fijar predeterminada: {err or name}")


# --- Impresión ---------------------------------------------------------------
def _lp(args: list[str], data: bytes) -> str:
    cp = _run(args, input_bytes=data)
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace").strip()
        raise PrinterError(f"Fallo al imprimir: {err or 'error de lp'}")
    out = (cp.stdout or b"").decode(errors="replace")
    m = re.search(r"request id is (\S+)", out)
    return m.group(1) if m else "encolado"


def print_bytes(printer: str, data: bytes, raw: bool = False, title: str = "etiqueta") -> str:
    """Envía bytes a una impresora CUPS. raw=True para ZPL (sin filtros)."""
    if not data:
        raise PrinterError("Documento vacío.")
    args = ["lp", "-d", _safe_name(printer), "-t", title]
    if raw:
        args += ["-o", "raw"]
    args += ["--", "-"]  # leer de stdin
    job = _lp(args, data)
    log.debug("Enviado «%s» a «%s» (%.0f KB%s) → job %s",
              title, printer, len(data) / 1024, ", raw" if raw else "", job)
    return job


def print_network_raw(ip: str, data: bytes, port: int = 9100) -> str:
    """Envía datos crudos (ZPL) a una impresora de red por socket (puerto 9100)."""
    host = ip.strip()
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(data)
        return f"enviado a {host}:{port}"
    except OSError as exc:
        raise PrinterError(f"No se pudo conectar a {host}:{port}: {exc}") from exc


# --- Diagnóstico de impresora ------------------------------------------------
def _parse_lpoptions(text: str) -> dict:
    """Parsea la salida `clave=valor` de `lpoptions -p` (valores con comillas)."""
    opts: dict[str, str] = {}
    for m in re.finditer(r"(\S+?)=('[^']*'|\"[^\"]*\"|\S*)", text):
        v = m.group(2)
        if v[:1] in ("'", '"'):
            v = v[1:-1]
        opts[m.group(1)] = v
    return opts


def _avail_defaults(text: str) -> dict:
    """De `lpoptions -p NAME -l` extrae {Etiqueta: opción por defecto (*)}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\S+/([^:]+):\s*(.+)", line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        default = next((c[1:] for c in m.group(2).split() if c.startswith("*")), "")
        if default:
            out[label] = default
    return out


def _classify_fault(state_num: str, reasons: str, connection: str) -> list[dict]:
    """Clasifica un posible fallo en sistema / hardware / software según CUPS."""
    r = (reasons or "").lower().strip()
    cats = [
        ("Hardware", ["media-empty", "media-needed", "media-jam", "jam", "cover-open",
                      "door-open", "marker-supply", "toner", "ink", "input-tray",
                      "output-area", "marker-waste", "marker-low"]),
        ("Sistema", ["offline", "connecting-to-device", "timed-out", "unreachable",
                     "shutdown", "com-failure", "network"]),
        ("Software", ["paused", "stopped", "hold", "rendering", "filter", "spool"]),
    ]
    if r in ("", "none"):
        if str(state_num) == "5":
            return [{"cat": "Software", "detail": "Cola detenida en CUPS; no es fallo fisico."}]
        return [{"cat": "OK", "detail": "Sin incidencias reportadas por la impresora."}]
    out = [{"cat": cat, "detail": f"Reporta: {reasons}"}
           for cat, kws in cats if any(k in r for k in kws)]
    return out or [{"cat": "Revisar", "detail": f"Estado no clasificado: {reasons}"}]


def diagnostics(name: str) -> dict:
    """Recopila la configuración y estado que reporta la impresora vía CUPS."""
    safe = _safe_name(name)
    opts = _parse_lpoptions(_text(["lpoptions", "-p", safe]))
    avail = _avail_defaults(_text(["lpoptions", "-p", safe, "-l"]))
    st = _text(["lpstat", "-l", "-p", safe])

    def _grab(pat):
        m = re.search(pat, st)
        return m.group(1).strip() if m else ""

    uri = opts.get("device-uri", _device_uris().get(safe, ""))
    state_num = opts.get("printer-state", "")
    state_txt = {"3": "Inactiva (idle)", "4": "Procesando (processing)",
                 "5": "Detenida (stopped)"}.get(state_num, state_num or "—")
    reasons = opts.get("printer-state-reasons", "none")
    return {
        "name": safe,
        "make_model": opts.get("printer-make-and-model", "—"),
        "uri": uri,
        "connection": _classify(uri),
        "interface": _grab(r"Interface:\s*(.+)"),
        "description": _grab(r"Description:\s*(.+)") or opts.get("printer-info", ""),
        "location": _grab(r"Location:\s*(.+)"),
        "cups_connection": _grab(r"Connection:\s*(.+)"),
        "state": state_txt,
        "state_reasons": reasons,
        "accepting": opts.get("printer-is-accepting-jobs", "—"),
        "color_mode": opts.get("print-color-mode", "—"),
        "enabled_since": _grab(r"enabled since (.+)"),
        "defaults": avail,
        "verdict": _classify_fault(state_num, reasons, _classify(uri)),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- Página de prueba (PostScript con color + diagnóstico) -------------------
def _ps_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _read_stamp() -> str:
    try:
        return STAMP_PATH.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""


def _build_test_ps(diag: dict) -> bytes:
    W, H, M = 612, 792, 36
    out: list[str] = ["%!PS-Adobe-3.0", "%%Pages: 1", "%%Title: EtiquetaFlow prueba",
                      "%%EndComments", "%%Page: 1 1"]

    def text(x, y, size, s, font="Helvetica", gray=0.0):
        out.append(f"{gray} setgray /{font} findfont {size} scalefont setfont "
                   f"{x:.1f} {y:.1f} moveto ({_ps_escape(str(s))}) show")

    def hline(y, w=0.7, gray=0.7):
        out.append(f"{gray} setgray {w} setlinewidth {M} {y:.1f} moveto {W-M} {y:.1f} lineto stroke")

    def fill(x, y, w, h, rgb=None, cmyk=None):
        if cmyk:
            out.append(f"{cmyk[0]} {cmyk[1]} {cmyk[2]} {cmyk[3]} setcmykcolor")
        else:
            out.append(f"{rgb[0]} {rgb[1]} {rgb[2]} setrgbcolor")
        out.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} rectfill")
        out.append(f"0.45 setgray 0.4 setlinewidth {x:.1f} {y:.1f} {w:.1f} {h:.1f} rectstroke")

    y = H - M
    text(M, y, 16, "EtiquetaFlow  -  Pagina de prueba y diagnostico", font="Helvetica-Bold")
    y -= 15
    text(M, y, 9, f"{diag['name']}  -  {diag['make_model']}", gray=0.25)
    y -= 12
    text(M, y, 9, f"Generado: {diag['timestamp']}   (compare con la hora de salida fisica para medir latencia)", gray=0.25)
    y -= 8
    hline(y, 0.9, 0.4)
    y -= 14

    # Sello (arte, sin rótulo)
    for ln in _read_stamp().split("\n"):
        out.append(f"0 setgray /Courier findfont 6 scalefont setfont {M} {y:.1f} moveto ({_ps_escape(ln)}) show")
        y -= 6.0
    y -= 8
    hline(y)
    y -= 14

    # Configuración e identificación
    text(M, y, 11, "Configuracion e identificacion", font="Helvetica-Bold")
    y -= 14
    rows = [
        ("Conexion", diag["connection"]),
        ("URI dispositivo", diag["uri"]),
        ("Interfaz / PPD", diag["interface"]),
        ("Conexion CUPS", diag["cups_connection"]),
        ("Modo de color", diag["color_mode"]),
        ("Acepta trabajos", diag["accepting"]),
        ("Habilitada desde", diag["enabled_since"]),
    ]
    for k, v in diag["defaults"].items():
        rows.append((f"Predet. {k}", v))
    for k, v in rows:
        text(M, y, 8.5, f"{k}:", font="Helvetica-Bold")
        text(M + 140, y, 8.5, str(v)[:70])
        y -= 11
    y -= 6
    hline(y)
    y -= 14

    # Estado y diagnóstico
    text(M, y, 11, "Estado y diagnostico", font="Helvetica-Bold")
    y -= 14
    text(M, y, 8.5, "Estado:", font="Helvetica-Bold"); text(M + 140, y, 8.5, diag["state"]); y -= 11
    text(M, y, 8.5, "state-reasons:", font="Helvetica-Bold"); text(M + 140, y, 8.5, diag["state_reasons"]); y -= 11
    for v in diag["verdict"]:
        text(M, y, 8.5, f"[{v['cat']}]", font="Helvetica-Bold")
        text(M + 140, y, 8.5, v["detail"][:70])
        y -= 11
    y -= 6
    hline(y)
    y -= 14

    # Prueba de color y alineación
    text(M, y, 11, "Prueba de color y alineacion", font="Helvetica-Bold")
    y -= 16
    swatches = [("C", None, (1, 0, 0, 0)), ("M", None, (0, 1, 0, 0)),
                ("Y", None, (0, 0, 1, 0)), ("K", None, (0, 0, 0, 1)),
                ("R", (1, 0, 0), None), ("G", (0, 1, 0), None),
                ("B", (0, 0, 1), None), ("W", (1, 1, 1), None)]
    bw = (W - 2 * M) / 8 - 4
    bh = 34
    x = M
    for label, rgb, cmyk in swatches:
        fill(x, y - bh, bw, bh, rgb=rgb, cmyk=cmyk)
        text(x + bw / 2 - 3, y - bh - 10, 8, label)
        x += bw + 4
    y -= bh + 22

    text(M, y, 8.5, "Escala de grises (degradado):", font="Helvetica-Bold")
    y -= 12
    steps = 12
    gw = (W - 2 * M) / steps
    for i in range(steps):
        g = i / (steps - 1)
        out.append(f"{g:.3f} setgray {M + i * gw:.1f} {y - 18:.1f} {gw:.1f} 18 rectfill")
    out.append(f"0.45 setgray 0.4 setlinewidth {M} {y - 18:.1f} {W - 2 * M} 18 rectstroke")
    y -= 30

    text(M, y, 8.5, "Marcas de registro (alineacion):", font="Helvetica-Bold")
    cx, cyt = W / 2, y - 26
    out.append(f"0 setgray 0.6 setlinewidth {cx - 22:.1f} {cyt:.1f} moveto {cx + 22:.1f} {cyt:.1f} lineto stroke")
    out.append(f"{cx:.1f} {cyt - 22:.1f} moveto {cx:.1f} {cyt + 22:.1f} lineto stroke")
    out.append(f"{cx:.1f} {cyt:.1f} 12 0 360 arc stroke")
    for mx in (M + 30, W - M - 30):
        out.append(f"0 setgray 0.6 setlinewidth {mx:.1f} {cyt - 10:.1f} moveto {mx:.1f} {cyt + 10:.1f} lineto stroke")
        out.append(f"{mx - 10:.1f} {cyt:.1f} moveto {mx + 10:.1f} {cyt:.1f} lineto stroke")

    text(M, M - 4, 7.5,
         "Si las barras CMYK/RGB no salen o el color es incorrecto: revise tinta/cartucho (hardware) o el driver/PPD (software). "
         "Si no sale nada: conexion (sistema).", gray=0.4)
    out.append("showpage")
    out.append("%%EOF")
    return ("\n".join(out) + "\n").encode("latin-1", "replace")


def test_print(printer: str) -> dict:
    """Imprime una página de prueba con color + diagnóstico y devuelve los datos."""
    diag = diagnostics(printer)
    ps = _build_test_ps(diag)
    job = print_bytes(printer, ps, raw=False, title="Prueba EtiquetaFlow")
    return {"job": job, "diagnostics": diag}
