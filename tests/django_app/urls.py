from django.urls import include, path, re_path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("items", views.ItemViewSet, basename="item")

urlpatterns = [
    path("products/<int:pk>/", views.product),
    path("async/<int:pk>/", views.async_product),
    path("checkout/", views.checkout),
    path("place-order/", views.place_order),
    path("async/place-order/", views.async_place_order),
    path("healthz/", views.healthz),
    path("orders/<int:pk>/", views.OrderView.as_view()),
    path("api/", include([
        path("report/", views.report),
        re_path(r"^legacy/(?P<slug>[\w-]+)/$", views.product),
    ])),
    path("api/v2/", include(router.urls)),
]
