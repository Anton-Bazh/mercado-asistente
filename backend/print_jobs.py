"""Motor de impresión por lotes — seguro y vigilado.

Objetivo: nunca perder etiquetas. La API de Mercado Libre marca un envío como
`printed` en cuanto se pide su etiqueta, aunque físicamente no salga. Por eso el
lote NO se pide de golpe: se procesa **hoja por hoja**, pidiendo a ML solo lo de
la hoja en curso, empaquetando varias etiquetas por hoja (ahorro de papel),
esperando a que la impresora se libere (sin sobrecargar la cola) y **vigilando**
que cada trabajo complete de verdad. Si una hoja falla (atasco, impresora
detenida), se **detiene** el lote y esas etiquetas —ya pedidas a ML— se marcan
como **en riesgo** para revisarlas/reimprimirlas a mano (no quedan en limbo).

Corre en un hilo en segundo plano; la interfaz consulta el progreso y puede
detenerlo. Un solo lote activo a la vez (app local de un operador).
"""
from __future__ import annotations

import threading
import time
import uuid

import auth
import label_layout
import meli_client
import printers
import storage

# Estados por envío que ve la interfaz.
#   pending  = en espera
#   printing = pedida a ML / en la hoja actual
#   done     = impresa y confirmada
#   risk     = pedida a ML pero sin confirmar impresión (revisar)
#   blocked  = no se pidió a ML (impresora no lista) → sigue pendiente, a salvo
#   canceled = cancelada por el operador antes de pedirla

_lock = threading.Lock()
_job: dict | None = None       # estado del lote activo (o último terminado)
_thread: threading.Thread | None = None
_stop = threading.Event()


class BatchBusy(Exception):
    """Ya hay un lote en curso."""


def _snapshot() -> dict:
    """Copia serializable del estado del lote (bajo lock del llamador)."""
    if _job is None:
        return {"active": False, "job_id": None, "items": [], "counts": {},
                "running": False, "finished": True, "message": ""}
    counts: dict[str, int] = {}
    for it in _job["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {
        "active": True,
        "job_id": _job["id"],
        "printer": _job["printer"],
        "format": _job["format"],
        "total": len(_job["items"]),
        "counts": counts,
        "current_sheet": _job["current_sheet"],
        "sheets_done": _job["sheets_done"],
        "running": _job["running"],
        "finished": not _job["running"],
        "message": _job["message"],
        "items": [dict(i) for i in _job["items"]],
        "started": _job["started"],
        "finished_at": _job["finished_at"],
    }


def status() -> dict:
    with _lock:
        return _snapshot()


def stop() -> bool:
    """Solicita detener el lote en curso."""
    with _lock:
        if _job is None or not _job["running"]:
            return False
        _job["message"] = "Deteniendo… (se termina la hoja en curso)"
    _stop.set()
    return True


def start(items: list[dict], fmt: str, printer: str) -> str:
    """Arranca un lote en segundo plano. Devuelve el job_id."""
    global _job, _thread
    with _lock:
        if _job is not None and _job["running"]:
            raise BatchBusy("Ya hay un lote imprimiéndose.")
        job_id = uuid.uuid4().hex[:12]
        _job = {
            "id": job_id,
            "printer": printer,
            "format": fmt,
            "items": [
                {
                    "shipment_id": str(it["shipment_id"]),
                    "order_id": it.get("order_id"),
                    "buyer_name": it.get("buyer_name"),
                    "product_summary": it.get("product_summary"),
                    "status": "pending",
                }
                for it in items
            ],
            "current_sheet": 0,
            "sheets_done": 0,
            "running": True,
            "message": "Preparando…",
            "started": int(time.time()),
            "finished_at": None,
        }
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(job_id, fmt, printer), daemon=True)
    _thread.start()
    return job_id


# --- Ejecución ---------------------------------------------------------------
def _set(item: dict, status_val: str) -> None:
    with _lock:
        item["status"] = status_val


def _msg(text: str) -> None:
    with _lock:
        if _job is not None:
            _job["message"] = text


def _record(item: dict, fmt: str, status_val: str, printer: str,
            sheets: int | None, error: str | None) -> None:
    storage.add_print_history(
        batch_id=_job["id"] if _job else "batch",
        shipment_id=item["shipment_id"], fmt=fmt, status=status_val,
        order_id=item.get("order_id"), buyer_name=item.get("buyer_name"),
        product_summary=item.get("product_summary"),
        printer=printer, sheets=sheets, error=error,
    )


def _run(job_id: str, fmt: str, printer: str) -> None:
    items = _job["items"]
    per_sheet = 1 if fmt == "zpl" else None   # PDF: se mide con la 1ª etiqueta
    buffer: list[tuple[dict, bytes]] = []

    def flush() -> bool:
        """Imprime la hoja acumulada y confirma. True si salió bien."""
        nonlocal buffer
        if not buffer:
            return True
        group = buffer
        buffer = []
        b_items = [it for it, _ in group]

        # Backpressure: no sobrecargar; esperar a que la impresora se libere.
        _msg(f"Esperando a que la impresora termine… (hoja {_job['current_sheet']})")
        if not printers.wait_until_idle(printer, timeout=180):
            for it in b_items:
                _set(it, "risk"); _record(it, fmt, "risk", printer, None,
                                          "La impresora no se liberó a tiempo.")
            _msg("La impresora no se liberó; se detuvo el lote.")
            return False

        # Verificar justo antes de mandar la hoja.
        ok, reason = printers.preflight(printer)
        if not ok:
            for it in b_items:
                _set(it, "risk"); _record(it, fmt, "risk", printer, None,
                                          f"Impresora no lista al imprimir: {reason}")
            _msg(f"La impresora dejó de estar lista: {reason}")
            return False

        # Empaquetar la hoja (n-up en PDF; ZPL crudo concatenado).
        if fmt == "zpl":
            data = b"".join(pdf for _, pdf in group)
            sheets = None
        else:
            data, meta = label_layout.pack_pdf_list([pdf for _, pdf in group])
            sheets = meta.get("sheets")

        _msg(f"Imprimiendo hoja {_job['current_sheet']} ({len(b_items)} etiqueta(s))…")
        try:
            job = printers.print_bytes(printer, data, raw=(fmt == "zpl"),
                                       title=f"lote_{job_id}_h{_job['current_sheet']}")
        except printers.PrinterError as exc:
            for it in b_items:
                _set(it, "risk"); _record(it, fmt, "risk", printer, sheets, str(exc))
            _msg(f"Fallo al enviar la hoja: {exc}")
            return False

        result, why = printers.wait_for_job(printer, job)
        if result == "completed":
            for it in b_items:
                _set(it, "done"); _record(it, fmt, "ok", printer, sheets, None)
            with _lock:
                _job["sheets_done"] += 1
            return True
        # Falló / timeout: ya pedidas a ML, sin confirmar → riesgo.
        for it in b_items:
            _set(it, "risk"); _record(it, fmt, "risk", printer, sheets, why)
        _msg(f"La hoja no se confirmó: {why}")
        return False

    stopped = False
    for idx, item in enumerate(items):
        if _stop.is_set():
            stopped = True
            break
        # Verificar impresora ANTES de pedir la etiqueta a ML.
        ok, reason = printers.preflight(printer)
        if not ok:
            # Imprime lo ya pedido (buffer) para no dejarlo en limbo; bloquea el resto.
            flush()
            for rest in items[idx:]:
                _set(rest, "blocked")
                _record(rest, fmt, "blocked", printer, None,
                        f"No se pidió a ML: {reason}")
            _msg(f"Bloqueado para no perder etiquetas: {reason}")
            with _lock:
                _job["running"] = False
                _job["finished_at"] = int(time.time())
            return

        _set(item, "printing")
        _msg(f"Pidiendo etiqueta {idx + 1} de {len(items)} a Mercado Libre…")
        try:
            pdf, _ct, _fn = meli_client.get_label(item["shipment_id"], fmt)
        except (auth.AuthError, meli_client.MeliError) as exc:
            # No se pudo pedir esta etiqueta (NO consumida en ML). Imprime buffer y para.
            flush()
            _set(item, "blocked")
            _record(item, fmt, "blocked", printer, None, f"Error al pedir a ML: {exc}")
            for rest in items[idx + 1:]:
                _set(rest, "blocked")
                _record(rest, fmt, "blocked", printer, None, "Detenido tras error de ML.")
            _msg(f"Error al pedir la etiqueta a Mercado Libre: {exc}")
            with _lock:
                _job["running"] = False
                _job["finished_at"] = int(time.time())
            return

        buffer.append((item, pdf))
        if per_sheet is None:                    # medir con la primera etiqueta
            try:
                per_sheet = label_layout.per_sheet_for(pdf)
            except Exception:
                per_sheet = 1
        if len(buffer) >= per_sheet:
            with _lock:
                _job["current_sheet"] += 1
            if not flush():
                with _lock:
                    _job["running"] = False
                    _job["finished_at"] = int(time.time())
                return

    # Hoja final parcial (o vaciar buffer al detener).
    if buffer:
        with _lock:
            _job["current_sheet"] += 1
        flush()

    # Marcar como cancelados los que no se llegaron a tocar.
    if stopped:
        for it in items:
            if it["status"] == "pending":
                _set(it, "canceled")
        _msg("Lote detenido por el operador.")
    else:
        with _lock:
            done = sum(1 for it in items if it["status"] == "done")
            risk = sum(1 for it in items if it["status"] == "risk")
        parts = [f"{done} impresa(s)"]
        if risk:
            parts.append(f"{risk} en riesgo (revisar)")
        _msg("Lote terminado: " + ", ".join(parts) + ".")

    with _lock:
        _job["running"] = False
        _job["finished_at"] = int(time.time())
