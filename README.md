# Mercado Asistente · EtiquetaFlow

Aplicación web **local** para gestionar e imprimir etiquetas de envío de Mercado Libre.
Sin IA: solo algoritmos tradicionales y la API REST oficial de Mercado Libre.

Interfaz **EtiquetaFlow**: panel con barra lateral y 5 secciones —
**Cola en tiempo real**, **Control de impresión**, **Dispositivos**,
**Conexión Mercado Libre** y **Logs**.

- **Backend:** Python + FastAPI + Uvicorn (HTTPS local, solo `127.0.0.1`).
- **Frontend:** SPA en HTML + CSS (IBM Plex) + JS vanilla. Sin paso de build ni dependencias JS.
- **Seguridad:** Client Secret y tokens cifrados en reposo (Fernet); OAuth con state CSRF + PKCE.

## Secciones

| Sección | Datos reales | Notas |
|---------|-------------|-------|
| Cola en tiempo real | Ventas `ready_to_ship`, KPIs, impresión por venta | «Impresas hoy» es contador local del día |
| Control de impresión | Formato PDF/ZPL, imprimir siguiente / todas / reimprimir | Modo automático por horario es informativo (requiere servicio en 2.º plano) |
| Dispositivos | Impresoras CUPS (USB + red) | Alta por IP/USB, predeterminada, prueba; impresión server-side. Requiere `cupsd` activo |
| Conexión Mercado Libre | Estado OAuth, token, sincronización, credenciales | Alta de credenciales y conexión aquí |
| Logs | Registro real de eventos de la sesión | Filtrable por nivel (OK/INFO/WARN/ERROR) |

## Arranque

```bash
cd ~/mercado-asistente
./run.sh
```

El script crea el entorno virtual, instala dependencias, genera un certificado
autofirmado y levanta el servidor en **https://localhost:8443**.

> El navegador mostrará un aviso de certificado autofirmado (es localhost): acéptalo una vez.

## Configuración inicial

1. Abre **https://localhost:8443** → barra lateral → **Conexión Mercado Libre**.
2. En *Credenciales de la aplicación* pega tu **App ID**, **Client Secret** y **Redirect URI**.
   - En tu app de [developers.mercadolibre.com.mx](https://developers.mercadolibre.com.mx)
     registra exactamente este Redirect URI: `https://localhost:8443/callback`.
3. Guarda y pulsa **Conectar con Mercado Libre**.
4. Tras autorizar, vuelves conectado. La sección **Cola en tiempo real** lista las
   ventas `ready_to_ship`; imprime con los botones **PDF**/**ZPL** o desde **Control de impresión**.

Si el callback automático fallara, usa **"Canjear código"** (misma sección)
pegando el parámetro `code` de la URL de retorno.

## Notas

- El `access_token` (6 h) se **refresca solo**; el `refresh_token` (6 meses) rota
  en cada uso y se vuelve a guardar.
- Sitio configurado: **MLM (México)**. Para otro país, edita `AUTH_BASE` y
  `SITE_ID` en `backend/config.py`.
- Datos y claves viven en `data/` y `certs/` (ambos ignorados por git).

## Estructura

```
backend/    config.py · storage.py · auth.py · meli_client.py · main.py
frontend/   index.html (SPA EtiquetaFlow) · app.js
data/       meli.db · secret.key   (cifrado, perms 0600)
certs/      cert.pem · key.pem      (autofirmado)
run.sh      lanzador
```
