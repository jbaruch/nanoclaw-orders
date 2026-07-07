"""Tests for skills/check-orders/scripts/render-order-alerts.py.

Locks down the documented contract:

  - stdin: the JSON array get-flagged-orders.py prints
  - stdout: complete Telegram HTML message, one bullet per order in
    input order; EMPTY stdout for an empty array (stay-silent)
  - every field is HTML-escaped before interpolation — untrusted
    description/flag_reason/source/order_date can never break the
    Telegram HTML parse or inject tags/links
  - exit 1 + stderr diagnostic + no stdout on non-JSON stdin or a
    non-array payload
"""

import io
import json


def _run(module, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _payload(**fields):
    row = {
        "description": "Thing",
        "flag_reason": "Order cancelled",
        "source": "amazon",
        "order_date": "2026-04-01",
    }
    row.update(fields)
    return json.dumps([row])


def test_empty_array_prints_nothing(render_order_alerts, monkeypatch, capsys):
    code, out, err = _run(render_order_alerts, monkeypatch, capsys, "[]")
    assert code == 0
    assert out == ""
    assert err == ""


def test_renders_single_order_bullet(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload())
    assert code == 0
    assert out == (
        "<b>📦 Order alerts:</b>\n\n• <b>Thing</b> — Order cancelled (<i>amazon, 2026-04-01</i>)\n"
    )


def test_escapes_ampersand_and_angle_brackets(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(
        render_order_alerts, monkeypatch, capsys, _payload(description="A&B <tag>")
    )
    assert code == 0
    assert "A&amp;B &lt;tag&gt;" in out
    assert "<tag>" not in out


def test_escapes_tag_injection_in_description(render_order_alerts, monkeypatch, capsys):
    hostile = '</b><a href="https://example.invalid">x</a>'
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload(description=hostile))
    assert code == 0
    assert "</b><a" not in out.replace("• <b>", "", 1).replace("</b> —", "", 1)
    assert "&lt;/b&gt;&lt;a href=&quot;https://example.invalid&quot;&gt;x&lt;/a&gt;" in out
    # The only tags in the message are the template's own.
    stripped = out.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    assert "<" not in stripped.replace("&lt;", "")


def test_escapes_flag_reason_and_source(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(
        render_order_alerts,
        monkeypatch,
        capsys,
        _payload(flag_reason="a<b", source="shop&co"),
    )
    assert code == 0
    assert "a&lt;b" in out
    assert "shop&amp;co" in out


def test_multiple_orders_keep_input_order(render_order_alerts, monkeypatch, capsys):
    rows = [
        {"description": "First", "flag_reason": "R1", "source": "s", "order_date": "d"},
        {"description": "Second", "flag_reason": "R2", "source": "s", "order_date": "d"},
    ]
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, json.dumps(rows))
    assert code == 0
    assert out.index("First") < out.index("Second")
    assert out.count("• <b>") == 2


def test_null_field_renders_empty(render_order_alerts, monkeypatch, capsys):
    code, out, _err = _run(render_order_alerts, monkeypatch, capsys, _payload(flag_reason=None))
    assert code == 0
    assert "None" not in out
    assert "<b>Thing</b> —  (" in out


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
