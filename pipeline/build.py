"""Build dos dados: fontes oficiais -> build/fomenta.duckdb + Parquet + JSON estático + manifesto.

    python -m pipeline.build                 # baixa FINEP e CAPES e monta tudo
    python -m pipeline.build --no-download   # usa os arquivos já baixados em build/cache

Saída em `build/`:
- `fomenta.duckdb`: o banco que a API lê (somente leitura).
- `parquet/*.parquet`: as mesmas tabelas para download e análise.
- `static/v1/...`: respostas prontas das rotas fixas da API (servidas pela CDN).
- `manifest.json`: posição de cada fonte, contagens, totais e sha256 dos arquivos.

Validações: cada fonte precisa trazer um mínimo de registros e totais positivos;
se não, o build falha antes de gerar a saída (o deploy anterior continua no ar).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from pipeline.common import AWARD_FIELDS, SourceData
from pipeline.matching import REF_DIR, InstitutionMatcher
from pipeline.sources import capes, cnpq, finep
from pipeline.summary import build_summary

log = logging.getLogger("build")
ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
# Mínimo de awards por fonte para o build ser aceito (protege contra planilha vazia/formato novo).
MIN_AWARDS = {"finep": 10_000, "capes": 5_000, "cnpq": 1_000}
# O banco vai num commit da branch `deploy`: o GitHub recusa arquivo acima de 100 MB.
MAX_DB_BYTES = 95 * 1024 * 1024

SCHEMA = """
CREATE TABLE funding_source (
    id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, source_urls JSON, notes VARCHAR,
    data_as_of DATE, imported_at TIMESTAMP, sort_order INTEGER, summary JSON, stats JSON);
CREATE TABLE institution (cnpj VARCHAR PRIMARY KEY, name VARCHAR, initials VARCHAR, uf VARCHAR, city VARCHAR);
CREATE TABLE funding_award (
    id INTEGER PRIMARY KEY, source_id VARCHAR NOT NULL, source_key VARCHAR NOT NULL, ref VARCHAR, title VARCHAR,
    instrument VARCHAR, program VARCHAR, proponent_cnpj VARCHAR, proponent_name VARCHAR, executor_cnpj VARCHAR,
    executor_name VARCHAR, city VARCHAR, uf VARCHAR, signing_date DATE, end_date DATE,
    approved_value DOUBLE, paid_value DOUBLE, released_value DOUBLE, status VARCHAR, extra JSON);
CREATE TABLE funding_disbursement (
    award_id INTEGER NOT NULL, paid_on DATE, value DOUBLE, parcel_number INTEGER, release_number INTEGER);
CREATE TABLE funding_award_institution (
    award_id INTEGER NOT NULL, institution_cnpj VARCHAR NOT NULL, role VARCHAR NOT NULL, match_method VARCHAR NOT NULL);
"""


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat() + ("Z" if o.tzinfo is None else "")
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def _dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, default=_json_default, separators=(",", ":")),
                    encoding="utf-8")


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    t = pa.Table.from_pylist(rows)
    con.register("_rows", t)
    cols = ", ".join(t.column_names)
    con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _rows")
    con.unregister("_rows")


def _fetch(name: str, fn, directory: Path, required: str) -> str | None:
    """Baixa uma fonte. Se falhar e houver arquivos da última coleta boa em `directory`
    (no GitHub Actions eles vêm da Release `fontes-<nome>`), segue com eles e devolve o
    erro — a data de posição sai dos próprios arquivos, então a API mostra a idade real."""
    try:
        fn(directory)
        return None
    except Exception as e:  # noqa: BLE001 — qualquer falha de rede/servidor da fonte
        if not any(directory.glob(required)):
            raise
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        log.warning("%s: download falhou (%s); usando os arquivos da última coleta", name, msg)
        return msg


def load_sources(cache: Path, download: bool, only: set[str]) -> list[SourceData]:
    out = []
    if "finep" in only:
        err = _fetch("finep", finep.download, cache / "finep", "Liberacao.xlsx") if download else None
        out.append(finep.parse(cache / "finep"))
        if err:
            out[-1].stats["download_falhou"] = err
    if "capes" in only:
        err = _fetch("capes", capes.download, cache / "capes", "*.csv") if download else None
        out.append(capes.parse(cache / "capes"))
        if err:
            out[-1].stats["download_falhou"] = err
    if "cnpq" in only:
        out.append(cnpq.parse())
    return out


# Bancos à parte por prefixo de tabela: cada arquivo precisa ficar abaixo de 100 MB para entrar
# na branch deploy. A API anexa todos (`api/queries.py`) e lê as tabelas pelo nome simples.
# (o primeiro prefixo que casar vale: os mais específicos vêm antes)
SPLIT_DBS = {"elos_": "elos", "oa_keyword": "palavras", "oa_work_keyword": "palavras", "oa_": "obras"}


def load_snapshot(con: duckdb.DuckDBPyConnection, snapshot: Path,
                  others: dict[str, duckdb.DuckDBPyConnection] | None = None) -> dict[str, Any]:
    """Tabelas da plataforma (editais, congressos, PNIPE, sinergia, Elos) a partir do retrato
    exportado do Supabase (`tools/export_supabase_snapshot.py`, Release `snapshot-plataforma`)."""
    meta_path = snapshot / "snapshot.json"
    if not meta_path.exists():
        raise SystemExit(f"Retrato da plataforma não encontrado em {snapshot} (snapshot.json)")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # Busca sem acento e sem caixa, pré-calculada na criação (um UPDATE depois deixaria
    # blocos mortos no arquivo, que precisa ficar abaixo de 100 MB para ir à branch deploy).
    busca = {
        "edital": "concat_ws(' ', title, description_summary)",
        "congresso": "concat_ws(' ', title, acronym, location)",
        "pnipe_institution": "concat_ws(' ', social_reason, initials, city)",
        "pnipe_laboratory": "concat_ws(' ', name, initials, about)",
        "pnipe_equipment": "concat_ws(' ', name, initials, brand, model)",
        "elos_researcher": "concat_ws(' ', nome, instituicao_nome, instituicao_sigla)",
        "oa_work": "titulo",
    }
    for name in meta["tables"]:
        path = (snapshot / f"{name}.parquet").as_posix()
        extra = f", lower(strip_accents({busca[name]})) AS busca" if name in busca else ""
        db = next((d for pre, d in SPLIT_DBS.items() if name.startswith(pre)), None)
        target = (others or {}).get(db, con)
        target.execute(f"CREATE TABLE {name} AS SELECT *{extra} FROM read_parquet('{path}')")
    log.info("plataforma: %d tabelas do retrato de %s", len(meta["tables"]), meta["exported_at"])
    return meta


def validate(src: SourceData, released: float, approved: float) -> list[str]:
    errors = []
    if len(src.awards) < MIN_AWARDS.get(src.id, 1):
        errors.append(f"{src.id}: só {len(src.awards)} registros (mínimo {MIN_AWARDS.get(src.id)})")
    if released <= 0 or approved <= 0:
        errors.append(f"{src.id}: totais zerados (aprovado {approved}, pago {released})")
    if not src.as_of:
        errors.append(f"{src.id}: sem data de posição")
    return errors


def build(out: Path, cache: Path, download: bool, only: set[str], snapshot: Path | None) -> dict[str, Any]:
    sources = load_sources(cache, download, only)
    tmp = out / "fomenta.duckdb.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    split_tmp = {d: out / f"{d}.duckdb.tmp" for d in dict.fromkeys(SPLIT_DBS.values())}
    for t in split_tmp.values():
        t.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(SCHEMA)
    con.execute(
        "INSERT INTO institution SELECT cnpj, social_reason, nullif(initials, ''), nullif(state, ''), nullif(city, '') "
        "FROM read_csv(?, header=true, all_varchar=true)", [str(REF_DIR / "pnipe_institutions.csv")])
    matcher = InstitutionMatcher.from_ref()
    # UTC sem fuso no banco (TIMESTAMPTZ exigiria pytz na API); o manifesto leva o "Z".
    imported_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    manifest_sources, errors, next_id = [], [], 1

    for src in sources:
        ids = {}
        released_by_key: dict[str, float] = {}
        for d in src.disbursements:
            released_by_key[d["source_key"]] = released_by_key.get(d["source_key"], 0.0) + (d["value"] or 0.0)
        awards, links, methods = [], [], {}
        for key, a in src.awards.items():
            ids[key] = next_id
            row = {f: a.get(f) for f in AWARD_FIELDS}
            row["extra"] = json.dumps(a["extra"], ensure_ascii=False) if a.get("extra") else None
            awards.append({"id": next_id, "source_id": src.id, **row,
                           "released_value": round(released_by_key.get(key, 0.0), 2)})
            seen = set()
            for cnpj, role, method in matcher.links_for(a, src.link_mode):
                if (cnpj, role) not in seen:
                    seen.add((cnpj, role))
                    links.append({"award_id": next_id, "institution_cnpj": cnpj, "role": role, "match_method": method})
                    methods[method] = methods.get(method, 0) + 1
            next_id += 1
        disb = [{"award_id": ids[d["source_key"]], "paid_on": d["paid_on"], "value": d["value"],
                 "parcel_number": d["parcel_number"], "release_number": d["release_number"]} for d in src.disbursements]
        _insert(con, "funding_award", awards)
        _insert(con, "funding_disbursement", disb)
        _insert(con, "funding_award_institution", links)

        stats = {**src.stats, "registros": len(awards), "pagamentos": len(disb), "vinculos": len(links),
                 "metodos_de_casamento": methods}
        con.execute("INSERT INTO funding_source VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    [src.id, src.name, json.dumps(src.urls), src.notes, src.as_of, imported_at, src.sort_order,
                     json.dumps(stats, ensure_ascii=False)])
        summary = build_summary(con, src.id)
        con.execute("UPDATE funding_source SET summary = ? WHERE id = ?",
                    [json.dumps(summary, ensure_ascii=False, default=_json_default), src.id])
        k = summary["kpis"]
        errors += validate(src, k["released_total"], k["approved_total"])
        manifest_sources.append({"id": src.id, "name": src.name, "data_as_of": src.as_of, **k, "stats": stats})
        log.info("%s: %d registros, aprovado R$ %.2f, pago R$ %.2f, %d vínculos %s",
                 src.id, len(awards), k["approved_total"], k["released_total"], len(links), methods)

    if errors:
        con.close()
        tmp.unlink(missing_ok=True)
        raise SystemExit("Build recusado:\n- " + "\n- ".join(errors))

    # Agregado por instituição × financiador × ano (rota /v1/instituicoes/{cnpj}/investimentos).
    con.execute("""
        CREATE TABLE inst_funder_year AS
        SELECT l.institution_cnpj, a.source_id, year(a.signing_date) AS year, count(*) AS projects,
               round(sum(a.approved_value), 2) AS approved, round(sum(a.released_value), 2) AS released
        FROM funding_award a JOIN funding_award_institution l ON l.award_id = a.id
        GROUP BY ALL ORDER BY 1, 2, 3""")
    platform = None
    if snapshot:
        others = {d: duckdb.connect(str(t)) for d, t in split_tmp.items()}
        platform = load_snapshot(con, snapshot, others)
        for d, c in others.items():
            empty = not c.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
            c.execute("CHECKPOINT")
            c.close()
            if empty:
                split_tmp[d].unlink(missing_ok=True)
    con.execute("CHECKPOINT")

    # Limpa só os arquivos (apagar a pasta falha às vezes no Windows, com OneDrive sincronizando).
    (out / "parquet").mkdir(parents=True, exist_ok=True)
    for old in (out / "parquet").glob("*.parquet"):
        old.unlink()
    for t in ("funding_source", "institution", "funding_award", "funding_disbursement",
              "funding_award_institution", "inst_funder_year"):
        con.execute(f"COPY {t} TO '{(out / 'parquet' / f'{t}.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd)")

    funders = con.execute(
        "SELECT id, name, notes, data_as_of, imported_at, summary, source_urls FROM funding_source ORDER BY sort_order"
    ).fetchall()
    con.close()
    pairs = [(tmp, out / "fomenta.duckdb")] + [(t, out / f"{d}.duckdb") for d, t in split_tmp.items() if t.exists()]
    for t, f in pairs:
        if t.stat().st_size > MAX_DB_BYTES:
            size = t.stat().st_size / 1e6
            for t2, _ in pairs:
                t2.unlink(missing_ok=True)
            raise SystemExit(f"Build recusado: {f.name} com {size:.1f} MB passa do teto de 95 MB "
                             "(o GitHub recusa arquivos acima de 100 MB na branch deploy). Divida o banco.")
    for t, f in pairs:
        f.unlink(missing_ok=True)
        t.rename(f)
    final = out / "fomenta.duckdb"
    dbs = [f for _, f in pairs]

    static = out / "static" / "v1"
    shutil.rmtree(static, ignore_errors=True)
    listing = []
    for fid, name, notes, as_of, imp, summary, urls in funders:
        s = json.loads(summary)
        listing.append({"id": fid, "name": name, "notes": notes, "data_as_of": as_of, "imported_at": imp,
                        "kpis": s["kpis"]})
        _dump(static / "investimentos" / "financiadores" / f"{fid}.json",
              {"id": fid, "name": name, "sources": json.loads(urls), "notes": notes, "data_as_of": as_of,
               "imported_at": imp, **s})
    _dump(static / "investimentos" / "financiadores.json", {"items": listing})

    files = []
    for p in sorted([*dbs, *(out / "parquet").glob("*.parquet")]):
        files.append({"path": p.relative_to(out).as_posix(), "bytes": p.stat().st_size,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    manifest = {"schema_version": SCHEMA_VERSION, "built_at": imported_at, "sources": manifest_sources,
                "plataforma": platform, "files": files}
    _dump(out / "manifest.json", manifest)
    _dump(static / "meta.json", manifest)
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=ROOT / "build")
    p.add_argument("--cache", type=Path, default=ROOT / "build" / "cache")
    p.add_argument("--no-download", action="store_true", help="usar os arquivos já em --cache")
    p.add_argument("--only", default="finep,capes,cnpq", help="fontes, separadas por vírgula")
    p.add_argument("--snapshot", type=Path, default=ROOT / "build" / "snapshot",
                   help="retrato da plataforma (Parquet + snapshot.json)")
    p.add_argument("--sem-plataforma", action="store_true", help="só investimentos, sem o retrato")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    m = build(args.out, args.cache, not args.no_download, set(args.only.split(",")),
              None if args.sem_plataforma else args.snapshot)
    for s in m["sources"]:
        print(f"{s['id']:6s} posição {s['data_as_of']}  registros {s['projects']:>6}  "
              f"aprovado R$ {s['approved_total']:>18,.2f}  pago R$ {s['released_total']:>18,.2f}")
    for f in m["files"]:
        print(f"{f['bytes'] / 1e6:8.2f} MB  {f['path']}")


if __name__ == "__main__":
    sys.exit(main())
