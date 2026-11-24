"""Traz as edições da newsletter já enviadas, do painel /admin do FomentaBR, para o retrato da API.

    $env:FOMENTABR_ADMIN_TOKEN = "<o mesmo token do /admin>"     # PowerShell; não fica salvo em lugar nenhum
    .venv\\Scripts\\python tools/newsletter_fomentabr.py

Lê `GET /api/admin/newsletter/editions` e o detalhe de cada edição de e-mail que já saiu (status
`sent`, ou envio em lotes com pelo menos um e-mail entregue) e grava `newsletter_edicao.parquet`:
- em `build/snapshot/` (e na lista de `snapshot.json`), para o próximo build e a Release
  `snapshot-plataforma` (`gh release upload snapshot-plataforma build/snapshot/newsletter_edicao.parquet
  build/snapshot/snapshot.json --clobber`);
- em `build/tabelas/`, para a API local mostrar já (reinicie a API).

O HTML guardado no FomentaBR é o genérico da edição: o link de descadastro de cada assinante só entra
no envio, então nada pessoal é copiado. O token só vai no cabeçalho `X-Admin-Token` e nunca é impresso.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("FOMENTABR_URL", "https://www.fomentabr.org").rstrip("/")


def _get(path: str, token: str) -> Any:
    req = urllib.request.Request(f"{BASE}{path}", headers={
        "X-Admin-Token": token, "Accept": "application/json", "User-Agent": "fomenta-api-snapshot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _date(v: Any) -> date | None:
    return date.fromisoformat(str(v)[:10]) if v else None


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)


def was_sent(e: dict[str, Any]) -> bool:
    return e.get("status") == "sent" or (e.get("sent_success_count") or 0) > 0


def fetch(get: Callable[[str], Any]) -> list[dict[str, Any]]:
    """As edições de e-mail já enviadas, no formato da tabela `newsletter_edicao`."""
    rows = []
    for e in get("/api/admin/newsletter/editions").get("editions", []):
        if not was_sent(e):
            continue
        d = get(f"/api/admin/newsletter/editions/{e['id']}")
        rows.append({
            "id": int(d["id"]),
            "edition_date": _date(d.get("edition_date")),
            "subject": d.get("subject") or "",
            "markdown": d.get("markdown") or "",
            "html": d.get("html") or "",
            "call_ids": json.dumps(d.get("call_ids") or []),
            "conference_ids": json.dumps(d.get("conference_ids") or []),
            "send_completed_at": _ts(d.get("send_completed_at")),
        })
    return sorted(rows, key=lambda r: (r["edition_date"] or date.min, r["id"]), reverse=True)


SCHEMA = pa.schema([("id", pa.int64()), ("edition_date", pa.date32()), ("subject", pa.string()),
                    ("markdown", pa.string()), ("html", pa.string()), ("call_ids", pa.string()),
                    ("conference_ids", pa.string()), ("send_completed_at", pa.timestamp("us"))])


def write(rows: list[dict[str, Any]], build: Path = ROOT / "build") -> list[Path]:
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    out = []
    for folder in (build / "snapshot", build / "tabelas"):
        folder.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, folder / "newsletter_edicao.parquet")
        out.append(folder / "newsletter_edicao.parquet")
    meta_path = build / "snapshot" / "snapshot.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("tables", {})["newsletter_edicao"] = {"rows": len(rows)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    token = os.environ.get("FOMENTABR_ADMIN_TOKEN", "").strip()
    if not token:
        sys.exit("Defina FOMENTABR_ADMIN_TOKEN com o token do painel /admin do FomentaBR.")
    try:
        rows = fetch(lambda p: _get(p, token))
    except urllib.error.HTTPError as e:
        sys.exit(f"O FomentaBR respondeu {e.code}. Confira o token do /admin." if e.code in (401, 403)
                 else f"O FomentaBR respondeu {e.code}.")
    paths = write(rows)
    print(f"{len(rows)} edições enviadas gravadas em:")
    for p in paths:
        print(f"  {p.relative_to(ROOT)}")
    for r in rows:
        print(f"  {r['edition_date']}  {r['subject']}")


if __name__ == "__main__":
    main()
