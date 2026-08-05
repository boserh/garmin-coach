"""The generated OpenAPI schema (and the /docs, /redoc pages built from it) must stay
off. It's a personal single-tenant app: nothing consumes the schema, while a scanner
that fetches it gets the full route map, every form's field names, and our route
docstrings describing internal guards and cost paths. Anonymous is the case that
matters — a scanner never has a session — but keep it shut for logged-in users too,
since the schema is the same document either way.
"""


def test_openapi_json_not_served(client):
    assert client.get("/openapi.json").status_code == 404


def test_docs_pages_not_served(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_openapi_json_not_served_when_logged_in(auth_client):
    assert auth_client.get("/openapi.json").status_code == 404
