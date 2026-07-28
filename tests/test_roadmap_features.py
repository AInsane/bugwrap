"""Behaviour deltas, class hierarchy, two-hop impact, PageRank."""

from __future__ import annotations

from bugwrap.analysis.signature import compare, find_node
from test_analysis import build_graph

# --------------------------------------------------------------------------
# behaviour contract deltas
# --------------------------------------------------------------------------


def delta(old: str, new: str):
    return compare(find_node(old, "f"), find_node(new, "f"))


def test_new_raise_is_reported():
    d = delta(
        "def f(a):\n    return a\n",
        "def f(a):\n    if a < 0:\n        raise ValueError('neg')\n    return a\n",
    )
    assert d.kind == "body_only"
    assert any("now raises: ValueError" in b for b in d.behavior)
    assert d.is_contract_change  # behaviour changes pull in callers
    assert not d.breaking


def test_generator_toggle_is_breaking():
    d = delta(
        "def f(items):\n    return [i for i in items]\n",
        "def f(items):\n    for i in items:\n        yield i\n",
    )
    assert d.breaking is True
    assert any("generator" in b for b in d.behavior)


def test_return_shape_change_reported():
    d = delta(
        "def f(a):\n    return a\n",
        "def f(a):\n    return a, None\n",
    )
    assert any("return shape changed" in b for b in d.behavior)


def test_nested_function_raises_are_not_attributed():
    d = delta(
        "def f(a):\n    return a\n",
        "def f(a):\n    def g():\n        raise KeyError\n    return g\n",
    )
    assert not any("KeyError" in b for b in d.behavior)


def test_identical_behavior_is_quiet():
    d = delta(
        "def f(a):\n    raise ValueError\n",
        "def f(a):\n    raise ValueError('better message')\n",
    )
    assert d.behavior == []


# --------------------------------------------------------------------------
# class hierarchy
# --------------------------------------------------------------------------

BASE = "class Exporter:\n    def export(self, data):\n        return data\n"


def test_inherited_method_call_binds_to_base_definition():
    table, graph = build_graph({
        "pkg/base.py": BASE,
        "pkg/csv_out.py": "from pkg.base import Exporter\n\nclass CsvExporter(Exporter):\n    pass\n",
        "pkg/job.py": (
            "from pkg.csv_out import CsvExporter\n\n"
            "def run(rows):\n"
            "    exporter = CsvExporter()\n"
            "    return exporter.export(rows)\n"
        ),
    })
    (caller,) = graph.callers_of("pkg.base.Exporter.export")
    assert caller.path == "pkg/job.py"


def test_hierarchy_needs_import_evidence():
    # Same base name, but no import connects the files — no edge.
    table, _ = build_graph({
        "a/base.py": "class Base:\n    pass\n",
        "b/impl.py": "class Base:\n    pass\n\nclass Impl(Base):\n    pass\n",
    })
    assert table.bases_of.get("b.impl.Impl") == ["b.impl.Base"]  # local one wins


def test_overrides_found_transitively():
    table, _ = build_graph({
        "pkg/base.py": BASE,
        "pkg/mid.py": "from pkg.base import Exporter\n\nclass Mid(Exporter):\n    pass\n",
        "pkg/leaf.py": (
            "from pkg.mid import Mid\n\n"
            "class Leaf(Mid):\n"
            "    def export(self, data):\n"
            "        return list(data)\n"
        ),
    })
    method = table.by_fq["pkg.base.Exporter.export"]
    overrides = table.overrides_of(method)
    assert [o.fqname for o in overrides] == ["pkg.leaf.Leaf.export"]


# --------------------------------------------------------------------------
# field contracts + attribute reads
# --------------------------------------------------------------------------


def test_class_fields_covers_dataclass_and_init():
    from bugwrap.analysis.fields import class_fields

    src = (
        "class Order:\n"
        "    status: str = 'new'\n"
        "    LIMIT = 10\n"
        "    def __init__(self, total):\n"
        "        self.total = total\n"
        "        self.items = []\n"
        "    def close(self):\n"
        "        self.closed_at = 'now'\n"
    )
    fields = class_fields(src, "Order")
    assert set(fields) == {"status", "LIMIT", "total", "items", "closed_at"}


def test_removed_fields_detects_the_loss():
    from bugwrap.analysis.fields import removed_fields

    old = "class O:\n    def __init__(self):\n        self.a = 1\n        self.b = 2\n"
    new = "class O:\n    def __init__(self):\n        self.a = 1\n"
    assert [name for name, _ in removed_fields(old, new, "O")] == ["b"]


def test_attribute_reads_indexed_for_proven_receivers():
    _, graph = build_graph({
        "pkg/order.py": "class Order:\n    def __init__(self):\n        self.total = 0\n",
        "pkg/view.py": (
            "from pkg.order import Order\n\n"
            "def show(order: Order):\n"
            "    return order.total\n"
        ),
    })
    readers = graph.readers_of("pkg.order.Order", "total")
    paths = {r.path for r in readers}
    assert paths == {"pkg/view.py"}  # typed via annotation; the self-WRITE is not a read


def test_readers_of_walks_subclasses():
    _, graph = build_graph({
        "pkg/base.py": "class Base:\n    def __init__(self):\n        self.total = 0\n",
        "pkg/sub.py": "from pkg.base import Base\n\nclass Sub(Base):\n    pass\n",
        "pkg/use.py": (
            "from pkg.sub import Sub\n\n"
            "def show():\n"
            "    s = Sub()\n"
            "    return s.total\n"
        ),
    })
    assert any(r.path == "pkg/use.py" for r in graph.readers_of("pkg.base.Base", "total"))


# --------------------------------------------------------------------------
# pagerank
# --------------------------------------------------------------------------


def test_pagerank_ranks_the_hub_highest():
    from bugwrap.index.rank import pagerank

    _, graph = build_graph({
        "pkg/core.py": "def hub():\n    return 1\n",
        "pkg/a.py": "from pkg.core import hub\n\ndef a():\n    return hub()\n",
        "pkg/b.py": "from pkg.core import hub\n\ndef b():\n    return hub()\n",
        "pkg/c.py": "from pkg.a import a\n\ndef c():\n    return a()\n",
    })
    rank = pagerank(graph)
    assert rank["pkg.core.hub"] == max(rank.values())
