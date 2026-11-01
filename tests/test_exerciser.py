import pytest

from deployangel.cli.exerciser import Exerciser, parse_headers, parse_params


def fill(path, params):
    return Exerciser("https://shop.example.com", params=params).fill(path)


def test_fills_each_frameworks_path_parameters_by_name():
    assert fill("/apps/:app_id/deployments/:id", {"app_id": "3", "id": "212"}) == ("/apps/3/deployments/212", [])
    assert fill("/items/{item_id}", {"item_id": "9"}) == ("/items/9", [])
    assert fill("/orders/{id:int}/lines/{line?}", {"id": "4", "line": "2"}) == ("/orders/4/lines/2", [])
    assert fill("/orders/<int:pk>/", {"pk": "5"}) == ("/orders/5/", [])


def test_names_the_parameters_it_couldnt_fill_once_each():
    assert fill("/apps/:app_id/deployments/:id", {"id": "212"}) == ("/apps/:app_id/deployments/212", ["app_id"])
    assert fill("/a/:id/b/:id", {}) == ("/a/:id/b/:id", ["id"])


def test_escapes_values_keeping_slashes_only_in_parameters_that_span_segments():
    assert fill("/users/:id", {"id": "a b/c"}) == ("/users/a%20b%2Fc", [])
    assert fill("/pages/*id", {"id": "about/team"}) == ("/pages/about/team", [])
    assert fill("/files/{*path}", {"path": "a/b"}) == ("/files/a/b", [])
    assert fill("/files/{path:path}", {"path": "a/b"}) == ("/files/a/b", [])
    assert fill("/docs/<path:rest>", {"rest": "x/y"}) == ("/docs/x/y", [])


def test_reads_params_and_headers_and_never_puts_a_headers_value_in_an_error():
    assert parse_params(["id=42", "slug=a=b"]) == {"id": "42", "slug": "a=b"}
    with pytest.raises(ValueError, match="name=value"):
        parse_params(["id="])
    assert parse_headers(["Authorization:  Bearer x ", "", "Cookie: a=b"]) == {"Authorization": "Bearer x", "Cookie": "a=b"}
    with pytest.raises(ValueError) as error:
        parse_headers(["Cookie s3cret"])
    assert "s3cret" not in str(error.value)
    with pytest.raises(ValueError):
        parse_headers(["X: a\r\nHost: evil"])
