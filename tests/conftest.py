import json

import pytest

#: chave institucional com a permissão de ver editais, usada pelos testes das rotas
EDITAIS_KEY = "fmt_teste_institucional_editais"


def keys_env(extra: dict | None = None) -> str:
    from api.planos import key_hash
    return json.dumps({key_hash(EDITAIS_KEY): {"plano": "institucional", "nome": "Testes", "editais": True},
                       **(extra or {})})


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    """Cada teste começa com a cota cheia (todos saem do mesmo "IP" do TestClient) e com a chave dos testes."""
    from api.contador import counter
    counter.reset()
    monkeypatch.setenv("FOMENTA_API_KEYS", keys_env())
    yield
