from django.urls import path

from . import views

urlpatterns = [
    path("", views.threat_profile, name="intel_threat"),
    # No trailing slash: container probes ask for exactly this path.
    path("healthz", views.healthz, name="healthz"),
]
