from django.http import HttpResponse, JsonResponse
from django.views import View
from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response


def product(request, pk):
    return HttpResponse(f"product {pk}")


def checkout(request):
    raise ValueError("card 4242 declined")


def healthz(request):
    return HttpResponse("ok")


async def async_product(request, pk):
    return HttpResponse(f"async {pk}")


class OrderView(View):
    def get(self, request, pk):
        return JsonResponse({"pk": pk})

    def post(self, request, pk):
        return JsonResponse({"pk": pk}, status=201)


@api_view(["GET", "POST"])
def report(request):
    return Response({"ok": True})


class ItemViewSet(viewsets.ViewSet):
    def list(self, request):
        return Response([])

    def retrieve(self, request, pk=None):
        return Response({"pk": pk})
