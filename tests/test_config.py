import datetime as dt
from decimal import Decimal

import pytest

from eddie import config
from eddie.config import ConfigError, load_settings

TODAY = dt.date(2026, 11, 24)


def test_safe_defaults():
    s = load_settings({}, today=TODAY)
    assert s.backend == "mock"
    assert s.allow_live_ember is False
    assert s.lifetime_cap_nano == 8_000_000_000
    assert s.run_cap_nano == 100_000_000
    assert s.daily_cap_nano == 2_000_000_000
    assert s.max_concurrency == 4


@pytest.mark.parametrize("bad", ["50", "9.01", "nan", "NaN", "inf", "-1", "abc", ""])
def test_lifetime_cap_rejects_unsafe_values(bad):
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_LIFETIME_CAP_USD": bad}, today=TODAY)


def test_lifetime_cap_can_be_lowered():
    s = load_settings({"EDDIE_LIFETIME_CAP_USD": "3.50"}, today=TODAY)
    assert s.lifetime_cap_nano == 3_500_000_000


def test_run_cap_above_ten_cents_requires_dated_confirmation():
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_RUN_CAP_USD": "0.50"}, today=TODAY)
    ok = load_settings(
        {"EDDIE_RUN_CAP_USD": "0.50", "EDDIE_RUN_CAP_CONFIRM": "0.50:2026-11-24"}, today=TODAY
    )
    assert ok.run_cap_nano == 500_000_000


def test_run_cap_confirmation_expires_next_day():
    env = {"EDDIE_RUN_CAP_USD": "0.50", "EDDIE_RUN_CAP_CONFIRM": "0.50:2026-11-24"}
    with pytest.raises(ConfigError):
        load_settings(env, today=TODAY + dt.timedelta(days=1))


def test_run_cap_confirmation_must_match_amount():
    env = {"EDDIE_RUN_CAP_USD": "0.90", "EDDIE_RUN_CAP_CONFIRM": "0.50:2026-11-24"}
    with pytest.raises(ConfigError):
        load_settings(env, today=TODAY)


def test_run_cap_hard_ceiling_even_when_confirmed():
    env = {"EDDIE_RUN_CAP_USD": "2.50", "EDDIE_RUN_CAP_CONFIRM": "2.50:2026-11-24"}
    with pytest.raises(ConfigError):
        load_settings(env, today=TODAY)


def test_cli_run_cap_needs_confirmation_flag():
    with pytest.raises(ConfigError):
        load_settings({}, run_cap_usd=Decimal("0.40"), today=TODAY)
    s = load_settings({}, run_cap_usd=Decimal("0.40"), run_cap_confirmed=True, today=TODAY)
    assert s.run_cap_nano == 400_000_000


def test_daily_cap_raise_needs_confirmation():
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_DAILY_CAP_USD": "3.00"}, today=TODAY)
    s = load_settings(
        {"EDDIE_DAILY_CAP_USD": "3.00", "EDDIE_DAILY_CAP_CONFIRM": "3.00:2026-11-24"}, today=TODAY
    )
    assert s.daily_cap_nano == 3_000_000_000


def test_live_flag_must_be_exactly_true():
    assert load_settings({"EDDIE_ALLOW_LIVE_EMBER": "1"}, today=TODAY).allow_live_ember is False
    assert load_settings({"EDDIE_ALLOW_LIVE_EMBER": "yes"}, today=TODAY).allow_live_ember is False
    assert load_settings({"EDDIE_ALLOW_LIVE_EMBER": "true"}, today=TODAY).allow_live_ember is True


def test_backend_validation_and_concurrency_bounds():
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_BACKEND": "openai"}, today=TODAY)
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_MAX_CONCURRENCY": "0"}, today=TODAY)
    with pytest.raises(ConfigError):
        load_settings({"EDDIE_MAX_CONCURRENCY": "99"}, today=TODAY)


def test_paths_follow_redirected_home(offline_sandboxed_env):
    s = load_settings({}, today=TODAY)
    assert str(s.ledger_path).startswith(str(offline_sandboxed_env))
    assert str(s.anchor_path).startswith(str(offline_sandboxed_env))
    assert s.ledger_path.name == "ledger.sqlite"


def test_constants_are_consistent():
    assert config.CONTEST_HARD_CEILING_USD < config.CONTEST_DISQUALIFICATION_USD
    assert config.DEFAULT_LIFETIME_CAP_USD <= config.CONTEST_HARD_CEILING_USD
    assert config.HARD_INPUT_TOKEN_LIMIT < config.TIER_BOUNDARY_INPUT_TOKENS
    assert config.RESERVATION_TTL_S > config.HTTP_TOTAL_TIMEOUT_S
