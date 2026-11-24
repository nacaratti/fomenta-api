"""Retrato único (snapshot) dos dados da plataforma FomentaBR, do Supabase para Parquet.

Só leitura no banco. Gera `build/snapshot/*.parquet` e `build/snapshot/snapshot.json`,
que vão para a Release `snapshot-AAAA-MM-DD` do repositório; o build da API baixa de
lá. Por enquanto roda uma vez, à mão — a aquisição contínua fica para depois:

    DATABASE_URL=postgresql://... python tools/export_supabase_snapshot.py

O que fica de fora, de propósito:
- editais cancelados e o HTML bruto (o texto extraído entra em `edital_texto`);
- contato pessoal dos laboratórios (nome, telefone e e-mail do responsável);
- laboratórios "placeholder" (criados só para pendurar equipamentos);
- Elos: pesquisadores com pedido de remoção aprovado OU pendente, e vínculos
  pesquisador–laboratório com confiança abaixo de 0,8 (o mesmo corte do site);
- colunas internas (tsvector de busca, tentativas de extração, hashes) e o que traz
  nome de contato do PNIPE: a evidência de cada vínculo do Elos e o relatório de carga
  (`elos_meta`).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parents[1] / "build" / "snapshot"
DISPLAY_CUT = 0.8

# (tabela de saída, SQL). Os filtros de privacidade do Elos usam a CTE `fora`.
ELOS_OUT = """
WITH fora AS (
  SELECT r.id FROM elos_researcher r JOIN elos_optout o ON o.status IN ('aprovado', 'pendente') AND (
    r.id = o.openalex_id OR o.openalex_id = ANY(r.openalex_ids) OR (o.orcid IS NOT NULL AND o.orcid = r.orcid)))
"""
QUERIES: list[tuple[str, str]] = [
    ("agencia", "SELECT id, name, slug, type::text AS type, parent_id, base_url, is_active FROM agency"),
    ("area_conhecimento", "SELECT id, code, name, parent_id, level FROM knowledge_area"),
    ("edital", """
        SELECT id, agency_id, external_id, source_url, pdf_url, title, slug, description_summary,
               submission_deadline, publication_date, max_funding_value, total_funding_value, currency,
               modality::text AS modality, target_audience, status::text AS status, extraction_metadata,
               discovered_at, last_seen_at, updated_at
        FROM "call" WHERE status::text <> 'cancelled'"""),
    ("edital_area", """SELECT ca.call_id, ca.area_id, ca.confidence FROM call_area ca
                       JOIN "call" c ON c.id = ca.call_id WHERE c.status::text <> 'cancelled'"""),
    ("edital_texto", """SELECT id AS call_id, raw_text AS texto FROM "call"
                        WHERE status::text <> 'cancelled' AND coalesce(raw_text, '') <> ''"""),
    ("congresso", """SELECT id, title, acronym, source_url, location, start_date, end_date, submission_deadline,
                            notification_date, categories, description, updated_at FROM conference_event"""),
    ("pnipe_institution", """
        SELECT cnpj, initials, social_reason, zip_code, street, complement, district, city, state, number,
               phone_number, situation, total_labs, total_equipments, is_embrapii, embrapii_units, military_branch,
               updated_at
        FROM pnipe_institution"""),
    ("pnipe_laboratory", """
        SELECT id, name, initials, about, website, latitude, longitude, created_date, area_expertise_list,
               technique_list, institution_cnpj, zip_code, street, complement, district, city, state, number,
               resolved_state, area_keys, updated_at
        FROM pnipe_laboratory WHERE name NOT LIKE '%(Placeholder)%'"""),
    ("pnipe_equipment", """
        SELECT id, name, initials, code, is_multiuser, brand, model, purchase_year, situation, website,
               created_date, updated_date, technique_list, parameter_list, laboratory_id, institution_cnpj,
               updated_at
        FROM pnipe_equipment"""),
    ("synergy_lab", "SELECT lab_id, name, initials, inst_cnpj, inst_label, uf, equip_count, tt, et, em, areas, ga FROM synergy_lab"),
    ("synergy_graph", "SELECT id, key, kind, label, member_count, node_ids, edges, communities FROM synergy_graph"),
    ("synergy_token_label", "SELECT base, token_id, label FROM synergy_token_label"),
    ("synergy_meta", "SELECT key, value FROM synergy_meta"),
    ("elos_researcher", ELOS_OUT + """
        SELECT id, openalex_ids, orcid, nome, inst_cnpj, publicacoes, citacoes, h_index, desde_2019, ultimo_ano,
               grandes_areas, atualizado_em
        FROM elos_researcher WHERE id NOT IN (SELECT id FROM fora)"""),
    ("elos_researcher_lab", ELOS_OUT + f"""
        SELECT researcher_id, lab_id, papel, confianca FROM elos_researcher_lab
        WHERE confianca >= {DISPLAY_CUT} AND researcher_id NOT IN (SELECT id FROM fora)"""),
    ("elos_researcher_topic", ELOS_OUT + """
        SELECT researcher_id, topic_id, n FROM elos_researcher_topic WHERE researcher_id NOT IN (SELECT id FROM fora)"""),
    ("elos_topic", "SELECT id, nome, subarea, area, dominio, field_id, grande_area FROM elos_topic"),
    ("elos_institution", "SELECT cnpj, openalex_id, nome_openalex, ror, confianca, origem FROM elos_institution"),
    ("elos_edge", ELOS_OUT + """
        SELECT a, b, tipo, peso, temas_comuns, ultimo_ano FROM elos_edge
        WHERE a NOT IN (SELECT id FROM fora) AND b NOT IN (SELECT id FROM fora)"""),
    ("elos_lab_link", ELOS_OUT + """
        SELECT lab_a, lab_b, coautorias,
               ARRAY(SELECT p FROM unnest(pessoas) p WHERE p NOT IN (SELECT id FROM fora)) AS pessoas
        FROM elos_lab_link"""),
    # Edições da newsletter já enviadas por e-mail (o arquivo público do site). O HTML guardado é o
    # genérico: o link de descadastro de cada assinante só entra no momento do envio.
    ("newsletter_edicao", """
        SELECT id, edition_date, subject, markdown, html, call_ids, conference_ids, send_completed_at
        FROM newsletter_edition WHERE channel::text = 'email'
          AND (status::text = 'sent' OR coalesce(sent_success_count, 0) > 0)"""),
]


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("Defina DATABASE_URL (o mesmo do FomentaBR).")
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _cell(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)  # UTC sem fuso (o DuckDB lê sem precisar de pytz)
    if isinstance(v, (dict,)) or (isinstance(v, list) and v and isinstance(v[0], dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


async def main() -> None:
    import asyncpg

    OUT.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    # jsonb vem como texto por padrão no asyncpg: decodifica para reserializar de um jeito só.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    tables = {}
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):  # retrato consistente
            for name, sql in QUERIES:
                rows = await conn.fetch(sql)
                cols = list(rows[0].keys()) if rows else []
                data = {c: [_cell(r[c]) for r in rows] for c in cols}
                # Colunas JSON mistas (lista de strings em uma linha, dict em outra) viram texto JSON.
                for c, vals in data.items():
                    kinds = {type(v) for v in vals if v is not None}
                    if str in kinds and len(kinds) > 1:
                        data[c] = [None if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                                   for v in vals]
                table = pa.table(data) if rows else pa.table({})
                pq.write_table(table, OUT / f"{name}.parquet", compression="zstd")
                tables[name] = {"rows": len(rows), "bytes": (OUT / f"{name}.parquet").stat().st_size}
                print(f"{name:24s} {len(rows):>8} linhas  {tables[name]['bytes'] / 1e6:7.2f} MB")
    finally:
        await conn.close()
    (OUT / "snapshot.json").write_text(json.dumps({
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "Supabase do FomentaBR (somente leitura)",
        "elos_display_cut": DISPLAY_CUT,
        "tables": tables,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"total {sum(t['bytes'] for t in tables.values()) / 1e6:.1f} MB em {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
