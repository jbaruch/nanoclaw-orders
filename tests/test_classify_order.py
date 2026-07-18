"""Tests for skills/check-orders/scripts/classify-order.py.

Covers the #44 requirement that Shopify order mail recovered via
`from:t.shopifyemail.com` classifies as `source="shopify"` / `status="ordered"`
(subjects read "Order #NNN confirmed"), plus the documented limitation that
merchant-custom-domain Shopify senders classify as `other`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-orders/scripts/classify-order.py"


@pytest.fixture
def classify():
    spec = importlib.util.spec_from_file_location(
        "classify_order_under_test", REPO_ROOT / SCRIPT_REL
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SCRIPT_REL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shopify_transactional_confirmation_normalizes(classify):
    # The #44 acceptance case: fetched via from:t.shopifyemail.com, subject
    # "Order #NNN confirmed". Must be shopify/ordered, not other/unknown.
    result = classify.classify(
        from_header="NeuroHUD <store+72130101326@t.shopifyemail.com>",
        subject="Order #3396 confirmed",
        snippet="Thank you for your purchase!",
    )
    assert result == {"source": "shopify", "status": "ordered"}


def test_shopify_transactional_shipment_is_shipped(classify):
    result = classify.classify(
        from_header="store+55699177651@t.shopifyemail.com",
        subject="A shipment from order #OS82984024 is on the way",
        snippet="Your order is on the way.",
    )
    assert result == {"source": "shopify", "status": "shipped"}


def test_shopify_transactional_cancellation_is_cancelled(classify):
    # "confirmed" is absent; "canceled" must win and outranks the ordered rule.
    result = classify.classify(
        from_header="store+72130101326@t.shopifyemail.com",
        subject="Order #3396 has been canceled",
        snippet="Your order was canceled.",
    )
    assert result == {"source": "shopify", "status": "cancelled"}


def test_bare_shopifyemail_domain_normalizes(classify):
    result = classify.classify(
        from_header="orders@shopifyemail.com", subject="Order #1 confirmed", snippet=""
    )
    assert result["source"] == "shopify"


def test_merchant_custom_domain_is_other(classify):
    # Documented #44 limitation: a Shopify store on its own domain has no
    # queryable Shopify signal in the sender, so source falls to "other".
    # The status still classifies from the subject.
    result = classify.classify(
        from_header="support@pacagen.com",
        subject="Order #197191 confirmed",
        snippet="Thanks for your order.",
    )
    assert result == {"source": "other", "status": "ordered"}


def test_amazon_ordered(classify):
    result = classify.classify(
        from_header="auto-confirm@amazon.com",
        subject="Ordered: LG LT1000P Refrigerator Water Filter",
        snippet="Your order has been placed.",
    )
    assert result == {"source": "amazon", "status": "ordered"}


def test_amazon_shipment_is_shipped(classify):
    result = classify.classify(
        from_header="shipment-tracking@amazon.com",
        subject="Shipped: your package",
        snippet="on the way",
    )
    assert result == {"source": "amazon", "status": "shipped"}


def test_shop_app_source(classify):
    result = classify.classify(
        from_header="orders@shop.app", subject="Order delivered", snippet="has been delivered"
    )
    assert result == {"source": "shop", "status": "delivered"}


def test_refund_outranks_confirmed(classify):
    # A refund email may say "refund confirmed"; refunded must win over ordered.
    result = classify.classify(
        from_header="store+1@t.shopifyemail.com",
        subject="Your refund is confirmed",
        snippet="We have refunded your order.",
    )
    assert result["status"] == "refunded"


def test_unknown_status_and_other_source(classify):
    result = classify.classify(
        from_header="newsletter@example.com",
        subject="Weekly digest",
        snippet="Here is what's new.",
    )
    assert result == {"source": "other", "status": "unknown"}


def test_missing_from_is_other(classify):
    result = classify.classify(from_header=None, subject="Order #5 confirmed", snippet=None)
    assert result == {"source": "other", "status": "ordered"}


def test_main_reads_stdin_and_emits_json():
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        input=json.dumps(
            {"from": "store+9@t.shopifyemail.com", "subject": "Order #9 confirmed", "snippet": ""}
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"source": "shopify", "status": "ordered"}


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
        input='"a string"',
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "must be a JSON object" in proc.stderr
