"""The scan URL: the pilot names and the window live in the path, not the query string.

Every character an EVE character name can hold is legal unencoded in a path segment
except the space, so the only substitution is space -> underscore. A literal space and
a ``+`` are accepted on the way in too, which makes a hand-typed URL work; the spelling
written back out - and therefore the canonical - is always the underscore one.

The character class is deliberately tight. Names in the path mean *every* path is a
potential scan, so anything that cannot be an EVE name has to 404 rather than render an
empty profile - otherwise every stray request (``/wp-login.php``, ``/favicon.ico``) is a
crawlable 200 that also spends an ESI lookup.
"""

from collections.abc import Sequence

from .windows import DEFAULT_WINDOW

# Letters, digits, space, hyphen and apostrophe are what CCP allows in a name; comma,
# underscore and plus are our separators. The upper bound is the request line gunicorn
# accepts, not the cap on names: an over-long list must still reach the view, which
# reports how many pilots it ignored. See `--limit-request-line` in the entrypoint.
NAMES_RE: str = r"[A-Za-z0-9 '\-_+,]{3,4000}"

_AS_SPACE = str.maketrans({"_": " ", "+": " "})


def parse_names(segment: str) -> list[str]:
    """Path segment -> the pasted names, in order, without duplicates."""
    parts = (p.translate(_AS_SPACE).strip() for line in segment.splitlines() for p in line.split(","))
    return list(dict.fromkeys(p for p in parts if p))


def scan_path(names: Sequence[str], window: str) -> str:
    """The canonical path for a scan. The window is omitted when it is the default, so
    one scan has one shortest spelling for the canonical to point at."""
    if not names:
        return "/"
    path = "/" + ",".join(n.replace(" ", "_") for n in names)
    return path if window == DEFAULT_WINDOW else f"{path}/{window}"
