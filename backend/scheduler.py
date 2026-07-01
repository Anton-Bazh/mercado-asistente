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

import re
import threading
import time
from datetime import datetime

import auth
import label_layout
import meli_client
import print_jobs
import printers
import storage
from config import DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT

_lock = threading.Lock()
_started = False
_last_check_ts = 0.0
_status: dict = {
    "state": "off",
    "message": "Automático desactivado.",
    "last_run": None,
    "last_check": None,
}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --- Configuración -----------------------------------------------------------
def get_config() -> dict:
    return {
        "enabled": storage.get_value(storage.AUTO_ENABLED) == "1",
        "start": storage.get_value(storage.AUTO_START) or "01:00",
        "end": storage.get_value(storage.AUTO_END) or "05:00",
        "interval_min": int(storage.get_value(storage.AUTO_INTERVAL) or "60"),
    }


def set_config(enabled: bool, start: str, end: str, interval_min: int) -> dict:
    if not _HHMM.match(start or "") or not _HHMM.match(end or ""):
        raise ValueError("Horario inválido (usa HH:MM).")
    interval_min = max(5, min(int(interval_min), 720))
    storage.set_value(storage.AUTO_ENABLED, "1" if enabled else "0")
    storage.set_value(storage.AUTO_START, start)
    storage.set_value(storage.AUTO_END, end)
    storage.set_value(storage.AUTO_INTERVAL, str(interval_min))
    return get_config()


def status() -> dict:
    with _lock:
        st = dict(_status)
    cfg = get_config()
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    in_window = _in_window(now_min, _hm_to_min(cfg["start"]), _hm_to_min(cfg["end"]))
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
        "checks": {
            "connected": auth.is_connected(),
            "printer_ready": printer_ready,
            "size_known": size_known,
            "in_window": in_window,
        },
    }


def _set(state: str, message: str) -> None:
    with _lock:
        _status["state"] = state
        _status["message"] = message


# --- Utilidades --------------------------------------------------------------
def _hm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_window(now_min: int, start_min: int, end_min: int) -> bool:
    if start_min == end_min:
        return True                       # ventana de 24 h
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min   # cruza la medianoche


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

    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    start, end = _hm_to_min(cfg["start"]), _hm_to_min(cfg["end"])
    if not _in_window(now_min, start, end):
        _set("waiting_window", f"Fuera de horario. Ventana {cfg['start']}–{cfg['end']}.")
        return

    job = print_jobs.status()
    if job.get("active") and job.get("running"):
        _set("printing", "Imprimiendo un lote…")
        return

    now_ts = time.time()
    if now_ts - _last_check_ts < cfg["interval_min"] * 60:
        mins = int((cfg["interval_min"] * 60 - (now_ts - _last_check_ts)) / 60) + 1
        _set("waiting_interval", f"En horario. Próxima revisión en ~{mins} min.")
        return
    _last_check_ts = now_ts
    with _lock:
        _status["last_check"] = int(now_ts)

    if not auth.is_connected():
        _set("no_conn", "En horario, pero la cuenta de Mercado Libre no está conectada.")
        return

    w, h, real = _label_size()
    if not real:
        _set("no_size", "Falta imprimir una etiqueta manualmente una vez para aprender su tamaño.")
        return
    per = label_layout.plan(1000, w, h)["labels_per_sheet"]

    target = storage.get_value(storage.DEFAULT_PRINTER) or printers.system_default() or ""
    if not target:
        _set("no_printer", "En horario, pero no hay impresora predeterminada.")
        return
    ok, reason = printers.preflight(target)
    if not ok:
        _set("printer_not_ready", f"En horario, pero la impresora no está lista: {reason}")
        return

    try:
        rows, _recent = meli_client.list_ready_with_pending()
    except (auth.AuthError, meli_client.MeliError) as exc:
        _set("error", f"No se pudieron leer las ventas: {exc}")
        return

    pending = [r for r in rows if r.get("pending")]
    full = (len(pending) // per) * per        # solo hojas completas
    if full < per:
        _set("waiting_fill",
             f"Esperando etiquetas: {len(pending)} pendiente(s); faltan para llenar una hoja de {per}.")
        return

    items = [
        {
            "shipment_id": r["shipment_id"],
            "order_id": r.get("order_id"),
            "buyer_name": r.get("buyer_name"),
            "product_summary": _product_summary(r),
        }
        for r in pending[:full]
    ]
    try:
        print_jobs.start(items, "pdf", target)
        with _lock:
            _status["last_run"] = int(now_ts)
        _set("printing",
             f"Imprimiendo {full} etiqueta(s) en {full // per} hoja(s) en «{target}».")
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
                pass
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()
