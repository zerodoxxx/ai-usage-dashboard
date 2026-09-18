"""Tests for DeepSeek peak/off-peak pricing and time-based resolution."""

from datetime import datetime, timedelta, timezone

from src.pricing import (
    DEEPSEEK_OFF_PEAK_PRICING,
    MODEL_PRICING,
    PricingCatalog,
    PricingRates,
    calculate_cost,
    calculate_cost_strict,
    get_pricing,
    get_pricing_strict,
    is_deepseek_peak_utc,
    resolve_pricing_strict,
    to_utc_datetime,
)


def test_deepseek_peak_utc_helper() -> None:
    # None or unparseable defaults to True (peak)
    assert is_deepseek_peak_utc(None) is True
    assert is_deepseek_peak_utc("invalid-date") is True

    # Monday 01:30 UTC -> Peak (01:00 - 04:00)
    dt_mon_peak1 = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_mon_peak1) is True
    assert is_deepseek_peak_utc(dt_mon_peak1.isoformat()) is True
    assert is_deepseek_peak_utc(dt_mon_peak1.timestamp()) is True

    # Monday 07:00 UTC -> Peak (06:00 - 10:00)
    dt_mon_peak2 = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_mon_peak2) is True

    # Monday 04:30 UTC -> Off-peak
    dt_mon_off = datetime(2026, 9, 14, 4, 30, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_mon_off) is False
    assert is_deepseek_peak_utc(dt_mon_off.isoformat()) is False

    # Monday 11:00 UTC -> Off-peak
    dt_mon_off2 = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_mon_off2) is False

    # Saturday (weekend) 02:00 UTC -> Off-peak
    dt_sat = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_sat) is False

    # Sunday (weekend) 07:00 UTC -> Off-peak
    dt_sun = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
    assert is_deepseek_peak_utc(dt_sun) is False


def test_deepseek_peak_utc_edge_cases() -> None:
    # Monday 02:00 UTC (peak)
    dt_peak = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
    secs_peak = dt_peak.timestamp()

    # Monday 04:30 UTC (off-peak)
    dt_off = datetime(2026, 9, 14, 4, 30, tzinfo=timezone.utc)
    secs_off = dt_off.timestamp()

    # Microsecond timestamp (>1e14)
    assert is_deepseek_peak_utc(secs_peak * 1_000_000) is True
    assert is_deepseek_peak_utc(secs_off * 1_000_000) is False
    assert is_deepseek_peak_utc(f"{int(secs_off * 1_000_000)}") is False

    # Millisecond timestamp (>1e11)
    assert is_deepseek_peak_utc(secs_peak * 1_000) is True
    assert is_deepseek_peak_utc(secs_off * 1_000) is False
    assert is_deepseek_peak_utc(f"{int(secs_off * 1_000)}") is False

    # Numeric string timestamp
    assert is_deepseek_peak_utc(str(secs_peak)) is True
    assert is_deepseek_peak_utc(str(int(secs_peak))) is True
    assert is_deepseek_peak_utc(str(secs_off)) is False
    assert is_deepseek_peak_utc(str(int(secs_off))) is False

    # Invalid string timestamp returns True (peak)
    assert is_deepseek_peak_utc("not-a-timestamp") is True
    assert is_deepseek_peak_utc("") is True
    assert is_deepseek_peak_utc("2026-99-99T99:99:99Z") is True

    # Overflow timestamp returns True (peak) without crashing
    assert is_deepseek_peak_utc(1e30) is True
    assert is_deepseek_peak_utc(-1e30) is True
    assert is_deepseek_peak_utc("1e30") is True
    assert is_deepseek_peak_utc(float("inf")) is True
    assert is_deepseek_peak_utc(float("nan")) is True

    # Non-supported types return True
    assert is_deepseek_peak_utc([]) is True
    assert is_deepseek_peak_utc({}) is True


def test_deepseek_pricing_resolution_with_timestamp() -> None:
    dt_peak = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)
    dt_off = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)

    # Resolution during peak
    res_peak = resolve_pricing_strict("deepseek-v4-flash", provider="deepseek", timestamp=dt_peak)
    assert res_peak.status == "known"
    assert res_peak.pricing_tier == "peak"
    assert res_peak.rates == PricingRates(0.30, 0.006, 1.20)
    assert res_peak.as_dict()["pricing_tier"] == "peak"

    # Resolution during off-peak
    res_off = resolve_pricing_strict("deepseek-v4-flash", provider="deepseek", timestamp=dt_off)
    assert res_off.status == "known"
    assert res_off.pricing_tier == "off-peak"
    assert res_off.rates == PricingRates(0.15, 0.003, 0.60)
    assert res_off.as_dict()["pricing_tier"] == "off-peak"

    # Default (timestamp=None) -> peak
    res_default = resolve_pricing_strict("deepseek-v4-flash", provider="deepseek")
    assert res_default.status == "known"
    assert res_default.pricing_tier == "peak"
    assert res_default.rates == PricingRates(0.30, 0.006, 1.20)

    # deepseek-flash registered in MODEL_PRICING
    res_flash = resolve_pricing_strict("deepseek-flash", provider="deepseek", timestamp=dt_off)
    assert res_flash.canonical_model == "deepseek-flash"
    assert res_flash.pricing_tier == "off-peak"
    assert res_flash.rates == PricingRates(0.15, 0.003, 0.60)

    # Alias deepseek-chat resolves to deepseek-v4-flash
    res_chat = resolve_pricing_strict("deepseek-chat", provider="deepseek", timestamp=dt_off)
    assert res_chat.canonical_model == "deepseek-v4-flash"
    assert res_chat.pricing_tier == "off-peak"
    assert res_chat.rates == PricingRates(0.15, 0.003, 0.60)

    # Non-deepseek model has pricing_tier=None
    res_luna = resolve_pricing_strict("gpt-5.6-luna", timestamp=dt_off)
    assert res_luna.pricing_tier is None
    assert "pricing_tier" not in res_luna.as_dict()


def test_deepseek_cost_calculation_with_timestamp() -> None:
    dt_off = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)
    cost = calculate_cost("deepseek-v4-flash", 1_000_000, 1_000_000, 1_000_000, timestamp=dt_off)
    # Off-peak: 0.15 + 0.003 + 0.60 = 0.753
    assert cost["cost_cached_usd"] == 0.753

    strict_cost = calculate_cost_strict(
        "deepseek-v4-flash", 1_000_000, 1_000_000, 1_000_000, timestamp=dt_off
    )
    assert strict_cost["pricing_tier"] == "off-peak"
    assert strict_cost["cost_cached_usd"] == 0.753


def test_claude_parser_usage_event_peak_and_off_peak() -> None:
    from src.parsers.claude import _usage_event

    usage_data = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }

    # Peak: Wednesday 02:00 UTC
    record_peak = {"timestamp": "2026-09-16T02:00:00Z"}
    event_peak = _usage_event(record_peak, usage_data, "deepseek-v4-flash", 1)
    assert event_peak is not None
    assert event_peak.cost is not None
    assert float(event_peak.cost.cached_usd) == 1.506
    assert float(event_peak.cost.uncached_usd) == 1.80
    assert float(event_peak.cost.savings_usd) == 0.294

    # Off-peak: Wednesday 14:00 UTC
    record_off = {"timestamp": "2026-09-16T14:00:00Z"}
    event_off = _usage_event(record_off, usage_data, "deepseek-v4-flash", 1)
    assert event_off is not None
    assert event_off.cost is not None
    assert float(event_off.cost.cached_usd) == 0.753
    assert float(event_off.cost.uncached_usd) == 0.90
    assert float(event_off.cost.savings_usd) == 0.147

    # Off-peak: Saturday 02:00 UTC (weekend)
    record_weekend = {"timestamp": "2026-09-19T02:00:00Z"}
    event_weekend = _usage_event(record_weekend, usage_data, "deepseek-v4-flash", 1)
    assert event_weekend is not None
    assert event_weekend.cost is not None
    assert float(event_weekend.cost.cached_usd) == 0.753


def test_aggregator_event_cost_peak_and_off_peak() -> None:
    from src.parsers.aggregator import _event_cost

    # Peak event
    event_peak = {
        "model": "deepseek-v4-flash",
        "timestamp": "2026-09-16T02:00:00Z",
    }
    cost_peak = _event_cost({}, event_peak, 1_000_000, 1_000_000, 1_000_000)
    assert cost_peak["cost_cached_usd"] == 1.506
    assert cost_peak["cost_uncached_usd"] == 1.80
    assert cost_peak["savings_usd"] == 0.294

    # Off-peak event
    event_off = {
        "model": "deepseek-v4-flash",
        "timestamp": "2026-09-16T14:00:00Z",
    }
    cost_off = _event_cost({}, event_off, 1_000_000, 1_000_000, 1_000_000)
    assert cost_off["cost_cached_usd"] == 0.753
    assert cost_off["cost_uncached_usd"] == 0.90
    assert cost_off["savings_usd"] == 0.147


def test_aggregator_refresh_estimated_session_cost_peak_and_off_peak() -> None:
    from src.parsers.aggregator import _refresh_estimated_session_cost
    from src.parsers.contracts import TokenUsage, UsageEvent, UsageSession

    # Peak session and event
    session_peak = UsageSession(
        id="session-peak",
        tool="claude",
        model="deepseek-v4-flash",
        created_at=datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc),
        usage=TokenUsage(
            input_tokens=2_000_000,
            cached_input_tokens=1_000_000,
            output_tokens=1_000_000,
        ),
        events=[
            UsageEvent(
                timestamp=datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc),
                model="deepseek-v4-flash",
                usage=TokenUsage(
                    input_tokens=2_000_000,
                    cached_input_tokens=1_000_000,
                    output_tokens=1_000_000,
                ),
            )
        ],
    )
    _refresh_estimated_session_cost(session_peak)
    assert session_peak.cost is not None
    assert float(session_peak.cost.cached_usd) == 1.506
    assert float(session_peak.cost.uncached_usd) == 1.80
    assert session_peak.events[0].cost is not None
    assert float(session_peak.events[0].cost.cached_usd) == 1.506

    # Off-peak session and event
    session_off = UsageSession(
        id="session-off",
        tool="claude",
        model="deepseek-v4-flash",
        created_at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
        usage=TokenUsage(
            input_tokens=2_000_000,
            cached_input_tokens=1_000_000,
            output_tokens=1_000_000,
        ),
        events=[
            UsageEvent(
                timestamp=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
                model="deepseek-v4-flash",
                usage=TokenUsage(
                    input_tokens=2_000_000,
                    cached_input_tokens=1_000_000,
                    output_tokens=1_000_000,
                ),
            )
        ],
    )
    _refresh_estimated_session_cost(session_off)
    assert session_off.cost is not None
    assert float(session_off.cost.cached_usd) == 0.753
    assert float(session_off.cost.uncached_usd) == 0.90
    assert session_off.events[0].cost is not None
    assert float(session_off.events[0].cost.cached_usd) == 0.753


def test_to_utc_datetime_and_timezone_resolution() -> None:
    # 1. Test aware datetime with explicit timezone offset (+05:30 IST and -04:00 EDT)
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    # Wednesday 11:30 IST = 06:00 UTC -> Peak (06:00 - 10:00 UTC)
    dt_ist_peak = datetime(2026, 9, 16, 11, 30, tzinfo=tz_ist)
    assert is_deepseek_peak_utc(dt_ist_peak) is True
    assert is_deepseek_peak_utc("2026-09-16T11:30:00+05:30") is True

    # Wednesday 09:30 IST = 04:00 UTC -> Off-peak
    dt_ist_off = datetime(2026, 9, 16, 9, 30, tzinfo=tz_ist)
    assert is_deepseek_peak_utc(dt_ist_off) is False
    assert is_deepseek_peak_utc("2026-09-16T09:30:00+05:30") is False

    # EDT (-04:00)
    tz_edt = timezone(timedelta(hours=-4))
    # Wednesday 02:30 EDT = 06:30 UTC -> Peak
    dt_edt_peak = datetime(2026, 9, 16, 2, 30, tzinfo=tz_edt)
    assert is_deepseek_peak_utc(dt_edt_peak) is True
    assert is_deepseek_peak_utc("2026-09-16T02:30:00-04:00") is True

    # Wednesday 00:30 EDT = 04:30 UTC -> Off-peak
    dt_edt_off = datetime(2026, 9, 16, 0, 30, tzinfo=tz_edt)
    assert is_deepseek_peak_utc(dt_edt_off) is False
    assert is_deepseek_peak_utc("2026-09-16T00:30:00-04:00") is False

    # 2. Test to_utc_datetime resolves naive datetime and naive ISO string using system local time
    local_tz = datetime.now().astimezone().tzinfo
    naive_dt = datetime(2026, 9, 16, 12, 0, 0)
    expected_utc = naive_dt.replace(tzinfo=local_tz).astimezone(timezone.utc)

    utc_res = to_utc_datetime(naive_dt)
    assert utc_res is not None
    assert utc_res == expected_utc
    assert utc_res.tzinfo == timezone.utc

    utc_iso_res = to_utc_datetime("2026-09-16T12:00:00")
    assert utc_iso_res is not None
    assert utc_iso_res == expected_utc
    assert utc_iso_res.tzinfo == timezone.utc

    # 3. Dynamic test: naive datetime corresponding to peak and off-peak UTC
    target_utc_peak = datetime(2026, 9, 16, 6, 30, tzinfo=timezone.utc)
    local_peak = target_utc_peak.astimezone(local_tz)
    naive_local_peak = datetime(
        local_peak.year, local_peak.month, local_peak.day,
        local_peak.hour, local_peak.minute, local_peak.second
    )
    assert is_deepseek_peak_utc(naive_local_peak) is True
    assert is_deepseek_peak_utc(naive_local_peak.isoformat()) is True

    target_utc_off = datetime(2026, 9, 16, 4, 30, tzinfo=timezone.utc)
    local_off = target_utc_off.astimezone(local_tz)
    naive_local_off = datetime(
        local_off.year, local_off.month, local_off.day,
        local_off.hour, local_off.minute, local_off.second
    )
    assert is_deepseek_peak_utc(naive_local_off) is False
    assert is_deepseek_peak_utc(naive_local_off.isoformat()) is False


def test_contracts_timestamp_naive_interpreted_as_local() -> None:
    from src.parsers.contracts import _timestamp
    local_tz = datetime.now().astimezone().tzinfo
    naive_dt = datetime(2026, 9, 16, 12, 0, 0)
    expected_utc = naive_dt.replace(tzinfo=local_tz).astimezone(timezone.utc)

    assert _timestamp(naive_dt) == expected_utc
    assert _timestamp("2026-09-16T12:00:00") == expected_utc


