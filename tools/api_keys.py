"""Gera chaves de API e a entrada para a variável `FOMENTA_API_KEYS` (só o SHA-256 vai para o servidor).

    python tools/api_keys.py criar --plano pro --nome "Universidade X" --expira 2027-12-31
    python tools/api_keys.py criar --plano gratuito --nome "Fulana (doutorado UFRJ)" --atual chaves.json
    python tools/api_keys.py criar --plano institucional --nome "Pró-reitoria X" --editais --atual chaves.json
    python tools/api_keys.py revogar --hash <sha256> --atual chaves.json

A chave aparece uma vez só: entregue para quem pediu e não guarde em lugar nenhum. Com `--atual`
(o JSON vigente, num arquivo fora do repositório), o comando grava a versão nova no mesmo arquivo;
depois copie o conteúdo para a variável de ambiente da Vercel e faça um novo deploy:

    vercel env rm FOMENTA_API_KEYS production && vercel env add FOMENTA_API_KEYS production < chaves.json
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.planos import KEY_PREFIX, PLANOS, key_hash  # noqa: E402


def _load(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8") or "{}")


def _save(path: str | None, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"\n{path} atualizado ({len(data)} chaves).")
    else:
        print("\nFOMENTA_API_KEYS (com só esta chave):")
        print(text)


def criar(a: argparse.Namespace) -> None:
    key = KEY_PREFIX + secrets.token_urlsafe(24)
    data = _load(a.atual)
    entry = {"plano": a.plano, "nome": a.nome, "criada": datetime.now(UTC).date().isoformat()}
    if a.expira:
        entry["expira"] = a.expira
    if a.editais:
        entry["editais"] = True
    data[key_hash(key)] = entry
    print(f"Chave ({PLANOS[a.plano].nome}) para {a.nome}:\n\n    {key}\n")
    print("Entregue a chave para a pessoa. Ela não fica guardada em lugar nenhum.")
    _save(a.atual, data)


def revogar(a: argparse.Namespace) -> None:
    data = _load(a.atual)
    if data.pop(a.hash, None) is None:
        sys.exit("Hash não encontrado.")
    _save(a.atual, data)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar")
    c.add_argument("--plano", required=True, choices=[k for k, v in PLANOS.items() if v.precisa_chave])
    c.add_argument("--nome", required=True, help="quem usa a chave (aparece em /v1/conta)")
    c.add_argument("--expira", help="AAAA-MM-DD")
    c.add_argument("--editais", action="store_true",
                   help="libera os editais (fechados para a API pública); só para parceiros com contrato")
    c.add_argument("--atual", help="arquivo com o JSON vigente de FOMENTA_API_KEYS (fora do repositório)")
    c.set_defaults(fn=criar)
    r = sub.add_parser("revogar")
    r.add_argument("--hash", required=True)
    r.add_argument("--atual", required=True)
    r.set_defaults(fn=revogar)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
