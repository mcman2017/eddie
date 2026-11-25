"""Runtime settings, plus the budget constants that guard the contest's $10 line.

Every tunable comes from the environment with a safe default. The constants in the first
block are code-level ceilings that configuration can only tighten, never loosen.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from eddie import paths

NANO_PER_USD = 1_000_000_000

# --- Contest guard constants (configuration can never loosen these) -----------------
CONTEST_DISQUALIFICATION_USD = Decimal("10.00")
CONTEST_HARD_CEILING_USD = Decimal("9.00")  # no lifetime cap may ever exceed this
DEFAULT_LIFETIME_CAP_USD = Decimal("8.00")
DEFAULT_RUN_CAP_USD = Decimal("0.10")
RUN_CAP_UNCONFIRMED_MAX_USD = Decimal("0.10")  # above this a dated confirmation is required
RUN_CAP_HARD_CEILING_USD = Decimal("2.00")
DEFAULT_DAILY_CAP_USD = Decimal("2.00")  # rolling 24 h

HARD_INPUT_TOKEN_LIMIT = 190_000  # refuse before I/O; tier 2 (3x price) starts above 200K
TIER_BOUNDARY_INPUT_TOKENS = 200_000
CONTEXT_WINDOW_TOKENS = 1_048_576
MIN_MAX_OUTPUT_TOKENS = 16

DEFAULT_MAX_CONCURRENCY = 4
MAX_CONCURRENCY_CEILING = 16
RESERVATION_TTL_S = 900  # must stay above HTTP_TOTAL_TIMEOUT_S
HTTP_TOTAL_TIMEOUT_S = 600
CONSECUTIVE_FAILURES_TO_TRIP = 5

EMBER_MODEL = "muse-ember-1.0"
BACKENDS = ("mock", "dgx", "ember")
DEFAULT_DGX_MODEL = "gemma4-prod:20260910"

assert RESERVATION_TTL_S > HTTP_TOTAL_TIMEOUT_S


class ConfigError(ValueError):
    """Invalid or unsafe configuration. Always raised before any network I/O."""


# --- Money helpers ------------------------------------------------------------------


def parse_usd(raw: str | Decimal | int, *, name: str = "amount") -> Decimal:
    """Parse a dollar amount strictly: finite, non-negative, at most 9 decimals kept."""
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip().lstrip("$"))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{name}: not a dollar amount: {raw!r}") from exc
    if not value.is_finite():
        raise ConfigError(f"{name}: must be finite, got {raw!r}")
    if value < 0:
        raise ConfigError(f"{name}: must be >= 0, got {raw!r}")
    return value.quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)


def usd_to_nano(value: Decimal) -> int:
    return int((value * NANO_PER_USD).to_integral_value(rounding=ROUND_DOWN))


def nano_to_usd(nano: int) -> Decimal:
    return Decimal(nano) / NANO_PER_USD


def format_usd(nano: int, places: int = 6) -> str:
    return f"${nano_to_usd(nano):.{places}f}"


def check_dated_confirmation(
    value: Decimal, token: str | None, *, today: _dt.date | None = None
) -> bool:
    """A confirmation token is '<amount>:<YYYY-MM-DD>' and is valid only on that local date.

    Dating the token means a value pasted into an env file once cannot silently keep
    raising caps on later days.
    """
    if not token:
        return False
    amount, sep, date_s = token.strip().rpartition(":")
    if not sep:
        return False
    try:
        confirmed = parse_usd(amount, name="confirmation")
    except ConfigError:
        return False
    today = today or _dt.date.today()
    return confirmed == value and date_s == today.isoformat()


# --- Settings -----------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    backend: str
    allow_live_ember: bool
    lifetime_cap_nano: int  # requested; the ledger's persisted cap wins if it is lower
    run_cap_nano: int
    daily_cap_nano: int
    max_concurrency: int
    ledger_path: Path
    anchor_path: Path
    public_dir: Path
    kill_switch_path: Path
    key_file: Path
    shadow_ledger_path: Path
    data_dir: Path
    index_path: Path
    dgx_text_base_url: str | None
    dgx_text_model: str
    dgx_vision_base_url: str | None
    dgx_vision_model: str
    dgx_api_key_file: Path | None


def _path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = env.get(key)
    return Path(raw).expanduser() if raw else default


def resolve_run_cap(
    env: Mapping[str, str],
    *,
    cli_value: Decimal | None = None,
    cli_confirmed: bool = False,
    today: _dt.date | None = None,
) -> Decimal:
    if cli_value is not None:
        value = parse_usd(cli_value, name="--run-cap-usd")
        if value > RUN_CAP_UNCONFIRMED_MAX_USD and not cli_confirmed:
            raise ConfigError(
                f"run cap ${value} is above ${RUN_CAP_UNCONFIRMED_MAX_USD}: confirm it "
                "interactively or pass --confirm-run-cap <amount>:<YYYY-MM-DD>"
            )
    else:
        value = parse_usd(env.get("EDDIE_RUN_CAP_USD", DEFAULT_RUN_CAP_USD), name="EDDIE_RUN_CAP_USD")
        if value > RUN_CAP_UNCONFIRMED_MAX_USD and not check_dated_confirmation(
            value, env.get("EDDIE_RUN_CAP_CONFIRM"), today=today
        ):
            raise ConfigError(
                f"EDDIE_RUN_CAP_USD=${value} needs EDDIE_RUN_CAP_CONFIRM={value}:<today's date>"
            )
    if value > RUN_CAP_HARD_CEILING_USD:
        raise ConfigError(f"run cap ${value} exceeds the code ceiling ${RUN_CAP_HARD_CEILING_USD}")
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    run_cap_usd: Decimal | None = None,
    run_cap_confirmed: bool = False,
    today: _dt.date | None = None,
) -> Settings:
    env = os.environ if env is None else env

    backend = env.get("EDDIE_BACKEND", "mock").strip().lower()
    if backend not in BACKENDS:
        raise ConfigError(f"EDDIE_BACKEND must be one of {BACKENDS}, got {backend!r}")

    lifetime = parse_usd(
        env.get("EDDIE_LIFETIME_CAP_USD", DEFAULT_LIFETIME_CAP_USD), name="EDDIE_LIFETIME_CAP_USD"
    )
    if lifetime > CONTEST_HARD_CEILING_USD:
        raise ConfigError(
            f"EDDIE_LIFETIME_CAP_USD=${lifetime} exceeds the contest ceiling ${CONTEST_HARD_CEILING_USD}"
        )

    daily = parse_usd(env.get("EDDIE_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD), name="EDDIE_DAILY_CAP_USD")
    if daily > DEFAULT_DAILY_CAP_USD and not check_dated_confirmation(
        daily, env.get("EDDIE_DAILY_CAP_CONFIRM"), today=today
    ):
        raise ConfigError(
            f"EDDIE_DAILY_CAP_USD=${daily} needs EDDIE_DAILY_CAP_CONFIRM={daily}:<today's date>"
        )
    if daily > CONTEST_HARD_CEILING_USD:
        raise ConfigError(f"daily cap ${daily} exceeds the contest ceiling ${CONTEST_HARD_CEILING_USD}")

    run = resolve_run_cap(env, cli_value=run_cap_usd, cli_confirmed=run_cap_confirmed, today=today)

    try:
        concurrency = int(env.get("EDDIE_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY))
    except ValueError as exc:
        raise ConfigError("EDDIE_MAX_CONCURRENCY must be an integer") from exc
    if not 1 <= concurrency <= MAX_CONCURRENCY_CEILING:
        raise ConfigError(f"EDDIE_MAX_CONCURRENCY must be within 1..{MAX_CONCURRENCY_CEILING}")

    data_dir = _path(env, "EDDIE_DATA_DIR", paths.data_dir())
    dgx_key = env.get("DGX_API_KEY_FILE")

    return Settings(
        backend=backend,
        allow_live_ember=env.get("EDDIE_ALLOW_LIVE_EMBER", "").strip().lower() == "true",
        lifetime_cap_nano=usd_to_nano(lifetime),
        run_cap_nano=usd_to_nano(run),
        daily_cap_nano=usd_to_nano(daily),
        max_concurrency=concurrency,
        ledger_path=_path(env, "EDDIE_LEDGER_PATH", paths.default_ledger_path()),
        anchor_path=paths.default_anchor_path(),  # deliberately not env-overridable
        public_dir=_path(env, "EDDIE_PUBLIC_DIR", paths.default_public_dir()),
        kill_switch_path=paths.default_kill_switch_path(),
        key_file=_path(env, "META_API_KEY_FILE", paths.default_key_file()),
        shadow_ledger_path=_path(env, "EDDIE_SHADOW_LEDGER_PATH", data_dir / "shadow.sqlite"),
        data_dir=data_dir,
        index_path=_path(env, "EDDIE_INDEX_PATH", data_dir / "index" / "eddie_index.sqlite"),
        dgx_text_base_url=env.get("DGX_TEXT_BASE_URL") or None,
        dgx_text_model=env.get("DGX_TEXT_MODEL") or DEFAULT_DGX_MODEL,
        dgx_vision_base_url=env.get("DGX_VISION_BASE_URL") or None,
        dgx_vision_model=env.get("DGX_VISION_MODEL") or DEFAULT_DGX_MODEL,
        dgx_api_key_file=Path(dgx_key).expanduser() if dgx_key else None,
    )
