"""Tests for skills/check-orders/scripts/render-order-alerts.py.

Locks down the documented contract:

  - stdin: the JSON array get-flagged-orders.py prints
  - stdout: a single JSON object {"message": <str|null>, "count": <int>};
    `message` is the complete Telegram HTML text, one bullet per order
    in input order, and null for an empty array (stay-silent signal)
  - every field is HTML-escaped before interpolation — untrusted
    description/flag_reason/source/order_date can never break the
    Telegram HTML parse or inject tags/links
  - exit 1 + stderr diagnostic + no stdout on non-JSON stdin, a
    non-array payload, or a non-object array element
"""

import io
import json


def _run(module, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _message(out):
    return json.loads(out)["message"]


def _payload(**fields):
    row = {
        "description": "Thing",
        "flag_reason": "Order cancelled",
        "source": "amazon",
        "order_date": "2026-04-01",
    }
    row.update(fields)
    return json.dumps([row])


def test_empty_array_yields_null_message(render_order_alerts, monkeypatch, capsys):
    code, out, err = _run(render_order_alerts, monkeypatch, capsys, "[]")
    assert code == 0
    assert json.loads(out) == {"message": None, "count": 0}
    assert err == ""


def test_renders_single_order_bullet(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload())
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["message"] == (
        "<b>📦 Order alerts:</b>\n\n• <b>Thing</b> — Order cancelled (<i>amazon, 2026-04-01</i>)"
    )


def test_meta_prefers_merchant_over_source(render_order_alerts, monkeypatch, capsys):
    # `#55`: a captured merchant leads the meta so a `source=other` item is
    # identifiable.
    code, out, _err = _run(
        render_order_alerts,
        monkeypatch,
        capsys,
        _payload(source="other", merchant="Pacagen"),
    )
    assert code == 0
    assert _message(out) == (
        "<b>📦 Order alerts:</b>\n\n• <b>Thing</b> — Order cancelled (<i>Pacagen, 2026-04-01</i>)"
    )


def test_meta_falls_back_to_source_without_merchant(render_order_alerts, monkeypatch, capsys):
    # No merchant captured (or blank) → the coarse source is shown.
    code, out, _err = _run(
        render_order_alerts, monkeypatch, capsys, _payload(merchant=None, source="amazon")
    )
    assert code == 0
    assert "(<i>amazon, 2026-04-01</i>)" in _message(out)


def test_escapes_ampersand_and_angle_brackets(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(
        render_order_alerts, monkeypatch, capsys, _payload(description="A&B <tag>")
    )
    assert code == 0
    message = _message(out)
    assert "A&amp;B &lt;tag&gt;" in message
    assert "<tag>" not in message


def test_escapes_tag_injection_in_description(render_order_alerts, monkeypatch, capsys):
    hostile = '</b><a href="https://example.invalid">x</a>'
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload(description=hostile))
    assert code == 0
    message = _message(out)
    assert "&lt;/b&gt;&lt;a href=&quot;https://example.invalid&quot;&gt;x&lt;/a&gt;" in message
    # The only tags in the message are the template's own.
    stripped = message.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    assert "<" not in stripped.replace("&lt;", "")


def test_escapes_flag_reason_and_source(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(
        render_order_alerts,
        monkeypatch,
        capsys,
        _payload(flag_reason="a<b", source="shop&co"),
    )
    assert code == 0
    message = _message(out)
    assert "a&lt;b" in message
    assert "shop&amp;co" in message


def test_multiple_orders_keep_input_order(render_order_alerts, monkeypatch, capsys):
    rows = [
        {"description": "First", "flag_reason": "R1", "source": "s", "order_date": "d"},
        {"description": "Second", "flag_reason": "R2", "source": "s", "order_date": "d"},
    ]
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, json.dumps(rows))
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 2
    message = payload["message"]
    assert message.index("First") < message.index("Second")
    assert message.count("• <b>") == 2


def test_null_field_renders_empty(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload(flag_reason=None))
    assert code == 0
    message = _message(out)
    assert "None" not in message
    assert "<b>Thing</b> —  (" in message


def test_malformed_json_exits_1(render_order_alerts, monkeypatch, capsys):
    code, out, err = _run(render_order_alerts, monkeypatch, capsys, "not json")
    assert code == 1
    assert out == ""
    assert "not valid JSON" in err


def test_non_array_payload_exits_1(render_order_alerts, monkeypatch, capsys):
    code, out, err = _run(render_order_alerts, monkeypatch, capsys, '{"a": 1}')
    assert code == 1
    assert out == ""
    assert "expected a JSON array" in err


def test_non_object_array_element_exits_1(render_order_alerts, monkeypatch, capsys):
    code, out, err = _run(render_order_alerts, monkeypatch, capsys, '[1, "x"]')
    assert code == 1
    assert out == ""
    assert "every array element must be an order object" in err
