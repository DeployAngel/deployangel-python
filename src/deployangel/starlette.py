"""Starlette integration:

    app = Starlette(routes=[...])
    deployangel.starlette.init(app)
"""

from deployangel.asgi import DeployAngelMiddleware, init

__all__ = ["DeployAngelMiddleware", "init"]
