"""Contadores de uso compartilhados entre as instâncias da função.

Na Vercel cada instância tem a própria memória: um limite contado só em memória deixa passar
quem distribui as requisições. Em produção os contadores ficam num Redis com API REST (Upstash,
pela integração da Vercel Marketplace), que expõe `UPSTASH_REDIS_REST_URL` e
`UPSTASH_REDIS_REST_TOKEN` (ou `KV_REST_API_URL` / `KV_REST_API_TOKEN`). Sem essas variáveis,
ou se o Redis falhar, conta em memória (desenvolvimento local e testes).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("fomenta.contador")


class MemoryCounter:
    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def hit(self, key: str, ttl: int) -> int:
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > 60:
                self._data = {k: v for k, v in self._data.items() if v[1] > now}
                self._last_sweep = now
            count, exp = self._data.get(key, (0, now + ttl))
            if exp <= now:
                count, exp = 0, now + ttl
            count += 1
            self._data[key] = (count, exp)
            return count

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


class RedisRestCounter:
    """INCR + EXPIRE numa chamada só (pipeline da API REST do Upstash)."""

    def __init__(self, url: str, token: str, fallback: MemoryCounter) -> None:
        self.url = url.rstrip("/") + "/pipeline"
        self.token = token
        self.fallback = fallback
        self._warned = False

    def hit(self, key: str, ttl: int) -> int:
        body = json.dumps([["INCR", key], ["EXPIRE", key, str(ttl), "NX"]]).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=0.8) as r:
                return int(json.loads(r.read())[0]["result"])
        except Exception as e:  # noqa: BLE001 - qualquer falha do Redis cai para a memória
            if not self._warned:
                log.warning("Redis indisponível (%s); contando em memória nesta instância.", e)
                self._warned = True
            return self.fallback.hit(key, ttl)

    def reset(self) -> None:
        self.fallback.reset()


def _build() -> MemoryCounter | RedisRestCounter:
    mem = MemoryCounter()
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return RedisRestCounter(url, token, mem) if url and token else mem


counter = _build()
