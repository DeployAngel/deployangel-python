import pytest
from conftest import drain

import deployangel
from deployangel.core import work


def routes(payload):
    return {route["key"]: route["requests"] for route in payload["routes"]}


def checkpoints(payload):
    """key -> (count, recorded in a request, recorded in a job)"""
    return {entry["key"]: (entry["count"], entry["http"], entry["job"]) for entry in payload["checkpoints"]}


@pytest.fixture
def fastapi_app(started_agent):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import deployangel.fastapi
    from web_apps.fastapi_app import build

    agent = started_agent(framework="fastapi")
    app = build()
    deployangel.fastapi.init(app)
    return agent, app, TestClient(app, raise_server_exceptions=False)


def test_fastapi_records_requests_against_route_templates(fastapi_app):
    agent, _, client = fastapi_app
    client.get("/items/1")
    client.get("/items/2")
    client.get("/items/404")
    client.get("/orders/5")
    client.post("/orders/")
    client.get("/v1/legacy/abc")
    client.get("/admin/reports/3")
    payload = drain(agent)
    assert routes(payload) == {"GET /items/{item_id}": 3, "GET /orders/{order_id}": 1, "POST /orders/": 1,
                               "GET /v1/legacy/{slug}": 1, "GET /admin/reports/{report_id}": 1}
    assert payload["http"]["status_counts"] == {"404": 1}
    assert agent.runtime["framework"] == "fastapi"


def test_fastapi_unhandled_exceptions_are_500s_with_their_route(fastapi_app):
    agent, _, client = fastapi_app
    client.get("/boom", headers={"host": "shop.example.com"})
    payload = drain(agent)
    assert payload["http"]["status_counts"] == {"500": 1}
    assert payload["http"]["unhandled_exceptions"] == 1
    (exception,) = payload["exceptions"]
    assert exception["exception_class"] == "RuntimeError"
    assert exception["message"] == "upstream <n> from payments"
    assert exception["sources"] == {"route:GET /boom": 1}
    assert exception["top_frame"] == "tests/web_apps/fastapi_app.py#boom"


def test_fastapi_unmatched_paths_and_health_checks_stay_out_of_the_totals(fastapi_app):
    agent, _, client = fastapi_app
    client.get("/wp-admin")
    client.get("/healthz")
    payload = drain(agent)
    assert payload["http"]["requests"] == 0
    assert routes(payload) == {"GET unmatched": 1}


def test_fastapi_lists_routes_with_their_endpoints(fastapi_app):
    agent, app, _ = fastapi_app
    from deployangel.asgi import StarletteRoutes

    table = {route["key"]: route for route in StarletteRoutes(app).routes()}
    assert table["GET /items/{item_id}"] == {"key": "GET /items/{item_id}", "controller": "web_apps.fastapi_app",
                                             "action": "build.<locals>.read_item", "files": ["tests/web_apps/fastapi_app.py"]}
    assert {"GET /orders/{order_id}", "POST /orders/", "GET /v1/legacy/{slug}", "GET /admin/reports/{report_id}"} <= set(table)
    assert not any(key.startswith(("HEAD", "GET /docs", "GET /openapi")) for key in table)


def test_fastapi_checkpoints_count_as_recorded_in_a_request(fastapi_app):
    agent, app, client = fastapi_app

    @app.post("/carts/{cart_id}/checkout")
    async def checkout(cart_id: int):
        deployangel.checkpoint("order.created")
        return {"id": cart_id}

    @app.post("/carts/{cart_id}/sync-checkout")
    def sync_checkout(cart_id: int):  # runs in Starlette's thread pool
        deployangel.checkpoint("order.created")
        return {"id": cart_id}

    @app.post("/carts/{cart_id}/declined")
    async def declined(cart_id: int):
        deployangel.checkpoint("payment.attempted")
        raise RuntimeError("card declined")

    client.post("/carts/1/checkout")
    client.post("/carts/1/sync-checkout")
    client.post("/carts/1/declined")
    deployangel.checkpoint("order.created")
    assert checkpoints(drain(agent)) == {"order.created": (3, 2, 0), "payment.attempted": (1, 1, 0)}


async def test_asgi_middleware_ends_the_request_when_the_app_raises(started_agent):
    from deployangel.asgi import DeployAngelMiddleware

    agent = started_agent(framework="starlette")
    seen = []

    async def app(scope, receive, send):
        seen.append(work.current())
        deployangel.checkpoint("payment.attempted")
        raise RuntimeError("card declined")

    with pytest.raises(RuntimeError):
        await DeployAngelMiddleware(app)({"type": "http", "method": "POST", "path": "/pay", "headers": []}, None, None)
    assert seen == [work.HTTP]
    assert work.current() is None
    deployangel.checkpoint("payment.attempted")
    assert checkpoints(drain(agent)) == {"payment.attempted": (2, 1, 0)}


def test_starlette_without_fastapi(started_agent):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    import deployangel.starlette

    async def page(request):
        return PlainTextResponse("ok")

    agent = started_agent(framework="starlette")
    app = Starlette(routes=[Route("/pages/{slug:str}", page)])
    deployangel.starlette.init(app)
    TestClient(app).get("/pages/about")
    assert routes(drain(agent)) == {"GET /pages/{slug}": 1}


@pytest.fixture
def flask_app(started_agent):
    pytest.importorskip("flask")
    import deployangel.flask
    from web_apps.flask_app import build

    agent = started_agent(framework="flask")
    app = build()
    deployangel.flask.init_app(app)
    return agent, app, app.test_client()


def test_flask_records_requests_against_url_rules(flask_app):
    agent, _, client = flask_app
    client.get("/users/1")
    client.get("/users/404")
    client.post("/users")
    client.get("/nowhere")
    client.get("/healthz")
    payload = drain(agent)
    assert routes(payload) == {"GET /users/<int:user_id>": 2, "POST /users": 1, "GET unmatched": 1}
    assert payload["http"]["requests"] == 3
    assert payload["http"]["status_counts"] == {"404": 1}


def test_flask_unhandled_exceptions_are_500s_with_their_route(flask_app):
    agent, _, client = flask_app
    response = client.get("/boom")
    assert response.status_code == 500
    payload = drain(agent)
    assert payload["http"]["unhandled_exceptions"] == 1
    (exception,) = payload["exceptions"]
    assert (exception["exception_class"], exception["sources"]) == ("KeyError", {"route:GET /boom": 1})
    assert exception["message"] == "<string>"


def test_flask_records_exceptions_flask_propagates(flask_app):
    agent, app, client = flask_app
    app.testing = True  # Flask re-raises instead of rendering a 500
    with pytest.raises(KeyError):
        client.get("/boom")
    payload = drain(agent)
    assert payload["http"]["status_counts"] == {"500": 1}
    assert payload["exceptions"][0]["sources"] == {"route:GET /boom": 1}


def test_flask_checkpoints_count_as_recorded_in_a_request(flask_app):
    agent, app, client = flask_app

    @app.post("/orders")
    def create_order():
        deployangel.checkpoint("order.created")
        return {"id": 1}, 201

    @app.post("/payments")
    def pay():
        deployangel.checkpoint("payment.attempted")
        raise KeyError("declined")

    client.post("/orders")
    client.post("/payments")  # a 500 Flask renders itself
    app.testing = True  # Flask re-raises into this thread
    with pytest.raises(KeyError):
        client.post("/payments")
    assert work.current() is None
    deployangel.checkpoint("order.created")
    assert checkpoints(drain(agent)) == {"order.created": (2, 1, 0), "payment.attempted": (2, 2, 0)}


def test_flask_lists_url_rules(flask_app):
    _, app, _ = flask_app
    from deployangel.flask import FlaskRoutes

    table = {route["key"]: route for route in FlaskRoutes(app).routes()}
    assert set(table) == {"GET /users/<int:user_id>", "POST /users", "GET /boom", "GET /healthz"}
    assert table["POST /users"]["files"] == ["tests/web_apps/flask_app.py"]


def test_nothing_is_recorded_when_the_agent_is_inactive():
    import deployangel.flask
    from web_apps.flask_app import build

    app = build()
    deployangel.flask.init_app(app)
    assert app.test_client().get("/users/1").status_code == 200
    assert not deployangel.recording()
