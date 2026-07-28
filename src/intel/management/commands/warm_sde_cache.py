"""Rebuild the Redis-cached static lookups. Run at application start."""

from typing import Any

from django.core.management.base import BaseCommand

from intel import sde_cache


class Command(BaseCommand):
    help = "Rebuild the cached SDE / bucket lookups used by the killmail queries."

    def handle(self, *args: Any, **options: Any) -> None:
        counts = sde_cache.warm()
        for kind, n in counts.items():
            self.stdout.write(f"  {kind:<9} {n:>6} entries")
        self.stdout.write(self.style.SUCCESS(f"SDE cache warmed: {sum(counts.values())} entries"))
