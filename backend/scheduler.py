"""Modo automático: impresión por horario en el servidor.

Corre en un hilo de fondo dentro del proceso del servidor, así que funciona
**sin que nadie tenga la página abierta** (mientras el servidor esté encendido).
Dentro de una ventana horaria (p. ej. la madrugada) revisa cada N minutos y,
si hay etiquetas pendientes suficientes para **llenar al menos una hoja
completa** (n-up), imprime esas hojas con el motor seguro. El sobrante que no
llena una hoja espera al siguiente ciclo (nunca imprime media hoja).

Requisitos para disparar: servidor encendido, cuenta de ML conectada, impresora
predeterminada **lista** y tamaño de etiqueta ya aprendido (de una impresión
previa). Si algo falta, no imprime y lo explica en el estado.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import label_layout
import logutil
import orders_hub
import print_jobs
import printers
import rules
import storage
from config import DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT
from providers.base import ProviderError

log = logutil.get_logger("auto")

# Estados que ameritan aviso (WARNING) cuando el automático quiere imprimir
# pero no puede; el resto de transiciones se loguean como INFO/DEBUG.
_WARN_STATES = {"error", "printer_not_ready", "no_printer", "no_size", "no_conn"}
_QUIET_STATES = {"waiting_interval", "idle_ok", "off", "paused", "waiting_fill"}

_lock = threading.Lock()
_started = False
_last_check_ts = 0.0
_status: dict = {
    "state": "off",
    "message": "Automático desactivado.",
    "last_run": None,
    "last_check": None,
}


# --- Configuración -----------------------------------------------------------
def get_config() -> dict:
    try:
        thr = int(storage.get_value(storage.MULTIUNIT_THRESHOLD) or "1")
    except ValueError:
        thr = 1
    return {
        "enabled": storage.get_value(storage.AUTO_ENABLED) == "1",
        "interval_min": int(storage.get_value(storage.AUTO_INTERVAL) or "30"),
        "multiunit_threshold": thr,
        "rules": rules.get_rules(),
    }


def set_config(enabled: bool, interval_min: int, multiunit_threshold: int,
               rules_data: dict | None = None) -> dict:
    interval_min = max(5, min(int(interval_min), 720))
    storage.set_value(storage.AUTO_ENABLED, "1" if enabled else "0")
    storage.set_value(storage.AUTO_INTERVAL, str(interval_min))
    storage.set_value(storage.MULTIUNIT_THRESHOLD, str(max(1, int(multiunit_threshold))))
    if rules_data is not None:
        rules.set_rules(rules_data)     # valida y guarda
    return get_config()


def status() -> dict:
    with _lock:
        st = dict(_status)
    cfg = get_config()
    mode, label = rules.current_mode()
    _w, _h, size_known = _label_size()
    target = storage.get_value(storage.DEFAULT_PRINTER) or printers.system_default() or ""
    printer_ready = False
    if target:
        try:
            printer_ready = printers.printer_readiness(target)["ready"]
        except printers.PrinterError:
            printer_ready = False
    return {
        **st,
        "config": cfg,
        "printer": target,
        "mode_now": mode,
        "mode_label": label,
        "checks": {
            "connected": bool(orders_hub.connected_accounts()),
            "printer_ready": printer_ready,
            "size_known": size_known,
        },
    }


def _set(state: str, message: str) -> None:
    with _lock:
        changed = _status["state"] != state
        _status["state"] = state
        _status["message"] = message
    # Solo transiciones (el ciclo repite el mismo estado cada pocos segundos).
    if changed:
        if state in _WARN_STATES:
            log.warning("%s → %s", state, message)
        elif state in _QUIET_STATES:
            log.debug("%s → %s", state, message)
        else:
            log.info("%s → %s", state, message)


# --- Utilidades --------------------------------------------------------------
def _label_size() -> tuple[float, float, bool]:
    w = storage.get_value(storage.LABEL_W)
    h = storage.get_value(storage.LABEL_H)
    if w and h:
        try:
            return float(w), float(h), True
        except ValueError:
            pass
    return DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT, False


def _product_summary(r: dict) -> str:
    ps = r.get("products") or []
    if not ps:
        return "—"
    p0 = ps[0]
    extra = f" +{len(ps) - 1}" if len(ps) > 1 else ""
    return f"{p0.get('title', '—')} x{p0.get('quantity', 1)}{extra}"


# --- Ciclo -------------------------------------------------------------------
def _tick() -> None:
    global _last_check_ts
    cfg = get_config()
    if not cfg["enabled"]:
        _set("off", "Automático desactivado.")
        return

    mode, label = rules.current_mode()
    if mode == "pausa":
        _set("paused", f"En pausa: {label}.")
        return

    job = print_jobs.status()
    if job.get("active") and job.get("running"):
        _set("printing", "Imprimiendo un lote…")
        return

    now_ts = time.time()
    if now_ts - _last_check_ts < cfg["interval_min"] * 60:
        mins = int((cfg["interval_min"] * 60 - (now_ts - _last_check_ts)) / 60) + 1
        _set("waiting_interval", f"Activo ({mode}: {label}). Próxima revisión en ~{mins} min.")
        return
    _last_check_ts = now_ts
    with _lock:
        _status["last_check"] = int(now_ts)

    if not orders_hub.connected_accounts():
        _set("no_conn", "No hay ninguna tienda conectada.")
        return

    w, h, real = _label_size()
    if not real:
        _set("no_size", "Falta imprimir una etiqueta manualmente una vez para aprender su tamaño.")
        return
    per = label_layout.plan(1000, w, h)["labels_per_sheet"]

    target = storage.get_value(storage.DEFAULT_PRINTER) or printers.system_default() or ""
    if not target:
        _set("no_printer", "No hay impresora predeterminada.")
        return
    ok, reason = printers.preflight(target)
    if not ok:
        _set("printer_not_ready", f"La impresora no está lista: {reason}")
        return

    try:
        rows = orders_hub.list_all_pending()["orders"]
    except ProviderError as exc:
        _set("error", f"No se pudieron leer las ventas: {exc}")
        return

    # Multi-unidad NUNCA entra al automático (se gestiona manual en Separación).
    # Tampoco los "próximos": solo se imprime lo que el marketplace marca para hoy.
    pending = [r for r in rows if r.get("pending") and not r.get("multi_unit")
               and r.get("due") != "upcoming" and r.get("printable") is not False]
    if not pending:
        _set("idle_ok", f"Activo ({label}). No hay pendientes para imprimir hoy.")
        return

    if mode == "forzar":
        # Vaciado: imprime TODO lo pendiente, aunque la hoja no se llene.
        selected = pending
    else:  # ahorro
        full = (len(pending) // per) * per
        if full < per:
            _set("waiting_fill",
                 f"Ahorro ({label}): {len(pending)} pendiente(s); faltan para llenar una hoja de {per}.")
            return
        selected = pending[:full]

    items = [
        {
            "shipment_id": r["shipment_id"],
            "order_id": r.get("order_id"),
            "buyer_name": r.get("buyer_name"),
            "product_summary": _product_summary(r),
            "account_id": r.get("account_id"),
            "account_name": r.get("account_name"),
        }
        for r in selected
    ]
    try:
        print_jobs.start(items, "pdf", target, origin="auto")
        with _lock:
            _status["last_run"] = int(now_ts)
        sheets = -(-len(items) // per)   # techo
        _set("printing",
             f"{('Vaciado' if mode == 'forzar' else 'Ahorro')} ({label}): "
             f"imprimiendo {len(items)} etiqueta(s) en ~{sheets} hoja(s) en «{target}».")
    except print_jobs.BatchBusy:
        _set("printing", "Ya hay un lote en curso.")


def start_scheduler(interval: float = 30.0) -> None:
    """Arranca el hilo del modo automático (una sola vez)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop() -> None:
        while True:
            try:
                _tick()
            except Exception:
                log.exception("Fallo inesperado en el ciclo del modo automático "
                              "(el hilo sigue vivo).")
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="auto").start()
    log.debug("Hilo del modo automático arrancado (revisión cada %.0f s).", interval)
