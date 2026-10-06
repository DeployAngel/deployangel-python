from fastapi import APIRouter, FastAPI, HTTPException
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

router = APIRouter(prefix="/orders")


@router.get("/{order_id}")
def get_order(order_id: int):
    return {"id": order_id}


@router.post("/")
def create_order():
    return {"id": 1}


admin = APIRouter()


@admin.get("/reports/{report_id}")
def admin_report(report_id: int):
    return {"id": report_id}


def build():
    app = FastAPI()

    @app.get("/items/{item_id}")
    def read_item(item_id: int):
        if item_id == 404:
            raise HTTPException(status_code=404)
        return {"id": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("upstream 503 from payments")

    @app.get("/healthz")
    def healthz():
        return "ok"

    app.include_router(router)
    app.include_router(admin, prefix="/admin")

    async def legacy(request):
        return PlainTextResponse(request.path_params["slug"])

    app.mount("/v1", Starlette(routes=[Route("/legacy/{slug}", legacy)]))
    return app
