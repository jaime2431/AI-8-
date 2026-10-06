"""Each company must see only its own data and its own Claude conversations."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backbone.agents.assistant import answer
from backbone.db import assistant_messages, clean_invoices
from backbone.notify import ConsoleNotifier
from backbone.pipeline import run_nightly
from backbone.tenancy import ClientRegistry, SecretStore
from conftest import Block, FakeClaude, FakePowerBI, drop_csv, drop_ecf, make_ctx
from portal.app import create_app
from test_pipeline import CSV, DAY, fake_reader


def test_each_client_has_its_own_database_and_keys(registry):
    a, b = make_ctx(registry, "alpha"), make_ctx(registry, "beta")
    assert str(a.engine.url) != str(b.engine.url)
    assert SecretStore(registry.get("alpha")).get("anthropic-api-key") == "test-key-alpha"
    assert SecretStore(registry.get("beta")).get("anthropic-api-key") == "test-key-beta"


def test_registry_refuses_shared_tenant():
    from backbone.tenancy import ClientConfig
    c1 = ClientConfig("a", "A", "131000002", "same")
    c2 = ClientConfig("b", "B", "131000002", "same")
    with pytest.raises(ValueError):
        ClientRegistry({"a": c1, "b": c2})


def _contexts(registry):
    pbis = {"alpha": FakePowerBI({"VENTAS": 12390.0, "COMPRAS": 5860.0}), "beta": FakePowerBI()}
    built = {}

    def factory(reg, cid):
        if cid not in built:
            built[cid] = make_ctx(reg, cid, powerbi=pbis[cid])
        return built[cid]
    return factory, built, pbis


def test_portal_user_cannot_reach_another_company(registry):
    factory, built, _ = _contexts(registry)
    alpha = factory(registry, "alpha")
    beta = factory(registry, "beta")
    drop_ecf(registry, "alpha", "ecf_sale.xml")
    drop_csv(registry, "alpha", "pos.csv", CSV)      # one row goes to alpha's review queue
    run_nightly(alpha, DAY, ConsoleNotifier(), reader=fake_reader)

    app = create_app(registry, context_factory=factory, env="dev")
    http = TestClient(app)
    a_queue = http.get("/review-queue", headers={"X-Dev-Client": "alpha"}).json()
    b_queue = http.get("/review-queue", headers={"X-Dev-Client": "beta"}).json()
    assert len(a_queue) == 1 and b_queue == []

    # beta tries to approve alpha's item id: it does not exist in beta's database
    item = a_queue[0]["id"]
    r = http.post(f"/review-queue/{item}/approve", json={"corrections": {}}, headers={"X-Dev-Client": "beta"})
    assert r.status_code == 404
    with alpha.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(clean_invoices)).scalar() == 2   # unchanged


def test_dev_login_disabled_in_production(registry):
    factory, _, _ = _contexts(registry)
    http = TestClient(create_app(registry, context_factory=factory, env="prod"))
    assert http.get("/me", headers={"X-Dev-Client": "alpha"}).status_code == 401
    assert http.get("/me").status_code == 401


def test_unknown_company_refused(registry):
    factory, _, _ = _contexts(registry)
    http = TestClient(create_app(registry, context_factory=factory, env="dev"))
    assert http.get("/me", headers={"X-Dev-Client": "gamma"}).status_code == 403


def test_assistant_reads_only_its_company_and_keeps_chats_separate(registry):
    factory, built, pbis = _contexts(registry)
    alpha = factory(registry, "alpha")
    beta = factory(registry, "beta")
    alpha.__dict__["claude"] = FakeClaude([
        [Block("tool_use", name="run_dax", input={"query": "EVALUATE ROW(\"v\", [VENTAS])"})],
        [Block("text", text="Las ventas fueron RD$12,390.")],
    ])
    http = TestClient(create_app(registry, context_factory=factory, env="dev"))
    r = http.post("/ask", json={"question": "¿Cuánto vendimos ayer?"}, headers={"X-Dev-Client": "alpha", "X-Dev-User": "u1"})
    assert r.status_code == 200 and "12,390" in r.json()["answer"]
    assert pbis["alpha"].queries and not pbis["beta"].queries
    with alpha.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(assistant_messages)).scalar() == 2
    with beta.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(assistant_messages)).scalar() == 0


def test_assistant_refuses_non_read_queries():
    pbi = FakePowerBI()
    claude = FakeClaude([
        [Block("tool_use", name="run_dax", input={"query": "DELETE everything"})],
        [Block("text", text="No puedo hacer eso.")],
    ])
    text, queries = answer(claude, "m", pbi, "Alpha", "", "borra los datos")
    assert pbi.queries == [] and queries == ["DELETE everything"]
    tool_result = claude.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
