from django.templatetags.static import static
from django.urls import path, register_converter
from django.views.generic import RedirectView

from . import views
from .scan_url import NAMES_RE
from .windows import WINDOWS


class NamesConverter:
    """The pasted pilot list. See `scan_url` for why the class is this narrow."""

    regex = NAMES_RE

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


class WindowConverter:
    regex = "|".join(WINDOWS)

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(NamesConverter, "names")
register_converter(WindowConverter, "window")

# The fixed paths come first: `healthz` also matches the names converter, and order is
# what keeps the probe from being read as a scan for a pilot called "healthz".
urlpatterns = [
    path("", views.threat_profile, name="intel_threat"),
    # No trailing slash: container probes ask for exactly this path.
    path("healthz", views.healthz, name="healthz"),
    path("robots.txt", views.robots, name="robots"),
    # Browsers and feed readers still ask for this at the root, and the names converter
    # cannot match it (no dot), so it would otherwise 404.
    path(
        "favicon.ico",
        RedirectView.as_view(url=static("intel/img/favicon.ico"), permanent=True),
        name="favicon",
    ),
    path("<names:names>", views.threat_profile, name="intel_scan"),
    path("<names:names>/<window:window>", views.threat_profile, name="intel_scan_window"),
    # A hand-edited URL easily picks up a trailing slash. Send it to the canonical form
    # rather than 404ing; CommonMiddleware's APPEND_SLASH only ever adds one.
    path("<names:names>/", RedirectView.as_view(url="/%(names)s", permanent=True)),
    path("<names:names>/<window:window>/", RedirectView.as_view(url="/%(names)s/%(window)s", permanent=True)),
]
