"""Baseline tests for skills/check-orders/scripts/within-days.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - exit 0 — `0 <= (today - date_str).days <= days`
  - exit 1 — not within range (includes empty / malformed date strings,
            with diagnostic on stderr)
  - exit 2 — usage error (wrong arg count, non-integer or negative `days`)
  - `date_str` is parsed as the first 10 characters (`YYYY-MM-DD`),
    so trailing time/zone info is tolerated; leading whitespace is
    stripped before the empty-check and the slice.

Tests freeze `module.date` to a fixed-today subclass so the
"today - date_str" arithmetic is deterministic.
"""

from datetime import date


class _FrozenDate(date):
    """Subclass with a fixed `today()` for deterministic delta math."""

    _today_value = date(2026, 4, 30)

    @classmethod
    def today(cls):
        return cls._today_value


def _run(module, monkeypatch, capsys, *args):
    """Invoke the script's main() with the given argv. Returns
    (exit_code, stdout, stderr). main() only exits via SystemExit."""
    monkeypatch.setattr("sys.argv", ["within-days.py", *args])
    monkeypatch.setattr(module, "date", _FrozenDate)
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_today_within_zero_days(within_days, monkeypatch, capsys):
    """delta == 0 with days=0 — within the closed range [0, 0]."""
    code, out, err = _run(within_days, monkeypatch, capsys, "2026-04-30", "0")
    assert code == 0
    assert out == ""
    assert err == ""


def test_within_window(within_days, monkeypatch, capsys):
    """5 days ago against a 7-day window — within."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-25", "7")
    assert code == 0


def test_outside_window_too_old(within_days, monkeypatch, capsys):
    """10 days ago against a 7-day window — outside."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-20", "7")
    assert code == 1


def test_future_date_outside_window(within_days, monkeypatch, capsys):
    """Future date → delta < 0 → not within."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-05-01", "7")
    assert code == 1


def test_boundary_inclusive_low(within_days, monkeypatch, capsys):
    """delta == 0 is within [0, 7]."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-30", "7")
    assert code == 0


def test_boundary_inclusive_high(within_days, monkeypatch, capsys):
    """delta == days is the inclusive upper edge."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-23", "7")
    assert code == 0


def test_boundary_just_outside(within_days, monkeypatch, capsys):
    """delta == days + 1 is outside."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-22", "7")
    assert code == 1


def test_trailing_time_info_truncated_to_first_10(within_days, monkeypatch, capsys):
    """`date_str[:10]` parses just the date portion of an ISO timestamp."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "2026-04-30T12:34:56+00:00", "0")
    assert code == 0


def test_leading_whitespace_stripped_before_parse(within_days, monkeypatch, capsys):
    """Leading whitespace is stripped first; the empty-check and the
    slice see the same value (this is the bug the docstring calls out)."""
    code, _, _ = _run(within_days, monkeypatch, capsys, "  2026-04-30  ", "0")
    assert code == 0


def test_empty_date_exits_1(within_days, monkeypatch, capsys):
    code, out, err = _run(within_days, monkeypatch, capsys, "", "7")
    assert code == 1
    assert out == ""
    assert "empty or whitespace-only date string" in err


def test_whitespace_only_date_exits_1(within_days, monkeypatch, capsys):
    """Whitespace-only is empty after strip()."""
    code, _, err = _run(within_days, monkeypatch, capsys, "   ", "7")
    assert code == 1
    assert "empty or whitespace-only date string" in err


def test_malformed_date_exits_1(within_days, monkeypatch, capsys):
    code, out, err = _run(within_days, monkeypatch, capsys, "not-a-date", "7")
    assert code == 1
    assert out == ""
    assert "malformed date" in err
    assert "not-a-date" in err


def test_malformed_date_with_slashes_exits_1(within_days, monkeypatch, capsys):
    """`2026/04/30` does not match YYYY-MM-DD — fromisoformat rejects it."""
    code, _, err = _run(within_days, monkeypatch, capsys, "2026/04/30", "7")
    assert code == 1
    assert "malformed date" in err


def test_non_integer_days_exits_2(within_days, monkeypatch, capsys):
    code, _, err = _run(within_days, monkeypatch, capsys, "2026-04-30", "seven")
    assert code == 2
    assert "days must be an integer" in err


def test_negative_days_exits_2(within_days, monkeypatch, capsys):
    code, _, err = _run(within_days, monkeypatch, capsys, "2026-04-30", "-1")
    assert code == 2
    assert "days must be non-negative" in err


def test_too_few_args_exits_2(within_days, monkeypatch, capsys):
    code, _, err = _run(within_days, monkeypatch, capsys, "2026-04-30")
    assert code == 2
    assert "Usage:" in err


def test_too_many_args_exits_2(within_days, monkeypatch, capsys):
    code, _, err = _run(within_days, monkeypatch, capsys, "2026-04-30", "7", "extra")
    assert code == 2
    assert "Usage:" in err
