"""Mercado Asistente — servidor FastAPI (EtiquetaFlow).

Sirve la interfaz web (SPA) y expone la API local que orquesta OAuth y las
consultas a Mercado Libre. Escucha solo en 127.0.0.1 sobre HTTPS (ver run.sh).
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

import auth
import label_layout
import meli_client
import print_jobs
import printers
import scheduler
import storage
from config import FRONTEND_DIR, SITE_ID, STAMP_PATH

app = FastAPI(title="Mercado Asistente", docs_url=None, redoc_url=None)


# --- Sello del sistema -------------------------------------------------------
def _load_stamp() -> str:
    try:
        return STAMP_PATH.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""


STAMP = _load_stamp()


def _stamp_banner(title: str) -> None:
    """Imprime el sello del sistema enmarcado en la consola del servidor."""
    seal_w = max((len(s) for s in STAMP.splitlines()), default=0)
    line = "═" * max(72, seal_w, len(title) + 4)
    print("\n" + line, flush=True)
    print(f"  {title}", flush=True)
    print(line, flush=True)
    if STAMP:
        print(STAMP, flush=True)
    print(line + "\n", flush=True)


@app.on_event("startup")
def _startup() -> None:
    storage.init()
    printers.start_readiness_monitor()   # sondeo activo de impresoras en 2.º plano
    scheduler.start_scheduler()          # modo automático por horario (servidor)
    _stamp_banner("EtiquetaFlow · sello de sistema · servidor iniciado")


# --- Vistas (HTML) -----------------------------------------------------------
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html", headers=_NO_CACHE)


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "app.js",
        media_type="application/javascript",
        headers=_NO_CACHE,
    )


@app.get("/api/stamp")
def stamp() -> JSONResponse:
    """Devuelve el sello del sistema (ASCII art) para la interfaz."""
    return JSONResponse({"stamp": STAMP})


# --- Configuración -----------------------------------------------------------
@app.get("/api/config")
def get_config() -> JSONResponse:
    """Estado de configuración. NUNCA devuelve el secret ni los tokens."""
    return JSONResponse(
        {
            "configured": auth.is_configured(),
            "app_id": storage.get_value(storage.APP_ID) or "",
            "redirect_uri": storage.get_value(storage.REDIRECT_URI) or "",
            "has_secret": bool(storage.get_value(storage.CLIENT_SECRET)),
        }
    )


@app.post("/api/config")
def save_config(
    app_id: str = Form(...),
    client_secret: str = Form(""),
    redirect_uri: str = Form(...),
) -> JSONResponse:
    storage.set_value(storage.APP_ID, app_id.strip())
    storage.set_value(storage.REDIRECT_URI, redirect_uri.strip())
    # El secret solo se actualiza si se envía uno nuevo (campo vacío = conservar)
    if client_secret.strip():
        storage.set_value(storage.CLIENT_SECRET, client_secret.strip())
    return JSONResponse({"ok": True, "configured": auth.is_configured()})


# --- OAuth -------------------------------------------------------------------
@app.get("/api/connect")
def connect() -> JSONResponse:
    try:
        url = auth.build_authorization_url()
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"authorization_url": url})


@app.get("/callback")
def callback(code: str | None = None, state: str | None = None,
             error: str | None = None) -> Response:
    """Recibe la redirección de Mercado Libre tras autorizar."""
    if error:
        return _callback_html(False, f"Mercado Libre devolvió un error: {error}")
    if not code:
        return _callback_html(False, "No se recibió el parámetro 'code'.")
    try:
        auth.exchange_code(code, state)
        meli_client.get_seller()  # cachea seller_id y nickname
    except (auth.AuthError, meli_client.MeliError) as exc:
        return _callback_html(False, str(exc))
    return _callback_html(True, "Cuenta conectada correctamente.")


@app.post("/api/connect/manual")
def connect_manual(code: str = Form(...)) -> JSONResponse:
    """Fallback: intercambiar un code pegado a mano (sin validar state)."""
    try:
        auth.exchange_code(code.strip(), state=None)
        meli_client.get_seller()
    except (auth.AuthError, meli_client.MeliError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True})


@app.post("/api/refresh")
def refresh() -> JSONResponse:
    """Fuerza la renovación del access_token."""
    try:
        auth.refresh_access_token()
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, "token_expires_in": _token_expires_in()})


@app.post("/api/disconnect")
def disconnect() -> JSONResponse:
    auth.disconnect()
    return JSONResponse({"ok": True})


@app.get("/api/status")
def status() -> JSONResponse:
    connected = auth.is_connected()
    return JSONResponse(
        {
            "configured": auth.is_configured(),
            "connected": connected,
            "site": SITE_ID,
            "nickname": storage.get_value(storage.SELLER_NICKNAME) if connected else None,
            "seller_id": storage.get_value(storage.SELLER_ID) if connected else None,
            "token_expires_in": _token_expires_in() if connected else None,
        }
    )


# --- Datos de ventas ---------------------------------------------------------
@app.get("/api/orders")
def orders() -> JSONResponse:
    if not auth.is_connected():
        raise HTTPException(status_code=409, detail="La cuenta no está conectada.")
    try:
        rows, _recent = meli_client.list_ready_with_pending()
    except (auth.AuthError, meli_client.MeliError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({
        "orders": rows,
        "printed_today": storage.count_print_history_today(),
    })


# --- Etiquetas ---------------------------------------------------------------
@app.get("/api/label/{shipment_id}")
def label(shipment_id: str, format: str = "pdf") -> Response:
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    try:
        content, content_type, filename = meli_client.get_label(shipment_id, fmt)
    except (auth.AuthError, meli_client.MeliError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@app.get("/api/labels")
def labels(ids: str, format: str = "pdf") -> Response:
    """Etiqueta combinada para varios envíos (ids separados por coma)."""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    if not id_list:
        raise HTTPException(status_code=400, detail="No se indicaron envíos.")
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    try:
        content, content_type, filename = meli_client.get_labels(id_list, fmt)
    except (auth.AuthError, meli_client.MeliError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if fmt == "pdf" and len(id_list) > 1:
        content, _meta = label_layout.pack_labels_to_sheets(content)
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


# --- Impresoras (CUPS) -------------------------------------------------------
@app.get("/api/printers")
def get_printers() -> JSONResponse:
    try:
        items = printers.list_printers()
        cups = True
    except printers.PrinterError:
        items, cups = [], printers.cups_available()
    # Predeterminada propia de EtiquetaFlow: None=no fijada (cae a la del sistema),
    # ""=ninguna explícita, "X"=elegida.
    app_default = storage.get_value(storage.DEFAULT_PRINTER)
    sys_default = next((p["name"] for p in items if p.get("system_default")), None)
    effective = app_default if app_default is not None else sys_default
    for p in items:
        p["is_default"] = bool(effective) and p["name"] == effective
    return JSONResponse({"cups": cups, "printers": items,
                         "app_default": app_default, "system_default": sys_default})


@app.get("/api/printers/devices")
def get_printer_devices() -> JSONResponse:
    try:
        return JSONResponse({"devices": printers.discover_devices()})
    except printers.PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/printers")
def add_printer(
    name: str = Form(...),
    uri: str = Form(""),
    ip: str = Form(""),
    protocol: str = Form("ipp"),
) -> JSONResponse:
    try:
        if uri.strip():
            res = printers.add_printer(name, uri.strip())
        elif ip.strip():
            res = printers.add_network_printer(name, ip, protocol)
        else:
            raise HTTPException(status_code=400, detail="Indica una IP o una URI de dispositivo.")
    except printers.PrinterError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, **res})


@app.delete("/api/printers/{name}")
def del_printer(name: str) -> JSONResponse:
    try:
        printers.remove_printer(name)
    except printers.PrinterError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True})


@app.post("/api/printers/{name}/default")
def default_printer(name: str) -> JSONResponse:
    """Fija la predeterminada propia de EtiquetaFlow (sin tocar el SO)."""
    if not any(p["name"] == name for p in printers.list_printers()):
        raise HTTPException(status_code=404, detail="Impresora no encontrada.")
    storage.set_value(storage.DEFAULT_PRINTER, name)
    return JSONResponse({"ok": True})


@app.post("/api/printers/{name}/undefault")
def undefault_printer(name: str) -> JSONResponse:
    """Quita la predeterminada (ninguna queda fijada en EtiquetaFlow)."""
    storage.set_value(storage.DEFAULT_PRINTER, "")
    return JSONResponse({"ok": True})


@app.post("/api/printers/{name}/test")
def test_printer(name: str) -> JSONResponse:
    # No enviar una prueba a una impresora que no puede imprimir.
    ok, reason = printers.preflight(name)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"No se puede probar «{name}»: {reason}",
        )
    try:
        res = printers.test_print(name)
    except printers.PrinterError as exc:
        _stamp_banner(f"FALLO CRÍTICO DE IMPRESIÓN · prueba en «{name}» · {exc}")
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"ok": True, **res})


# --- Impresión server-side (etiqueta -> impresora) ---------------------------
def _parse_meta(meta: str) -> dict:
    """Metadatos opcionales por envío enviados por la UI (JSON keyed por id)."""
    if not meta.strip():
        return {}
    try:
        data = json.loads(meta)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _resolve_target(printer: str) -> str:
    target = printer.strip()
    if not target:
        app_default = storage.get_value(storage.DEFAULT_PRINTER)
        target = app_default or printers.system_default() or ""
        if not target:
            raise HTTPException(
                status_code=409,
                detail="No hay impresora seleccionada ni predeterminada. Elige una en «Dispositivos».",
            )
    return target


def _record1(sid: str, fmt: str, status_val: str, info: dict,
             printer: str | None, sheets: int | None, error: str | None) -> None:
    storage.add_print_history(
        batch_id=uuid.uuid4().hex[:12], shipment_id=str(sid), fmt=fmt,
        status=status_val, order_id=info.get("order_id"),
        buyer_name=info.get("buyer_name"), product_summary=info.get("product_summary"),
        printer=printer, sheets=sheets, error=error,
    )


@app.post("/api/print/{shipment_id}")
def print_label(shipment_id: str, format: str = Form("pdf"),
                printer: str = Form(""), meta: str = Form("")) -> JSONResponse:
    """Imprime una etiqueta: verifica → pide a ML → imprime → confirma."""
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    info = _parse_meta(meta).get(str(shipment_id), {})
    target = _resolve_target(printer)

    # 1) Verificación previa: si la impresora no está lista, NO se pide a ML.
    ok, reason = printers.preflight(target)
    if not ok:
        _record1(shipment_id, fmt, "blocked", info, target, None,
                 f"No se pidió a ML: {reason}")
        raise HTTPException(
            status_code=409,
            detail=(f"No se imprimió para no perder la etiqueta: {reason} "
                    "La venta sigue pendiente en Mercado Libre."),
        )

    # 2) Pedir la etiqueta (aquí ML la marca como impresa).
    try:
        content, _ctype, _fn = meli_client.get_label(shipment_id, fmt)
    except (auth.AuthError, meli_client.MeliError) as exc:
        _record1(shipment_id, fmt, "blocked", info, target, None, str(exc))
        raise HTTPException(status_code=502, detail=str(exc))

    # 3) Enviar a la impresora.
    try:
        job = printers.print_bytes(target, content, raw=(fmt == "zpl"),
                                   title=f"venta_{shipment_id}")
    except printers.PrinterError as exc:
        _stamp_banner(f"FALLO CRÍTICO DE IMPRESIÓN · impresora «{target}» · {exc}")
        _record1(shipment_id, fmt, "risk", info, target, None, str(exc))
        raise HTTPException(status_code=502, detail=(
            f"Se pidió la etiqueta pero la impresora falló: {exc} "
            "Quedó marcada EN RIESGO: revísala en Historial."))

    # 4) Vigilar que el trabajo complete de verdad.
    result, why = printers.wait_for_job(target, job)
    if result == "completed":
        _record1(shipment_id, fmt, "ok", info, target, 1, None)
        return JSONResponse({"ok": True, "printer": target, "job": job})
    _record1(shipment_id, fmt, "risk", info, target, None, why)
    raise HTTPException(status_code=502, detail=(
        f"Se envió pero no se confirmó la impresión: {why} "
        "Quedó EN RIESGO: revísala en Historial."))


@app.post("/api/print-batch")
def print_batch(ids: str = Form(...), format: str = Form("pdf"),
                printer: str = Form(""), meta: str = Form("")) -> JSONResponse:
    """Arranca el motor de lote en segundo plano (impresión hoja por hoja)."""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    if not id_list:
        raise HTTPException(status_code=400, detail="No se indicaron envíos.")
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    target = _resolve_target(printer)
    meta_map = _parse_meta(meta)
    items = [{"shipment_id": sid, **meta_map.get(sid, {})} for sid in id_list]
    try:
        job_id = print_jobs.start(items, fmt, target)
    except print_jobs.BatchBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse({"ok": True, "job_id": job_id, "printer": target,
                         "count": len(id_list)})


@app.get("/api/batch/status")
def batch_status() -> JSONResponse:
    return JSONResponse(print_jobs.status())


# --- Modo automático (impresión por horario) ---------------------------------
@app.get("/api/auto")
def auto_status() -> JSONResponse:
    return JSONResponse(scheduler.status())


@app.post("/api/auto")
def auto_config(enabled: str = Form("0"), start: str = Form("01:00"),
                end: str = Form("05:00"), interval_min: int = Form(60)) -> JSONResponse:
    try:
        cfg = scheduler.set_config(enabled in ("1", "true", "on", "True"),
                                   start.strip(), end.strip(), interval_min)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, "config": cfg})


@app.post("/api/batch/stop")
def batch_stop() -> JSONResponse:
    return JSONResponse({"ok": print_jobs.stop()})


# --- Vista previa del acomodo (n-up) -----------------------------------------
def _label_size() -> tuple[float, float, bool]:
    """Tamaño de etiqueta a usar (real si ya se aprendió, si no el estándar)."""
    from config import DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT
    w = storage.get_value(storage.LABEL_W)
    h = storage.get_value(storage.LABEL_H)
    if w and h:
        try:
            return float(w), float(h), True
        except ValueError:
            pass
    return DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT, False


@app.get("/api/layout-plan")
def layout_plan(count: int = 1) -> JSONResponse:
    """Cómo se acomodarán N etiquetas (para el texto en vivo)."""
    w, h, real = _label_size()
    p = label_layout.plan(max(0, count), w, h)
    p["size_source"] = "real" if real else "estimado"
    p["label_cm"] = f"{w / 28.3465:.1f}×{h / 28.3465:.1f} cm"
    return JSONResponse(p)


@app.get("/api/layout-preview")
def layout_preview(count: int = 4) -> Response:
    """PDF de muestra con el acomodo real (sin tocar ML ni gastar papel)."""
    w, h, _real = _label_size()
    pdf = label_layout.build_preview(max(1, count), w, h)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="acomodo.pdf"'})


# --- Historial de impresión --------------------------------------------------
@app.get("/api/print-history")
def print_history(limit: int = 50, offset: int = 0,
                  date_from: int | None = None, date_to: int | None = None,
                  format: str | None = None, result: str | None = None) -> JSONResponse:
    fmt = format if format in ("pdf", "zpl") else None
    res = result if result in ("ok", "error", "risk", "blocked") else None
    items, total = storage.list_print_history(
        limit=max(1, min(limit, 500)), offset=max(0, offset),
        date_from=date_from, date_to=date_to, fmt=fmt, result=res,
    )
    return JSONResponse({"items": items, "total": total,
                         "printed_today": storage.count_print_history_today(),
                         "risk_total": storage.count_risk()})


# --- Utilidades --------------------------------------------------------------
def _token_expires_in() -> int:
    """Segundos restantes de validez del access_token (0 si no hay)."""
    expires_at = storage.get_value(storage.TOKEN_EXPIRES_AT)
    if not expires_at:
        return 0
    return max(0, int(expires_at) - int(time.time()))


def _callback_html(ok: bool, message: str) -> Response:
    """Página simple de retorno tras el OAuth; vuelve a la app en 2.5 s."""
    color = "#15824a" if ok else "#c43232"
    icon = "✓" if ok else "✕"
    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Mercado Asistente</title>
<style>body{{font-family:'IBM Plex Sans',system-ui,sans-serif;background:#f4f5f8;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#181b21}}
.card{{background:#fff;border:1px solid #e7e9ee;border-radius:14px;padding:32px 40px;
text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.05)}}
.icon{{width:48px;height:48px;border-radius:50%;background:{color};color:#fff;
font-size:24px;display:flex;align-items:center;justify-content:center;margin:0 auto 16px}}
a{{color:#2f6bf0;text-decoration:none;font-weight:600}}</style>
<meta http-equiv="refresh" content="2.5;url=/"></head>
<body><div class="card"><div class="icon">{icon}</div>
<div style="font-weight:600;font-size:16px;margin-bottom:6px">{message}</div>
<div style="color:#8a92a0;font-size:13px">Volviendo a EtiquetaFlow… o <a href="/">entrar ahora</a></div>
</div></body></html>"""
    return Response(content=html, media_type="text/html")
