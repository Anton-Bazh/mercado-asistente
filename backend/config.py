"""Configuración central de Mercado Asistente.

Constantes, rutas de ficheros y endpoints de la API de Mercado Libre.
No contiene credenciales: estas se introducen desde la interfaz y se guardan
cifradas (ver storage.py).
"""
from __future__ import annotations

from pathlib import Path

# --- Rutas del proyecto -----------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CERTS_DIR = BASE_DIR / "certs"
FRONTEND_DIR = BASE_DIR / "frontend"

DB_PATH = DATA_DIR / "meli.db"
KEY_PATH = DATA_DIR / "secret.key"

# Sello del sistema (ASCII art). Se muestra en el arranque y en fallos críticos.
STAMP_PATH = Path(__file__).resolve().parent / "sello.txt"

CERT_FILE = CERTS_DIR / "cert.pem"
KEY_FILE = CERTS_DIR / "key.pem"

# --- Servidor local ---------------------------------------------------------
HOST = "127.0.0.1"  # solo loopback, nunca expuesto a la red
PORT = 8443

# --- Sitio de Mercado Libre -------------------------------------------------
# MLM = México. Cambiar el dominio de auth si se opera en otro país.
SITE_ID = "MLM"
AUTH_BASE = "https://auth.mercadolibre.com.mx"
API_BASE = "https://api.mercadolibre.com"

# Endpoints de la API
TOKEN_URL = f"{API_BASE}/oauth/token"
AUTHORIZATION_URL = f"{AUTH_BASE}/authorization"
USERS_ME_URL = f"{API_BASE}/users/me"
ORDERS_SEARCH_URL = f"{API_BASE}/orders/search"
SHIPMENTS_URL = f"{API_BASE}/shipments"
LABELS_URL = f"{API_BASE}/shipment_labels"

# --- Walmart Marketplace (México) --------------------------------------------
# Auth por client_credentials (sin redirect OAuth): token de 15 min que se
# renueva solo. Todas las llamadas llevan WM_MARKET=mx.
WALMART_API_BASE = "https://marketplace.walmartapis.com"
WALMART_TOKEN_URL = f"{WALMART_API_BASE}/v3/token"
WALMART_ORDERS_URL = f"{WALMART_API_BASE}/v3/orders"
WALMART_LABELS_URL = f"{WALMART_API_BASE}/v3/orders/labels"   # bulk (PDF/ZIP) por trackingNumbers
WALMART_LABEL_URL = f"{WALMART_API_BASE}/v3/orders/label"     # individual (PNG) por trackingNumber
WALMART_MARKET = "mx"
WALMART_SVC_NAME = "EtiquetaFlow"
WALMART_ORDERS_LOOKBACK_DAYS = 30   # ventana de búsqueda de pedidos pendientes

# --- TikTok Shop (Open Platform) ----------------------------------------------
# OAuth con código: el vendedor autoriza en auth.tiktok-shops.com y el code se
# canjea SIN firma; el resto de llamadas van firmadas (HMAC-SHA256 con el App
# Secret) contra open-api con el shop_cipher de la tienda autorizada.
TIKTOK_AUTH_BASE = "https://auth.tiktok-shops.com"
TIKTOK_AUTHORIZE_URL = f"{TIKTOK_AUTH_BASE}/oauth/authorize"
TIKTOK_TOKEN_URL = f"{TIKTOK_AUTH_BASE}/api/v2/token/get"
TIKTOK_REFRESH_URL = f"{TIKTOK_AUTH_BASE}/api/v2/token/refresh"
TIKTOK_API_BASE = "https://open-api.tiktokglobalshop.com"
TIKTOK_API_VERSION = "202309"

# --- Empaquetado de etiquetas (n-up) ----------------------------------------
# Hoja de destino para acomodar varias etiquetas y ahorrar papel. En puntos
# PostScript (1 pt = 1/72"). Carta = 8.5" x 11" = 612 x 792 pt (misma
# convención que la página de prueba de printers.py).
SHEET_WIDTH_PT = 612.0
SHEET_HEIGHT_PT = 792.0
# Margen exterior de la hoja y separación entre etiquetas (pt).
SHEET_MARGIN_PT = 18.0   # ~0.25"
LABEL_GAP_PT = 10.0      # separación entre celdas de la grilla

# Tamaño de etiqueta por defecto para la vista previa mientras no se conozca el
# real (10 x 15 cm, estándar de Mercado Envíos). 1 cm = 28.3465 pt.
DEFAULT_LABEL_W_PT = 10 * 28.3465
DEFAULT_LABEL_H_PT = 15 * 28.3465

# Margen (segundos) antes de la expiración para refrescar el token de forma proactiva
TOKEN_REFRESH_MARGIN = 300  # 5 minutos

# Tiempo de espera por defecto para llamadas HTTP
HTTP_TIMEOUT = 30.0
