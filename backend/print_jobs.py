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

import label_layout
import logutil
import orders_hub
import printers
import storage
from providers.base import ProviderError

log = logutil.get_logger("lotes")

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
        "origin": _job.get("origin", "manual"),
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


def start(items: list[dict], fmt: str, printer: str, origin: str = "manual") -> str:
    """Arranca un lote en segundo plano. Devuelve el job_id.

    origin: 'manual' (persona) o 'auto' (scheduler por reglas de horario).
    """
    global _job, _thread
    with _lock:
        if _job is not None and _job["running"]:
            raise BatchBusy("Ya hay un lote imprimiéndose.")
        job_id = uuid.uuid4().hex[:12]
        _job = {
            "id": job_id,
            "printer": printer,
            "format": fmt,
            "origin": origin,
            "items": [
                {
                    "shipment_id": str(it["shipment_id"]),
                    "order_id": it.get("order_id"),
                    "buyer_name": it.get("buyer_name"),
                    "product_summary": it.get("product_summary"),
                    "account_id": it.get("account_id"),
                    "account_name": it.get("account_name"),
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
    by_account: dict[str, int] = {}
    for it in items:
        key = str(it.get("account_name") or "¿sin tienda?")
        by_account[key] = by_account.get(key, 0) + 1
    log.info("Lote %s iniciado: %d etiqueta(s) (%s) → «%s», origen %s · %s",
             job_id, len(items), fmt, printer, origin,
             ", ".join(f"{n}: {c}" for n, c in sorted(by_account.items())))
    _stop.clear()
    _thread = threading.Thread(target=_run_safe, args=(job_id, fmt, printer),
                               daemon=True, name=f"lote-{job_id}")
    _thread.start()
    return job_id


# --- Ejecución ---------------------------------------------------------------
def _run_safe(job_id: str, fmt: str, printer: str) -> None:
    """Aísla el hilo del lote: un fallo inesperado se loguea y cierra el lote
    (sin esto quedaría 'imprimiendo' para siempre y sin rastro)."""
    try:
        _run(job_id, fmt, printer)
    except Exception:
        log.exception("Lote %s: fallo inesperado del motor de lotes", job_id)
        with _lock:
            if _job is not None and _job["id"] == job_id:
                _job["running"] = False
                _job["finished_at"] = int(time.time())
                _job["message"] = "Fallo inesperado del lote; revisa el log del servidor."


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
        account=item.get("account_name"),
        origin=_job.get("origin", "manual") if _job else "manual",
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
            log.error("Lote %s: «%s» no se liberó en 180 s — %d etiqueta(s) EN "
                      "RIESGO: %s", job_id, printer, len(b_items),
                      ", ".join(it["shipment_id"] for it in b_items))
            return False

        # Verificar justo antes de mandar la hoja.
        ok, reason = printers.preflight(printer)
        if not ok:
            for it in b_items:
                _set(it, "risk"); _record(it, fmt, "risk", printer, None,
                                          f"Impresora no lista al imprimir: {reason}")
            _msg(f"La impresora dejó de estar lista: {reason}")
            log.error("Lote %s: «%s» dejó de estar lista (%s) — %d etiqueta(s) EN "
                      "RIESGO: %s", job_id, printer, reason, len(b_items),
                      ", ".join(it["shipment_id"] for it in b_items))
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
            log.critical("Lote %s: fallo al enviar la hoja %d a «%s»: %s — %d "
                         "etiqueta(s) EN RIESGO", job_id, _job["current_sheet"],
                         printer, exc, len(b_items), exc_info=True)
            return False

        result, why = printers.wait_for_job(printer, job)
        if result == "completed":
            for it in b_items:
                _set(it, "done"); _record(it, fmt, "ok", printer, sheets, None)
            with _lock:
                _job["sheets_done"] += 1
            log.info("Lote %s: hoja %d confirmada (%d etiqueta(s), job CUPS %s)",
                     job_id, _job["current_sheet"], len(b_items), job)
            return True
        # Falló / timeout: ya pedidas a ML, sin confirmar → riesgo.
        for it in b_items:
            _set(it, "risk"); _record(it, fmt, "risk", printer, sheets, why)
        _msg(f"La hoja no se confirmó: {why}")
        log.error("Lote %s: hoja %d NO confirmada (job CUPS %s): %s — %d "
                  "etiqueta(s) EN RIESGO: %s", job_id, _job["current_sheet"],
                  job, why, len(b_items),
                  ", ".join(it["shipment_id"] for it in b_items))
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
            log.warning("Lote %s: bloqueado en la etiqueta %d/%d — «%s» no lista "
                        "(%s); %d etiqueta(s) siguen pendientes a salvo",
                        job_id, idx + 1, len(items), printer, reason,
                        len(items) - idx)
            with _lock:
                _job["running"] = False
                _job["finished_at"] = int(time.time())
            return

        _set(item, "printing")
        _msg(f"Pidiendo etiqueta {idx + 1} de {len(items)}…")
        try:
            pdf, _ct, _fn = orders_hub.get_label(item.get("account_id"), item["shipment_id"], fmt)
        except ProviderError as exc:
            # No se pudo pedir esta etiqueta (NO consumida). Imprime buffer y para.
            flush()
            _set(item, "blocked")
            _record(item, fmt, "blocked", printer, None, f"Error al pedir la etiqueta: {exc}")
            for rest in items[idx + 1:]:
                _set(rest, "blocked")
                _record(rest, fmt, "blocked", printer, None, "Detenido tras error de ML.")
            _msg(f"Error al pedir la etiqueta a Mercado Libre: {exc}")
            log.error("Lote %s: %s error al pedir la etiqueta del envío %s: %s — "
                      "lote detenido, %d etiqueta(s) bloqueadas a salvo",
                      job_id,
                      logutil.ctx(None, item.get("account_name")),
                      item["shipment_id"], exc, len(items) - idx)
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
        canceled = 0
        for it in items:
            if it["status"] == "pending":
                _set(it, "canceled")
                canceled += 1
        _msg("Lote detenido por el operador.")
        log.info("Lote %s detenido por el operador (%d etiqueta(s) canceladas "
                 "sin pedir).", job_id, canceled)
    else:
        with _lock:
            done = sum(1 for it in items if it["status"] == "done")
            risk = sum(1 for it in items if it["status"] == "risk")
        parts = [f"{done} impresa(s)"]
        if risk:
            parts.append(f"{risk} en riesgo (revisar)")
        _msg("Lote terminado: " + ", ".join(parts) + ".")
        summary = ("Lote %s terminado: %d impresa(s), %d en riesgo, "
                   "%d hoja(s), %.0f s")
        elapsed = time.time() - _job["started"]
        if risk:
            log.warning(summary, job_id, done, risk, _job["sheets_done"], elapsed)
        else:
            log.info(summary, job_id, done, risk, _job["sheets_done"], elapsed)

    with _lock:
        _job["running"] = False
        _job["finished_at"] = int(time.time())
