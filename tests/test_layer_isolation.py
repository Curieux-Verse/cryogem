"""
Guardrail test: the Tier-3 news layer must never reach the screening layer.

Spec acceptance criterion: "Verified by code review: no import path connects
news.py to layer1_kill.py or layer2_score.py."

Code review is the wrong instrument for this. An import is one line, it is easy
to add while chasing something else, and it is invisible in a diff that touches
forty other lines. So the import graph is walked mechanically instead, and CI
runs this file as its own step.

Why the rule exists at all: news is the most tempting input in the system and
the least tradeable. Binance's announcement API has shown 15s+ lag, Telegram
adds 150ms, Twitter is minutes late, and prices move 20-100% within seconds of
a listing notice. Anything scored off news is scored off information the market
already had.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

#: Modules that must never, even transitively, depend on the news layer.
SCREENING_MODULES = (
    "src.screening.layer1_kill",
    "src.screening.layer2_score",
    "src.screening.pipeline",
    # Pulse scores too, so news must never reach it either.
    "src.pulse.features",
    "src.pulse.score",
    "src.pulse.structure",
)

#: The forbidden dependency.
NEWS_MODULE = "src.collectors.news"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def _imports_of(path: Path) -> set[str]:
    """Every `src.*` module a file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("src."):
                found.add(node.module)
                # `from src.collectors import news` imports a MODULE, not a name.
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def build_import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        graph[_module_name(path)] = _imports_of(path)
    return graph


def reachable_from(graph: dict[str, set[str]], start: str) -> set[str]:
    """Every module transitively importable from `start`."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for imported in graph.get(current, set()):
            if imported not in seen:
                seen.add(imported)
                stack.append(imported)
    return seen


@pytest.mark.parametrize("module", SCREENING_MODULES)
def test_screening_never_reaches_the_news_layer(module):
    graph = build_import_graph()
    if module not in graph:
        pytest.skip(f"{module} not implemented yet; nothing can violate the rule")

    reached = reachable_from(graph, module)
    assert NEWS_MODULE not in reached, (
        f"{module} can reach {NEWS_MODULE} through the import graph. "
        "Tier-3 news is for journal labelling only and must never influence a "
        "PASS/FAIL or a score."
    )


def test_news_table_is_not_queried_by_screening():
    """A raw SQL query bypasses the import graph entirely."""
    for module in SCREENING_MODULES:
        path = SRC.parent / (module.replace(".", "/") + ".py")
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "news_item" not in text, (
            f"{module} references the news_item table directly. The isolation "
            "rule is about influence, not just imports."
        )


def test_the_guard_itself_is_wired_correctly():
    """A test that can never fail is not a guard. Prove the detector works."""
    fake = {
        "src.screening.layer1_kill": {"src.collectors.news"},
        "src.collectors.news": set(),
    }
    assert NEWS_MODULE in reachable_from(fake, "src.screening.layer1_kill")

    fake_transitive = {
        "src.screening.layer1_kill": {"src.helper"},
        "src.helper": {"src.collectors.news"},
        "src.collectors.news": set(),
    }
    assert NEWS_MODULE in reachable_from(fake_transitive, "src.screening.layer1_kill"), (
        "the guard must catch INDIRECT paths, which are the realistic way this breaks"
    )
