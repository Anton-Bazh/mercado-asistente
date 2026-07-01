"""Flujo OAuth 2.0 de Mercado Libre.

Maneja:
  - construcción de la URL de autorización (con state CSRF + PKCE),
  - intercambio del authorization code por tokens,
  - refresh automático del access_token cuando está por expirar.

Todos los valores sensibles se guardan cifrados a través de storage.py.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

import storage
from config import (
    AUTHORIZATION_URL,
    TOKEN_URL,
    TOKEN_REFRESH_MARGIN,
    HTTP_TIMEOUT,
)


class AuthError(Exception):
    """Error en el flujo de autenticación."""


# --- PKCE --------------------------------------------------------------------
def _generate_pkce() -> tuple[str, str]:
    """Devuelve (code_verifier, code_challenge) según RFC 7636 (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# --- Configuración -----------------------------------------------------------
def is_configured() -> bool:
    return bool(
        storage.get_value(storage.APP_ID)
        and storage.get_value(storage.CLIENT_SECRET)
        and storage.get_value(storage.REDIRECT_URI)
    )


def is_connected() -> bool:
    return bool(storage.get_value(storage.REFRESH_TOKEN))


# --- Inicio del flujo --------------------------------------------------------
def build_authorization_url() -> str:
    """Construye la URL de autorización y guarda state + PKCE verifier."""
    if not is_configured():
        raise AuthError("Faltan credenciales. Configura la app primero.")

    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce()
    storage.set_value(storage.OAUTH_STATE, state)
    storage.set_value(storage.PKCE_VERIFIER, verifier)

    params = {
        "response_type": "code",
        "client_id": storage.get_value(storage.APP_ID),
        "redirect_uri": storage.get_value(storage.REDIRECT_URI),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


# --- Intercambio y persistencia de tokens ------------------------------------
def _store_tokens(payload: dict) -> None:
    """Guarda los tokens devueltos por el endpoint /oauth/token."""
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = int(payload.get("expires_in", 21600))
    if not access:
        raise AuthError("Respuesta de token inválida (sin access_token).")
    storage.set_value(storage.ACCESS_TOKEN, access)
    if refresh:  # el refresh_token rota en cada uso
        storage.set_value(storage.REFRESH_TOKEN, refresh)
    storage.set_value(storage.TOKEN_EXPIRES_AT, str(int(time.time()) + expires_in))


def exchange_code(code: str, state: str | None = None) -> None:
    """Intercambia el authorization code por tokens (grant_type=authorization_code)."""
    expected_state = storage.get_value(storage.OAUTH_STATE)
    if state is not None and expected_state and state != expected_state:
        raise AuthError("El parámetro 'state' no coincide (posible CSRF).")

    verifier = storage.get_value(storage.PKCE_VERIFIER)
    data = {
        "grant_type": "authorization_code",
        "client_id": storage.get_value(storage.APP_ID),
        "client_secret": storage.get_value(storage.CLIENT_SECRET),
        "code": code,
        "redirect_uri": storage.get_value(storage.REDIRECT_URI),
    }
    if verifier:
        data["code_verifier"] = verifier

    try:
        resp = httpx.post(
            TOKEN_URL,
            data=data,
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Error de red al obtener el token: {exc}") from exc

    if resp.status_code != 200:
        raise AuthError(_describe_token_error(resp))

    _store_tokens(resp.json())
    # Limpiar valores transitorios del login
    storage.delete_value(storage.OAUTH_STATE)
    storage.delete_value(storage.PKCE_VERIFIER)


def refresh_access_token() -> None:
    """Renueva el access_token usando el refresh_token (rota en cada uso)."""
    refresh = storage.get_value(storage.REFRESH_TOKEN)
    if not refresh:
        raise AuthError("No hay refresh_token; vuelve a conectar la cuenta.")

    data = {
        "grant_type": "refresh_token",
        "client_id": storage.get_value(storage.APP_ID),
        "client_secret": storage.get_value(storage.CLIENT_SECRET),
        "refresh_token": refresh,
    }
    try:
        resp = httpx.post(
            TOKEN_URL,
            data=data,
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Error de red al refrescar el token: {exc}") from exc

    if resp.status_code != 200:
        raise AuthError(_describe_token_error(resp))

    _store_tokens(resp.json())


def get_valid_access_token() -> str:
    """Devuelve un access_token válido, refrescándolo si está por expirar."""
    if not is_connected():
        raise AuthError("La cuenta no está conectada.")

    expires_at = storage.get_value(storage.TOKEN_EXPIRES_AT)
    access = storage.get_value(storage.ACCESS_TOKEN)
    needs_refresh = (
        not access
        or not expires_at
        or int(expires_at) - int(time.time()) <= TOKEN_REFRESH_MARGIN
    )
    if needs_refresh:
        refresh_access_token()
        access = storage.get_value(storage.ACCESS_TOKEN)
    return access


def disconnect() -> None:
    """Borra los tokens y datos de sesión (mantiene las credenciales de la app)."""
    for key in (
        storage.ACCESS_TOKEN,
        storage.REFRESH_TOKEN,
        storage.TOKEN_EXPIRES_AT,
        storage.SELLER_ID,
        storage.SELLER_NICKNAME,
        storage.OAUTH_STATE,
        storage.PKCE_VERIFIER,
    ):
        storage.delete_value(key)


def _describe_token_error(resp: httpx.Response) -> str:
    """Mensaje de error legible sin filtrar secretos."""
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error_description") or body.get("error")
    except Exception:
        msg = None
    return f"Mercado Libre devolvió {resp.status_code}: {msg or 'error desconocido'}"
