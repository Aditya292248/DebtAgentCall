"""Dialer: check, record, then place each call exactly once.

What it checks:
  - ``FakePhoneClient`` and the AgentPhone error mapping (429 with and without
    ``Retry-After``, 409, 402; a 5xx or timeout is not a ``CallError``).
  - ``uv run dialer`` refuses a real run without both the API key and
    ``--confirm``.
  - Against the real local database (``db``): blocked debtors are never
    dialed; the attempt row is committed before ``make_call``; when two
    runners race on a debt with 6 calls, one places the 7th and the other's
    (the 8th) is blocked; a refused call (429/409/402) is dialed once and marked; a timeout
    is dialed once and left ``placed_unknown``; the dry run writes nothing.

The database tests write real ``call_attempt`` rows (the dialer must commit
before it dials) and delete them afterwards. Their audit rows stay, because
the audit log can't be edited; they are ordinary, valid entries.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from dialer import cli
from dialer.service import check_debt_status
from phone.client import CallError, FakePhoneClient, call_error_for

# 2026-11-05 14:00 in New York, 11:00 in Los Angeles; the seed is dated around it.
WHEN = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
SAM = "ACC-1001"  # allowed
ALEX = "ACC-1002"  # 6 calls in the last 7 days: one more is allowed
CASEY = "ACC-1004"  # 7 calls: blocked by call_frequency
RILEY = "ACC-1005"  # revoked consent: blocked


# --- offline: the phone client ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "kind"),
    [
        (429, {"Retry-After": "3"}, "throttled"),
        (429, {}, "concurrency"),
        (409, {}, "busy"),
        (402, {}, "no_funds"),
        (422, {}, "other"),
    ],
)
def test_refusals_map_to_a_call_error(status, headers, kind):
    error = call_error_for(status, headers)
    assert isinstance(error, CallError) and error.kind == kind


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_server_error_is_not_a_refusal(status):
    """A 5xx might have created the call, so it must not look like 'not placed'."""
    assert call_error_for(status, {}) is None


def test_fake_phone_records_calls_and_raises_what_it_is_told():
    phone = FakePhoneClient()
    assert phone.make_call("+12135550101").startswith("fake-call-")
    phone.fail_next("no_funds")
    with pytest.raises(CallError) as info:
        phone.make_call("+12135550101")
    assert info.value.kind == "no_funds"
    assert [c["to_number"] for c in phone.calls] == ["+12135550101", "+12135550101"]


# --- offline: only active debts may be called -------------------------------------


def test_only_an_active_debt_may_be_called():
    assert check_debt_status("active").allow
    for status in ("disputed", "paused", "closed", None):
        d = check_debt_status(status)
        assert not d.allow and d.rule_id == "debt_status"


# --- offline: the command refuses a real run -----------------------------------------


@pytest.fixture
def no_dotenv(monkeypatch):
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.delenv("AGENTPHONE_API_KEY", raising=False)


@pytest.mark.parametrize(
    ("key", "argv"),
    [
        (None, []),
        (None, ["--confirm"]),
        ("sk-test", []),
        ("sk-test", ["--confirm", "--when", "2026-11-05T19:00:00+00:00"]),
    ],
    ids=[
        "no key, no confirm",
        "confirm without key",
        "key without confirm",
        "real run with --when",
    ],
)
def test_a_real_run_refuses_without_key_and_confirm(no_dotenv, monkeypatch, capsys, key, argv):
    if key:
        monkeypatch.setenv("AGENTPHONE_API_KEY", key)

    def no_db():
        raise AssertionError("a refused run must not even open the database")

    monkeypatch.setattr("harness.db.connect", no_db)
    assert cli.main(argv) == 2
    assert "refusing to place real calls" in capsys.readouterr().err


# --- against the real local database -----------------------------------------------


def _attempt_ids(conn, account: str) -> set[str]:
    rows = conn.execute(
        """select a.id::text from public.call_attempt a
           join public.debt d on d.id = a.debt_id where d.account_number = %s""",
        (account,),
    ).fetchall()
    conn.commit()
    return {r[0] for r in rows}


@pytest.fixture
def conn() -> Iterator:
    """A connection outside any transaction; attempts written during the test are removed."""
    from harness.db import connect

    with connect() as c:
        before = {a: _attempt_ids(c, a) for a in (SAM, ALEX, CASEY, RILEY)}
        yield c
        c.rollback()
        for account, ids in before.items():
            c.execute(
                """delete from public.call_attempt a using public.debt d
                   where d.id = a.debt_id and d.account_number = %s
                   and not (a.id::text = any(%s))""",
                (account, list(ids)),
            )
        c.commit()


def _attempt(conn, attempt_id: str) -> tuple[str, str | None]:
    row = conn.execute(
        "select outcome, agentphone_call_id from public.call_attempt where id = %s",
        (attempt_id,),
    ).fetchone()
    conn.commit()
    return row


@pytest.mark.db
def test_blocked_debtors_are_never_dialed(conn):
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    result = dial_queue([CASEY, RILEY, SAM], WHEN, phone, conn)

    assert [c["to_number"] for c in phone.calls] == ["+12135550101"]  # Sam only
    blocked = {o.account_number: o.reasons for o in result.blocked}
    assert set(blocked) == {CASEY, RILEY}
    assert any(r.startswith("call_frequency") for r in blocked[CASEY])
    assert any(r.startswith("tcpa_consent") for r in blocked[RILEY])
    assert [o.account_number for o in result.placed] == [SAM]


@pytest.mark.db
def test_the_attempt_is_committed_before_make_call(conn):
    from dialer.service import dial_queue
    from harness.db import connect

    seen: list[list[str]] = []

    def look(to_number: str) -> None:
        # A different connection: it only sees rows that are already committed.
        with connect() as other:
            seen.append(
                [
                    r[0]
                    for r in other.execute(
                        """select a.outcome from public.call_attempt a
                           join public.debt d on d.id = a.debt_id
                           where d.account_number = %s and a.placed_at = %s""",
                        (SAM, WHEN),
                    ).fetchall()
                ]
            )

    phone = FakePhoneClient(on_call=look)
    result = dial_queue([SAM], WHEN, phone, conn)

    assert seen == [["placing"]]
    (placed,) = result.placed
    assert _attempt(conn, placed.attempt_id) == ("placed", placed.call_id)


@pytest.mark.db
def test_two_racing_runners_cannot_both_place_the_last_allowed_call(conn):
    """Alex has 6 calls; the 7th is allowed once, and the next one is blocked."""
    from dialer.service import reserve, reserve_attempt
    from harness.db import connect

    first = None
    second: list = []
    with connect() as a, connect() as b:
        with a.transaction():
            first = reserve(a, ALEX, WHEN)  # holds the lock on Alex's debt
            assert first.attempt_id is not None
            racer = threading.Thread(target=lambda: second.append(reserve_attempt(b, ALEX, WHEN)))
            racer.start()
            racer.join(timeout=1.0)
            assert racer.is_alive(), "the second runner should be waiting on the lock"
        racer.join(timeout=10)  # first committed, so the second may go on
        assert not racer.is_alive()

    (later,) = second
    assert later.attempt_id is None
    freq = [d for d in later.decisions if d.rule_id == "call_frequency"]
    assert freq and not freq[0].allow
    assert freq[0].evidence["attempts_in_window"] == 7


@pytest.mark.db
def test_parallel_workers_place_the_7th_call_only_once(conn):
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    results: list = []  # two whole runs at once, both after Alex's 7th call

    def go() -> None:
        from harness.db import connect

        with connect() as own:
            results.append(dial_queue([ALEX], WHEN + timedelta(seconds=1), phone, own))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    placed = [o for r in results for o in r.placed]
    assert len(placed) == 1 and len(phone.calls) == 1


@pytest.mark.db
@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("throttled", "throttled"),
        ("concurrency", "concurrency_limit"),
        ("busy", "number_busy"),
        ("no_funds", "no_funds"),
    ],
)
def test_a_refused_call_is_not_retried_and_is_marked(conn, kind, outcome):
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    phone.fail_next(kind)
    result = dial_queue([SAM], WHEN, phone, conn)

    assert len(phone.calls) == 1
    (o,) = result.outcomes
    assert o.outcome == outcome
    assert _attempt(conn, o.attempt_id) == (outcome, None)


@pytest.mark.db
def test_a_timeout_is_not_retried_and_stays_placed_unknown(conn):
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    phone.fail_next(TimeoutError("read timed out"))
    result = dial_queue([SAM], WHEN, phone, conn)

    assert len(phone.calls) == 1
    (o,) = result.outcomes
    assert o.outcome == "placed_unknown"
    assert _attempt(conn, o.attempt_id) == ("placed_unknown", None)


@pytest.mark.db
def test_the_dry_run_writes_nothing_and_dials_nobody(conn):
    from dialer.service import plan_queue

    before = _attempt_ids(conn, SAM)
    result = plan_queue([CASEY, SAM, RILEY], WHEN, conn)
    conn.rollback()

    assert _attempt_ids(conn, SAM) == before
    assert {o.account_number for o in result.blocked} == {CASEY, RILEY}
    assert [o.account_number for o in result.outcomes if not o.blocked] == [SAM]


@pytest.mark.db
def test_the_queue_puts_the_oldest_last_attempt_first(conn):
    from dialer.service import queue_order

    # Last calls in the seed: Sam 2026-10-01, Alex 2026-11-04, Casey 2026-11-05.
    order = queue_order(conn, [CASEY, ALEX, SAM, ALEX])
    conn.rollback()
    assert order == [SAM, ALEX, CASEY]  # and the repeat is dropped


@pytest.fixture
def sam_disputed(conn):
    """Sam's debt marked disputed (committed, since the dialer reads it fresh), then restored."""
    conn.execute("update public.debt set status = 'disputed' where account_number = %s", (SAM,))
    conn.commit()
    yield
    conn.rollback()
    conn.execute("update public.debt set status = 'active' where account_number = %s", (SAM,))
    conn.commit()


@pytest.mark.db
def test_a_disputed_debt_is_never_dialed_even_when_named(conn, sam_disputed):
    from dialer.service import dial_one, plan_queue

    phone = FakePhoneClient()
    outcome = dial_one(conn, SAM, WHEN, phone)

    assert phone.calls == []
    assert outcome.blocked and outcome.attempt_id is None
    assert any(r.startswith("debt_status") for r in outcome.reasons)
    (planned,) = plan_queue([SAM], WHEN, conn).outcomes
    conn.rollback()
    assert planned.blocked


@pytest.mark.db
@pytest.mark.parametrize(
    "error", ["no_funds", "concurrency", "throttled", "busy", TimeoutError("timed out")]
)
def test_the_queue_stops_when_every_call_would_fail(conn, error):
    """Sam's call fails for a reason that hits every call, so Alex is not tried."""
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    phone.fail_next(error)
    result = dial_queue([SAM, ALEX], WHEN, phone, conn)

    assert len(phone.calls) == 1
    assert result.stopped and result.stopped.startswith(SAM)
    assert result.not_tried == [ALEX]
    assert [o.account_number for o in result.outcomes] == [SAM]


@pytest.mark.db
def test_a_refusal_for_one_number_does_not_stop_the_queue(conn):
    from dialer.service import dial_queue

    phone = FakePhoneClient()
    phone.fail_next("other")  # e.g. 422 for a bad number: only this call
    result = dial_queue([SAM, ALEX], WHEN, phone, conn)

    assert result.stopped is None and result.not_tried == []
    assert [c["to_number"] for c in phone.calls] == ["+12135550101", "+14155550102"]


@pytest.mark.db
def test_the_call_id_is_logged_if_saving_it_fails(conn, monkeypatch, caplog):
    import dialer.service as service

    real_mark = service._mark

    def broken(c, attempt_id, outcome, call_id):
        if outcome == "placed":
            raise RuntimeError("database went away")
        real_mark(c, attempt_id, outcome, call_id)

    monkeypatch.setattr(service, "_mark", broken)
    phone = FakePhoneClient()
    with pytest.raises(RuntimeError, match="database went away"):
        service.dial_one(conn, SAM, WHEN, phone)

    assert len(phone.calls) == 1
    assert "fake-call-1" in caplog.text
