"""Mercado Asistente — servidor FastAPI (EtiquetaFlow).

Sirve la interfaz web (SPA) y expone la API local que orquesta OAuth y las
consultas a Mercado Libre. Escucha solo en 127.0.0.1 sobre HTTPS (ver run.sh).
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

import label_layout
import label_stub
import orders_hub
import pdf_import
import print_jobs
import printers
import scheduler
import storage
import tiktok_import
from config import FRONTEND_DIR, SITE_ID, STAMP_PATH
from providers.base import ProviderError
from providers.registry import CATALOG, get_provider

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


# --- Cuentas / tiendas (multi-proveedor) -------------------------------------
def _account_or_404(account_id: str) -> dict:
    acc = storage.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada.")
    return acc


@app.get("/api/providers")
def providers_catalog() -> JSONResponse:
    return JSONResponse({"providers": CATALOG})


@app.get("/api/accounts")
def accounts() -> JSONResponse:
    return JSONResponse({
        "accounts": [orders_hub.account_public(a) for a in storage.list_accounts()],
    })


@app.post("/api/accounts")
def save_account(provider: str = Form("ml"), name: str = Form(""),
                 app_id: str = Form(...), client_secret: str = Form(""),
                 redirect_uri: str = Form(""),
                 account_id: str = Form("")) -> JSONResponse:
    aid = account_id.strip() or uuid.uuid4().hex[:12]
    fields = {"provider": provider, "name": name.strip() or "Tienda",
              "app_id": app_id.strip(), "redirect_uri": redirect_uri.strip()}
    if provider == "ml":
        fields["site"] = SITE_ID   # en TikTok `site` guarda el shop_cipher: no pisarlo
    if client_secret.strip():   # vacío = conservar el existente
        fields["client_secret"] = client_secret.strip()
    storage.upsert_account(aid, **fields)
    return JSONResponse({"ok": True, "account_id": aid})


@app.delete("/api/accounts/{account_id}")
def del_account(account_id: str) -> JSONResponse:
    _account_or_404(account_id)
    storage.delete_account(account_id)
    return JSONResponse({"ok": True})


@app.post("/api/accounts/{account_id}/enabled")
def enable_account(account_id: str, enabled: str = Form("1")) -> JSONResponse:
    _account_or_404(account_id)
    storage.set_account_enabled(account_id, enabled in ("1", "true", "on", "True"))
    return JSONResponse({"ok": True})


@app.get("/api/accounts/{account_id}/connect")
def account_connect(account_id: str) -> JSONResponse:
    acc = _account_or_404(account_id)
    provider = get_provider(acc["provider"])
    # Proveedores sin redirect (Walmart): conectar = validar credenciales aquí.
    if provider.auth_mode == "client_credentials":
        try:
            provider.connect(acc)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"connected": True})
    state = uuid.uuid4().hex
    storage.set_value("oauthstate_" + state, account_id)
    try:
        url = provider.authorize_url(acc, state)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"authorization_url": url})


@app.get("/callback")
def callback(code: str | None = None, state: str | None = None,
             error: str | None = None) -> Response:
    """Redirección OAuth: resuelve la cuenta por 'state'."""
    if error:
        return _callback_html(False, f"El proveedor devolvió un error: {error}")
    if not code or not state:
        return _callback_html(False, "Faltan parámetros (code/state).")
    account_id = storage.get_value("oauthstate_" + state)
    if not account_id:
        return _callback_html(False, "Sesión de conexión no reconocida (state).")
    acc = storage.get_account(account_id)
    if not acc:
        return _callback_html(False, "La cuenta ya no existe.")
    try:
        get_provider(acc["provider"]).exchange_code(acc, code)
    except ProviderError as exc:
        return _callback_html(False, str(exc))
    storage.delete_value("oauthstate_" + state)
    return _callback_html(True, f"Tienda «{acc.get('name') or acc.get('nickname') or ''}» conectada.")


@app.post("/api/accounts/{account_id}/connect/manual")
def account_connect_manual(account_id: str, code: str = Form(...)) -> JSONResponse:
    acc = _account_or_404(account_id)
    try:
        get_provider(acc["provider"]).exchange_code(acc, code.strip())
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True})


@app.post("/api/accounts/{account_id}/refresh")
def account_refresh(account_id: str) -> JSONResponse:
    acc = _account_or_404(account_id)
    try:
        get_provider(acc["provider"]).refresh(acc)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True})


@app.post("/api/accounts/{account_id}/disconnect")
def account_disconnect(account_id: str) -> JSONResponse:
    _account_or_404(account_id)
    storage.clear_account_tokens(account_id)
    return JSONResponse({"ok": True})


@app.get("/api/status")
def status() -> JSONResponse:
    accs = storage.list_accounts()
    connected = [a for a in accs if a.get("refresh_token") and a.get("enabled")]
    return JSONResponse({
        "connected": bool(connected),
        "site": SITE_ID,
        "accounts_total": len(accs),
        "accounts_connected": len(connected),
    })


# --- Datos de ventas (cola combinada de todas las tiendas) -------------------
@app.get("/api/orders")
def orders() -> JSONResponse:
    data = orders_hub.list_all_pending()
    data["printed_today"] = storage.count_print_history_today()
    return JSONResponse(data)


# --- Etiquetas (respaldo en navegador) ---------------------------------------
@app.get("/api/label/{shipment_id}")
def label(shipment_id: str, account_id: str, format: str = "pdf") -> Response:
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    try:
        content, content_type, filename = orders_hub.get_label(account_id, shipment_id, fmt)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(content=content, media_type=content_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@app.get("/api/labels")
def labels(ids: str, account_id: str, format: str = "pdf") -> Response:
    """Etiqueta combinada de varios envíos de UNA cuenta (respaldo navegador)."""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    if not id_list:
        raise HTTPException(status_code=400, detail="No se indicaron envíos.")
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    try:
        pages = [orders_hub.get_label(account_id, sid, fmt)[0] for sid in id_list]
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if fmt == "pdf":
        content, _meta = (label_layout.pack_pdf_list(pages) if len(pages) > 1
                          else (pages[0], {}))
        ctype, fn = "application/pdf", "etiquetas.pdf"
    else:
        content, ctype, fn = b"".join(pages), "text/plain; charset=utf-8", "etiquetas.zpl"
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(content=content, media_type=ctype,
                    headers={"Content-Disposition": f'{disposition}; filename="{fn}"'})


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
        printer=printer, sheets=sheets, error=error, account=info.get("account_name"),
    )


@app.post("/api/print/{shipment_id}")
def print_label(shipment_id: str, format: str = Form("pdf"),
                printer: str = Form(""), meta: str = Form("")) -> JSONResponse:
    """Imprime una etiqueta: verifica → pide la etiqueta → imprime → confirma."""
    fmt = "zpl" if format.lower() == "zpl" else "pdf"
    info = _parse_meta(meta).get(str(shipment_id), {})
    account_id = info.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="Falta la tienda del envío (account_id).")
    target = _resolve_target(printer)

    # 1) Verificación previa: si la impresora no está lista, NO se pide la etiqueta.
    ok, reason = printers.preflight(target)
    if not ok:
        _record1(shipment_id, fmt, "blocked", info, target, None,
                 f"No se pidió la etiqueta: {reason}")
        raise HTTPException(
            status_code=409,
            detail=(f"No se imprimió para no perder la etiqueta: {reason} "
                    "La venta sigue pendiente."),
        )

    # 2) Pedir la etiqueta al proveedor de esa tienda (aquí se marca impresa).
    try:
        content, _ctype, _fn = orders_hub.get_label(account_id, shipment_id, fmt)
    except ProviderError as exc:
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


@app.get("/api/auto/default")
def auto_default() -> JSONResponse:
    import rules
    return JSONResponse(rules.default_rules())


@app.post("/api/auto/split")
def auto_split(shipment_id: str = Form(...), order_id: str = Form(...),
               account_id: str = Form(...), quantity: int = Form(1),
               reason: str = Form("DIMENSIONS_EXCEEDED")) -> JSONResponse:
    """Separa un envío en su marketplace (acción manual desde la lista)."""
    acc = _account_or_404(account_id)
    try:
        res = get_provider(acc["provider"]).split(acc, shipment_id, order_id, quantity, reason)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"ok": True, "result": res})


@app.post("/api/auto")
def auto_config(enabled: str = Form("0"), interval_min: int = Form(30),
                multiunit_threshold: int = Form(1),
                rules: str = Form("")) -> JSONResponse:
    rules_data = None
    if rules.strip():
        try:
            rules_data = json.loads(rules)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Reglas con formato inválido.")
    try:
        cfg = scheduler.set_config(
            enabled in ("1", "true", "on", "True"),
            interval_min, multiunit_threshold, rules_data)
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


_PROVIDER_BRANDS = {"ml": "MERCADO ENVIOS", "walmart": "WALMART", "tiktok": "TIKTOK SHOP"}


def _provider_label_size(provider: str) -> tuple[float, float, bool]:
    """Tamaño de etiqueta por proveedor (TikTok: guía recortada del PDF)."""
    if provider == "tiktok":
        return tiktok_import.GUIDE_W_PT, tiktok_import.GUIDE_H_PT, True
    return _label_size()


@app.get("/api/layout-plan")
def layout_plan(count: int = 1, provider: str = "") -> JSONResponse:
    """Cómo se acomodarán N etiquetas (para el texto en vivo)."""
    w, h, real = _provider_label_size(provider)
    stub = bool(provider) and label_stub.enabled_for(provider)
    if stub:
        h += label_stub.STUB_H_PT
    p = label_layout.plan(max(0, count), w, h)
    p["size_source"] = "real" if real else "estimado"
    p["label_cm"] = f"{w / 28.3465:.1f}×{h / 28.3465:.1f} cm"
    p["stub"] = stub
    return JSONResponse(p)


@app.get("/api/layout-preview")
def layout_preview(count: int = 4, provider: str = "") -> Response:
    """PDF de muestra con el acomodo real (sin tocar el marketplace ni gastar papel).

    Con `provider`, la muestra usa el tamaño de ese marketplace y, si el talón
    de control está activo para él, las etiquetas de muestra lo incluyen.
    """
    w, h, _real = _provider_label_size(provider)
    count = max(1, count)
    brand = _PROVIDER_BRANDS.get(provider, "MERCADO ENVIOS")
    sample = label_layout.sample_labels(count, w, h, brand=brand)
    if provider and label_stub.enabled_for(provider):
        sample = label_stub.add_stub(sample, label_stub.SAMPLE_PRODUCTS,
                                     order_ref="PEDIDO-DEMO")
    pdf = label_layout.build_preview(count, w, h, pages=sample)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="acomodo.pdf"'})


# --- Talón de control de producto (Walmart / TikTok) --------------------------
@app.get("/api/stub-config")
def stub_config() -> JSONResponse:
    cfg = label_stub.get_config()
    return JSONResponse({**cfg, "stub_h_pt": label_stub.STUB_H_PT})


@app.post("/api/stub-config")
def set_stub_config(provider: str = Form(""), account_id: str = Form(""),
                    enabled: str = Form("")) -> JSONResponse:
    """Ajusta el talón por marketplace (provider) o por tienda (account_id).

    Para account_id, enabled vacío borra la excepción (hereda del marketplace).
    """
    try:
        if account_id.strip():
            on = None if enabled == "" else enabled in ("1", "true", "on", "True")
            cfg = label_stub.set_account_enabled(account_id.strip(), on)
        elif provider.strip():
            cfg = label_stub.set_enabled(provider.strip(),
                                         enabled in ("1", "true", "on", "True"))
        else:
            raise HTTPException(status_code=400, detail="Falta provider o account_id.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/stub-preview")
def stub_preview(provider: str = "walmart") -> Response:
    """Una etiqueta de muestra tal como saldrá impresa con la config actual
    (con talón si está activo para ese marketplace; sin él si no)."""
    w, h, _real = _provider_label_size(provider)
    pdf = label_layout.sample_labels(1, w, h,
                                     brand=_PROVIDER_BRANDS.get(provider, "MUESTRA"))
    if label_stub.enabled_for(provider):
        pdf = label_stub.add_stub(pdf, label_stub.SAMPLE_PRODUCTS,
                                  order_ref="PEDIDO-DEMO")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="etiqueta.pdf"'})


# --- Importar PDF de etiquetas (TikTok Shop, Walmart) --------------------------
# Resultados en memoria para ver/imprimir tras importar (se conservan los últimos 5).
_PDF_IMPORTS: dict[str, dict] = {}


@app.post("/api/labels/import")
async def labels_pdf_import(file: UploadFile = File(...),
                            provider: str = Form("tiktok")) -> JSONResponse:
    if provider not in pdf_import.PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"Marketplace no soportado: {provider}")
    raw = await file.read()
    with_stub = label_stub.enabled_for(provider)
    try:
        pdf, meta = pdf_import.import_pdf(provider, raw, with_stub=with_stub)
    except pdf_import.PdfImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    packed, layout = label_layout.pack_labels_to_sheets(pdf)
    token = uuid.uuid4().hex[:12]
    while len(_PDF_IMPORTS) >= 5:
        _PDF_IMPORTS.pop(next(iter(_PDF_IMPORTS)))
    _PDF_IMPORTS[token] = {"pdf": packed, "meta": meta, "ts": int(time.time()),
                           "name": file.filename, "provider": provider}
    return JSONResponse({
        "ok": True, "token": token, "guides": len(meta), "layout": layout,
        "stub": with_stub, "provider": provider,
        "without_product": sum(1 for m in meta if not m["products"]),
        "items": [{"order_id": m["order_id"], "tracking": m["tracking"],
                   "products": m["products"]} for m in meta],
    })


def _pdf_import_or_404(token: str) -> dict:
    imp = _PDF_IMPORTS.get(token)
    if not imp:
        raise HTTPException(status_code=404,
                            detail="Importación no encontrada (vuelve a subir el PDF).")
    return imp


@app.get("/api/labels/import/{token}/pdf")
def labels_import_pdf_view(token: str) -> Response:
    imp = _pdf_import_or_404(token)
    return Response(content=imp["pdf"], media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="guias_importadas.pdf"'})


@app.post("/api/labels/import/{token}/print")
def labels_import_print(token: str, printer: str = Form("")) -> JSONResponse:
    imp = _pdf_import_or_404(token)
    target = _resolve_target(printer)
    ok, reason = printers.preflight(target)
    if not ok:
        raise HTTPException(status_code=409, detail=f"Impresora no lista: {reason}")
    batch = uuid.uuid4().hex[:10]
    try:
        job = printers.print_bytes(target, imp["pdf"], raw=False,
                                   title=f"import_{imp['provider']}_{token}")
    except printers.PrinterError as exc:
        _history_pdf_import(imp, batch, target, "error", str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    result, why = printers.wait_for_job(target, job)
    status = "ok" if result == "completed" else "risk"
    _history_pdf_import(imp, batch, target, status, why)
    if status == "ok":
        return JSONResponse({"ok": True, "printer": target, "job": job,
                             "guides": len(imp["meta"])})
    raise HTTPException(status_code=502, detail=(
        f"Se envió pero no se confirmó la impresión: {why} Revisa el Historial."))


def _history_pdf_import(imp: dict, batch: str, printer_name: str,
                        status: str, error: str | None) -> None:
    """Una fila de historial por guía importada (rastreable por tracking)."""
    account = f"{pdf_import.PROVIDERS.get(imp['provider'], imp['provider'])} (PDF)"
    for m in imp["meta"]:
        summary = "; ".join(f"{p['quantity']}× {p['title']}" for p in m["products"])[:200]
        storage.add_print_history(
            batch, m.get("tracking") or m.get("order_id") or imp["provider"],
            "pdf", status=status, order_id=m.get("order_id"),
            product_summary=summary or None, printer=printer_name,
            error=error, account=account)


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
