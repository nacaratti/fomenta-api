"""Rotas da plataforma sobre um retrato mínimo, carregado pelo mesmo `load_snapshot` do build."""
import json
from datetime import datetime, timedelta

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

FUTURE = datetime.now() + timedelta(days=30)
PAST = datetime.now() - timedelta(days=30)


def _write(d, name, rows):
    pq.write_table(pa.Table.from_pylist(rows), d / f"{name}.parquet")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    snap = tmp_path_factory.mktemp("snap")
    lab_areas = json.dumps([{"id": 1, "name": "Espectrometria de Massas"}], ensure_ascii=False)
    tables = {
        "agencia": [{"id": 1, "name": "FAPEMIG", "slug": "fapemig", "type": "state_fap", "parent_id": None,
                     "base_url": "https://fapemig.br", "is_active": True}],
        "area_conhecimento": [{"id": 10, "code": "3.00.00.00-9", "name": "Engenharias", "parent_id": None, "level": 1}],
        "edital": [
            {"id": 1, "agency_id": 1, "external_id": "a", "source_url": "u1", "pdf_url": None,
             "title": "Chamada de Inovação em Saúde", "slug": None, "description_summary": "Projetos de pesquisa",
             "submission_deadline": FUTURE, "publication_date": None, "max_funding_value": 1000.0,
             "total_funding_value": None, "currency": "BRL", "modality": "rd", "target_audience": ["ict"],
             "status": "extracted", "extraction_metadata": json.dumps({"call_kind": "chamada", "funding_items": ["x"]}),
             "discovered_at": PAST, "last_seen_at": PAST, "updated_at": PAST},
            {"id": 2, "agency_id": 1, "external_id": "b", "source_url": "u2", "pdf_url": None,
             "title": "Bolsas de mestrado", "slug": None, "description_summary": None,
             "submission_deadline": PAST, "publication_date": None, "max_funding_value": None,
             "total_funding_value": None, "currency": "BRL", "modality": "scholarship", "target_audience": ["researcher"],
             "status": "expired", "extraction_metadata": json.dumps({}),
             "discovered_at": PAST, "last_seen_at": PAST, "updated_at": PAST},
        ],
        "edital_area": [{"call_id": 1, "area_id": 10, "confidence": 0.9}],
        "edital_texto": [{"call_id": 1, "texto": "Texto integral"}],
        "congresso": [{"id": 7, "title": "Simpósio Brasileiro de Óptica", "acronym": "SBO", "source_url": "c",
                       "location": "Recife", "start_date": FUTURE.date(), "end_date": FUTURE.date(),
                       "submission_deadline": FUTURE, "notification_date": None, "categories": ["optics"],
                       "description": "d", "updated_at": PAST}],
        "pnipe_institution": [{"cnpj": "63025530000104", "initials": "USP", "social_reason": "Universidade de São Paulo",
                               "zip_code": None, "street": None, "complement": None, "district": None,
                               "city": "São Paulo", "state": "SP", "number": None, "phone_number": None,
                               "situation": "ATIVA", "total_labs": 1, "total_equipments": 1, "is_embrapii": False,
                               "embrapii_units": None, "military_branch": None, "updated_at": PAST}],
        "pnipe_laboratory": [{"id": 5, "name": "Laboratório de Espectrometria", "initials": "LE", "about": "massas",
                              "website": None, "latitude": None, "longitude": None, "created_date": None,
                              "area_expertise_list": lab_areas, "technique_list": None,
                              "institution_cnpj": "63025530000104", "zip_code": None, "street": None,
                              "complement": None, "district": None, "city": "São Paulo", "state": "SP",
                              "number": None, "resolved_state": "SP", "area_keys": ["espectrometria de massas"],
                              "updated_at": PAST}],
        "pnipe_equipment": [{"id": 9, "name": "Microscópio Eletrônico", "initials": None, "code": None,
                             "is_multiuser": True, "brand": "JEOL", "model": "X", "purchase_year": None,
                             "situation": "ACTIVE", "website": None, "created_date": None, "updated_date": None,
                             "technique_list": None, "parameter_list": None, "laboratory_id": 5,
                             "institution_cnpj": "63025530000104", "updated_at": PAST}],
        "synergy_lab": [{"lab_id": 5, "name": "Laboratório de Espectrometria", "initials": "LE",
                         "inst_cnpj": "63025530000104", "inst_label": "USP", "uf": "SP", "equip_count": 1,
                         "tt": [1], "et": [2], "em": [2], "areas": ["quimica"], "ga": ["1"]}],
        "synergy_graph": [{"id": 1, "key": "ga:1", "kind": "grande_area", "label": "Ciências Exatas", "member_count": 1,
                           "node_ids": [5], "edges": json.dumps({"hybrid": {"affinity": [[5, 6, 2, 0.5]]}}),
                           "communities": json.dumps({"hybrid": {"5": 0}})}],
        "synergy_token_label": [{"base": "technique", "token_id": 1, "label": "HPLC"},
                                {"base": "equipment", "token_id": 2, "label": "Microscópio"}],
        "elos_researcher": [{"id": "A1", "openalex_ids": ["A1"], "orcid": None, "nome": "Ana Pérez", "inst_cnpj": "63025530000104",
                             "publicacoes": 10, "citacoes": 5, "h_index": 2, "desde_2019": 4, "ultimo_ano": 2025,
                             "grandes_areas": ["1"], "atualizado_em": PAST, "origem": "responsavel",
                             "instituicao_openalex": "I1", "instituicao_nome": "Universidade de São Paulo", "instituicao_sigla": "USP", "instituicao_pais": "BR"},
                            {"id": "A2", "openalex_ids": ["A2"], "orcid": None, "nome": "Bruno Lima", "inst_cnpj": None,
                             "publicacoes": 3, "citacoes": 1, "h_index": 1, "desde_2019": 1, "ultimo_ano": 2024,
                             "grandes_areas": ["1"], "atualizado_em": PAST, "origem": "coautor",
                             "instituicao_openalex": "I9", "instituicao_nome": "Universidad de Chile", "instituicao_sigla": None, "instituicao_pais": "CL"}],
        "elos_researcher_lab": [{"researcher_id": "A1", "lab_id": 5, "papel": "responsavel", "confianca": 1.0,
                                 "responsavel_id": None, "publicacoes_em_comum": None, "ultimo_ano": None, "obras": None,
                                 "anos_em_comum": None, "colaborador_estavel": None},
                                {"researcher_id": "A2", "lab_id": 5, "papel": "coautor", "confianca": 1.0,
                                 "responsavel_id": "A1", "publicacoes_em_comum": 2, "ultimo_ano": 2024,
                                 "obras": ["W2", "W1"], "anos_em_comum": 2, "colaborador_estavel": True}],
        "elos_researcher_topic": [{"researcher_id": "A1", "topic_id": "T1", "n": 3}],
        "elos_topic": [{"id": "T1", "nome": "Mass Spectrometry", "subarea": "s", "area": "a", "dominio": "d",
                        "field_id": 1, "grande_area": "1"}],
        "elos_edge": [{"a": "A1", "b": "A2", "tipo": "coautoria", "peso": 2.0, "ultimo_ano": 2024, "obras": ["W2", "W1"]}],
        "elos_lab_link": [{"lab_a": 5, "lab_b": 6, "coautorias": 4, "pessoas": ["A1"]}],
        "oa_work": [{"id": "W1", "doi": None, "titulo": "Espectrometria de massas em proteínas", "ano": 2024,
                     "data": "2024-03-01", "tipo": "article", "idioma": "pt", "veiculo_id": "S1", "veiculo": "Química Nova",
                     "veiculo_tipo": "journal", "issn_l": None, "acesso_aberto": True, "oa_url": None, "citacoes": 7,
                     "fwci": 1.4, "percentil": 0.8, "top10": False, "top1": False, "referencias": 30, "tema": "T1",
                     "n_autores": 2},
                    {"id": "W2", "doi": None, "titulo": "Lasers de fibra", "ano": 2025, "data": "2025-01-01",
                     "tipo": "article", "idioma": "en", "veiculo_id": None, "veiculo": None, "veiculo_tipo": None,
                     "issn_l": None, "acesso_aberto": False, "oa_url": None, "citacoes": 1, "fwci": None,
                     "percentil": None, "top10": None, "top1": None, "referencias": 5, "tema": None, "n_autores": 1}],
        "oa_work_author": [{"work_id": "W1", "researcher_id": "A1", "posicao": "first", "correspondente": True},
                           {"work_id": "W1", "researcher_id": "A2", "posicao": "last", "correspondente": False},
                           {"work_id": "W2", "researcher_id": "A1", "posicao": "first", "correspondente": True}],
        "oa_keyword": [{"id": "mass-spec", "nome": "Mass spectrometry"}],
        "oa_work_keyword": [{"work_id": "W1", "keyword_id": "mass-spec", "nota": 0.6}],
        "oa_work_sdg": [{"work_id": "W1", "ods_id": "3", "ods": "Saúde e bem-estar", "nota": 0.5}],
        "oa_funder": [{"id": "F1", "nome": "Fundação de Amparo à Pesquisa do Estado de São Paulo", "ror": "r"}],
        "oa_work_funder": [{"work_id": "W1", "funder_id": "F1"}],
        "oa_award": [{"id": "G1", "processo": "2021/01234-5", "nome": None, "funder_id": "F1", "financiador": "FAPESP"},
                     {"id": "G4", "processo": "FINEP 01.20.0026.00", "nome": None, "funder_id": "F3",
                      "financiador": "FINEP"}],
        "oa_work_award": [{"work_id": "W1", "award_id": "G1"}, {"work_id": "W2", "award_id": "G4"}],
        "oa_funder_br": [{"id": "F1", "nome": "Fundação de Amparo à Pesquisa do Estado de São Paulo", "ror": "r",
                          "crossref": None, "auxilios": 10, "publicacoes": 20},
                         {"id": "F2", "nome": "Fundação de Amparo à Pesquisa do Estado de Minas Gerais", "ror": None,
                          "crossref": None, "auxilios": 5, "publicacoes": 9}],
        "oa_funder_agencia": [{"funder_id": "F2", "agencia": "fapemig"}],
        "oa_auxilio": [
            {"id": "G2", "processo": "APQ-01234-24", "titulo": "Proteômica de plantas", "funder_id": "F2",
             "modalidade": "Universal", "tipo": "grant", "inicio": "2024-03-01", "fim": "2099-12-31", "ano": 2024,
             "valor": 50000.0, "moeda": "BRL", "valor_usd": 9000.0, "fonte": "fapemig_projetos_contratados",
             "pagina": None, "publicacoes": 0, "researcher_id": "A1", "investigador": "Ana Pérez", "orcid": None,
             "inst_cnpj": "63025530000104", "afiliacao": "USP"},
            {"id": "G3", "processo": "2020/00001-0", "titulo": "Óptica integrada", "funder_id": "F1",
             "modalidade": "Regular", "tipo": "grant", "inicio": "2020-01-01", "fim": "2022-01-01", "ano": 2020,
             "valor": None, "moeda": "BRL", "valor_usd": None, "fonte": "fapesp_bv", "pagina": "p",
             "publicacoes": 2, "researcher_id": None, "investigador": None, "orcid": None,
             "inst_cnpj": "63025530002824", "afiliacao": "USP. EESC"}],
        "oa_award_finep": [{"award_id": "G4", "contrato": "01.20.0026"}],
        "edital_perfil": [{"call_id": 1, "situacao": "tematico"}, {"call_id": 2, "situacao": "todas_as_areas"}],
        "edital_pesquisador": [{"call_id": 1, "researcher_id": "A1", "posicao": 1, "nota": 0.31, "semelhanca": 0.6,
                                "auxilios_da_agencia": 1, "publicacoes_da_agencia": 0}],
        "edital_laboratorio": [{"call_id": 1, "lab_id": 5, "posicao": 1, "nota": 0.3, "semelhanca_laboratorio": 0.55,
                                "semelhanca_equipe": 0.6}],
    }
    for name, rows in tables.items():
        _write(snap, name, rows)
    (snap / "snapshot.json").write_text(json.dumps({"exported_at": "2026-11-17T00:00:00Z",
                                                    "tables": {n: {} for n in tables}}), encoding="utf-8")

    from pipeline.build import SCHEMA, load_snapshot

    db = tmp_path_factory.mktemp("db") / "fomenta.duckdb"
    con = duckdb.connect(str(db))
    con.execute(SCHEMA)
    con.execute("INSERT INTO funding_award (id, source_id, source_key, title, instrument, executor_name, signing_date, "
                "approved_value, released_value) VALUES (1, 'finep', '01.20.0026.01', 'Laboratório NB3', "
                "'Não Reembolsável a ICTs', 'UFMG', DATE '2020-06-01', 1000000, 900000)")
    load_snapshot(con, snap, db.parent / "tabelas")  # elos_/oa_ em Parquet, como no build
    con.close()

    from fastapi.testclient import TestClient

    from api import index, queries
    queries.DB_PATH = db
    queries._connection.cache_clear()
    queries._search_path.cache_clear()
    return TestClient(index.app)


def test_editais(client):
    assert client.get("/v1/editais").json()["total"] == 2
    abertos = client.get("/v1/editais", params={"situacao": "abertos"}).json()
    assert [e["id"] for e in abertos["items"]] == [1]
    assert client.get("/v1/editais", params={"tipo": "bolsa"}).json()["items"][0]["id"] == 2  # derivado da modalidade
    assert client.get("/v1/editais", params={"q": "INOVACAO saude"}).json()["total"] == 1  # sem acento e sem caixa
    assert client.get("/v1/editais", params={"area": "3.00.00.00-9"}).json()["total"] == 1
    d = client.get("/v1/editais/1").json()
    assert d["areas"][0]["nome"] == "Engenharias" and d["funding_items"] == ["x"] and d["texto_url"]
    assert client.get("/v1/editais/1/texto").json()["texto"] == "Texto integral"
    assert client.get("/v1/editais/2/texto").status_code == 404
    assert client.get("/v1/agencias").json()["items"][0]["editais"] == 2


def test_congressos(client):
    assert client.get("/v1/congressos", params={"situacao": "proximos"}).json()["total"] == 1
    assert client.get("/v1/congressos", params={"categoria": "optics"}).json()["total"] == 1
    assert client.get("/v1/congressos/7").json()["descricao"] == "d"


def test_infraestrutura(client):
    assert client.get("/v1/instituicoes", params={"q": "sao paulo"}).json()["total"] == 1
    assert client.get("/v1/instituicoes/63.025.530/0001-04".replace("/", "")).status_code == 404  # sem barra só dígitos
    inst = client.get("/v1/instituicoes/63025530000104").json()
    assert inst["sigla"] == "USP" and inst["laboratorios_url"].endswith("63025530000104")
    labs = client.get("/v1/laboratorios", params={"area": "Espectrometria de Massas"}).json()
    assert labs["total"] == 1 and labs["items"][0]["equipamentos"] == 1
    lab = client.get("/v1/laboratorios/5").json()
    assert lab["areas_de_atuacao"] == ["Espectrometria de Massas"]
    assert not {"email", "contato", "telefone_contato"} & set(lab)
    assert client.get("/v1/equipamentos", params={"q": "microscopio", "uf": "sp", "multiusuario": True}).json()["total"] == 1


def test_sinergia(client):
    g = client.get("/v1/sinergia/grafos/ga:1").json()
    assert g["nos"][0]["comunidade"] == 0 and g["arestas"][0] == {"a": 5, "b": 6, "em_comum": 2, "score": 0.5}
    assert client.get("/v1/sinergia/grafos/ga:1", params={"base": "outra"}).status_code == 422
    perfil = client.get("/v1/sinergia/laboratorios/5").json()
    assert perfil["tecnicas"] == ["HPLC"] and perfil["grafos"] == ["ga:1"]


def test_elos(client):
    assert client.get("/v1/elos/pesquisadores", params={"q": "perez"}).json()["total"] == 1
    assert client.get("/v1/elos/pesquisadores", params={"laboratorio": 5}).json()["items"][0]["id"] == "A1"
    p = client.get("/v1/elos/pesquisadores/A1").json()
    assert p["laboratorios"][0]["papel"] == "responsavel" and p["coautores"][0]["id"] == "A2"
    assert client.get("/v1/elos/laboratorios/5/coautorias").json()["items"][0]["coautorias"] == 4
    assert client.get("/v1/elos/pesquisadores/X").status_code == 404


def test_elos_coautor_do_responsavel(client):
    # quem publicou com o responsável fica ligado ao laboratório, com evidência
    co = client.get("/v1/elos/pesquisadores", params={"laboratorio": 5, "papel": "coautor"}).json()
    assert [x["id"] for x in co["items"]] == ["A2"] and co["items"][0]["pais"] == "CL"
    lab = client.get("/v1/elos/laboratorios/5").json()
    assert [x["id"] for x in lab["pesquisadores"]] == ["A1"] and lab["total_coautores"] == 1
    c = lab["coautores"][0]
    assert c["publicacoes_com_responsavel"] == 2 and c["publicacoes"][0] == "https://openalex.org/W2"
    p = client.get("/v1/elos/pesquisadores/A2").json()
    assert p["laboratorios"][0]["papel"] == "coautor" and p["laboratorios"][0]["responsavel"]["nome"] == "Ana Pérez"
    todos = client.get("/v1/elos/pesquisadores/A1/coautores").json()
    assert todos["total"] == 1 and todos["items"][0]["publicacoes_em_comum"] == 2
    assert client.get("/v1/elos/pesquisadores", params={"q": "chile"}).json()["total"] == 1  # busca pela instituição
    assert client.get("/v1/elos/pesquisadores", params={"papel": "chefe"}).status_code == 422


def test_publicacoes_e_financiamento(client):
    assert client.get("/v1/publicacoes", params={"q": "espectrometria"}).json()["total"] == 1
    w = client.get("/v1/publicacoes/W1").json()
    assert {a["id"] for a in w["autores"]} == {"A1", "A2"} and w["auxilios"][0]["processo"] == "2021/01234-5"
    assert w["palavras_chave"] == ["Mass spectrometry"] and w["ods"] == ["Saúde e bem-estar"]
    assert client.get("/v1/publicacoes", params={"financiador": "f1"}).json()["total"] == 1
    assert client.get("/v1/publicacoes", params={"ods": "3"}).json()["total"] == 1
    assert client.get("/v1/publicacoes/financiadores").json()["items"][0]["publicacoes"] == 1
    f = client.get("/v1/publicacoes/financiadores/F1").json()
    assert f["laboratorios"][0]["id"] == 5 and f["pesquisadores"][0]["publicacoes"] == 1
    assert client.get("/v1/elos/pesquisadores/A1/publicacoes").json()["total"] == 2
    assert client.get("/v1/elos/pesquisadores/A1/publicacoes", params={"ordem": "citadas"}).json()["items"][0]["id"] == "W1"
    fin = client.get("/v1/elos/laboratorios/5/financiamentos").json()
    assert fin["publicacoes"] == 2 and fin["com_financiador"] == 1 and fin["auxilios"][0]["processo"] == "2021/01234-5"
    assert client.get("/v1/elos/laboratorios/5/publicacoes", params={"ano": 2025}).json()["total"] == 1
    assert client.get("/v1/publicacoes/W9").status_code == 404


def test_auxilios_e_financiadores(client):
    fs = client.get("/v1/financiadores").json()
    assert fs["total"] == 2 and {f["id"] for f in fs["items"]} == {"F1", "F2"}
    assert client.get("/v1/financiadores", params={"com_editais": True}).json()["items"][0]["agencia"]["slug"] == "fapemig"
    f = client.get("/v1/financiadores/fapemig").json()  # pelo slug da agência
    assert f["id"] == "F2" and [e["id"] for e in f["editais_abertos"]] == [1]
    assert f["pesquisadores"][0] == {"id": "A1", "nome": "Ana Pérez", "instituicao_sigla": f["pesquisadores"][0]["instituicao_sigla"],
                                     "auxilios": 1, "publicacoes": 0}
    assert f["laboratorios"][0]["id"] == 5 and f["auxilios_por_ano"][0]["valor_brl"] == 50000.0
    assert client.get("/v1/financiadores/F9").status_code == 404
    assert client.get("/v1/auxilios").json()["total"] == 2
    assert [a["id"] for a in client.get("/v1/auxilios", params={"vigentes": True}).json()["items"]] == ["G2"]
    assert client.get("/v1/auxilios", params={"financiador": "F1"}).json()["items"][0]["pesquisador"] is None
    assert client.get("/v1/auxilios", params={"q": "proteomica"}).json()["total"] == 1
    assert client.get("/v1/auxilios", params={"cnpj": "63.025.530/0001-04"}).json()["total"] == 2
    assert client.get("/v1/elos/pesquisadores/A1/auxilios").json()["items"][0]["processo"] == "APQ-01234-24"
    assert client.get("/v1/elos/laboratorios/5/auxilios").json()["total"] == 1
    g4 = client.get("/v1/auxilios/G4").json()  # citado nas publicações, contrato FINEP
    assert g4["projetos_finep"][0]["contrato"] == "01.20.0026.01" and g4["publicacoes"][0]["id"] == "W2"
    assert client.get("/v1/auxilios/G2").json()["financiador"]["nome"].endswith("Minas Gerais")
    assert client.get("/v1/auxilios/G9").status_code == 404
    p = client.get("/v1/investimentos/projetos/finep/01.20.0026.01/publicacoes").json()
    assert p["total"] == 1 and p["items"][0]["id"] == "W2"


def test_perfil_para_editais(client):
    p = client.get("/v1/editais/1/pesquisadores").json()
    assert p["perfil"] == "tematico" and p["motivo_sem_lista"] is None
    assert p["items"][0]["id"] == "A1" and p["items"][0]["laboratorios"][0]["id"] == 5
    assert p["items"][0]["auxilios_da_agencia"] == 1
    g = client.get("/v1/editais/2/pesquisadores").json()
    assert g["items"] == [] and g["perfil"] == "todas_as_areas" and "todas as áreas" in g["motivo_sem_lista"]
    assert client.get("/v1/editais/1/laboratorios").json()["items"][0]["id"] == 5
    assert client.get("/v1/editais/99/pesquisadores").status_code == 404
    e = client.get("/v1/elos/pesquisadores/A1/editais").json()["items"]
    assert e[0]["id"] == 1 and e[0]["posicao_no_edital"] == 1 and e[0]["agencia"]["slug"] == "fapemig"
    assert client.get("/v1/elos/laboratorios/5/editais").json()["items"][0]["id"] == 1


def test_colaborador_estavel(client):
    lab = client.get("/v1/elos/laboratorios/5").json()
    assert lab["colaboradores_estaveis"] == 1 and lab["coautores"][0]["colaborador_estavel"] is True
    assert lab["coautores"][0]["anos_em_comum"] == 2
    assert client.get("/v1/elos/laboratorios/5", params={"estavel": False}).json()["coautores"] == []
    assert client.get("/v1/elos/pesquisadores", params={"laboratorio": 5, "papel": "coautor", "estavel": True}).json()["total"] == 1
    assert client.get("/v1/elos/pesquisadores/A2").json()["laboratorios"][0]["colaborador_estavel"] is True
