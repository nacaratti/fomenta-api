"""Planos, chaves, limites contra raspagem, cache e o servidor MCP."""
import dataclasses
import json

import pytest

from tests.conftest import EDITAIS_KEY, keys_env
from tests.test_plataforma import make_client


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    return make_client(tmp_path_factory)  # sem chave: plano Aberto

GRATUITA = "fmt_teste_gratuita_123456"
VENCIDA = "fmt_teste_vencida_123456"


@pytest.fixture(autouse=True)
def _chaves(monkeypatch):
    from api.planos import key_hash
    monkeypatch.setenv("FOMENTA_API_KEYS", keys_env({
        key_hash(GRATUITA): {"plano": "gratuito", "nome": "Teste"},
        key_hash(VENCIDA): {"plano": "pro", "nome": "Antiga", "expira": "2020-01-01"},
    }))


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_planos(client):
    r = client.get("/v1/planos")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["items"]]
    assert ids == ["anonimo", "gratuito", "pro", "institucional"]
    assert r.headers["x-plano"] == "anonimo"


def test_anonimo_pagina_limitada(client):
    assert client.get("/v1/laboratorios", params={"per_page": 51}).status_code == 403
    assert client.get("/v1/laboratorios", params={"per_page": 50, "page": 21}).status_code == 403
    r = client.get("/v1/laboratorios", params={"per_page": 50, "page": 20})
    assert r.status_code == 200
    assert r.headers["cache-control"].startswith("public")
    assert "no-store" in client.get("/v1/laboratorios", params={"per_page": 51}).headers["cache-control"]


def test_limite_de_itens_em_outros_parametros(client):
    assert client.get("/v1/elos/laboratorios/5/coautorias", params={"limite": 30}).status_code == 200
    assert client.get("/v1/elos/laboratorios/5/coautorias", params={"limite": 60}).status_code == 403
    r = client.get("/v1/elos/laboratorios/5", params={"coautores": 80})
    assert r.status_code == 403


def test_chave_sobe_os_limites_e_nao_vai_para_o_cache(client):
    r = client.get("/v1/laboratorios", params={"per_page": 100}, headers=auth(GRATUITA))
    assert r.status_code == 200
    assert r.headers["x-plano"] == "gratuito"
    assert r.headers["cache-control"] == "private, no-store"
    r = client.get("/v1/laboratorios", params={"per_page": 10}, headers={"X-API-Key": GRATUITA})
    assert r.status_code == 200 and r.headers["cache-control"].startswith("public")
    # 10.000 registros no plano Pesquisa
    assert client.get("/v1/laboratorios", params={"per_page": 100, "page": 101}, headers=auth(GRATUITA)).status_code == 403


def test_chave_invalida_ou_vencida(client):
    assert client.get("/v1/laboratorios", headers=auth("fmt_nao_existe")).status_code == 401
    assert client.get("/v1/laboratorios", headers=auth("qualquer")).status_code == 401
    r = client.get("/v1/laboratorios", headers=auth(VENCIDA))
    assert r.status_code == 401 and "expirada" in r.json()["detail"]


def test_limite_por_minuto(client, monkeypatch):
    from api import planos
    monkeypatch.setitem(planos.PLANOS, "anonimo", dataclasses.replace(planos.PLANOS["anonimo"], por_minuto=3))
    codes = [client.get("/v1/agencias").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.get("/v1/agencias")
    assert int(r.headers["retry-after"]) >= 1 and "no-store" in r.headers["cache-control"]
    # com chave, a cota é outra
    assert client.get("/v1/agencias", headers=auth(GRATUITA)).status_code == 200


def test_cabecalhos_de_cota(client):
    r = client.get("/v1/agencias", headers=auth(GRATUITA))
    assert r.headers["x-ratelimit-limit"] == "120"
    assert int(r.headers["x-ratelimit-remaining"]) == 119
    assert r.headers["x-ratelimit-limit-day"] == "10000"


def test_conta(client):
    d = client.get("/v1/conta", headers=auth(GRATUITA)).json()
    assert d["com_chave"] and d["nome"] == "Teste" and d["plano"]["id"] == "gratuito"
    assert client.get("/v1/conta").json()["com_chave"] is False


def test_redis_indisponivel_conta_em_memoria():
    from api.contador import MemoryCounter, RedisRestCounter
    c = RedisRestCounter("http://127.0.0.1:9", "x", MemoryCounter())
    assert [c.hit("k", 60) for _ in range(3)] == [1, 2, 3]


# --------------------------------------------------------------------------- MCP
def rpc(client, method, params=None, id_=1, headers=None):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                       headers=headers or {})


def test_mcp_initialize_e_lista(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "teste", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "fomenta-br"
    assert "tools" in res["capabilities"]
    n = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert n.status_code == 202
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"laboratorio", "buscar_laboratorios", "pesquisador", "investimentos"} <= names
    assert all(t["inputSchema"]["type"] == "object" for t in tools)


def test_mcp_chama_ferramentas(client):
    r = rpc(client, "tools/call", {"name": "buscar_laboratorios", "arguments": {"q": "espectrometria"}}).json()
    data = json.loads(r["result"]["content"][0]["text"])
    assert data["total"] == 1 and data["items"][0]["id"] == 5
    r = rpc(client, "tools/call", {"name": "buscar_editais", "arguments": {}}, headers=auth(EDITAIS_KEY)).json()
    assert json.loads(r["result"]["content"][0]["text"])["total"] == 1  # só o aberto
    r = rpc(client, "tools/call", {"name": "laboratorio", "arguments": {"id": 5}}).json()
    assert json.loads(r["result"]["content"][0]["text"])["laboratorio"]["sigla"] == "LE"


def test_mcp_erros(client):
    r = rpc(client, "tools/call", {"name": "laboratorio", "arguments": {"id": 999}}).json()
    assert r["result"]["isError"] and "não encontrado" in r["result"]["content"][0]["text"]
    r = rpc(client, "tools/call", {"name": "buscar_laboratorios", "arguments": {"q": "ab"}}).json()
    assert r["result"]["isError"]
    r = rpc(client, "tools/call", {"name": "buscar_laboratorios", "arguments": {"per_page": 80}}).json()
    assert r["result"]["isError"] and "itens por consulta" in r["result"]["content"][0]["text"]
    assert rpc(client, "tools/call", {"name": "nao_existe", "arguments": {}}).json()["error"]["code"] == -32602
    assert rpc(client, "resources/list").json()["error"]["code"] == -32601
    assert rpc(client, "tools/list", headers=auth("fmt_errada")).status_code == 401
    assert client.get("/mcp").status_code == 405
    assert client.post("/mcp", content=b"{nao json", headers={"Content-Type": "application/json"}).status_code == 400


# --------------------------------------------------------------------------- editais fechados
EDITAIS_ROTAS = ["/v1/editais", "/v1/editais/1", "/v1/editais/1/texto", "/v1/editais/1/pesquisadores",
                 "/v1/editais/1/laboratorios", "/v1/elos/pesquisadores/A1/editais", "/v1/elos/laboratorios/5/editais"]
SITE = {"Origin": "http://localhost:5173"}


def test_editais_fechados_para_a_api_publica(client):
    for path in EDITAIS_ROTAS:
        r = client.get(path)
        assert r.status_code == 403, path
        assert r.headers["x-fomenta-motivo"] == "sessao" and "no-store" in r.headers["cache-control"]
        assert client.get(path, headers=auth(GRATUITA)).status_code == 403, path  # chave comum não basta
    # o detalhe do financiador não traz os editais da agência
    assert client.get("/v1/financiadores/fapemig").json()["editais_abertos"] == []


def test_editais_com_chave_permitida_sem_cache(client):
    r = client.get("/v1/editais", headers=auth(EDITAIS_KEY))
    assert r.status_code == 200 and r.json()["total"] == 2
    assert r.headers["cache-control"] == "private, no-store"
    assert "noindex" in r.headers["x-robots-tag"]
    f = client.get("/v1/financiadores/fapemig", headers=auth(EDITAIS_KEY))
    assert [e["id"] for e in f.json()["editais_abertos"]] == [1] and "no-store" in f.headers["cache-control"]


def test_sessao_do_site(client):
    assert client.post("/v1/sessao").status_code == 403  # sem Origin
    assert client.post("/v1/sessao", headers={"Origin": "https://outro-site.com"}).status_code == 403
    r = client.post("/v1/sessao", headers=SITE)
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    tok = r.json()["token"]
    ok = client.get("/v1/editais", headers={"X-Fomenta-Sessao": tok})
    assert ok.status_code == 200 and ok.headers["x-plano"] == "site"
    # sessão: no máximo 50 por página e 200 registros alcançáveis
    assert client.get("/v1/editais", params={"per_page": 60}, headers={"X-Fomenta-Sessao": tok}).status_code == 403
    assert client.get("/v1/editais", params={"per_page": 50, "page": 5}, headers={"X-Fomenta-Sessao": tok}).status_code == 403
    # token de outro IP, adulterado ou vencido não vale
    from api import sessao
    outro, _ = sessao.issue("203.0.113.9")
    assert client.get("/v1/editais", headers={"X-Fomenta-Sessao": outro}).status_code == 403
    assert client.get("/v1/editais", headers={"X-Fomenta-Sessao": tok[:-2] + "xx"}).status_code == 403
    assert not sessao.verify(tok, "testclient", now=10**12)


def test_robots(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /v1/editais" in r.text and "GPTBot" in r.text


def test_mcp_sem_editais_para_publico(client):
    names = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "buscar_editais" not in names and "edital" not in names
    r = rpc(client, "tools/call", {"name": "buscar_editais", "arguments": {}}).json()
    assert r["result"]["isError"] and "site" in r["result"]["content"][0]["text"]
    full = {t["name"] for t in rpc(client, "tools/list", headers=auth(EDITAIS_KEY)).json()["result"]["tools"]}
    assert {"buscar_editais", "edital"} <= full


# --------------------------------------------------------------------------- newsletter
def test_newsletter_lista_publica_e_conteudo_fechado(client):
    r = client.get("/v1/newsletter/edicoes")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == [4, 3]  # mais recente primeiro
    assert items[1]["editais"] == 2 and items[1]["congressos"] == 1
    assert items[1]["resumo"] == "Editais da FAPEMIG & congressos."  # do HTML enviado, sem o cabeçalho fixo
    # o conteúdo traz editais: só pelo site ou com a permissão
    assert client.get("/v1/newsletter/edicoes/3").status_code == 403
    assert client.get("/v1/newsletter/edicoes/3", headers=auth(EDITAIS_KEY)).json()["html"].startswith("<html>")
    tok = client.post("/v1/sessao", headers=SITE).json()["token"]
    assert client.get("/v1/newsletter/edicoes/3", headers={"X-Fomenta-Sessao": tok}).status_code == 200
    assert client.get("/v1/newsletter/edicoes/99", headers={"X-Fomenta-Sessao": tok}).status_code == 404
