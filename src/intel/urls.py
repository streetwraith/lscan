from django.urls import path

from . import views

urlpatterns = [
    path("", views.threat_profile, name="intel_threat"),
]
