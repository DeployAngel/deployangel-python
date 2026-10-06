from flask import Flask, abort


def build():
    app = Flask(__name__)

    @app.get("/users/<int:user_id>")
    def show_user(user_id):
        if user_id == 404:
            abort(404)
        return {"id": user_id}

    @app.post("/users")
    def create_user():
        return {"id": 1}, 201

    @app.get("/boom")
    def boom():
        raise KeyError("missing sku-17")

    @app.get("/healthz")
    def healthz():
        return "ok"

    return app
