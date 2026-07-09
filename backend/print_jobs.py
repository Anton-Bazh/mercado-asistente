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
from datetime import datetime, timezone

import label_enrich
import label_layout
import label_verify
import logutil
import orders_hub
import printers
import storage
import supa
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
        "operador": _job.get("operador", ""),
        "code_lote": _job.get("code_lote", ""),
        "total": len(_job["items"]),
        "counts": counts,
        "current_sheet": _job["current_sheet"],
        "sheets_done": _job["sheets_done"],
        "running": _job["running"],
        "finished": not _job["running"],
        "message": _job["message"],
        "items": [{k: v for k, v in i.items() if k != "_reg"} for i in _job["items"]],
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


def start(items: list[dict], fmt: str, printer: str, origin: str = "manual",
         operador: str = "", code_lote: str = "") -> str:
    """Arranca un lote en segundo plano. Devuelve el job_id.

    origin: 'manual' (persona) o 'auto' (scheduler por reglas de horario).
    operador/code_lote: identifican el registro de etiquetas (unificación con
    el Extractor, Fase 3) — solo aplica a envíos de Mercado Libre en formato
    PDF; Walmart/TikTok siguen imprimiendo sin registrar (Cambio 3.2 pendiente).
    """
    global _job, _thread
    with _lock:
        if _job is not None and _job["running"]:
            raise BatchBusy("Ya hay un lote imprimiéndose.")

    # Markups del lote en UNA sola consulta (no por envío) — solo aplica a
    # Mercado Libre en PDF, alcance de registro de las fases 1-3.
    markups: dict[str, dict] = {}
    if fmt == "pdf":
        pack_ids = []
        for it in items:
            acc = storage.get_account(it.get("account_id") or "")
            if acc and acc.get("provider") == "ml":
                info = orders_hub.order_info(str(it.get("shipment_id") or ""))
                if info and info.get("pack_id"):
                    pack_ids.append(info["pack_id"])
        if pack_ids:
            markups = supa.get_markups(pack_ids)

    with _lock:
        if _job is not None and _job["running"]:
            raise BatchBusy("Ya hay un lote imprimiéndose.")
        job_id = uuid.uuid4().hex[:12]
        _job = {
            "id": job_id,
            "printer": printer,
            "format": fmt,
            "origin": origin,
            "operador": operador,
            "code_lote": code_lote,
            "markups": markups,     # pack_id → {markup, tienda} (una sola consulta)
            "folio_next": {},       # organización → siguiente folio a asignar
            "folio_min": None,      # rango de folios confirmados del lote (auditoría)
            "folio_max": None,
            "first_code": None,     # code_i.first_code: 1er barcode confirmado del lote
            "v_code_written": False,
            "audit_needed": False,  # hubo al menos un envío confirmado con saldo negativo
            "audit_venta_id": None,
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


def _registration_context(item: dict, fmt: str) -> dict | None:
    """Datos para registrar el envío (unificación con el Extractor, Fase 3).

    None si no aplica: alcance de las fases 1-3 es SOLO Mercado Libre en PDF
    (Walmart/TikTok siguen imprimiendo sin registrar hasta el Cambio 3.2), o si
    el pedido no está en caché (p. ej. tras un reinicio del servidor) — nunca
    bloquea la impresión, solo se pierde el registro de esa etiqueta.
    """
    if fmt != "pdf":
        return None
    acc = storage.get_account(item.get("account_id") or "")
    if not acc or acc.get("provider") != "ml":
        return None
    info = orders_hub.order_info(item["shipment_id"])
    if not info:
        log.debug("Lote: envío %s sin datos de pedido en caché; se imprime "
                  "SIN registrar en etiquetas_i.", item["shipment_id"])
        return None
    pack_id = info.get("pack_id")
    markup_info = _job["markups"].get(str(pack_id), {}) if pack_id else {}
    organization = label_enrich.normalize_company(
        markup_info.get("tienda") or item.get("account_name"))
    return {"info": info, "pack_id": pack_id, "markup": markup_info.get("markup"),
            "organization": organization}


def _stub_stamp_for(item: dict, fmt: str) -> dict | None:
    """Bloque de estampado del talón removible para Walmart/TikTok (Cambio
    3.2) — Mercado Libre no pasa por aquí: su etiqueta ya trae el estampado
    directo (_registration_context + label_enrich.enrich). Usa el mismo
    contador de folio por tienda que ML (D1); sin punto rojo porque Walmart/
    TikTok aún no tienen una fuente de saldo/markup (nota de Antonio, guía de
    unificación §Cambio 3.2) — se activará cuando se defina esa fuente."""
    if fmt != "pdf":
        return None
    acc = storage.get_account(item.get("account_id") or "")
    if not acc or acc.get("provider") not in ("walmart", "tiktok"):
        return None
    organization = label_enrich.normalize_company(item.get("account_name"))
    return label_enrich.stub_stamp(
        folio=_next_folio(organization), company=organization,
        batch_code=_job.get("code_lote") or "")


def _next_folio(organization: str) -> int:
    """Folio consecutivo por empresa/tienda (D1, 08-jul-2026)."""
    if organization not in _job["folio_next"]:
        _job["folio_next"][organization] = supa.max_folio(organization) + 1
    with _lock:
        folio = _job["folio_next"][organization]
        _job["folio_next"][organization] += 1
    return folio


def _register_confirmed(b_items: list[dict], job_id: str) -> None:
    """Al confirmar una hoja: inserta etiquetas_i (una fila por SKU) y, en la
    primera hoja confirmada del lote, v_code. Nunca detiene ni arriesga el
    lote: cualquier fallo de la BD local solo se loguea."""
    rows: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in b_items:
        reg = it.get("_reg")
        if not reg:
            continue
        info = reg["info"]
        receiver = info.get("receiver") or {}
        for p in (info.get("products") or []):
            rows.append({
                "code": reg.get("code"), "sales_num": info.get("order_id"),
                "product": p.get("title"), "sku": p.get("sku"),
                "quantity": p.get("quantity"), "deli_date": info.get("delivery_estimate"),
                "organization": reg["organization"],
                "sou_file": f"API:ml lote {job_id}",
                "personal_inc": _job.get("operador") or "",
                "hour": now_iso, "imp_date": now_iso,
                "client": receiver.get("name"), "cp": receiver.get("zip"),
                "state": receiver.get("state"), "city": receiver.get("city"),
                "folio": reg.get("folio"), "client_name": receiver.get("name"),
                "code_i": _job.get("code_lote") or "", "pack_id": reg.get("pack_id"),
            })
        with _lock:
            folio = reg.get("folio")
            if folio is not None:
                if _job["folio_min"] is None or folio < _job["folio_min"]:
                    _job["folio_min"] = folio
                if _job["folio_max"] is None or folio > _job["folio_max"]:
                    _job["folio_max"] = folio
            if reg.get("markup") is not None and reg["markup"] <= label_enrich.LOW_MARKUP:
                _job["audit_needed"] = True
                if _job["audit_venta_id"] is None:
                    _job["audit_venta_id"] = reg.get("pack_id")

    if rows:
        try:
            supa.insert_etiquetas(rows)
        except supa.SupaError:
            log.exception("Lote %s: fallo al registrar etiquetas en BD local "
                          "(la impresión SIGUE confirmada; no se pierde).", job_id)

    with _lock:
        need_vcode = not _job["v_code_written"] and _job["first_code"]
    if need_vcode:
        try:
            supa.insert_v_code({
                "code_i": _job.get("code_lote") or "", "corte_etiquetas": None,
                "personal_inc": _job.get("operador") or "",
                "first_code": _job["first_code"], "personal_bar": None,
            })
            with _lock:
                _job["v_code_written"] = True
        except supa.SupaError:
            log.exception("Lote %s: fallo al registrar v_code.", job_id)


def _finish(job_id: str) -> None:
    """Cierra el lote: si algún envío confirmado quedó con saldo negativo,
    registra la auditoría (una fila por lote); siempre marca 'terminado'."""
    if _job is not None and _job["id"] == job_id and _job["audit_needed"]:
        try:
            op = supa.lookup_operador(_job.get("operador") or "") or {}
            supa.insert_auditoria({
                "venta_id": str(_job.get("audit_venta_id") or ""),
                "folio_venta": (f"{_job['folio_min']}-{_job['folio_max']}"
                               if _job["folio_min"] is not None else ""),
                "auditor_uid": op.get("uid"),
                "auditor_nombre_completo": op.get("nombre_completo") or _job.get("operador"),
                "auditor_email": op.get("email"),
                "fecha_revision": datetime.now(timezone.utc).isoformat(),
                "code_i": _job.get("code_lote") or "",
            })
        except supa.SupaError:
            log.exception("Lote %s: fallo al registrar la auditoría de saldo negativo.", job_id)
    with _lock:
        if _job is not None and _job["id"] == job_id:
            _job["running"] = False
            _job["finished_at"] = int(time.time())


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
            _register_confirmed(b_items, job_id)
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
            _finish(job_id)
            return

        _set(item, "printing")
        _msg(f"Pidiendo etiqueta {idx + 1} de {len(items)}…")
        stamp = _stub_stamp_for(item, fmt)
        try:
            pdf, _ct, _fn = orders_hub.get_label(item.get("account_id"), item["shipment_id"],
                                                 fmt, stamp=stamp)
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
            _finish(job_id)
            return

        # Cuadre PDF↔API + estampado + folio (unificación con el Extractor,
        # Fase 3) — solo Mercado Libre en PDF; nunca bloquea la impresión.
        reg = _registration_context(item, fmt)
        if reg is not None:
            verify_res = label_verify.verify(pdf, "ml", reg["info"].get("tracking_number"))
            reg.update(verify_res)
            reg["folio"] = _next_folio(reg["organization"])
            try:
                pdf = label_enrich.enrich(
                    pdf, folio=reg["folio"], company=reg["organization"],
                    batch_code=_job.get("code_lote") or "", markup=reg.get("markup"))
            except Exception:
                log.warning("Lote %s: no se pudo estampar el envío %s (sale sin "
                            "estampado; el registro sigue intacto).",
                            job_id, item["shipment_id"], exc_info=True)
            item["_reg"] = reg
            with _lock:
                if not _job["first_code"] and verify_res.get("code"):
                    _job["first_code"] = verify_res["code"]

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
                _finish(job_id)
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

    _finish(job_id)
