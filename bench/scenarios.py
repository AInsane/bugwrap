"""Seeded-bug scenarios for contract-bench.

Each scenario is a small realistic package, a mutation (the "PR"), and golden
findings — the file/line pairs a competent reviewer must flag. Controls have an
empty golden list: flagging anything on them is a false positive, which is what
keeps the precision number honest (the CodeRabbit comparison lives or dies here).

Format:
    files:  path -> source at base revision
    change: path -> source after the PR
    golden: list of (path, line_in_new_file, must_contain_keyword_or_empty)
"""

SCENARIOS: dict[str, dict] = {}


def scenario(name):
    def wrap(fn):
        SCENARIOS[name] = fn()
        return fn

    return wrap


_PRICING = """\
def calculate_discount(price, pct):
    return price * (1 - pct)
"""

_ORDER = """\
from shop.pricing import calculate_discount


def total(items, pct):
    subtotal = sum(i.price for i in items)
    return calculate_discount(subtotal, pct)


def refund(order):
    return calculate_discount(order.paid, 1.0)
"""

_INIT = ""


@scenario("required_param_added")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/pricing.py": _PRICING,
            "shop/order.py": _ORDER,
        },
        "change": {
            "shop/pricing.py": (
                "def calculate_discount(price, pct, currency):\n"
                "    return price * (1 - pct)\n"
            )
        },
        "golden": [
            ("shop/order.py", 6, "currency"),
            ("shop/order.py", 10, "currency"),
        ],
    }


@scenario("param_removed")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/fees.py": "def fee(amount, rate, cap):\n    return min(amount * rate, cap)\n",
            "shop/checkout.py": (
                "from shop.fees import fee\n\n\n"
                "def charge(amount):\n"
                "    return amount + fee(amount, 0.03, 10.0)\n"
            ),
        },
        "change": {
            "shop/fees.py": "def fee(amount, rate):\n    return amount * rate\n"
        },
        "golden": [("shop/checkout.py", 5, "positional")],
    }


@scenario("kwarg_renamed")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/mail.py": (
                "def send(to, subject, body, retries=3):\n"
                "    return (to, subject, body, retries)\n"
            ),
            "shop/notify.py": (
                "from shop.mail import send\n\n\n"
                "def alert(user):\n"
                "    return send(user.email, 'alert', 'x', retries=5)\n"
            ),
        },
        "change": {
            "shop/mail.py": (
                "def send(to, subject, body, attempts=3):\n"
                "    return (to, subject, body, attempts)\n"
            )
        },
        "golden": [("shop/notify.py", 5, "retries")],
    }


@scenario("function_deleted")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/legacy.py": (
                "def old_tax(amount):\n    return amount * 0.2\n\n\n"
                "def keep_me(x):\n    return x\n"
            ),
            "shop/invoice.py": (
                "from shop.legacy import old_tax\n\n\n"
                "def build(amount):\n"
                "    return amount + old_tax(amount)\n"
            ),
        },
        "change": {
            "shop/legacy.py": "def keep_me(x):\n    return x\n"
        },
        "golden": [("shop/invoice.py", 5, "removed")],
    }


@scenario("sync_to_async")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/stock.py": "def reserve(sku, qty):\n    return {'sku': sku, 'qty': qty}\n",
            "shop/cart.py": (
                "from shop.stock import reserve\n\n\n"
                "def add(sku):\n"
                "    result = reserve(sku, 1)\n"
                "    return result['qty']\n"
            ),
        },
        "change": {
            "shop/stock.py": (
                "async def reserve(sku, qty):\n"
                "    return {'sku': sku, 'qty': qty}\n"
            )
        },
        "golden": [("shop/cart.py", 5, "await")],
    }


@scenario("default_removed")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/ship.py": "def quote(weight, express=False):\n    return weight * (2 if express else 1)\n",
            "shop/basket.py": (
                "from shop.ship import quote\n\n\n"
                "def shipping(weight):\n"
                "    return quote(weight)\n"
            ),
        },
        "change": {
            "shop/ship.py": "def quote(weight, express):\n    return weight * (2 if express else 1)\n"
        },
        "golden": [("shop/basket.py", 5, "express")],
    }


@scenario("posonly_migration")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/rates.py": "def convert(amount, currency):\n    return amount\n",
            "shop/display.py": (
                "from shop.rates import convert\n\n\n"
                "def show(x):\n"
                "    return convert(amount=x, currency='EUR')\n"
            ),
        },
        "change": {
            "shop/rates.py": "def convert(amount, currency, /):\n    return amount\n"
        },
        "golden": [("shop/display.py", 5, "")],
    }


@scenario("method_signature_changed")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/cart.py": (
                "class Cart:\n"
                "    def __init__(self):\n"
                "        self.items = []\n\n"
                "    def add(self, sku):\n"
                "        self.items.append(sku)\n\n"
                "    def submit(self):\n"
                "        self.add('bonus')\n"
                "        return len(self.items)\n"
            ),
            "shop/flow.py": (
                "from shop.cart import Cart\n\n\n"
                "def run(skus):\n"
                "    cart = Cart()\n"
                "    for sku in skus:\n"
                "        cart.add(sku)\n"
                "    return cart.submit()\n"
            ),
        },
        "change": {
            "shop/cart.py": (
                "class Cart:\n"
                "    def __init__(self):\n"
                "        self.items = []\n\n"
                "    def add(self, sku, qty):\n"
                "        self.items.extend([sku] * qty)\n\n"
                "    def submit(self):\n"
                "        self.add('bonus')\n"
                "        return len(self.items)\n"
            )
        },
        "golden": [
            ("shop/cart.py", 9, "qty"),
            ("shop/flow.py", 7, "qty"),
        ],
    }


@scenario("inherited_method_call")
def _():
    # The change is in Base; the caller holds a Derived that doesn't override.
    # Only hierarchy-aware resolution connects the two.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/base.py": (
                "class Exporter:\n"
                "    def export(self, data):\n"
                "        return list(data)\n"
            ),
            "shop/csv_out.py": (
                "from shop.base import Exporter\n\n\n"
                "class CsvExporter(Exporter):\n"
                "    pass\n"
            ),
            "shop/job.py": (
                "from shop.csv_out import CsvExporter\n\n\n"
                "def run(rows):\n"
                "    exporter = CsvExporter()\n"
                "    return exporter.export(rows)\n"
            ),
        },
        "change": {
            "shop/base.py": (
                "class Exporter:\n"
                "    def export(self, data, fmt):\n"
                "        return list(data)\n"
            )
        },
        "golden": [("shop/job.py", 6, "fmt")],
    }


@scenario("override_divergence")
def _():
    # Parent gains a parameter; the subclass override doesn't. Polymorphic call
    # sites now break only on the subclass.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/base.py": (
                "class Handler:\n"
                "    def handle(self, event):\n"
                "        return event\n"
            ),
            "shop/custom.py": (
                "from shop.base import Handler\n\n\n"
                "class AuditHandler(Handler):\n"
                "    def handle(self, event):\n"
                "        return ('audit', event)\n"
            ),
        },
        "change": {
            "shop/base.py": (
                "class Handler:\n"
                "    def handle(self, event, ctx):\n"
                "        return (event, ctx)\n"
            )
        },
        "golden": [("shop/custom.py", 5, "diverges")],
    }


@scenario("generator_toggle")
def _():
    # Body-only by signature, but every caller now gets a generator object.
    # Golden: the *definition* is the finding (static layer flags the contract
    # in the packet; the LLM layer flags call sites). We accept either location.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/feed.py": "def batch(items):\n    return [i for i in items]\n",
            "shop/loader.py": (
                "from shop.feed import batch\n\n\n"
                "def load(items):\n"
                "    result = batch(items)\n"
                "    return len(result)\n"
            ),
        },
        "change": {
            "shop/feed.py": (
                "def batch(items):\n"
                "    for i in items:\n"
                "        yield i\n"
            )
        },
        # detected as a breaking behaviour delta; surfaced via packet + LLM.
        # The static layer alone has no call-site finding here, so golden is
        # scoped to what the deterministic layer CAN say: nothing provable.
        "golden": [],
        "expect_breaking_delta": True,
    }


@scenario("field_removed")
def _():
    # A dataclass-style field disappears; a typed reader elsewhere breaks.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/models.py": (
                "class Invoice:\n"
                "    def __init__(self, total, tax):\n"
                "        self.total = total\n"
                "        self.tax = tax\n"
            ),
            "shop/report.py": (
                "from shop.models import Invoice\n\n\n"
                "def summarize(inv: Invoice):\n"
                "    return inv.total + inv.tax\n"
            ),
        },
        "change": {
            "shop/models.py": (
                "class Invoice:\n"
                "    def __init__(self, total):\n"
                "        self.total = total\n"
            )
        },
        "golden": [("shop/report.py", 5, "tax")],
    }


@scenario("control_field_renamed_with_property")
def _():
    # Field "renamed" but a property keeps the old name alive — silence is correct.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/models.py": (
                "class Invoice:\n"
                "    def __init__(self, total):\n"
                "        self.total = total\n"
            ),
            "shop/report.py": (
                "from shop.models import Invoice\n\n\n"
                "def summarize(inv: Invoice):\n"
                "    return inv.total\n"
            ),
        },
        "change": {
            "shop/models.py": (
                "class Invoice:\n"
                "    def __init__(self, amount):\n"
                "        self.amount = amount\n\n"
                "    @property\n"
                "    def total(self):\n"
                "        return self.amount\n"
            )
        },
        "golden": [],
    }


# ---------------------------------------------------------------------------
# controls: the correct review is silence
# ---------------------------------------------------------------------------


@scenario("control_body_only")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/pricing.py": _PRICING,
            "shop/order.py": _ORDER,
        },
        "change": {
            "shop/pricing.py": (
                "def calculate_discount(price, pct):\n"
                "    return round(price * (1 - pct), 2)\n"
            )
        },
        "golden": [],
    }


@scenario("control_optional_added")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/pricing.py": _PRICING,
            "shop/order.py": _ORDER,
        },
        "change": {
            "shop/pricing.py": (
                "def calculate_discount(price, pct, *, floor=0.0):\n"
                "    return max(floor, price * (1 - pct))\n"
            )
        },
        "golden": [],
    }


@scenario("control_kwargs_wrapper")
def _():
    # The caller forwards **kwargs — a naive checker screams "missing argument",
    # a correct one stays silent.
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/core.py": "def render(template, mode):\n    return (template, mode)\n",
            "shop/wrap.py": (
                "from shop.core import render\n\n\n"
                "def render_page(template, **kwargs):\n"
                "    return render(template, **kwargs)\n"
            ),
        },
        "change": {
            "shop/core.py": "def render(template, mode, theme):\n    return (template, mode, theme)\n"
        },
        "golden": [],
    }


@scenario("control_star_args")
def _():
    return {
        "files": {
            "shop/__init__.py": _INIT,
            "shop/calc.py": "def add(a, b):\n    return a + b\n",
            "shop/use.py": (
                "from shop.calc import add\n\n\n"
                "def run(pairs):\n"
                "    return [add(*p) for p in pairs]\n"
            ),
        },
        "change": {
            "shop/calc.py": "def add(a, b, c):\n    return a + b + c\n"
        },
        "golden": [],
    }
