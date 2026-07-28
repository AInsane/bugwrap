"""End-to-end: a real git repo, a real contract change, no model involved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bugwrap.analysis import build_units
from bugwrap.config import Config
from bugwrap.gitio import diff_working, parse_unified_diff
from bugwrap.index import build_index
from bugwrap.models import ChangeUnit
from bugwrap.review.prompts import render_unit
from bugwrap.review.runner import parse_findings, postprocess

PRICING = '''\
def calculate_discount(price, pct):
    """Apply a percentage discount."""
    return price * (1 - pct)
'''

ORDER = '''\
from shop.pricing import calculate_discount


def total(cart):
    subtotal = sum(i.price for i in cart.items)
    return calculate_discount(subtotal, cart.discount_pct)


def refund(cart):
    return calculate_discount(cart.paid, 1.0)
'''

TEST_FILE = '''\
from shop.pricing import calculate_discount


def test_discount():
    assert calculate_discount(100, 0.1) == 90
'''

UNRELATED = '''\
def helper():
    return 42
'''


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "shop"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "pricing.py").write_text(PRICING)
    (pkg / "order.py").write_text(ORDER)
    (pkg / "util.py").write_text(UNRELATED)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_pricing.py").write_text(TEST_FILE)

    git(["init", "-q", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@example.com"], tmp_path)
    git(["config", "user.name", "test"], tmp_path)
    git(["add", "-A"], tmp_path)
    git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


def make_units(repo: Path, cfg: Config | None = None) -> list[ChangeUnit]:
    cfg = cfg or Config(use_cache=False)
    diff_text, base = diff_working(repo)
    diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
    index = build_index(repo, cfg)
    return build_units(diffs, index, cfg, repo, base)


def test_contract_change_finds_cross_file_call_sites(repo: Path):
    # add a required parameter -> every caller is now broken
    (repo / "shop" / "pricing.py").write_text(
        "def calculate_discount(price, pct, currency):\n"
        '    """Apply a percentage discount."""\n'
        "    return price * (1 - pct)\n"
    )

    units = make_units(repo)
    assert len(units) == 1
    unit = units[0]

    assert unit.symbol.qualname == "calculate_discount"
    assert unit.delta.kind == "signature_changed"
    assert unit.delta.breaking is True
    assert any("REQUIRED parameter added: currency" in d for d in unit.delta.details)

    # the impact snippet must contain the actual call sites, from other files
    call_paths = {i.site.path for i in unit.callers}
    assert "shop/order.py" in call_paths
    assert str(Path("tests/test_pricing.py")) in call_paths
    assert unit.total_callers >= 3

    # ... and it must NOT contain the unrelated file
    assert "shop/util.py" not in call_paths

    prompt = render_unit(unit)
    assert "CONTRACT CHANGE" in prompt
    assert "shop/order.py:6" in prompt
    assert "calculate_discount(subtotal, cart.discount_pct)" in prompt
    assert "def calculate_discount(price, pct)" in prompt  # the before signature


def test_body_only_change_does_not_scream_contract(repo: Path):
    (repo / "shop" / "pricing.py").write_text(
        "def calculate_discount(price, pct):\n"
        '    """Apply a percentage discount."""\n'
        "    return round(price * (1 - pct), 2)\n"
    )
    (unit,) = make_units(repo)
    assert unit.delta.kind == "body_only"
    assert unit.delta.is_contract_change is False
    assert "BODY-ONLY CHANGE" in render_unit(unit)


def test_only_the_changed_symbol_becomes_a_unit(repo: Path):
    source = (repo / "shop" / "order.py").read_text()
    (repo / "shop" / "order.py").write_text(
        source.replace("return calculate_discount(cart.paid, 1.0)", "return 0")
    )
    (unit,) = make_units(repo)
    assert unit.symbol.qualname == "refund"  # not `total`, which is untouched


def test_deleted_symbol_still_finds_its_callers(repo: Path):
    # calculate_discount is gone; nothing is left for the resolver to bind to,
    # so this only works via the name index.
    (repo / "shop" / "pricing.py").write_text("def other():\n    return 1\n")
    units = make_units(repo)
    removed = [u for u in units if u.delta.kind == "removed"]
    assert removed, [u.delta.kind for u in units]
    unit = removed[0]
    assert unit.symbol.qualname == "calculate_discount"
    assert unit.delta.breaking is True
    assert "shop/order.py" in {i.site.path for i in unit.callers}
    assert "REMOVED" in render_unit(unit)


def test_deleted_file_reports_its_lost_symbols(repo: Path):
    (repo / "shop" / "pricing.py").unlink()
    git(["add", "-A"], repo)
    units = make_units(repo)
    deleted = [u for u in units if u.path == "shop/pricing.py"]
    assert deleted
    assert deleted[0].delta.breaking is True
    assert any("calculate_discount" in d for d in deleted[0].delta.details)


def test_new_file_is_reviewed_as_added(repo: Path):
    (repo / "shop" / "shipping.py").write_text("def cost(weight):\n    return weight * 2\n")
    git(["add", "-A"], repo)
    units = make_units(repo)
    kinds = {u.delta.kind for u in units}
    assert "added" in kinds


def test_packets_respect_the_token_budget(repo: Path):
    (repo / "shop" / "pricing.py").write_text(
        "def calculate_discount(price, pct, currency):\n    return price\n"
    )
    cfg = Config(use_cache=False, num_ctx=2048, max_callers=8)
    units = make_units(repo, cfg)
    for unit in units:
        assert unit.tokens <= cfg.unit_budget


def test_two_hop_reaches_through_kwargs_wrapper(repo: Path):
    (repo / "shop" / "wrap.py").write_text(
        "from shop.pricing import calculate_discount\n\n\n"
        "def discounted(*args, **kwargs):\n"
        "    return calculate_discount(*args, **kwargs)\n"
    )
    (repo / "shop" / "front.py").write_text(
        "from shop.wrap import discounted\n\n\n"
        "def price_of(item):\n"
        "    return discounted(item.price, 0.1)\n"
    )
    git(["add", "-A"], repo)
    git(["commit", "-qm", "wrapper"], repo)
    # breaking change: required param added
    (repo / "shop" / "pricing.py").write_text(
        "def calculate_discount(price, pct, currency):\n    return price\n"
    )
    units = make_units(repo)
    unit = next(u for u in units if u.symbol and u.symbol.qualname == "calculate_discount")
    two_hop = [i for i in unit.callers if i.hop == 2]
    assert two_hop, [(i.site.path, i.hop) for i in unit.callers]
    assert two_hop[0].site.path == "shop/front.py"
    assert two_hop[0].via == "shop.wrap.discounted"
    # and the static checker must NOT bind-check the 2-hop site
    from bugwrap.api import static_check
    from bugwrap.index import build_index as bi

    index = bi(repo, Config(use_cache=False))
    findings = static_check(units, index)
    assert not any(f.file == "shop/front.py" for f in findings)


def test_module_level_change_produces_a_module_unit(repo: Path):
    source = (repo / "shop" / "order.py").read_text()
    (repo / "shop" / "order.py").write_text("import os\n" + source)
    units = make_units(repo)
    assert any(u.symbol is None for u in units)


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------


class _Unit:
    path = "a.py"
    symbol = None


def test_parse_findings_accepts_clean_json():
    payload = (
        '{"findings":[{"file":"a.py","line":3,"severity":"high","category":"contract",'
        '"title":"Missing arg","detail":"caller breaks","confidence":0.9}]}'
    )
    (finding,) = parse_findings(payload, _Unit())
    assert finding.severity == "high"
    assert finding.line == 3
    assert finding.confidence == 0.9


def test_parse_findings_recovers_from_prose_wrapping():
    payload = 'Sure! Here is the result:\n```json\n{"findings": []}\n```'
    assert parse_findings(payload, _Unit()) == []


def test_parse_findings_survives_garbage():
    assert parse_findings("I could not analyze this.", _Unit()) == []
    assert parse_findings("", _Unit()) == []


def test_merge_findings_static_owns_its_lines():
    from bugwrap.models import Finding
    from bugwrap.review.runner import merge_findings

    static = [Finding("a.py", 10, None, "high", "contract", "missing arg", "x", 0.95)]
    llm = [
        Finding("a.py", 10, None, "high", "contract", "call broken differently worded", "x", 0.8),
        Finding("a.py", 11, None, "medium", "logic", "adjacent duplicate", "x", 0.7),
        Finding("a.py", 40, None, "medium", "logic", "genuinely new", "x", 0.7),
        Finding("b.py", 10, None, "low", "logic", "other file, same line no.", "x", 0.6),
    ]
    merged = merge_findings(static, llm)
    titles = [f.title for f in merged]
    assert "missing arg" in titles
    assert "genuinely new" in titles
    assert "other file, same line no." in titles
    assert "call broken differently worded" not in titles
    assert "adjacent duplicate" not in titles


def test_postprocess_filters_and_dedupes():
    from bugwrap.models import Finding

    findings = [
        Finding("a.py", 1, None, "high", "logic", "Dup", "x", 0.9),
        Finding("a.py", 1, None, "high", "logic", "dup", "x", 0.9),
        Finding("a.py", 2, None, "low", "logic", "Unsure", "x", 0.1),
        Finding("a.py", 3, None, "critical", "security", "Injection", "x", 0.8),
    ]
    kept = postprocess(findings, Config(min_confidence=0.45))
    assert [f.title for f in kept] == ["Injection", "Dup"]  # sorted by severity
