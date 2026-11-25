"""Metabolic Spend Governor: caps, live guards, reservations and settlement policy.

The governor wraps one ledger:
* billable=True: the canonical Ember ledger (caps, breaker, anchor, snapshots)
* billable=False: the shadow ledger for mock/dgx calls (records what Ember WOULD have
  charged, never touches real caps)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eddie.config import CONTEXT_WINDOW_TOKENS, EMBER_MODEL, NANO_PER_USD, Settings
from eddie.llm import pricing
from eddie.llm.errors import (
    KillSwitchError,
    LedgerIntegrityError,
    LiveEmberDisabledError,
    UnboundedInputError,
)
from eddie.llm.estimate import Estimate
from eddie.llm.ledger import Caps, Ledger
from eddie.llm.usage import UsageCheck, evaluate_usage

SAFE_FILESYSTEMS = frozenset({"ext4", "ext3", "xfs", "btrfs", "tmpfs", "zfs", "f2fs"})
_SNAPSHOT_LOCK = threading.Lock()


# --- environment probes -------------------------------------------------------------------


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def _root_fs_type() -> str:
    return filesystem_type(Path("/"))


def running_in_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        cgroup = ""
    if any(word in cgroup for word in ("docker", "kubepods", "containerd", "libpod")):
        return True
    return _root_fs_type() == "overlay"


def _unescape_mount(path: str) -> str:
    return path.encode().decode("unicode_escape") if "\\" in path else path


def filesystem_type(path: Path) -> str:
    """Filesystem type of the mount holding `path`, from /proc/self/mountinfo."""
    target = os.path.realpath(path if path.exists() else path.parent)
    best_mount, best_type = "", "unknown"
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return "unknown"
    for line in lines:
        parts = line.split()
        if "-" not in parts:
            continue
        sep = parts.index("-")
        mount_point = _unescape_mount(parts[4])
        fs_type = parts[sep + 1]
        prefix = mount_point.rstrip("/") + "/"
        if (target == mount_point or target.startswith(prefix) or mount_point == "/") and len(
            mount_point
        ) >= len(best_mount):
            best_mount, best_type = mount_point, fs_type
    return best_type


# --- reservations ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    call_id: str
    backend: str
    billable: bool
    region: str
    effort: str
    max_output: int
    est_input_bound: int
    bounded: bool
    reserved_nano: int
    w_nano: int
    body_sha256: str


@dataclass(frozen=True)
class Settlement:
    state: str  # settled | orphan_charged | released
    charged_nano: int
    shadow_nano: int
    check: UsageCheck | None
    tripped: str | None = None


class SpendGovernor:
    def __init__(self, settings: Settings, *, billable: bool, ledger: Ledger | None = None) -> None:
        self.settings = settings
        self.billable = billable
        path = settings.ledger_path if billable else settings.shadow_ledger_path
        self.ledger = ledger or Ledger(path, billable=billable)
        self.run_id = uuid.uuid4().hex
        self.caps = Caps(
            lifetime_nano=settings.lifetime_cap_nano,
            daily_nano=settings.daily_cap_nano,
            run_nano=settings.run_cap_nano,
            max_concurrency=settings.max_concurrency,
        )
        self.ledger.register_run(self.run_id, settings.run_cap_nano)

    # --- live guards ------------------------------------------------------------------------------

    def assert_live_allowed(self) -> None:
        """Every condition for a paid call. Raises before any network object exists."""
        s = self.settings
        if not self.billable:
            raise LiveEmberDisabledError("this governor is not attached to the billable ledger")
        if s.backend != "ember":
            raise LiveEmberDisabledError("EDDIE_BACKEND is not 'ember'")
        if not s.allow_live_ember:
            raise LiveEmberDisabledError("EDDIE_ALLOW_LIVE_EMBER is not 'true'")
        if running_under_pytest():
            raise LiveEmberDisabledError("live Ember calls are impossible under pytest")
        if running_in_container():
            raise LiveEmberDisabledError("live Ember calls run only from the host CLI, never in a container")
        fs = filesystem_type(self.ledger.path)
        if fs not in SAFE_FILESYSTEMS:
            raise LedgerIntegrityError(f"ledger is on a {fs!r} filesystem; SQLite locking is only trusted on local disks")
        self.ledger.verify_anchor(s.anchor_path)

    # --- reserve / dispatch -----------------------------------------------------------------------

    def reserve(
        self,
        *,
        backend: str,
        region: str,
        effort: str,
        max_output: int,
        estimate: Estimate,
        body_sha256: str,
        allow_unbounded: bool,
        tag: str | None = None,
        model: str = EMBER_MODEL,
    ) -> Reservation:
        w = pricing.worst_case_w_nano(max_output)
        if estimate.bounded:
            reserved = pricing.reserve_nano(estimate.input_tokens, max_output)
            est_bound = estimate.input_tokens
        else:
            if not allow_unbounded:
                raise UnboundedInputError(
                    "request carries parts that cannot be bounded locally: "
                    + "; ".join(estimate.unbounded)
                    + " (set allow_unbounded=True to reserve the worst case W(m))"
                )
            reserved = w
            est_bound = CONTEXT_WINDOW_TOKENS - max_output
        call_id = self.ledger.reserve(
            run_id=self.run_id,
            backend=backend,
            model=model,
            region=region,
            effort=effort,
            tag=tag,
            body_sha256=body_sha256,
            est_input=est_bound,
            max_output=max_output,
            bounded=estimate.bounded,
            reserved_nano=reserved,
            w_nano=w,
            caps=self.caps if self.billable else None,
            kill_switch=self.settings.kill_switch_path if self.billable else None,
        )
        return Reservation(
            call_id=call_id,
            backend=backend,
            billable=self.billable,
            region=region,
            effort=effort,
            max_output=max_output,
            est_input_bound=est_bound,
            bounded=estimate.bounded,
            reserved_nano=reserved,
            w_nano=w,
            body_sha256=body_sha256,
        )

    def dispatch(self, res: Reservation) -> None:
        if self.billable and self.settings.kill_switch_path.exists():
            self.ledger.release(res.call_id, http_status=None, error="kill switch present at dispatch")
            raise KillSwitchError("kill switch present at dispatch")
        self.ledger.mark_dispatched(res.call_id)

    # --- settlement -------------------------------------------------------------------------------

    def settle_response(
        self,
        res: Reservation,
        response: dict[str, Any] | None,
        *,
        http_status: int | None,
        latency_ms: int,
    ) -> Settlement:
        usage_raw = response.get("usage") if isinstance(response, dict) else None
        check = evaluate_usage(
            usage_raw,
            est_input_tokens=res.est_input_bound,
            max_output_tokens=res.max_output,
            reserved_nano=res.reserved_nano,
        )
        response_id = response.get("id") if isinstance(response, dict) else None
        status = response.get("status") if isinstance(response, dict) else None

        if check.parse_error is not None:
            error = f"2xx without usable usage: {check.parse_error}"
            self.ledger.orphan_charge(res.call_id, http_status=http_status, error=error)
            tripped = None
            if check.hard and self.billable:
                tripped = "usage anomaly: " + "; ".join(check.hard)
                self.ledger.trip(tripped)
            self._after_change()
            charged = res.reserved_nano if self.billable else 0
            return Settlement("orphan_charged", charged, 0, check, tripped)

        cost = check.cost_nano or 0
        tripped = None
        charge = cost
        if check.hard:
            charge = max(res.reserved_nano, cost)
            tripped = "usage anomaly: " + "; ".join(check.hard)
        usage = check.usage
        self.ledger.settle(
            res.call_id,
            actual_nano=charge if self.billable else 0,
            shadow_nano=cost,
            input_tokens=max(usage.input_tokens, 0),
            cached_tokens=max(usage.cached_tokens, 0),
            output_tokens=max(usage.output_tokens, 0),
            reasoning_tokens=max(usage.reasoning_tokens, 0),
            response_id=response_id,
            status=status,
            http_status=http_status,
            latency_ms=latency_ms,
            anomalies=list(check.hard),
            warnings=list(check.warnings),
            trip_reason=tripped if self.billable else None,
        )
        self._after_change()
        return Settlement("settled", charge if self.billable else 0, cost, check, tripped if self.billable else None)

    def settle_failure(self, res: Reservation, *, http_status: int | None, error: str, kind: str) -> Settlement:
        """kind: never_sent | rate_limited | auth | billing | ambiguous."""
        if kind in ("never_sent", "rate_limited"):
            self.ledger.release(res.call_id, http_status=http_status, error=error)
            state, charged, tripped = "released", 0, None
        elif kind in ("auth", "billing"):
            tripped = f"HTTP {http_status} ({kind}): {error[:200]}"
            self.ledger.release(
                res.call_id, http_status=http_status, error=error, trip_reason=tripped if self.billable else None
            )
            state, charged = "released", 0
        elif kind == "ambiguous":
            self.ledger.orphan_charge(res.call_id, http_status=http_status, error=error)
            state, charged, tripped = "orphan_charged", res.reserved_nano if self.billable else 0, None
        else:
            raise ValueError(f"unknown failure kind {kind!r}")
        self._after_change()
        return Settlement(state, charged, 0, None, tripped if self.billable else None)

    # --- snapshots ---------------------------------------------------------------------------------

    def _after_change(self) -> None:
        if not self.billable:
            return
        self.ledger.raise_high_water(self.settings.anchor_path)
        self.write_snapshot()

    def effective_lifetime_cap_nano(self, totals: dict[str, Any] | None = None) -> int:
        totals = totals or self.ledger.totals()
        persisted = totals.get("persisted_lifetime_cap_nano")
        return self.caps.lifetime_nano if persisted is None else min(self.caps.lifetime_nano, persisted)

    def status(self) -> dict[str, Any]:
        totals = self.ledger.totals()
        lifetime = self.effective_lifetime_cap_nano(totals)
        committed = totals["committed_nano"]
        headroom = lifetime - committed - totals["inflight_worst_case_nano"]
        breaker = self.ledger.breaker()
        recent = []
        for row in self.ledger.recent_calls(20):
            recent.append(
                {
                    "created_at": _iso(row["created_at"]),
                    "region": row["region"],
                    "effort": row["effort"],
                    "state": row["state"],
                    "input_tokens": row["input_tokens"],
                    "cached_tokens": row["cached_tokens"],
                    "output_tokens": row["output_tokens"],
                    "reasoning_tokens": row["reasoning_tokens"],
                    "cost_usd": _usd(row["actual_nano"] or 0),
                    "shadow_usd": _usd(row["shadow_nano"] or 0),
                }
            )
        return {
            "generated_at": _iso(time.time()),
            "billable": self.billable,
            "model": EMBER_MODEL,
            "lifetime_cap_usd": _usd(lifetime),
            "committed_usd": _usd(committed),
            "adjustments_usd": _usd(totals["adjustments_nano"]),
            "inflight_reserved_usd": _usd(totals["inflight_reserved_nano"]),
            "inflight_worst_case_usd": _usd(totals["inflight_worst_case_nano"]),
            "headroom_usd": _usd(max(headroom, 0)),
            "daily_cap_usd": _usd(self.caps.daily_nano),
            "daily_committed_usd": _usd(totals["daily_committed_nano"]),
            "run_cap_usd": _usd(self.caps.run_nano),
            "shadow_usd": _usd(totals["shadow_nano"]),
            "calls_by_state": totals["calls_by_state"],
            "anomalous_calls": totals["anomalous_calls"],
            "breaker": {"state": breaker["state"], "reason": breaker["reason"]},
            "kill_switch": self.settings.kill_switch_path.exists(),
            "recent": recent,
        }

    def write_snapshot(self) -> Path:
        public = self.settings.public_dir
        public.mkdir(parents=True, exist_ok=True)
        target = public / "status.json"
        with _SNAPSHOT_LOCK:
            tmp = public / f".status.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w") as fh:
                json.dump(self.status(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        return target


def _usd(nano: int) -> float:
    return round(nano / NANO_PER_USD, 9)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat(timespec="milliseconds")
