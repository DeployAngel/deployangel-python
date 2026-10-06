"""FastAPI integration:

    app = FastAPI()
    deployangel.fastapi.init(app)
"""

from deployangel.asgi import DeployAngelMiddleware, init

__all__ = ["DeployAngelMiddleware", "init"]
