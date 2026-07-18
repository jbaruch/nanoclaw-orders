"""Tests for skills/check-orders/scripts/extract-amount.py.

Fixtures mirror the shapes the issue-#38 live sample surfaced: Amazon order
confirmations with the total only in the body, Shopify store confirmations
whose body carries a struck list price larger than the true total, refunds
whose amount sits in the snippet, and subtotal/total disambiguation. The body
strings are pre-flattened to a single space-collapsed line, matching what
heartbeat's `sanitize()` hands the agent (no newlines survive sanitization).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-orders/scripts/extract-amount.py"


@pytest.fixture
def extract():
    spec = importlib.util.spec_from_file_location(
        "extract_amount_under_test", REPO_ROOT / SCRIPT_REL
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SCRIPT_REL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_amazon_total_in_body_only(extract):
    # The #38 acceptance fixture: subject+snippet carry no amount at all, the
    # total lives only in the body. The pre-#38 subject/snippet rule returned 0.
    result = extract.extract_amount(
        subject="Ordered: LG LT1000P Refrigerator Water Filter",
        snippet="Hi Baruch, your order has been placed and is being prepared.",
        body="Order Confirmation Items Subtotal: $99.99 Tax: $9.75 Order Total: $109.74 "
        "Arriving Tuesday Ship to Baruch",
    )
    assert result["amount"] == 109.74
    assert result["matched"] == "labeled_total"


def test_shopify_struck_list_price_larger_than_total_is_not_taken(extract):
    # Live sample: a Shopify body prints a struck-through list price LARGER than
    # the true total. A "largest amount in body" rule would return 98.99; the
    # labeled-total rule must return 94.14.
    result = extract.extract_amount(
        subject="Order #197233 confirmed",
        snippet="Thank you for your purchase!",
        body="Tales of Valhalla Set Was $98.99 Now $94.14 Subtotal $94.14 "
        "Shipping Free Total $94.14",
    )
    assert result["amount"] == 94.14


def test_refund_amount_in_snippet_wins_when_no_total_label(extract):
    # Refund emails carry the amount above the fold and no "Total" label; the
    # subject+snippet largest-amount fallback must pick it, ignoring the smaller
    # item and tax lines in the body.
    result = extract.extract_amount(
        subject="Your refund was issued",
        snippet="Your refund of $7.12 has been processed to your card.",
        body="Item price $6.49 Tax $0.63 Refund method Visa ending 4242",
    )
    assert result["amount"] == 7.12
    assert result["matched"] == "subject_snippet_largest"


def test_subtotal_is_never_mistaken_for_total(extract):
    result = extract.extract_amount(
        subject="Order confirmation",
        snippet="Your order is confirmed.",
        body="Subtotal: $84.32 Shipping: $5.00 Estimated tax: $8.20 Order Total: $109.74",
    )
    assert result["amount"] == 109.74
    assert result["matched"] == "labeled_total"


def test_hyphenated_sub_total_is_not_treated_as_total(extract):
    # "Sub-total" (hyphen) slips past a letter-only lookbehind — the char before
    # "total" is "-", not a letter. With no genuine total label and no amount
    # above the fold, the result must be 0.0/none, never the sub-total figure.
    result = extract.extract_amount(
        subject="Order confirmation",
        snippet="Your order is confirmed.",
        body="Sub-total: $84.32 Shipping: $5.00 Estimated tax: $8.20",
    )
    assert result["amount"] == 0.0
    assert result["matched"] == "none"


def test_spaced_sub_total_is_not_treated_as_total(extract):
    result = extract.extract_amount(
        subject="Order confirmation",
        snippet="Your order is confirmed.",
        body="Sub total $84.32 Shipping $5.00 Order Total: $97.52",
    )
    assert result["amount"] == 97.52
    assert result["matched"] == "labeled_total"


def test_last_labeled_total_wins_over_earlier_one(extract):
    # A multi-line summary can print a per-shipment "Order Total" before the
    # final "Grand Total"; the last labeled figure is the one charged.
    result = extract.extract_amount(
        subject="Order confirmation",
        snippet="Confirmed.",
        body="Shipment 1 Order Total: $50.00 Shipment 2 Order Total: $59.74 Grand Total: $109.74",
    )
    assert result["amount"] == 109.74


def test_bare_total_label_used_when_no_grand_or_order_total(extract):
    result = extract.extract_amount(
        subject="Order #170910 confirmed",
        snippet="Thanks for shopping with Knife Aid.",
        body="Sharpening service x2 Total: $62.10",
    )
    assert result["amount"] == 62.10
    assert result["matched"] == "bare_total"


def test_amount_in_subject_is_read(extract):
    result = extract.extract_amount(
        subject="Order confirmation - $32.88",
        snippet="Your Anker cables are on the way.",
        body="Tracking number 1Z999 no total printed here",
    )
    assert result["amount"] == 32.88
    assert result["matched"] == "subject_snippet_largest"


def test_comma_grouped_thousands(extract):
    result = extract.extract_amount(
        subject="Order confirmation",
        snippet="Confirmed.",
        body="Items Subtotal: $1,199.00 Tax: $100.00 Grand Total: $1,299.00",
    )
    assert result["amount"] == 1299.00


def test_no_amount_anywhere_defaults_to_zero(extract):
    result = extract.extract_amount(
        subject="Your order has shipped",
        snippet="On the way — arriving soon.",
        body="Your package is on the way. Track it with the link below.",
    )
    assert result["amount"] == 0.0
    assert result["matched"] == "none"


def test_dollar_off_copy_is_not_read_as_amount(extract):
    # Marketing copy like "$5 off" has no cents and must not be picked up.
    result = extract.extract_amount(
        subject="Your order shipped — $5 off your next order",
        snippet="Save $10 on your next purchase!",
        body="Your item is on the way.",
    )
    assert result["amount"] == 0.0
    assert result["matched"] == "none"


def test_missing_fields_are_tolerated(extract):
    result = extract.extract_amount(subject=None, snippet=None, body="Order Total: $19.99")
    assert result["amount"] == 19.99
    assert result["matched"] == "labeled_total"


def test_main_reads_stdin_and_emits_json():
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        input=json.dumps({"subject": "x", "snippet": "y", "body": "Order Total: $42.00"}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload == {"amount": 42.0, "currency": "USD", "matched": "labeled_total"}


def test_main_exits_2_on_empty_stdin():
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "no JSON on stdin" in proc.stderr


def test_main_exits_2_on_non_object_json():
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        input="[1, 2, 3]",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "must be a JSON object" in proc.stderr
