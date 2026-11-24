"""Sessão do site: um token curto que só o site oficial do Fomenta BR consegue pedir.

Os editais ficam fora da API pública por enquanto. O site pede um token em `POST /v1/sessao`
(o navegador sempre manda `Origin` num POST, e só as origens em `FOMENTA_SITE_ORIGINS` recebem
token). O token vale 30 minutos, fica preso ao IP de quem pediu (não adianta copiar para uma
rede de robôs) e é assinado com `FOMENTA_SITE_SECRET`. Sem esse segredo definido, cada instância
sorteia o seu (funciona localmente; em produção, defina a variável).

Não é uma barreira absoluta: um robô que finge ser navegador consegue um token. Por isso o token
vem junto de limites próprios e mais apertados por IP, sem cache público e com robots.txt/noindex.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

TTL = 30 * 60
DEFAULT_ORIGINS = ("http://localhost:5173", "http://localhost:5174", "http://localhost:4173",
                   "http://127.0.0.1:5173", "http://127.0.0.1:5174", "http://127.0.0.1:4173")
_FALLBACK_SECRET = secrets.token_bytes(32)


def _secret() -> bytes:
    s = os.environ.get("FOMENTA_SITE_SECRET")
    return s.encode() if s else _FALLBACK_SECRET


def allowed_origins() -> set[str]:
    raw = os.environ.get("FOMENTA_SITE_ORIGINS")
    items = raw.split(",") if raw else DEFAULT_ORIGINS
    return {o.strip().rstrip("/") for o in items if o.strip()}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ip_tag(ip: str) -> str:
    return hashlib.sha256(f"fomenta:{ip}".encode()).hexdigest()[:16]


def issue(ip: str, now: float | None = None) -> tuple[str, int]:
    exp = int((now or time.time()) + TTL)
    payload = _b64(json.dumps({"ip": _ip_tag(ip), "exp": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}", exp


def verify(token: str | None, ip: str, now: float | None = None) -> bool:
    if not token or token.count(".") != 1:
        return False
    payload, sig = token.split(".")
    good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return False
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return data.get("ip") == _ip_tag(ip) and int(data.get("exp", 0)) > (now or time.time())
