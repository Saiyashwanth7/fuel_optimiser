from django.urls import path, include

from route.views import health_check, frontend_view

urlpatterns = [
    path("", frontend_view),
    path("health/", health_check),
    path("api/", include("route.urls")),
]
