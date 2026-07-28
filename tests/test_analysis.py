import ast

import pytest

from bugwrap.analysis.signature import compare, find_node, params_of
from bugwrap.gitio import parse_unified_diff
from bugwrap.index.callgraph import collect_calls
from bugwrap.index.symbols import SymbolTable, extract_symbols

# --------------------------------------------------------------------------
# diff parsing
# --------------------------------------------------------------------------

DIFF = """\
diff --git a/pkg/pricing.py b/pkg/pricing.py
index 1111111..2222222 100644
--- a/pkg/pricing.py
+++ b/pkg/pricing.py
@@ -3,7 +3,7 @@ import math

-def calculate_discount(price, pct):
-    return price * (1 - pct)
+def calculate_discount(price, pct, *, floor=0.0):
+    return max(floor, price * (1 - pct))

 def unrelated():
     pass
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+def fresh():
+    return 1
"""


def test_parse_unified_diff_files_and_lines():
    diffs = parse_unified_diff(DIFF)
    assert [d.path for d in diffs] == ["pkg/pricing.py", "pkg/new.py"]
    assert diffs[0].status == "M"
    assert diffs[1].status == "A"
    # hunk starts at line 3 with one blank context line, so the edits land on 4 and 5
    assert diffs[0].new_lines == {4, 5}
    assert diffs[0].old_lines == {4, 5}
    assert diffs[1].new_lines == {1, 2}


def test_parse_handles_deletion_and_rename():
    text = """\
diff --git a/a.py b/b.py
similarity index 90%
rename from a.py
rename to b.py
--- a/a.py
+++ b/b.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
"""
    (fd,) = parse_unified_diff(text)
    assert fd.status == "R"
    assert fd.old_path == "a.py"
    assert fd.path == "b.py"


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------

SOURCE = '''\
import math

CONST = 1


@decorated
def top(a, b=2) -> int:
    """doc"""
    return a + b


class Cart:
    def total(self, items):
        return sum(self.price(i) for i in items)

    @staticmethod
    async def fetch(url):
        return await get(url)
'''


def test_extract_symbols_kinds_and_spans():
    symbols = extract_symbols(SOURCE, "pkg.cart", "pkg/cart.py")
    by_name = {s.qualname: s for s in symbols}

    assert set(by_name) == {"top", "Cart", "Cart.total", "Cart.fetch"}
    assert by_name["top"].kind == "function"
    assert by_name["Cart.total"].kind == "method"
    assert by_name["Cart.fetch"].is_async is True
    assert by_name["top"].signature == "def top(a, b=2) -> int"
    assert by_name["Cart.fetch"].decorators == ("staticmethod",)
    # start_lineno includes the decorator, lineno points at `def`
    assert by_name["top"].start_lineno == 6
    assert by_name["top"].lineno == 7


def test_symbol_table_line_ownership():
    table = SymbolTable()
    table.add(extract_symbols(SOURCE, "pkg.cart", "pkg/cart.py"))

    assert table.owning_function("pkg/cart.py", 14).qualname == "Cart.total"
    assert table.enclosing("pkg/cart.py", 12).qualname == "Cart"
    assert table.owning_function("pkg/cart.py", 3) is None  # module level
    assert table.by_fq["pkg.cart.Cart.total"].path == "pkg/cart.py"


# --------------------------------------------------------------------------
# call graph
# --------------------------------------------------------------------------


def test_call_resolution_across_import_styles():
    src = '''\
from pkg.pricing import calculate_discount
from . import helpers
import pkg.tax as tax

def checkout(cart):
    net = calculate_discount(cart.price, 0.2)
    fee = tax.compute(net)
    return helpers.round_cents(net + fee)

class Order:
    def apply(self):
        return self.recalc()
    def recalc(self):
        return 1
'''
    calls = {c.raw: c for c in collect_calls(src, "shop.checkout", "shop/checkout.py")}

    assert calls["calculate_discount"].target == "pkg.pricing.calculate_discount"
    assert calls["calculate_discount"].resolution == "import"
    assert calls["tax.compute"].target == "pkg.tax.compute"
    assert calls["helpers.round_cents"].target == "shop.helpers.round_cents"
    assert calls["self.recalc"].target == "shop.checkout.Order.recalc"
    assert calls["self.recalc"].resolution == "self"
    assert calls["calculate_discount"].caller == "shop.checkout.checkout"


def test_relative_import_levels():
    src = "from ..core import engine\n\ndef go():\n    return engine.start()\n"
    calls = {c.raw: c for c in collect_calls(src, "a.b.c.mod", "a/b/c/mod.py")}
    assert calls["engine.start"].target == "a.b.core.engine.start"


# --------------------------------------------------------------------------
# resolver precision — the difference between useful context and noise
# --------------------------------------------------------------------------


def build_graph(files: dict[str, str]):
    import ast

    from bugwrap.index.callgraph import CallGraph, collect_calls_from_tree
    from bugwrap.index.symbols import extract_symbols_from_tree

    table = SymbolTable()
    graph = CallGraph(table)
    parsed = []
    imports_by_path: dict[str, set[str]] = {}
    for path, source in files.items():
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        tree = ast.parse(source)
        table.add(extract_symbols_from_tree(tree, module, path))
        parsed.append((path, collect_calls_from_tree(tree, module, path, source.splitlines())))
    for path, collected in parsed:
        graph.add(collected.calls)
        graph.add_imports(path, collected.imports)
        graph.add_reads(collected.reads)
        imports_by_path[path] = collected.imports
    table.link_hierarchy(imports_by_path)
    graph.link()
    return table, graph


CART = "class Cart:\n    def recalculate(self, items):\n        return 1\n"


def test_untyped_receiver_resolves_when_the_file_imports_the_module():
    _, graph = build_graph({
        "shop/cart.py": CART,
        "shop/view.py": "from shop.cart import Cart\n\ndef render(cart):\n    return cart.recalculate([])\n",
    })
    callers = graph.callers_of("shop.cart.Cart.recalculate")
    assert [c.path for c in callers] == ["shop/view.py"]


def test_untyped_receiver_is_dropped_without_import_evidence():
    # `thing.recalculate()` here is some unrelated object — no import ties this
    # file to shop.cart, so calling it a caller would be a fabrication.
    _, graph = build_graph({
        "shop/cart.py": CART,
        "other/widget.py": "def draw(thing):\n    return thing.recalculate([])\n",
    })
    assert graph.callers_of("shop.cart.Cart.recalculate") == []


def test_protocol_method_names_never_resolve_by_duck_typing():
    _, graph = build_graph({
        "shop/bag.py": "class Bag:\n    def append(self, x):\n        return x\n",
        "shop/use.py": "from shop.bag import Bag\n\ndef go(items):\n    items.append(1)\n",
    })
    # `items` is a list, not a Bag — a name match on `append` proves nothing
    assert graph.callers_of("shop.bag.Bag.append") == []


def test_builtin_shadowing_does_not_create_edges():
    _, graph = build_graph({
        "shop/helpers.py": "def str(x):\n    return x\n",
        "shop/use.py": "def go(x):\n    return str(x)\n",
    })
    assert graph.callers_of("shop.helpers.str") == []


def test_local_constructor_binds_receiver_type():
    _, graph = build_graph({
        "shop/cart.py": CART,
        "shop/flow.py": (
            "from shop.cart import Cart\n\n"
            "def run(items):\n"
            "    cart = Cart()\n"
            "    return cart.recalculate(items)\n"
        ),
    })
    (caller,) = graph.callers_of("shop.cart.Cart.recalculate")
    assert caller.resolution == "typed"
    assert caller.line == 5


def test_annotated_param_binds_receiver_type():
    _, graph = build_graph({
        "shop/cart.py": CART,
        "shop/flow.py": (
            "from shop.cart import Cart\n\n"
            "def run(cart: Cart, items):\n"
            "    return cart.recalculate(items)\n"
        ),
    })
    (caller,) = graph.callers_of("shop.cart.Cart.recalculate")
    assert caller.resolution == "typed"


def test_typed_binding_beats_protocol_method_guard():
    # `add` is a protocol method, but a proven receiver type overrides the guard
    _, graph = build_graph({
        "shop/bag.py": "class Bag:\n    def add(self, x):\n        return x\n",
        "shop/use.py": (
            "from shop.bag import Bag\n\n"
            "def go():\n"
            "    bag = Bag()\n"
            "    bag.add(1)\n"
        ),
    })
    assert len(graph.callers_of("shop.bag.Bag.add")) == 1


def test_typed_binding_ignores_lowercase_factories():
    # `conn = connect()` — lowercase, could return anything; must not bind
    _, graph = build_graph({
        "db/conn.py": "class Conn:\n    def execute(self, q):\n        return q\n\ndef connect():\n    return Conn()\n",
        "db/use.py": (
            "from db.conn import connect\n\n"
            "def go():\n"
            "    conn = connect()\n"
            "    conn.execute('x')\n"
        ),
    })
    # execute isn't a protocol method and db/use.py imports db.conn, so the
    # evidence-rule fallback may still find it — but never as a `typed` edge.
    for site in graph.callers_of("db.conn.Conn.execute"):
        assert site.resolution != "typed"


def test_deleted_symbol_lookup_ignores_resolution():
    _, graph = build_graph({
        "shop/pricing.py": "def calc(a):\n    return a\n",
        "shop/order.py": "from shop.pricing import calc\n\ndef go():\n    return calc(1)\n",
    })
    sites = graph.by_name("calc", exclude_path="shop/pricing.py")
    assert [s.path for s in sites] == ["shop/order.py"]


# --------------------------------------------------------------------------
# signature deltas
# --------------------------------------------------------------------------


def sig_delta(old: str, new: str, name: str = "f"):
    return compare(find_node(old, name), find_node(new, name))


def test_body_only_change_is_not_a_contract_change():
    delta = sig_delta("def f(a, b):\n    return a+b\n", "def f(a, b):\n    return a*b\n")
    assert delta.kind == "body_only"
    assert delta.is_contract_change is False


def test_removed_parameter_is_breaking():
    delta = sig_delta("def f(a, b):\n    pass\n", "def f(a):\n    pass\n")
    assert delta.kind == "signature_changed"
    assert delta.breaking is True
    assert "parameter removed: b" in delta.details


def test_optional_parameter_added_is_not_breaking():
    delta = sig_delta("def f(a):\n    pass\n", "def f(a, b=1):\n    pass\n")
    assert delta.kind == "signature_changed"
    assert delta.breaking is False
    assert any("optional parameter added" in d for d in delta.details)


def test_required_parameter_added_is_breaking():
    delta = sig_delta("def f(a):\n    pass\n", "def f(a, b):\n    pass\n")
    assert delta.breaking is True
    assert any("REQUIRED parameter added" in d for d in delta.details)


def test_sync_to_async_is_breaking():
    delta = sig_delta("def f(a):\n    pass\n", "async def f(a):\n    pass\n")
    assert delta.breaking is True
    assert any("sync/async" in d for d in delta.details)


def test_default_removed_is_breaking():
    delta = sig_delta("def f(a, b=1):\n    pass\n", "def f(a, b):\n    pass\n")
    assert delta.breaking is True
    assert any("default removed" in d for d in delta.details)


def test_annotation_change_is_reported_but_not_breaking():
    delta = sig_delta("def f(a: int) -> str:\n    pass\n", "def f(a: float) -> str:\n    pass\n")
    assert delta.kind == "signature_changed"
    assert delta.breaking is False
    assert any("type int -> float" in d for d in delta.details)


def test_positional_reorder_is_breaking():
    delta = sig_delta("def f(a, b):\n    pass\n", "def f(b, a):\n    pass\n")
    assert delta.breaking is True


def test_added_and_removed_definitions():
    assert compare(None, find_node("def f():\n    pass\n", "f")).kind == "added"
    removed = compare(find_node("def f():\n    pass\n", "f"), None)
    assert removed.kind == "removed" and removed.breaking


def test_nested_qualname_lookup():
    src = "class A:\n    def m(self, x):\n        pass\n"
    node = find_node(src, "A.m")
    assert isinstance(node, ast.FunctionDef)
    assert [p.name for p in params_of(node)] == ["self", "x"]


def test_params_of_covers_every_kind():
    node = find_node("def f(a, /, b, *args, c=1, **kw):\n    pass\n", "f")
    kinds = {p.name: p.kind for p in params_of(node)}
    assert kinds == {
        "a": "posonly",
        "b": "arg",
        "args": "vararg",
        "c": "kwonly",
        "kw": "kwarg",
    }
