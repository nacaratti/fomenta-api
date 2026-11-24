"""Avisos de editais: inscrição pelo site e o planejamento/envio das notificações."""
from datetime import datetime

import pytest

from tests.test_plataforma import make_client

SITE = {"Origin": "http://localhost:5173"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {"p256dh": "B" * 87, "auth": "a" * 22}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    return make_client(tmp_path_factory)


@pytest.fixture(autouse=True)
def _arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv("FOMENTA_AVISOS_FILE", str(tmp_path / "avisos.json"))
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)


def inscrever(client, segue, endpoint=ENDPOINT, headers=SITE):
    return client.post("/v1/avisos/inscricao", json={"inscricao": {"endpoint": endpoint, "keys": KEYS}, "segue": segue},
                       headers=headers)


def test_chave_publica(client, monkeypatch):
    assert client.get("/v1/avisos/chave").status_code == 503
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUB")
    assert client.get("/v1/avisos/chave").json() == {"chave": "BPUB"}


def test_inscricao_so_pelo_site_e_servicos_conhecidos(client):
    assert inscrever(client, {}, headers={}).status_code == 403
    assert inscrever(client, {}, headers={"Origin": "https://outro.com"}).status_code == 403
    assert inscrever(client, {}, endpoint="https://evil.example.com/x").status_code == 422
    assert inscrever(client, {}, endpoint="http://fcm.googleapis.com/x").status_code == 422
    assert inscrever(client, {"laboratorios": ["abc"]}).status_code == 422
    assert inscrever(client, {"agencias": ["x"] * 51}).status_code == 200  # repetidos contam uma vez


def test_inscrever_consultar_cancelar(client):
    r = inscrever(client, {"agencias": ["fapemig"], "laboratorios": ["5"], "pesquisadores": ["A1"]})
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    assert r.json()["segue"]["laboratorios"] == ["5"]
    c = client.post("/v1/avisos/consulta", json={"endpoint": ENDPOINT}, headers=SITE).json()
    assert c["segue"]["agencias"] == ["fapemig"]
    inscrever(client, {"agencias": []})  # atualizar mantém a inscrição
    assert client.post("/v1/avisos/consulta", json={"endpoint": ENDPOINT}, headers=SITE).json()["segue"]["agencias"] == []
    assert client.post("/v1/avisos/cancelar", json={"endpoint": ENDPOINT}, headers=SITE).json() == {"cancelado": True}
    assert client.post("/v1/avisos/consulta", json={"endpoint": ENDPOINT}, headers=SITE).status_code == 404


def test_planejamento_e_envio(client):
    from api import queries
    from api.avisos import store, sub_id
    from tools.notificar_editais import plan, run

    cur = queries.cursor()
    now = datetime.now()
    # edital 1 (aberto, da FAPEMIG, com perfil para o laboratório 5 e para A1) foi descoberto há 30 dias
    assert plan(cur, {"agencias": ["fapemig"]}, set(), now, days=14) == []
    (a,) = plan(cur, {"agencias": ["fapemig"]}, set(), now, days=60)
    assert a.url == "/editais/1" and "FAPEMIG" in a.titulo and a.editais == [1]
    # o mesmo edital por dois caminhos vira um aviso só; já avisado, não volta
    both = plan(cur, {"agencias": ["fapemig"], "laboratorios": ["5"]}, set(), now, days=60)
    assert len(both) == 1
    (lab,) = plan(cur, {"laboratorios": ["5"]}, set(), now, days=60)
    assert "LE" in lab.titulo
    assert plan(cur, {"laboratorios": ["5"]}, {1}, now, days=60) == []

    inscrever(client, {"pesquisadores": ["A1"]})
    st = store()
    enviados = []
    stats = run(st, cur, lambda sub, payload: enviados.append(payload) or 201, days=60, log=lambda *_: None)
    assert stats["avisos"] == 1 and '"url": "/editais/1"' in enviados[0]
    assert run(st, cur, lambda s, p: 201, days=60)["avisos"] == 0  # não repete
    # navegador cancelou a inscrição (410): ela some
    st.delete(sub_id(ENDPOINT))
    inscrever(client, {"agencias": ["fapemig"]})
    assert run(st, cur, lambda s, p: 410, days=60)["removidos"] == 1
    assert st.ids() == []


def test_importa_edicoes_enviadas_do_painel(tmp_path):
    from tools.newsletter_fomentabr import fetch, write

    painel = {
        "/api/admin/newsletter/editions": {"editions": [
            {"id": 1, "status": "sent", "sent_success_count": 10},
            {"id": 2, "status": "draft", "sent_success_count": None},
            {"id": 3, "status": "sending", "sent_success_count": 4},  # envio em lotes, já saiu
        ]},
        "/api/admin/newsletter/editions/1": {"id": 1, "edition_date": "2026-09-01", "subject": "Setembro",
                                             "markdown": "m", "html": "<p>s</p>", "call_ids": [1, 2],
                                             "conference_ids": [], "send_completed_at": "2026-09-01T12:00:00Z"},
        "/api/admin/newsletter/editions/3": {"id": 3, "edition_date": "2026-10-01", "subject": "Outubro",
                                             "markdown": "m", "html": "<p>o</p>", "call_ids": [],
                                             "conference_ids": [7], "send_completed_at": None},
    }
    rows = fetch(painel.__getitem__)
    assert [r["subject"] for r in rows] == ["Outubro", "Setembro"]  # rascunho fica de fora
    assert rows[1]["call_ids"] == "[1, 2]"
    (snap := tmp_path / "snapshot").mkdir()
    (snap / "snapshot.json").write_text('{"tables": {"edital": {}}}', encoding="utf-8")
    paths = write(rows, tmp_path)
    assert all(p.exists() for p in paths)
    assert "newsletter_edicao" in (snap / "snapshot.json").read_text(encoding="utf-8")
