"""Lesson 10 - the AI worked; the operational envelope didn't.

A simulation of a knowledge-base service running under an orchestrator that
expects short, stateless requests. Two settings decide whether it survives:

  1. The health probe: how long the service may stay busy before it is
     declared dead and restarted.
  2. Where the list of loaded collections lives: in memory (lost on restart)
     or saved to disk (survives a restart).

Time is simulated, so it runs instantly. Standard library only.
Run:  python envelope.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


class KnowledgeService:
    def __init__(self, registry_file: Path | None) -> None:
        self.registry_file = registry_file  # None = registry held in memory only
        self.collections: dict[str, int] = {}
        if registry_file and registry_file.exists():
            self.collections = json.loads(registry_file.read_text())

    def register(self, name: str, passages: int) -> None:
        self.collections[name] = passages
        if self.registry_file:
            self.registry_file.write_text(json.dumps(self.collections))

    def search(self, name: str) -> str:
        if name not in self.collections:
            return f"404 collection '{name}' not found"
        return f"200 searched {self.collections[name]:,} passages in '{name}'"


def run(title: str, probe_timeout_s: int, persist: bool) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)
    folder = Path(tempfile.mkdtemp())
    registry = folder / "registry.json" if persist else None
    service = KnowledgeService(registry)

    # A small collection loads quickly and is registered.
    service.register("billing-policies", 1_200)
    print("Loaded 'billing-policies' (1,200 passages) in 20 s")

    # A big collection takes a long time to embed, and the service is busy throughout.
    embed_time_s = 900
    print(f"Embedding 'regulations' (40,000 passages) needs {embed_time_s} s of busy time."
          f" Probe gives up after {probe_timeout_s} s.")
    if embed_time_s > probe_timeout_s:
        print(f"  t={probe_timeout_s}s  probe: no answer -> container killed and restarted mid-embed")
        service = KnowledgeService(registry)  # the restart
        print("  after restart:", service.search("billing-policies"))
        print("  'regulations' was never finished; the job has to start again, and the probe will kill it again")
    else:
        service.register("regulations", 40_000)
        print("  embedding finished, collection registered")
        service = KnowledgeService(registry)  # a routine restart later (deploy, node move)
        print("  after a routine restart:", service.search("billing-policies"))
        print("  after a routine restart:", service.search("regulations"))


def main() -> None:
    run("1. Defaults: short probe, registry in memory", probe_timeout_s=30, persist=False)
    run("2. Probe relaxed only: embedding survives, but the next restart forgets everything",
        probe_timeout_s=1_200, persist=False)
    run("3. Probe relaxed and registry saved: survives the embed and the restart",
        probe_timeout_s=1_200, persist=True)
    print("\nNothing here is about the model. Every failure is in the envelope around it.")


if __name__ == "__main__":
    main()
