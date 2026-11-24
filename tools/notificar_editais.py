"""Envia avisos de editais novos para quem segue agências, laboratórios ou pesquisadores no site.

    python tools/notificar_editais.py chaves               # gera o par VAPID (uma vez só)
    python tools/notificar_editais.py enviar --simular     # mostra o que seria enviado
    python tools/notificar_editais.py enviar               # envia

Roda depois de cada build (GitHub Actions), lendo o `build/fomenta.duckdb`. Para cada aparelho
inscrito (`api/avisos.py`), procura editais abertos, descobertos nos últimos `--dias` dias e ainda
não avisados àquele aparelho:
- da agência que a pessoa segue;
- em que o laboratório ou o pesquisador seguido aparece com perfil (inferência do Fomenta).
Manda no máximo três notificações por aparelho a cada rodada, agrupadas por item seguido.
Inscrições que o navegador cancelou (404/410) são apagadas.

Variáveis: `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTATO` (e-mail para o serviço de push),
e as do Redis (as mesmas da API). Sem as chaves VAPID, só `--simular` funciona.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_PER_RUN = 3


@dataclass
class Aviso:
    titulo: str
    texto: str
    url: str
    editais: list[int] = field(default_factory=list)
    tag: str = ""

    def payload(self) -> str:
        return json.dumps({"title": self.titulo, "body": self.texto, "url": self.url, "tag": self.tag},
                          ensure_ascii=False)


def _fmt_date(d: Any) -> str:
    return d.strftime("%d/%m") if hasattr(d, "strftime") else str(d)[:10]


def _editais(cur, kind: str, ids: list[str], since: datetime, now: datetime) -> dict[str, list[tuple]]:
    """Editais abertos e recentes de cada item seguido: {item: [(id, título, agência, prazo), ...]}."""
    if not ids:
        return {}
    base = ("SELECT {key}, e.id, e.title, a.name, e.submission_deadline FROM edital e "
            "JOIN agencia a ON a.id = e.agency_id {join} "
            "WHERE {where} AND e.submission_deadline >= ? AND e.discovered_at >= ? "
            "ORDER BY e.submission_deadline, e.id")
    if kind == "agencias":
        sql = base.format(key="a.slug", join="", where="list_contains(?, a.slug)")
        params: list[Any] = [ids, now, since]
    elif kind == "laboratorios":
        sql = base.format(key="CAST(x.lab_id AS VARCHAR)", join="JOIN edital_laboratorio x ON x.call_id = e.id",
                          where="list_contains(?, x.lab_id)")
        params = [[int(i) for i in ids], now, since]
    else:
        sql = base.format(key="x.researcher_id", join="JOIN edital_pesquisador x ON x.call_id = e.id",
                          where="list_contains(?, x.researcher_id)")
        params = [ids, now, since]
    out: dict[str, list[tuple]] = {}
    for key, *row in cur.execute(sql, params).fetchall():
        out.setdefault(str(key), []).append(tuple(row))
    return out


def _names(cur, kind: str, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    if kind == "agencias":
        rows = cur.execute("SELECT slug, name FROM agencia WHERE list_contains(?, slug)", [ids]).fetchall()
    elif kind == "laboratorios":
        rows = cur.execute("SELECT CAST(id AS VARCHAR), coalesce(initials, name) FROM pnipe_laboratory "
                           "WHERE list_contains(?, id)", [[int(i) for i in ids]]).fetchall()
    else:
        rows = cur.execute("SELECT id, nome FROM elos_researcher WHERE list_contains(?, id)", [ids]).fetchall()
    return {str(k): v for k, v in rows}


def plan(cur, follows: dict[str, list[str]], sent: set[int], now: datetime, days: int) -> list[Aviso]:
    """Os avisos de um aparelho nesta rodada (sem repetir edital já avisado)."""
    since = now - timedelta(days=days)
    avisos: list[Aviso] = []
    used: set[int] = set(sent)
    for kind in ("agencias", "laboratorios", "pesquisadores"):
        ids = follows.get(kind) or []
        found = _editais(cur, kind, ids, since, now)
        names = _names(cur, kind, list(found))
        for item, rows in found.items():
            rows = [r for r in rows if r[0] not in used]
            if not rows:
                continue
            used.update(r[0] for r in rows)
            nome = names.get(item, item)
            first = rows[0]
            if kind == "agencias":
                titulo = f"{len(rows)} editais novos: {nome}" if len(rows) > 1 else f"Edital novo: {nome}"
                lista = f"/editais?agencia={item}&situacao=abertos"
            elif kind == "laboratorios":
                titulo = (f"{len(rows)} editais com perfil para {nome}" if len(rows) > 1
                          else f"Edital com perfil para {nome}")
                lista = f"/laboratorios/{item}#editais"
            else:
                titulo = (f"{len(rows)} editais com perfil para {nome}" if len(rows) > 1
                          else f"Edital com perfil para {nome}")
                lista = f"/pesquisadores/{item}#editais"
            texto = f"{first[1]} ({first[2]}), prazo {_fmt_date(first[3])}"
            if len(rows) > 1:
                texto += f" e mais {len(rows) - 1}"
            url = f"/editais/{first[0]}" if len(rows) == 1 else lista
            avisos.append(Aviso(titulo, texto, url, [r[0] for r in rows], tag=f"{kind}:{item}"))
    return avisos[:MAX_PER_RUN]


Sender = Callable[[dict[str, Any], str], int]


def webpush_sender() -> Sender:
    from pywebpush import WebPushException, webpush

    key = os.environ["VAPID_PRIVATE_KEY"]
    claims = {"sub": f"mailto:{os.environ.get('VAPID_CONTATO', 'contato@fomentabr.org')}"}

    def send(sub: dict[str, Any], payload: str) -> int:
        try:
            r = webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, payload, vapid_private_key=key,
                        vapid_claims=dict(claims), ttl=3 * 86400, headers={"Urgency": "normal"})
            return r.status_code
        except WebPushException as e:
            return e.response.status_code if e.response is not None else 0

    return send


def run(st, cur, send: Sender | None, *, days: int = 14, now: datetime | None = None, log=print) -> dict[str, int]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    stats = {"aparelhos": 0, "avisos": 0, "removidos": 0, "falhas": 0}
    for sid in st.ids():
        sub = st.get(sid)
        if not sub:
            continue
        stats["aparelhos"] += 1
        for a in plan(cur, sub.get("segue") or {}, st.sent(sid), now, days):
            if send is None:
                log(f"[simulação] {sid[:8]}: {a.titulo} | {a.texto} -> {a.url}")
                stats["avisos"] += 1
                continue
            status = send(sub, a.payload())
            if status in (404, 410):  # o navegador cancelou: some com a inscrição
                st.delete(sid)
                stats["removidos"] += 1
                break
            if 200 <= status < 300:
                st.mark_sent(sid, a.editais)
                stats["avisos"] += 1
            else:
                stats["falhas"] += 1
    return stats


def chaves() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    k = ec.generate_private_key(ec.SECP256R1())
    priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
    pub = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    print("Guarde como segredo (Vercel não precisa; GitHub Actions sim):")
    print(f"VAPID_PRIVATE_KEY={priv}\n")
    print("Pública (na Vercel, para a API, e no GitHub Actions):")
    print(f"VAPID_PUBLIC_KEY={pub}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chaves")
    e = sub.add_parser("enviar")
    e.add_argument("--simular", action="store_true", help="não envia, só mostra")
    e.add_argument("--dias", type=int, default=14, help="editais descobertos nos últimos N dias")
    a = p.parse_args()
    if a.cmd == "chaves":
        chaves()
        return
    from api import queries
    from api.avisos import store

    send = None if a.simular else webpush_sender()
    stats = run(store(), queries.cursor(), send, days=a.dias)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
