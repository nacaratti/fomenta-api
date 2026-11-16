"""API sobre um fomenta.duckdb mínimo, montado com o mesmo SCHEMA do build."""
import json
import os
from datetime import date

import duckdb
import pytest


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from pipeline.build import SCHEMA

    db = tmp_path_factory.mktemp("db") / "fomenta.duckdb"
    con = duckdb.connect(str(db))
    con.execute(SCHEMA)
    summary = {"kpis": {"projects": 2, "approved_total": 300.0, "released_total": 250.0, "institutions": 1,
                        "year_min": 2024, "year_max": 2025}, "by_year": [], "rankings": {}}
    con.execute("INSERT INTO funding_source VALUES ('finep', 'FINEP', '[]', 'nota', DATE '2026-10-23', "
                "TIMESTAMP '2026-11-16 12:00:00', 0, ?, '{}')", [json.dumps(summary)])
    con.execute("INSERT INTO institution VALUES ('63025530000104', 'Universidade de São Paulo', 'USP', 'SP', 'São Paulo')")
    for i, (title, uf, d, v) in enumerate([("Projeto grande", "SP", date(2024, 5, 1), 200.0),
                                           ("Projeto pequeno", "RJ", date(2025, 5, 1), 100.0)], start=1):
        con.execute("INSERT INTO funding_award (id, source_id, source_key, title, instrument, executor_name, uf, "
                    "signing_date, approved_value, released_value) VALUES (?, 'finep', ?, ?, 'Crédito Direto', "
                    "'USP', ?, ?, ?, ?)", [i, f"k{i}", title, uf, d, v, v - 25])
        con.execute("INSERT INTO funding_award_institution VALUES (?, '63025530000104', 'executor', 'cnpj')", [i])
    con.execute("CREATE TABLE inst_funder_year AS SELECT l.institution_cnpj, a.source_id, year(a.signing_date) AS year, "
                "count(*) AS projects, sum(a.approved_value) AS approved, sum(a.released_value) AS released "
                "FROM funding_award a JOIN funding_award_institution l ON l.award_id = a.id GROUP BY ALL")
    con.close()
    os.environ["FOMENTA_DB"] = str(db)

    from fastapi.testclient import TestClient

    from api import index, queries
    queries.DB_PATH = db
    queries._connection.cache_clear()
    return TestClient(index.app)


def test_funders(client):
    r = client.get("/v1/investimentos/financiadores")
    assert r.status_code == 200
    assert r.json()["items"][0]["kpis"]["projects"] == 2
    assert "s-maxage" in r.headers["cache-control"]


def test_funder_404(client):
    assert client.get("/v1/investimentos/financiadores/xpto").status_code == 404


def test_awards_filters_and_order(client):
    items = client.get("/v1/investimentos/projetos").json()["items"]
    assert [i["title"] for i in items] == ["Projeto grande", "Projeto pequeno"]
    r = client.get("/v1/investimentos/projetos", params={"uf": "rj"}).json()
    assert r["total"] == 1 and r["items"][0]["released"] == 75.0
    assert client.get("/v1/investimentos/projetos", params={"ano": 2024}).json()["total"] == 1
    assert client.get("/v1/investimentos/projetos", params={"q": "pequeno"}).json()["total"] == 1
    assert client.get("/v1/investimentos/projetos", params={"cnpj": "63.025.530/0001-04"}).json()["total"] == 2
    assert client.get("/v1/investimentos/projetos", params={"q": "ab"}).status_code == 422
    assert client.get("/v1/investimentos/projetos", params={"per_page": 101}).status_code == 422


def test_sql_injection_is_just_text(client):
    r = client.get("/v1/investimentos/projetos", params={"q": "x' OR 1=1 --"})
    assert r.status_code == 200 and r.json()["total"] == 0


def test_institution(client):
    data = client.get("/v1/instituicoes/63025530000104/investimentos").json()
    (f,) = data["funders"]
    assert f["approved"] == 300.0 and [y["year"] for y in f["by_year"]] == [2024, 2025]
    assert client.get("/v1/instituicoes/00000000000000/investimentos").status_code == 404


def test_docs_and_cors(client):
    assert client.get("/v1/openapi.json").status_code == 200
    r = client.options("/v1/investimentos/financiadores",
                       headers={"Origin": "https://exemplo.org", "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] == "*"
