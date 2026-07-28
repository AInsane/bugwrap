"""The static binding checker must mirror CPython's rules — and stay silent
whenever the call site uses unpacking it cannot see through."""

from __future__ import annotations

import ast

from bugwrap.analysis.callcheck import bind_call
from bugwrap.analysis.signature import find_node, params_of
from bugwrap.models import CallSite, ImpactSite, Symbol


def make_symbol(kind: str = "function", decorators: tuple = ()) -> Symbol:
    return Symbol(
        module="m",
        qualname="f",
        kind=kind,
        path="m.py",
        lineno=1,
        end_lineno=2,
        start_lineno=1,
        signature="",
        decorators=decorators,
    )


def impact(resolution: str = "import") -> ImpactSite:
    return ImpactSite(
        site=CallSite(
            path="x.py", line=1, caller="x.g", target="m.f", raw="f", resolution=resolution
        ),
        snippet="",
    )


def problems(defn: str, call: str, kind: str = "function", resolution: str = "import",
             decorators: tuple = ()) -> list[str]:
    node = find_node(defn, "f")
    params = params_of(node)
    call_node = ast.parse(call).body[0].value
    return bind_call(call_node, params, make_symbol(kind, decorators), impact(resolution))


def test_ok_call_binds():
    assert problems("def f(a, b=1):\n    pass\n", "f(1)") == []
    assert problems("def f(a, b=1):\n    pass\n", "f(1, b=2)") == []


def test_missing_required():
    out = problems("def f(a, b):\n    pass\n", "f(1)")
    assert out == ["is missing required argument `b`"]


def test_missing_required_kwonly():
    out = problems("def f(a, *, currency):\n    pass\n", "f(1)")
    assert out == ["is missing required argument `currency`"]


def test_too_many_positional():
    out = problems("def f(a):\n    pass\n", "f(1, 2)")
    assert any("positional" in p for p in out)


def test_unexpected_keyword():
    out = problems("def f(a):\n    pass\n", "f(1, mode='x')")
    assert out == ["passes unexpected keyword argument `mode`"]


def test_star_args_silences_positional_checks():
    assert problems("def f(a, b, c):\n    pass\n", "f(*items)") == []


def test_double_star_silences_missing_checks():
    assert problems("def f(a, b):\n    pass\n", "f(1, **kw)") == []


def test_vararg_accepts_extra_positional():
    assert problems("def f(a, *rest):\n    pass\n", "f(1, 2, 3)") == []


def test_kwarg_accepts_any_keyword():
    assert problems("def f(a, **kw):\n    pass\n", "f(1, whatever=2)") == []


def test_posonly_by_keyword():
    out = problems("def f(a, /):\n    pass\n", "f(a=1)")
    assert any("positional-only" in p for p in out)
    # ... and it is also reported missing, matching CPython's actual TypeError
    assert any("missing required" in p for p in out)


def test_duplicate_positional_and_keyword():
    out = problems("def f(a):\n    pass\n", "f(1, a=2)")
    assert any("both positionally and as a keyword" in p for p in out)


def test_bound_method_skips_self():
    src = "class C:\n    def f(self, a):\n        pass\n"
    node = find_node(src, "C.f")
    params = params_of(node)
    call_node = ast.parse("obj.f(1)").body[0].value
    sym = make_symbol(kind="method")
    assert bind_call(call_node, params, sym, impact()) == []
    # missing the real arg is still caught through the bound form
    call_node = ast.parse("obj.f()").body[0].value
    out = bind_call(call_node, params, sym, impact())
    assert out == ["is missing required argument `a`"]


def test_staticmethod_keeps_all_params():
    src = "class C:\n    @staticmethod\n    def f(a):\n        pass\n"
    node = find_node(src, "C.f")
    params = params_of(node)
    call_node = ast.parse("C.f(1)").body[0].value
    sym = make_symbol(kind="method", decorators=("staticmethod",))
    assert bind_call(call_node, params, sym, impact()) == []


def test_unbound_direct_method_call_stays_silent():
    src = "class C:\n    def f(self, a):\n        pass\n"
    node = find_node(src, "C.f")
    params = params_of(node)
    call_node = ast.parse("f(1)").body[0].value  # plain name, unbound — ambiguous
    assert bind_call(call_node, params, make_symbol(kind="method"), impact("name")) == []
