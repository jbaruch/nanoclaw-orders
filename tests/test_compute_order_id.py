"""Baseline tests for skills/check-orders/scripts/compute-order-id.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - stdout: f"{source}-{order_date}-{hash}" where `hash` is the first 8
    lowercase hex characters of `sha1(description.encode("utf-8"))`
  - exit 0 on success
  - exit 2 on usage error (wrong arg count)

The expected hash prefixes below are pre-computed (`echo -n '<text>' |
shasum -a 1 | cut -c1-8`) so the tests are deterministic and match the
script's UTF-8 SHA-1 construction.
"""


def _run(module, monkeypatch, capsys, *args):
    """Invoke the script's main() with the given argv and return
    (exit_code, stdout, stderr).

    A normal return from main() is treated as exit code 0. If main()
    raises SystemExit (for example, `sys.exit(2)` on a usage error),
    the exception is captured and its exit code is returned instead.
    """
    monkeypatch.setattr("sys.argv", ["compute-order-id.py", *args])
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_known_amazon_order(compute_order_id, monkeypatch, capsys):
    """sha1('Kindle Paperwhite')[:8] == '5467173c'."""
    code, out, err = _run(
        compute_order_id,
        monkeypatch,
        capsys,
        "amazon",
        "2026-04-15",
        "Kindle Paperwhite",
    )
    assert code == 0
    assert err == ""
    assert out.strip() == "amazon-2026-04-15-5467173c"


def test_known_audible_order(compute_order_id, monkeypatch, capsys):
    """sha1('The Pragmatic Programmer')[:8] == '9303b839'."""
    code, out, _ = _run(
        compute_order_id,
        monkeypatch,
        capsys,
        "audible",
        "2026-03-10",
        "The Pragmatic Programmer",
    )
    assert code == 0
    assert out.strip() == "audible-2026-03-10-9303b839"


def test_determinism_same_input(compute_order_id, monkeypatch, capsys):
    """Same description → same hash on every call."""
    code1, out1, _ = _run(compute_order_id, monkeypatch, capsys, "amazon", "2026-04-15", "Widget")
    code2, out2, _ = _run(compute_order_id, monkeypatch, capsys, "amazon", "2026-04-15", "Widget")
    assert code1 == 0 and code2 == 0
    assert out1 == out2


def test_different_descriptions_diverge(compute_order_id, monkeypatch, capsys):
    """Different description text → different hash suffix."""
    _, out1, _ = _run(compute_order_id, monkeypatch, capsys, "amazon", "2026-04-15", "Widget A")
    _, out2, _ = _run(compute_order_id, monkeypatch, capsys, "amazon", "2026-04-15", "Widget B")
    assert out1 != out2
    # source + date prefix is stable; only the hash suffix differs.
    assert out1.startswith("amazon-2026-04-15-")
    assert out2.startswith("amazon-2026-04-15-")


def test_unicode_description_uses_utf8_sha1(compute_order_id, monkeypatch, capsys):
    """sha1('café'.encode('utf-8'))[:8] == 'f424452a' — verifies the
    explicit utf-8 encoding the script applies before hashing."""
    code, out, _ = _run(compute_order_id, monkeypatch, capsys, "amazon", "2026-04-15", "café")
    assert code == 0
    assert out.strip() == "amazon-2026-04-15-f424452a"


def test_hash_is_8_lowercase_hex(compute_order_id, monkeypatch, capsys):
    """Structural check: suffix is exactly 8 chars, all `[0-9a-f]`."""
    _, out, _ = _run(
        compute_order_id,
        monkeypatch,
        capsys,
        "amazon",
        "2026-04-15",
        "anything goes here",
    )
    suffix = out.strip().rsplit("-", 1)[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_too_few_args_exits_2(compute_order_id, monkeypatch, capsys):
    code, out, err = _run(compute_order_id, monkeypatch, capsys, "amazon")
    assert code == 2
    assert out == ""
    assert "Usage:" in err


def test_too_many_args_exits_2(compute_order_id, monkeypatch, capsys):
    code, _, err = _run(
        compute_order_id,
        monkeypatch,
        capsys,
        "amazon",
        "2026-04-15",
        "Widget",
        "extra",
    )
    assert code == 2
    assert "Usage:" in err


def test_no_args_exits_2(compute_order_id, monkeypatch, capsys):
    code, _, err = _run(compute_order_id, monkeypatch, capsys)
    assert code == 2
    assert "Usage:" in err
