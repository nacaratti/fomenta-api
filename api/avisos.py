"""Avisos de editais por notificação (Web Push): inscrições do site e onde elas ficam guardadas.

O site pede permissão de notificação, inscreve o aparelho no serviço de push do navegador e manda
para cá o endereço da inscrição e o que a pessoa segue (agências, laboratórios, pesquisadores).
O envio é feito por `tools/notificar_editais.py`, depois de cada atualização dos editais.

Não guardamos nome, e-mail nem IP: só o endereço da inscrição (um link do serviço de push), as
duas chaves públicas que o navegador gera e a lista do que é seguido. Cancelar apaga tudo.

Guarda em Redis com API REST (Upstash: `UPSTASH_REDIS_REST_URL`/`_TOKEN` ou `KV_REST_API_*`).
Sem Redis, num arquivo JSON local (`FOMENTA_AVISOS_FILE`, padrão `.data/avisos.json`), que serve para
desenvolvimento; na Vercel, sem Redis, as rotas respondem 503.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response

from api import sessao
from api.contador import counter
from api.guard import NO_STORE, client_ip

router = APIRouter(prefix="/v1/avisos", include_in_schema=False)

#: serviços de push dos navegadores; o envio só vai para estes endereços
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
              "push.apple.com", "notify.windows.com")
KINDS = {
    "agencias": re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$"),
    "laboratorios": re.compile(r"^\d{1,9}$"),
    "pesquisadores": re.compile(r"^A\d{1,15}$"),
}
MAX_PER_KIND = 50


# --------------------------------------------------------------------------- armazenamento
class StoreUnavailable(Exception):
    pass


class RedisStore:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    def _pipe(self, *cmds: list[str]) -> list[Any]:
        req = urllib.request.Request(f"{self.url}/pipeline", data=json.dumps(list(cmds)).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return [x.get("result") for x in json.loads(r.read())]
        except Exception as e:  # noqa: BLE001
            raise StoreUnavailable(str(e)) from e

    def save(self, sid: str, data: dict[str, Any]) -> None:
        self._pipe(["SET", f"avisos:inscricao:{sid}", json.dumps(data)], ["SADD", "avisos:inscricoes", sid])

    def get(self, sid: str) -> dict[str, Any] | None:
        raw = self._pipe(["GET", f"avisos:inscricao:{sid}"])[0]
        return json.loads(raw) if raw else None

    def delete(self, sid: str) -> None:
        self._pipe(["DEL", f"avisos:inscricao:{sid}"], ["DEL", f"avisos:enviados:{sid}"], ["SREM", "avisos:inscricoes", sid])

    def ids(self) -> list[str]:
        return list(self._pipe(["SMEMBERS", "avisos:inscricoes"])[0] or [])

    def sent(self, sid: str) -> set[int]:
        return {int(x) for x in (self._pipe(["SMEMBERS", f"avisos:enviados:{sid}"])[0] or [])}

    def mark_sent(self, sid: str, ids: list[int]) -> None:
        if ids:
            self._pipe(["SADD", f"avisos:enviados:{sid}", *[str(i) for i in ids]])


class FileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"inscricoes": {}, "enviados": {}}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}") or {"inscricoes": {}, "enviados": {}}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            raise StoreUnavailable(str(e)) from e

    def save(self, sid: str, data: dict[str, Any]) -> None:
        with self._lock:
            d = self._load()
            d["inscricoes"][sid] = data
            self._write(d)

    def get(self, sid: str) -> dict[str, Any] | None:
        return self._load()["inscricoes"].get(sid)

    def delete(self, sid: str) -> None:
        with self._lock:
            d = self._load()
            d["inscricoes"].pop(sid, None)
            d["enviados"].pop(sid, None)
            self._write(d)

    def ids(self) -> list[str]:
        return list(self._load()["inscricoes"])

    def sent(self, sid: str) -> set[int]:
        return set(self._load()["enviados"].get(sid, []))

    def mark_sent(self, sid: str, ids: list[int]) -> None:
        with self._lock:
            d = self._load()
            d["enviados"][sid] = sorted(set(d["enviados"].get(sid, [])) | set(ids))
            self._write(d)


def store() -> RedisStore | FileStore:
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and token:
        return RedisStore(url, token)
    default = Path(__file__).resolve().parents[1] / ".data" / "avisos.json"
    return FileStore(Path(os.environ.get("FOMENTA_AVISOS_FILE", default)))


# --------------------------------------------------------------------------- validação
def sub_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


def check_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        raise HTTPException(422, "Inscrição de notificação inválida.")
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise HTTPException(422, "Serviço de notificação não reconhecido.")
    return endpoint


def clean_follows(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in KINDS}
    if not isinstance(raw, dict):
        return out
    for kind, rx in KINDS.items():
        vals = raw.get(kind) or []
        if not isinstance(vals, list):
            raise HTTPException(422, f"`{kind}` precisa ser uma lista.")
        seen: list[str] = []
        for v in vals:
            v = str(v).strip()
            if not rx.match(v):
                raise HTTPException(422, f"Item inválido em `{kind}`: {v[:40]}")
            if v not in seen:
                seen.append(v)
        if len(seen) > MAX_PER_KIND:
            raise HTTPException(422, f"No máximo {MAX_PER_KIND} itens em `{kind}`.")
        out[kind] = seen
    return out


def _site_only(request: Request, response: Response) -> None:
    """Só o site oficial inscreve aparelhos; com limite por IP."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in sessao.allowed_origins():
        raise HTTPException(403, "Avisos disponíveis só pelo site do Fomenta BR.", headers={"Cache-Control": NO_STORE})
    minute = int(datetime.now(UTC).timestamp() // 60)
    if counter.hit(f"rl:avisos:{client_ip(request)}:{minute}", 70) > 30:
        raise HTTPException(429, "Muitos pedidos. Tente de novo em instantes.",
                            headers={"Retry-After": "60", "Cache-Control": NO_STORE})
    response.headers["Cache-Control"] = NO_STORE


async def _body(request: Request) -> dict[str, Any]:
    try:
        b = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(400, "JSON inválido.") from e
    if not isinstance(b, dict):
        raise HTTPException(400, "JSON inválido.")
    return b


def _store_call(fn, *args):  # noqa: ANN001, ANN202
    try:
        return fn(*args)
    except StoreUnavailable as e:
        raise HTTPException(503, "Avisos temporariamente indisponíveis.", headers={"Cache-Control": NO_STORE}) from e


# --------------------------------------------------------------------------- rotas
@router.get("/chave")
def public_key(response: Response) -> dict[str, str]:
    """Chave pública VAPID que o navegador usa para inscrever o aparelho."""
    key = os.environ.get("VAPID_PUBLIC_KEY")
    if not key:
        raise HTTPException(503, "Avisos ainda não configurados neste servidor.", headers={"Cache-Control": NO_STORE})
    response.headers["Cache-Control"] = "public, s-maxage=3600"
    return {"chave": key}


@router.post("/inscricao")
async def subscribe(request: Request, response: Response) -> dict[str, Any]:
    """Cria ou atualiza a inscrição do aparelho com o que a pessoa segue (lista vazia mantém a inscrição)."""
    _site_only(request, response)
    b = await _body(request)
    sub = b.get("inscricao") or {}
    endpoint = check_endpoint(sub.get("endpoint"))
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(p256dh, str) and isinstance(auth, str) and 20 <= len(p256dh) <= 200 and 8 <= len(auth) <= 100):
        raise HTTPException(422, "Chaves da inscrição inválidas.")
    follows = clean_follows(b.get("segue"))
    sid = sub_id(endpoint)
    st = store()
    old = _store_call(st.get, sid) or {}
    data = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "segue": follows,
            "criada": old.get("criada") or datetime.now(UTC).isoformat(timespec="seconds"),
            "atualizada": datetime.now(UTC).isoformat(timespec="seconds")}
    _store_call(st.save, sid, data)
    return {"id": sid, "segue": follows}


@router.post("/consulta")
async def lookup_subscription(request: Request, response: Response) -> dict[str, Any]:
    """O que este aparelho segue (o endereço da inscrição funciona como senha)."""
    _site_only(request, response)
    endpoint = check_endpoint((await _body(request)).get("endpoint"))
    data = _store_call(store().get, sub_id(endpoint))
    if not data:
        raise HTTPException(404, "Este aparelho não está inscrito.", headers={"Cache-Control": NO_STORE})
    return {"id": sub_id(endpoint), "segue": data["segue"], "criada": data.get("criada")}


@router.post("/cancelar")
async def unsubscribe(request: Request, response: Response) -> dict[str, bool]:
    """Apaga a inscrição e o histórico de envios deste aparelho."""
    _site_only(request, response)
    endpoint = check_endpoint((await _body(request)).get("endpoint"))
    _store_call(store().delete, sub_id(endpoint))
    return {"cancelado": True}
