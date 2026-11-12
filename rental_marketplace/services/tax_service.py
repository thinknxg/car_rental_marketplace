"""Replaceable tax service.

Real obligations depend on operating jurisdictions; this default
implementation applies rule-level tax % falling back to the
marketplace default. The calculated snapshot is stored on the
transaction — historical documents are never recomputed.
"""

from decimal import Decimal, ROUND_HALF_UP

TWO = Decimal("0.01")


def q2(x):
    return Decimal(str(x or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


def calculate(taxable_amount, context=None):
    """context: {"tax_percentage": rule override, "default_tax_percentage": ...}"""
    context = context or {}
    pct = context.get("tax_percentage")
    if pct in (None, "", 0) and context.get("tax_percentage") != 0:
        pct = context.get("default_tax_percentage") or 0
    pct = Decimal(str(pct or 0))
    taxable = q2(taxable_amount)
    total_tax = q2(taxable * pct / Decimal("100"))
    return {
        "taxable_amount": float(taxable),
        "tax_lines": [{"description": f"Tax {pct}%",
                       "rate": float(pct),
                       "amount": float(total_tax)}] if total_tax else [],
        "total_tax": float(total_tax),
    }
