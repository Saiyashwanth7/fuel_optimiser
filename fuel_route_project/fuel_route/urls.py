from django.urls import path, include

from route.views import health_check

urlpatterns = [
    path("health/", health_check),
    path("api/", include("route.urls")),
]
