"""$0 precomputation for the 3D critical strip: `python -m eddie.cli build-landscape`.

Two surfaces over the box 0 <= sigma <= 1, 0 <= t <= T, plus everything the explorer needs to
draw the proof on top of them. Nothing here calls a model, and the API never imports this.

1. The Riemann zeta function on the grid (mpmath, double precision, checked against its
   functional equation), with its zeros on sigma = 1/2 computed separately to 15 digits.

2. Z(s) = sum_m w_m exp(-s l_m): a finite Dirichlet polynomial of Gödel-coded structural
   signatures, once with the live declarations as weights and once with the dead ones.

   Every module gets an exponent tuple x(m) and the integer N(m) = prod p_k^x_k:
       2^bucket(height) * 3^bucket(fan_in) * 5^bucket(fan_out)
       * q_r^2 for its own pillar (chapter rank r), q_j^1 for every other pillar it imports,
       * a reserved prime squared when it belongs to no pillar,
   with bucket(v) = floor(log2(1 + v)) and q_r the (4 + r)-th prime. Unique factorization
   makes the code injective on tuples, so modules with equal tuples form exact classes.
   l = kappa * ln N / L, where L is the live mean of ln N and kappa is the scale at which a
   Gaussian random series with the live mass profile would have as many expected zeros in
   the box as zeta does (Kac-Rice / Edelman-Kostlan: zero density Var(l) / pi under the
   squared, sigma-tilted weights). That only chooses which window of the raw series
   D(u) = sum_m w_m N(m)^-u is viewed: Z(s) = D(kappa * s / L).

   What this is and is not. The code is blind to names (renaming declarations changes
   nothing) but not to layout: pillars are a directory partition, and which prime codes
   which feature is a convention. Z has no functional equation, no Euler product and no
   distinguished line; sigma = 1/2 is drawn only to share zeta's frame. Where a zero or a
   dip sits depends on the convention, so `findings.json` reports statistics that hold
   across an ensemble of encodings and against random surrogates, and says so when a
   statement does not. Bottlenecks of the proof are measured by dominators, not by Z.

3. A layout of the proof's chapters on the strip (a map projection, not a computation), the
   net reference flows between them, and the dead declarations as foothills outside it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import mpmath
import numpy as np
from scipy import ndimage, optimize, signal, special

from eddie.planner import pillars as pl

SCHEMA = "eddie-landscape/1"
ENCODING = "godel-signature/1"
CONE_VERSION = "2"
SEED = 20261125
NEAR_LINE = 0.05  # "near sigma = 1/2" for the scatter statistic
T_ALIGN = 12.0  # the alignment test starts here (the density of zeta zeros is positive beyond 2 pi)
POPULATIONS = ("live", "dead")


@dataclass(frozen=True)
class Params:
    sigma_steps: int = 81
    t_steps: int = 481
    t_max: float = 60.0
    bands: int = 8
    surrogates: int = 199  # torus surrogates of the window
    offsets: int = 12  # order-preserving prime offsets (the first pillar prime runs 7..47; 7 is the encoding shown)
    permutations: int = 8  # seeded permutations of the pillar primes
    subsamples: int = 200  # size-matched live subsamples
    omega_max: float = 240.0
    omega_steps: int = 2401
    arcs: int = 40

    @classmethod
    def quick(cls) -> Params:
        """Tiny grids for tests: the same code paths in well under a second."""
        return cls(sigma_steps=9, t_steps=61, t_max=30.0, bands=4, surrogates=6, offsets=3, permutations=2,
                   subsamples=4, omega_max=60.0, omega_steps=121, arcs=8)


# --- zeta -----------------------------------------------------------------------------------------


def zeta_grid(sigmas: np.ndarray, ts: np.ndarray) -> tuple[np.ndarray, list[int] | None]:
    """zeta(sigma + it) on the grid as complex128 [sigma, t]; the pole node is NaN and reported."""
    out = np.empty((len(sigmas), len(ts)), dtype=np.complex128)
    pole = None
    zeta = mpmath.fp.zeta
    for i, s in enumerate(sigmas):
        for j, t in enumerate(ts):
            if abs(s - 1.0) < 1e-12 and abs(t) < 1e-12:
                out[i, j] = np.nan
                pole = [i, j]
            else:
                out[i, j] = zeta(complex(float(s), float(t)))
    return out, pole


def zeta_zeros(t_max: float) -> list[dict[str, float]]:
    """Ordinates of the nontrivial zeros with 0 < t <= t_max and |zeta'| there (for exact wells)."""
    n = int(mpmath.nzeros(t_max)) if t_max >= 14 else 0
    out = []
    for k in range(1, n + 1):
        rho = mpmath.zetazero(k)
        if abs(float(rho.real) - 0.5) > 1e-12 or (out and float(rho.imag) <= out[-1]["t"]):
            raise RuntimeError(f"zetazero({k}) returned an unexpected value: {rho}")
        out.append({"t": float(rho.imag), "slope": float(abs(mpmath.zeta(rho, derivative=1)))})
    return out


def functional_equation_error(z: np.ndarray, sigmas: np.ndarray, ts: np.ndarray) -> float:
    """max |zeta(s) - chi(s) zeta(1 - s)| / |zeta(s)| over the grid (needs a sigma-symmetric grid)."""
    if not np.allclose(sigmas + sigmas[::-1], 1.0):
        return float("nan")
    s = sigmas[:, None] + 1j * ts[None, :]
    chi = 2.0**s * np.pi ** (s - 1.0) * np.sin(np.pi * s / 2.0) * special.gamma(1.0 - s)
    mirror = np.conj(z[::-1, :])  # zeta(1 - s) = conj(zeta(1 - sigma + it))
    ok = np.isfinite(z) & np.isfinite(mirror) & np.isfinite(chi) & (np.abs(z) > 0)
    if not ok.any():
        return float("nan")
    return float((np.abs(z - chi * mirror)[ok] / np.abs(z)[ok]).max())


# --- structural signatures --------------------------------------------------------------------------


def first_primes(n: int) -> list[int]:
    out: list[int] = []
    c = 2
    while len(out) < n:
        if all(c % p for p in out if p * p <= c):
            out.append(c)
        c += 1
    return out


def bucket(values) -> np.ndarray:
    """floor(log2(1 + v)) for non-negative integers, exactly (bit length minus one)."""
    v = np.maximum(np.asarray(values, dtype=np.int64), 0) + 1
    return (np.frexp(v.astype(np.float64))[1] - 1).astype(np.int64)


@dataclass
class Signatures:
    """Exact classes of modules with equal exponent tuples.

    Columns of `exponents`: height bucket, fan-in bucket, fan-out bucket, one per chapter
    rank (2 = own pillar, 1 = imported pillar), and the reserved pillar-less column.
    """

    exponents: np.ndarray  # (classes, 3 + pillars + 1) int64
    live: np.ndarray  # live declarations per class
    dead: np.ndarray
    modules: np.ndarray  # modules per class
    own: np.ndarray  # own chapter rank per class, -1 when pillar-less
    n_pillars: int
    module_class: np.ndarray  # class of every module that has declarations
    module_live: np.ndarray
    module_dead: np.ndarray

    def mass(self, population: str) -> np.ndarray:
        return self.live if population == "live" else self.dead


def signatures(height, fan_in, fan_out, rank, live, dead, imports, n_pillars: int) -> Signatures:
    """Class table from per-module arrays. `rank` is the chapter rank of the module's pillar
    (-1 = none) and `imports` is an (edges, 2) array of module rows (importer, imported)."""
    rank = np.asarray(rank, dtype=np.int64)
    n = len(rank)
    exps = np.zeros((n, 3 + n_pillars + 1), dtype=np.int64)
    exps[:, 0] = bucket(height)
    exps[:, 1] = bucket(fan_in)
    exps[:, 2] = bucket(fan_out)
    rows = np.arange(n)
    has = rank >= 0
    exps[rows[has], 3 + rank[has]] = 2
    exps[rows[~has], 3 + n_pillars] = 2
    imports = np.asarray(imports, dtype=np.int64).reshape(-1, 2)
    if len(imports):
        src, dst = imports[:, 0], imports[:, 1]
        cross = (rank[dst] >= 0) & (rank[dst] != rank[src])
        np.maximum.at(exps, (src[cross], 3 + rank[dst][cross]), 1)
    live = np.asarray(live, dtype=np.float64)
    dead = np.asarray(dead, dtype=np.float64)
    keep = (live + dead) > 0
    uniq, inverse = np.unique(exps[keep], axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    c = len(uniq)
    own = np.full(c, -1, dtype=np.int64)
    own[inverse] = rank[keep]
    return Signatures(
        exponents=uniq,
        live=np.bincount(inverse, weights=live[keep], minlength=c),
        dead=np.bincount(inverse, weights=dead[keep], minlength=c),
        modules=np.bincount(inverse, minlength=c),
        own=own, n_pillars=n_pillars, module_class=inverse, module_live=live[keep], module_dead=dead[keep],
    )


def log_primes(n_pillars: int, *, offset: int = 0, perm=None) -> np.ndarray:
    """ln p for every exponent column. `offset` shifts the pillar primes up the prime list and
    `perm` reassigns them between chapters; both are other equally valid conventions."""
    ps = first_primes(3 + offset + n_pillars + 1)
    pillar = np.log(np.array(ps[3 + offset : 3 + offset + n_pillars], dtype=np.float64))
    if perm is not None:
        pillar = pillar[np.asarray(perm, dtype=np.int64)]
    return np.concatenate([np.log([2.0, 3.0, 5.0]), pillar, [math.log(ps[3 + offset + n_pillars])]])


def godel_integers(sig: Signatures) -> list[int]:
    """The exact integer codes (Python ints: the largest exceed 2^63)."""
    ps = first_primes(3 + sig.n_pillars + 1)
    out = []
    for row in sig.exponents.tolist():
        n = 1
        for p, e in zip(ps, row, strict=True):
            n *= p**e
        out.append(n)
    return out


def expected_zeros(ell: np.ndarray, w: np.ndarray, sigmas: np.ndarray, t_max: float) -> float:
    """Expected zeros in the box of the Gaussian random series with these masses:
    (T / pi) * integral over sigma of Var(l) under weights w^2 exp(-2 sigma l)."""
    a2 = (w * w)[None, :] * np.exp(-2.0 * np.outer(sigmas, ell))
    tot = a2.sum(axis=1)
    if not np.all(tot > 0):
        return 0.0
    mean = (a2 * ell).sum(axis=1) / tot
    var = np.maximum((a2 * ell * ell).sum(axis=1) / tot - mean * mean, 0.0)
    return float(t_max / math.pi * np.trapezoid(var, sigmas))


def frequency_scale(ln_n: np.ndarray, w_live: np.ndarray, sigmas: np.ndarray, t_max: float, dt: float,
                    target_zeros: int) -> dict[str, float]:
    """kappa and the unit L.

    kappa solves expected_zeros(kappa * ln N / L) = target_zeros (the tilt of the weights moves
    with kappa too, so this is a one-dimensional root, not a square root). It falls back to 1
    when there is nothing to solve, and the cap keeps even the fastest class below 0.9 pi of
    phase per grid step.
    """
    unit = float(w_live @ ln_n)
    ell1 = ln_n / unit
    at_one = expected_zeros(ell1, w_live, sigmas, t_max)
    kappa = 1.0
    if at_one > 0 and target_zeros > 0:
        guess = math.sqrt(target_zeros / at_one)

        def gap(k: float) -> float:
            return expected_zeros(k * ell1, w_live, sigmas, t_max) - target_zeros

        lo, hi = guess / 8.0, guess * 8.0
        kappa = float(optimize.brentq(gap, lo, hi, xtol=1e-12)) if gap(lo) < 0 < gap(hi) else guess
    cap = 0.9 * math.pi / (dt * float(ell1.max()))
    chosen = min(kappa, cap)
    return {"kappa": chosen, "kappa_uncapped": kappa, "unit": unit,
            "expected_zeros": expected_zeros(chosen * ell1, w_live, sigmas, t_max)}


# --- evaluating the series ----------------------------------------------------------------------------


def evaluate(ell: np.ndarray, weights: np.ndarray, sigmas: np.ndarray, ts: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """Z[g, i, j] = sum_c weights[g, c] exp(-(sigma_i + i t_j) l_c), exactly (real matrix products)."""
    weights = np.atleast_2d(np.asarray(weights, dtype=np.float64))
    g, c = weights.shape
    s, t = len(sigmas), len(ts)
    re = np.zeros((g * s, t))
    im = np.zeros((g * s, t))
    for a in range(0, c, chunk):
        lc = ell[a : a + chunk]
        amp = (weights[:, None, a : a + chunk] * np.exp(-np.outer(sigmas, lc))[None]).reshape(g * s, -1)
        phase = np.outer(lc, ts)
        re += amp @ np.cos(phase)
        im -= amp @ np.sin(phase)
    return (re + 1j * im).reshape(g, s, t)


def dirichlet(ell: np.ndarray, w: np.ndarray, s) -> np.ndarray:
    s = np.atleast_1d(np.asarray(s, dtype=np.complex128))
    return (w[None, :] * np.exp(-np.outer(s, ell))).sum(axis=1)


def band_index(ell: np.ndarray, w_live: np.ndarray, bands: int) -> tuple[np.ndarray, list[float]]:
    """Harmonic bands: mass quantiles of l under the live weights. A class is never split."""
    order = np.argsort(ell, kind="stable")
    total = float(w_live.sum())
    cdf = np.cumsum(w_live[order]) / total if total > 0 else np.linspace(1 / len(ell), 1, len(ell))
    edges = []
    for k in range(1, bands):
        i = min(int(np.searchsorted(cdf, k / bands - 1e-12)), len(ell) - 1)
        edges.append(float(ell[order][i]))
    return (ell[:, None] > np.array(edges)[None, :]).sum(axis=1).astype(np.int64) if edges else np.zeros(len(ell), dtype=np.int64), edges


def _wrap(d: np.ndarray) -> np.ndarray:
    return (d + np.pi) % (2.0 * np.pi) - np.pi


def winding(z: np.ndarray) -> np.ndarray:
    """Phase winding (in turns) around every grid cell of a field indexed [sigma, t]."""
    ph = np.angle(z)
    turns = (_wrap(ph[1:, :-1] - ph[:-1, :-1]) + _wrap(ph[1:, 1:] - ph[1:, :-1])
             + _wrap(ph[:-1, 1:] - ph[1:, 1:]) + _wrap(ph[:-1, :-1] - ph[:-1, 1:]))
    return np.rint(turns / (2.0 * np.pi)).astype(np.int64)


def _newton(ell: np.ndarray, w: np.ndarray, s0: complex, charge: int = 1) -> tuple[complex, float, bool]:
    s = complex(s0)
    eps = np.finfo(np.float64).eps
    f = complex(0.0)
    for _ in range(80):
        e = w * np.exp(-s * ell)
        f = complex(e.sum())
        level = float((w * np.exp(-s.real * ell)).sum())
        if abs(f) <= 64 * eps * level:
            return s, abs(f) / max(level, 1e-300), True
        df = complex(-(e * ell).sum())
        if df == 0:
            break
        step = charge * f / df
        s -= step
        if abs(step) <= 4 * eps * max(1.0, abs(s)):
            e = w * np.exp(-s * ell)
            level = float((w * np.exp(-s.real * ell)).sum())
            return s, abs(complex(e.sum())) / max(level, 1e-300), True
    level = float((w * np.exp(-s.real * ell)).sum())
    return s, abs(f) / max(level, 1e-300), False


def find_zeros(ell: np.ndarray, weights: np.ndarray, t_max: float, *, base_ds: float, base_dt: float,
               max_refine: int = 3) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    """Zeros of several series (rows of `weights`) in the box [0, 1] x [0, t_max].

    Detection is by phase winding on a grid fine enough that the fastest class turns at most
    pi / 4 per step, padded two cells beyond the box; flagged cells are clustered, refined by
    Newton on the exact sum, and only refined roots are returned. Z is entire, so every
    charge must be positive: a negative one means the grid is too coarse and is retried finer.
    """
    weights = np.atleast_2d(weights)
    active = weights.sum(axis=0) > 0
    if not active.any():
        return [[] for _ in weights], {"cells": 0, "negative": 0, "dt": base_dt, "ds": base_ds}
    lmax = float(ell[active].max())
    k = 0
    while lmax * base_dt / 2**k > math.pi / 4 and k < 8:
        k += 1
    info: dict[str, Any] = {}
    for attempt in range(max_refine + 1):
        dt = base_dt / 2 ** (k + attempt)
        ds = base_ds / 2**attempt
        sig = (np.arange(-2, round(1.0 / ds) + 3)) * ds
        ts = (np.arange(-2, round(t_max / dt) + 3)) * dt
        z = evaluate(ell, weights, sig, ts)
        turns = [winding(z[g]) for g in range(len(weights))]
        negative = int(sum((t < 0).sum() for t in turns))
        info = {"cells": int(sum((t != 0).sum() for t in turns)), "negative": negative, "dt": dt, "ds": ds,
                "attempts": attempt + 1}
        if not negative:
            break
    out: list[list[dict[str, Any]]] = []
    for g, tw in enumerate(turns):
        w = weights[g]
        labels, n = ndimage.label(tw != 0, structure=np.ones((3, 3), dtype=np.int64))
        roots: list[dict[str, Any]] = []
        for lab in range(1, n + 1):
            cells = np.argwhere(labels == lab)
            charge = int(tw[labels == lab].sum())
            if charge <= 0:
                continue
            start = complex(sig[cells[:, 0]].mean() + ds / 2, ts[cells[:, 1]].mean() + dt / 2)
            root, _residual, ok = _newton(ell, w, start, charge)
            if not ok or abs(root - start) > 4 * max(ds, dt) * max(1, len(cells)):
                root, ok = start, False
            if not (-1e-9 <= root.real <= 1 + 1e-9 and -1e-9 <= root.imag <= t_max + 1e-9):
                continue
            if any(abs(complex(r["sigma"], r["t"]) - root) < 1e-8 for r in roots):
                continue
            roots.append({"sigma": float(min(max(root.real, 0.0), 1.0)), "t": float(min(max(root.imag, 0.0), t_max)),
                          "charge": charge, "refined": ok})
        roots.sort(key=lambda r: (r["t"], r["sigma"]))
        out.append(roots)
    return out, info


# --- statistics ----------------------------------------------------------------------------------------


def mass_stats(ell: np.ndarray, w: np.ndarray) -> dict[str, Any]:
    """Effective numbers of classes and the exact long-run RMS level of |Z(sigma + it)| / Z(sigma)."""
    total = float(w.sum())
    if total <= 0:
        return {"present": False}
    p = w[w > 0] / total
    out: dict[str, Any] = {
        "present": True,
        "classes": int((w > 0).sum()),
        "n1": float(np.exp(-(p * np.log(p)).sum())),  # exp(Shannon entropy)
        "n2": float(1.0 / (p * p).sum()),  # participation number
        "mean_l": float((w @ ell) / total),
    }
    for name, sigma in (("0", 0.0), ("half", 0.5), ("1", 1.0)):
        a = w * np.exp(-sigma * ell)
        out[f"r_inf_{name}"] = float(np.sqrt((a * a).sum()) / a.sum())
    return out


def line_profile(ell: np.ndarray, w: np.ndarray, sigma: float, ts: np.ndarray) -> np.ndarray:
    """R(t) = |Z(sigma + it)| / Z(sigma) on a line of constant sigma."""
    a = w * np.exp(-sigma * ell)
    z = evaluate(ell, a, np.array([0.0]), ts)[0, 0]
    return np.abs(z) / float(a.sum())


def line_minima(ell: np.ndarray, w: np.ndarray, ts: np.ndarray, profile: np.ndarray, r_inf: float,
                zeros: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Local minima of R on sigma = 1/2 with prominence ln 2, refined on the exact sum. A dip is
    `sharp` at or below 0.1 of the long-run level; a random-phase sum is there about 1% of the time."""
    g = np.log(np.maximum(profile, 1e-300))
    idx, _ = signal.find_peaks(-g, prominence=math.log(2.0))
    level = float((w * np.exp(-0.5 * ell)).sum())
    out = []
    for i in idx:
        lo, hi = float(ts[max(i - 1, 0)]), float(ts[min(i + 1, len(ts) - 1)])
        res = optimize.minimize_scalar(lambda t: float(abs(dirichlet(ell, w, 0.5 + 1j * t)[0]) ** 2), bounds=(lo, hi),
                                       method="bounded", options={"xatol": 1e-10})
        t = float(res.x)
        r = math.sqrt(max(float(res.fun), 0.0)) / level
        near = min(zeros, key=lambda z: abs(complex(z["sigma"] - 0.5, z["t"] - t)), default=None)
        zero = near if near is not None and abs(complex(near["sigma"] - 0.5, near["t"] - t)) <= 0.25 else None
        out.append({"t": t, "r": r, "sharp": bool(r <= 0.1 * r_inf),
                    "zero": {"sigma": zero["sigma"], "t": zero["t"]} if zero else None})
    return out


def cancellation_budget(ell: np.ndarray, w: np.ndarray, s: complex, groups: np.ndarray, n_groups: int) -> dict[str, Any]:
    """Where the amplitude goes at the point s, for a partition of the classes into groups.

    within + between + residual = 1: `within` is lost to dephasing inside groups, `between` to
    cancellation between group sums, `residual` is what is left, |Z| / A. The two `sides`
    split the group sums along their principal axis.
    """
    a = w * np.exp(-s.real * ell)
    z = a * np.exp(-1j * s.imag * ell)
    total = float(a.sum())
    zg = np.bincount(groups, weights=z.real, minlength=n_groups) + 1j * np.bincount(groups, weights=z.imag, minlength=n_groups)
    ag = np.bincount(groups, weights=a, minlength=n_groups)
    mags = np.abs(zg)
    phi = 0.5 * math.atan2(2.0 * float((zg.real * zg.imag).sum()), float((zg.real**2).sum() - (zg.imag**2).sum()))
    proj = (zg * np.exp(-1j * phi)).real
    plus, minus = proj > 0, proj < 0
    if ag[minus].sum() > ag[plus].sum():
        plus, minus = minus, plus
    s_plus, s_minus = complex(zg[plus].sum()), complex(zg[minus].sum())
    denom = abs(s_plus) + abs(s_minus)
    order = np.argsort(-mags, kind="stable")
    return {
        "within": float(1.0 - mags.sum() / total),
        "between": float((mags.sum() - abs(zg.sum())) / total),
        "residual": float(abs(zg.sum()) / total),
        "opposition": float(1.0 - abs(s_plus + s_minus) / denom) if denom > 0 else 0.0,
        "collinearity": float(denom / mags.sum()) if mags.sum() > 0 else 0.0,
        "groups": [{"group": int(g), "share": float(mags[g] / total), "side": 1 if plus[g] else (-1 if minus[g] else 0)}
                   for g in order[:6] if mags[g] > 0],
    }


def alignment(ts: np.ndarray, log_profile: np.ndarray, zeros_t: list[float], t_max: float, shifts: int = 2000) -> dict[str, Any]:
    """Do the zeta zeros sit in dips of ln R more than chance allows?

    The comb of zeros is shifted in the unfolded variable U(t) = (t / 2 pi) ln(t / 2 pi e) + 7/8,
    which preserves the growing density of zeros (a plain shift in t does not, and reports
    false alignment because ln R trends downward). Deterministic: no random numbers.
    """
    comb = np.array([z for z in zeros_t if T_ALIGN <= z <= t_max])
    if len(comb) < 3 or t_max <= T_ALIGN:
        return {"available": False}

    def unfold(t):
        return t / (2 * np.pi) * np.log(t / (2 * np.pi * np.e)) + 0.875

    grid = np.linspace(T_ALIGN, t_max, 4001)
    ug = unfold(grid)
    span = float(ug[-1] - ug[0])
    base = unfold(comb) - ug[0]

    def score(delta: float) -> float:
        return float(np.interp(np.interp(ug[0] + (base + delta) % span, ug, grid), ts, log_profile).mean())

    observed = score(0.0)
    null = np.array([score(k * span / (shifts + 1)) for k in range(1, shifts + 1)])
    return {"available": True, "zeros": int(len(comb)), "observed": observed, "null_mean": float(null.mean()),
            "p": float((1 + int((null <= observed).sum())) / (shifts + 1))}


def uniforms(purpose: int, n: int) -> np.ndarray:
    """Seeded uniforms in [0, 1) from raw PCG64 output (stable across numpy versions)."""
    raw = np.random.PCG64(np.random.SeedSequence([SEED, purpose])).random_raw(n)
    return (raw >> np.uint64(11)).astype(np.float64) * 2.0**-53


class WindowStats:
    """Cheap, uniform statistics of the window for many variants of the series: zeros in the
    box, zeros near the line, sharp dips on the line, and the RMS of R beyond T_ALIGN.

    Evaluated by FFT with l rounded to a fine lattice; the phase error at t_max is at most
    `phase_error` radians. The same path serves the observed series, the torus surrogates and
    the encoding ensemble, so their numbers are comparable with each other.
    """

    def __init__(self, t_max: float, dt: float, sigma_steps: int = 41) -> None:
        self.t_max, self.dt = t_max, dt
        self.sigmas = np.linspace(0.0, 1.0, sigma_steps)
        self.n_t = round(t_max / dt) + 1
        m = 1 << 14
        while m < 4 * self.n_t or t_max * math.pi / (m * dt) > 0.05:
            m <<= 1
        self.m = m
        self.delta = 2.0 * math.pi / (m * dt)
        self.phase_error = t_max * self.delta / 2.0
        self.ts = np.arange(self.n_t) * dt
        self.half = int(np.argmin(np.abs(self.sigmas - 0.5)))

    def field(self, ell: np.ndarray, coeff: np.ndarray) -> np.ndarray:
        bins = np.rint(ell / self.delta).astype(np.int64)
        if bins.max(initial=0) >= self.m // 2:
            raise ValueError("l is too large for this FFT lattice: use a smaller dt")
        s = len(self.sigmas)
        vals = coeff[None, :] * np.exp(-np.outer(self.sigmas, ell))
        flat = (np.arange(s)[:, None] * self.m + bins[None, :]).ravel()
        grid = (np.bincount(flat, weights=vals.real.ravel(), minlength=s * self.m)
                + 1j * np.bincount(flat, weights=vals.imag.ravel(), minlength=s * self.m)).reshape(s, self.m)
        return np.fft.fft(grid, axis=1)[:, : self.n_t]

    def stats(self, ell: np.ndarray, w: np.ndarray, phases: np.ndarray | None = None) -> dict[str, float]:
        if w.sum() <= 0:
            return {"zeros": 0, "near_line": 0, "sharp": 0, "rms": 0.0}
        coeff = w.astype(np.complex128) if phases is None else w * np.exp(-1j * phases)
        z = self.field(ell, coeff)
        turns = winding(z)
        cells = np.argwhere(turns > 0)
        centers = self.sigmas[cells[:, 0]] + 0.5 * (self.sigmas[1] - self.sigmas[0])
        a = w * np.exp(-0.5 * ell)
        r = np.abs(z[self.half]) / float(a.sum())
        r_inf = float(np.sqrt((a * a).sum()) / a.sum())
        dips = (r[1:-1] < r[:-2]) & (r[1:-1] <= r[2:]) & (r[1:-1] <= 0.1 * r_inf)
        tail = r[self.ts >= min(T_ALIGN, 0.2 * self.t_max)]
        return {"zeros": int(len(cells)), "near_line": int((np.abs(centers - 0.5) <= NEAR_LINE).sum()),
                "sharp": int(dips.sum()), "rms": float(np.sqrt((tail * tail).mean())) if len(tail) else 0.0}


def _band(values: list[float], observed: float) -> dict[str, float]:
    v = np.asarray(values, dtype=np.float64)
    return {"observed": float(observed), "mean": float(v.mean()), "sd": float(v.std()),
            "lo": float(np.percentile(v, 2.5)), "hi": float(np.percentile(v, 97.5)),
            "rank": float((v <= observed).mean())}


def size_matched(sig: Signatures, draws: int) -> dict[str, Any]:
    """Effective classes of random live subsamples with as many modules as carry dead code."""
    live_rows = np.flatnonzero(sig.module_live > 0)
    n = int((sig.module_dead > 0).sum())
    if draws <= 0 or n == 0 or n >= len(live_rows):
        return {"available": False}
    n1, n2 = [], []
    c = len(sig.live)
    for b in range(draws):
        pick = live_rows[np.argsort(uniforms(1000 + b, len(live_rows)), kind="stable")[:n]]
        w = np.bincount(sig.module_class[pick], weights=sig.module_live[pick], minlength=c)
        p = w[w > 0] / w.sum()
        n1.append(float(np.exp(-(p * np.log(p)).sum())))
        n2.append(float(1.0 / (p * p).sum()))
    return {"available": True, "modules": n, "draws": draws, "n1_mean": float(np.mean(n1)), "n1_sd": float(np.std(n1)),
            "n2_mean": float(np.mean(n2)), "n2_sd": float(np.std(n2))}


def variance_shares(sig: Signatures, logp: np.ndarray, w: np.ndarray) -> dict[str, float]:
    """Which part of the tuple drives ln N (covariance shares under the given mass; they sum to 1)."""
    total = float(w.sum())
    if total <= 0:
        return {}
    p = w / total
    x = sig.exponents * logp[None, :]
    rows = np.arange(len(sig.own))
    own_col = np.where(sig.own >= 0, 3 + sig.own, 3 + sig.n_pillars)
    own = x[rows, own_col]
    parts = {"height": x[:, 0], "fan_in": x[:, 1], "fan_out": x[:, 2], "own_pillar": own,
             "imported_pillars": x[:, 3:].sum(axis=1) - own}
    ln_n = x.sum(axis=1)
    mean = float(p @ ln_n)
    var = float(p @ (ln_n - mean) ** 2)
    if var <= 0:
        return {}
    return {k: float(p @ ((v - float(p @ v)) * (ln_n - mean))) / var for k, v in parts.items()}


# --- layout ---------------------------------------------------------------------------------------------


def strip_layout(chapters: list[dict[str, Any]], weights: np.ndarray, t_max: float, n_arcs: int) -> tuple[list[dict], list[dict]]:
    """Beacon positions and flow arcs. A map projection of the chapter order, not a computation:
    t is the share of the live proof upstream of the chapter, the wings close toward
    sigma = 1/2 as the flow approaches Main, and the two sides are balanced by mass."""
    total = sum(c["live"] for c in chapters) or 1
    cum = 0.0
    side_mass = {-1: 0.0, 1: 0.0}
    out = []
    for c in chapters:
        u = (cum + c["live"] / 2) / total
        cum += c["live"]
        if c["tier"] == "main":
            sigma, side, t = 0.5, 0, t_max
        else:
            side = -1 if side_mass[-1] <= side_mass[1] else 1
            side_mass[side] += c["live"]
            sigma = 0.5 + side * (0.06 + 0.36 * (1.0 - u) ** 0.8)
            t = t_max * u
        out.append({**c, "sigma": round(sigma, 5), "t": round(t, 4), "side": side})
    order = [c["id"] for c in chapters]
    flows = pl.net_flows(weights, order)
    core = {c["id"] for c in chapters if c["tier"] == "main"}
    chosen = flows[:n_arcs]
    seen = {(f["provider"], f["consumer"]) for f in chosen}
    for f in flows[n_arcs:]:
        covered = {x for g in chosen for x in (g["provider"], g["consumer"])}
        if f["consumer"] in core or f["provider"] not in covered or f["consumer"] not in covered:
            if (f["provider"], f["consumer"]) not in seen:
                chosen.append(f)
                seen.add((f["provider"], f["consumer"]))
    arcs = [{"from": f["provider"], "to": f["consumer"], "net": f["net"], "forward": f["forward"],
             "backward": f["backward"], "final": f["consumer"] in core} for f in chosen]
    return out, arcs


def apron_field(layout: list[dict[str, Any]], sigmas_ext: np.ndarray, ts: np.ndarray, t_max: float) -> np.ndarray:
    """Dead declarations as mounds outside the strip, one per chapter on its own side; the
    volume of a mound is proportional to the chapter's dead count. Heights are in [0, 1]."""
    field = np.zeros((len(sigmas_ext), len(ts)))
    total_live = sum(c["live"] for c in layout) or 1
    ws = 0.07
    for c in layout:
        if not c["dead"] or not c["side"]:
            continue
        wt = max(0.02 * t_max, 0.5 * t_max * c["live"] / total_live)
        center = -0.15 if c["side"] < 0 else 1.15
        bump = np.exp(-0.5 * ((sigmas_ext[:, None] - center) / ws) ** 2) * np.exp(-0.5 * ((ts[None, :] - c["t"]) / wt) ** 2)
        field += c["dead"] / (ws * wt) * bump
    inside = np.clip(np.minimum(sigmas_ext, 1.0 - sigmas_ext) / 0.04, 0.0, 1.0)  # 0 outside, 1 well inside
    field *= (1.0 - inside * inside * (3 - 2 * inside))[:, None]
    peak = float(field.max())
    return field / peak if peak > 0 else field


# --- database -------------------------------------------------------------------------------------------


def load(index_path: Path, cone_path: Path) -> dict[str, Any]:
    """Everything the build needs from the index and the cone, with deterministic ordering."""
    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("ATTACH ? AS idx", (f"file:{index_path}?mode=ro",))
        meta = dict(con.execute("SELECT key, value FROM cone_meta").fetchall())
        if meta.get("cone_version") != CONE_VERSION:
            raise RuntimeError(f"cone was built by v{meta.get('cone_version')}, need v{CONE_VERSION}: run `cone build` again")
        chapters = [dict(r) for r in con.execute(
            "SELECT id, slug, title, tier, flow_ord, live, dead, modules, max_dom_share, head_decl_id FROM pillars ORDER BY flow_ord")]
        n = len(chapters)
        rank_of = {c["id"]: c["flow_ord"] for c in chapters}
        weights = np.zeros((max(rank_of) + 1, max(rank_of) + 1), dtype=np.int64)
        for s, t, w in con.execute("SELECT source, target, weight FROM pillar_edges ORDER BY source, target"):
            weights[s, t] = w
        mods = np.array(con.execute(
            "SELECT m.id, mm.height, mm.in_degree, mm.out_degree, COALESCE(mp.pillar_id, -1) FROM idx.modules m "
            "JOIN idx.module_metrics mm ON mm.module_id = m.id LEFT JOIN module_pillar mp ON mp.module_id = m.id ORDER BY m.id"
        ).fetchall(), dtype=np.int64).reshape(-1, 5)
        ids = mods[:, 0]
        row_of = np.full(int(ids.max()) + 1, -1, dtype=np.int64)
        row_of[ids] = np.arange(len(ids))
        rank = np.array([rank_of.get(int(p), -1) for p in mods[:, 4]], dtype=np.int64)
        live = np.zeros(len(ids))
        dead = np.zeros(len(ids))
        for module_id, n_live, n_all in con.execute(
                "SELECT module_id, SUM(live), COUNT(*) FROM decl_metrics GROUP BY module_id ORDER BY module_id"):
            live[row_of[module_id]] = n_live
            dead[row_of[module_id]] = n_all - n_live
        imports = np.array(con.execute(
            "SELECT DISTINCT src_id, dst_id FROM idx.imports WHERE dst_id IS NOT NULL ORDER BY src_id, dst_id"
        ).fetchall(), dtype=np.int64).reshape(-1, 2)
        gate = con.execute(
            "SELECT d.full_name, m.dom_mass, m.pillar_id FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id "
            "WHERE m.live = 1 AND m.is_root = 0 AND d.full_name IS NOT NULL ORDER BY m.dom_mass DESC, m.decl_id LIMIT 1").fetchone()
        sig = signatures(mods[:, 1], mods[:, 2], mods[:, 3], rank, live, dead, row_of[imports], n)
        return {"sig": sig, "chapters": chapters, "weights": weights, "meta": meta, "modules": int(len(ids)),
                "gate": dict(gate) if gate else None, "live_total": int(live.sum()), "dead_total": int(dead.sum())}
    finally:
        con.close()


# --- build ----------------------------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _json(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=1, allow_nan=False) + "\n").encode()


def _round(x: float, digits: int = 6) -> float:
    return float(round(float(x), digits))


def build_landscape(index_path: Path, cone_path: Path, out_dir: Path, params: Params | None = None,
                    log: Callable[[str], None] = print) -> dict[str, Any]:
    p = params or Params()
    t0 = time.time()
    data = load(Path(index_path), Path(cone_path))
    sig: Signatures = data["sig"]
    chapters = data["chapters"]
    sigmas = np.arange(p.sigma_steps) / (p.sigma_steps - 1)
    ts = np.arange(p.t_steps) * (p.t_max / (p.t_steps - 1))
    dt = float(ts[1] - ts[0])
    ds = float(sigmas[1] - sigmas[0])

    # 1) zeta
    zeta, pole = zeta_grid(sigmas, ts)
    zeros = zeta_zeros(p.t_max)
    fe_error = functional_equation_error(zeta, sigmas, ts)
    log(f"zeta      {p.sigma_steps}x{p.t_steps} grid, {len(zeros)} zeros up to t={p.t_max:g}, "
        f"functional equation residual {fe_error:.1e}  ({time.time() - t0:.1f}s)")

    # 2) the signature code and the frequency scale
    w_live = sig.live / max(sig.live.sum(), 1.0)
    w_dead = sig.dead / sig.dead.sum() if sig.dead.sum() > 0 else np.zeros_like(sig.dead)
    logp = log_primes(sig.n_pillars)
    ln_n = sig.exponents @ logp
    scale = frequency_scale(ln_n, w_live, sigmas, p.t_max, dt, len(zeros))
    ell = scale["kappa"] * ln_n / scale["unit"]
    order = np.lexsort((np.arange(len(ell)), ell))  # deterministic class order: by l, then tuple order
    log(f"code      {data['modules']:,} modules -> {len(ell):,} classes; unit L={scale['unit']:.4f}, "
        f"kappa={scale['kappa']:.4f} (l from {ell.min():.3f} to {ell.max():.3f})")

    # 3) fields per harmonic band and population
    band, edges = band_index(ell, w_live, p.bands)
    masks = (band[None, :] == np.arange(p.bands)[:, None]).astype(np.float64)
    group_w = np.concatenate([masks * w_live[None, :], masks * w_dead[None, :]])  # (2K, C)
    fields = evaluate(ell[order], group_w[:, order], sigmas, ts).reshape(2, p.bands, p.sigma_steps, p.t_steps)
    cumulative = np.cumsum(group_w.reshape(2, p.bands, -1), axis=1).reshape(2 * p.bands, -1)
    zero_lists, detect = find_zeros(ell, cumulative, p.t_max, base_ds=ds, base_dt=dt)
    zeros_by = {pop: zero_lists[i * p.bands : (i + 1) * p.bands] for i, pop in enumerate(POPULATIONS)}
    log(f"fields    {2 * p.bands} band fields; zeros in the box: live {len(zeros_by['live'][-1])}, dead {len(zeros_by['dead'][-1])} "
        f"(detection step dt={detect['dt']:.4g}, negative charges {detect['negative']})  ({time.time() - t0:.1f}s)")

    # 4) the critical line, the spectrum, and per-zero budgets
    fine = np.arange(round(p.t_max / (dt / 8)) + 1) * (dt / 8)
    omegas = np.linspace(0.0, p.omega_max, p.omega_steps)
    pops: dict[str, Any] = {}
    spectra = np.zeros((2, p.omega_steps))
    lines = {}
    own_group = np.where(sig.own >= 0, sig.own, sig.n_pillars)
    for i, (pop, w) in enumerate((("live", w_live), ("dead", w_dead))):
        stats = mass_stats(ell, w)
        entry: dict[str, Any] = {"mass": int(round(float(sig.mass(pop).sum()))), "stats": stats,
                                 "zeros_by_band": zeros_by[pop], "band_mass": [_round(float((masks[k] * w).sum())) for k in range(p.bands)]}
        if stats["present"]:
            profile = line_profile(ell, w, 0.5, fine)
            lines[pop] = np.log(np.maximum(profile, 1e-300))
            full = zeros_by[pop][-1]
            for z in full:
                s = complex(z["sigma"], z["t"])
                z["by_chapter"] = cancellation_budget(ell, w, s, own_group, sig.n_pillars + 1)
                z["by_band"] = cancellation_budget(ell, w, s, band, p.bands)
            minima = line_minima(ell, w, fine, profile, stats["r_inf_half"], full)
            for m in minima:
                s = complex(0.5, m["t"])
                m["by_chapter"] = cancellation_budget(ell, w, s, own_group, sig.n_pillars + 1)
                m["by_band"] = cancellation_budget(ell, w, s, band, p.bands)
            spec = np.abs(evaluate(ell, w, np.array([0.0]), omegas)[0, 0]) ** 2
            spectra[i] = spec
            peaks, props = signal.find_peaks(profile, prominence=0.0)
            keep = [int(k) for k in peaks if fine[k] >= min(2.0, 0.1 * p.t_max)]
            keep.sort(key=lambda k: -profile[k])
            entry.update({
                "minima": minima,
                "peaks": [{"t": _round(fine[k], 4), "r": _round(profile[k])} for k in sorted(keep[:12])],
                "alignment": alignment(fine, lines[pop], [z["t"] for z in zeros], p.t_max),
                "variance_shares": variance_shares(sig, logp, w),
                "spectrum_mean": _round(float(spec[omegas >= T_ALIGN].mean()) if (omegas >= T_ALIGN).any() else 0.0, 8),
            })
        pops[pop] = entry

    # 5) is this window typical, and what survives a change of convention?
    plan = WindowStats(p.t_max, dt / 2)
    observed = {pop: plan.stats(ell, w) for pop, w in (("live", w_live), ("dead", w_dead))}
    keys = ("zeros", "near_line", "sharp", "rms")
    surrogate: dict[str, Any] = {"draws": p.surrogates, "phase_error": _round(plan.phase_error)}
    if p.surrogates > 0:
        draws = {pop: {k: [] for k in keys} for pop in POPULATIONS}
        n_primes = sig.exponents.shape[1]
        for b in range(p.surrogates):
            theta = 2.0 * math.pi * uniforms(2000 + b, n_primes)
            phases = sig.exponents @ theta  # a random point of the torus that t -> (t ln p_k) winds on
            for pop, w in (("live", w_live), ("dead", w_dead)):
                st = plan.stats(ell, w, phases)
                for k in keys:
                    draws[pop][k].append(st[k])
        for pop in POPULATIONS:
            surrogate[pop] = {k: _band(draws[pop][k], observed[pop][k]) for k in keys}
    ensemble_rows = []
    variants: list[tuple[str, int, Any]] = [("offset", o, None) for o in range(1, p.offsets)]
    for j in range(p.permutations):
        variants.append(("permutation", j, np.argsort(uniforms(3000 + j, sig.n_pillars), kind="stable")))
    for kind, j, perm in variants:
        lp = log_primes(sig.n_pillars, offset=j if kind == "offset" else 0, perm=perm)
        ln_v = sig.exponents @ lp
        sc = frequency_scale(ln_v, w_live, sigmas, p.t_max, dt, len(zeros))
        ell_v = sc["kappa"] * ln_v / sc["unit"]
        row: dict[str, Any] = {"kind": kind, "index": j, "kappa": _round(sc["kappa"], 4)}
        for pop, w in (("live", w_live), ("dead", w_dead)):
            row[pop] = plan.stats(ell_v, w)
            row[pop]["r_inf_half"] = mass_stats(ell_v, w).get("r_inf_half")
        ensemble_rows.append(row)
    log(f"controls  {p.surrogates} torus surrogates, {len(ensemble_rows)} encodings  ({time.time() - t0:.1f}s)")

    # 6) the layout
    layout, arcs = strip_layout(chapters, data["weights"], p.t_max, p.arcs)
    ext_steps = round(1.6 / ds) + 1
    sigmas_ext = -0.3 + np.arange(ext_steps) * ds
    apron = apron_field(layout, sigmas_ext, ts, p.t_max)

    # 7) write
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zeta_out = np.where(np.isfinite(zeta), zeta, 0.0)
    blocks: list[tuple[str, np.ndarray, list[str]]] = [
        ("zeta", np.stack([zeta_out.real.T, zeta_out.imag.T]), ["part", "t", "sigma"]),
        ("proof", np.stack([fields.real, fields.imag], axis=2).transpose(0, 1, 2, 4, 3), ["population", "band", "part", "t", "sigma"]),
        ("apron", apron.T, ["t", "sigma_ext"]),
        ("spectrum", spectra, ["population", "omega"]),
    ]
    table: dict[str, Any] = {}
    chunks = []
    offset = 0
    for name, arr, axes in blocks:
        a32 = np.ascontiguousarray(arr, dtype="<f4")
        table[name] = {"offset": offset, "shape": list(a32.shape), "axes": axes}
        chunks.append(a32.tobytes())
        offset += a32.size
    blob = b"".join(chunks)
    build_id = hashlib.sha256(blob).hexdigest()[:16]
    _write_atomic(out_dir / "fields.f32", blob)

    integers = godel_integers(sig)
    lines_out = ["ln_n\tgodel_n\tlive\tdead\tmodules\town_rank\texponents"]
    for c in order.tolist():
        lines_out.append(f"{ln_n[c]:.12f}\t{integers[c]}\t{int(sig.live[c])}\t{int(sig.dead[c])}\t{int(sig.modules[c])}\t"
                         f"{int(sig.own[c])}\t{' '.join(str(e) for e in sig.exponents[c].tolist())}")
    with gzip.GzipFile(out_dir / "classes.tsv.gz.tmp", "wb", mtime=0) as fh:
        fh.write(("\n".join(lines_out) + "\n").encode())
    os.replace(out_dir / "classes.tsv.gz.tmp", out_dir / "classes.tsv.gz")

    findings = _findings(data, pops, observed, surrogate, ensemble_rows, zeros, scale, sig, p)
    gate = gate_record(data, layout)
    summary = takeaway(data, pops, findings, gate)
    build = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": round(time.time() - t0, 1),
             "cone_built_at": data["meta"].get("built_at")}
    logabs = np.log(np.abs(zeta[np.isfinite(zeta)]))
    landscape = {
        "schema": SCHEMA, "encoding": ENCODING, "build_id": build_id, "build": build,
        "params": asdict(p),
        "grid": {"sigma": {"min": 0.0, "max": 1.0, "n": p.sigma_steps}, "t": {"min": 0.0, "max": p.t_max, "n": p.t_steps},
                 "sigma_ext": {"min": -0.3, "max": _round(float(sigmas_ext[-1])), "n": ext_steps},
                 "omega": {"min": 0.0, "max": p.omega_max, "n": p.omega_steps}},
        "fields": table,
        "zeta": {"zeros": [{"t": z["t"], "slope": _round(z["slope"], 9)} for z in zeros], "pole": pole,
                 "functional_equation_error": fe_error if math.isfinite(fe_error) else None,
                 "log_abs_range": [_round(float(logabs.min())), _round(float(logabs.max()))]},
        "proof": {
            "modules": data["modules"], "classes": int(len(ell)), "kappa": _round(scale["kappa"]), "unit": _round(scale["unit"]),
            "kappa_uncapped": _round(scale["kappa_uncapped"]), "expected_zeros": _round(scale["expected_zeros"], 4),
            "l_min": _round(float(ell.min())), "l_max": _round(float(ell.max())),
            "band_edges": [_round(e) for e in edges], "detection": detect,
            "primes": {"height": 2, "fan_in": 3, "fan_out": 5, "pillars": first_primes(3 + sig.n_pillars)[3:],
                       "reserved": first_primes(4 + sig.n_pillars)[-1]},
            **pops,
        },
        "counts": {"live": data["live_total"], "dead": data["dead_total"]},
        "chapters": [{"id": c["id"], "slug": c["slug"], "title": c["title"], "tier": c["tier"], "chapter": c["flow_ord"] + 1,
                      "sigma": c["sigma"], "t": c["t"], "side": c["side"], "live": c["live"], "dead": c["dead"]} for c in layout],
        "arcs": arcs,
        "findings": findings,
        "gate": gate,
        "takeaway": summary,
    }
    _write_atomic(out_dir / "landscape.json", _json(landscape))
    _write_atomic(out_dir / "findings.json", _json({
        "schema": "eddie-findings/1", "build_id": build_id, "encoding": ENCODING, "findings": findings,
        "gate": gate, "takeaway": summary,
        "observed_window": observed, "surrogates": surrogate, "encodings": ensemble_rows,
        "size_matched": findings_extra(sig, p),
    }))
    log(f"written   {out_dir} ({len(blob) / 1e6:.1f} MB of fields, build {build_id}) in {time.time() - t0:.1f}s")
    return {"build_id": build_id, "out_dir": str(out_dir), "classes": int(len(ell)), "kappa": scale["kappa"],
            "zeros": {pop: len(zeros_by[pop][-1]) for pop in POPULATIONS}, "zeta_zeros": len(zeros),
            "functional_equation_error": fe_error, "findings": findings, "bytes": len(blob)}


def findings_extra(sig: Signatures, p: Params) -> dict[str, Any]:
    return size_matched(sig, p.subsamples)


def gate_record(data: dict[str, Any], layout: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The theorem that dominates the most of the live proof, and the chapter it lives in."""
    gate = data["gate"]
    if not gate or not data["live_total"]:
        return None
    home = next((c for c in layout if c["id"] == gate["pillar_id"]), None)
    return {"full_name": gate["full_name"], "dom_mass": int(gate["dom_mass"]),
            "share": _round(100.0 * gate["dom_mass"] / data["live_total"], 4),
            "chapter": home["flow_ord"] + 1 if home else None, "slug": home["slug"] if home else None}


def _thousands(n: int) -> str:
    return f"{round(n / 1000):,}K" if n >= 10_000 else f"{n:,}"


def takeaway(data: dict[str, Any], pops: dict[str, Any], findings: list[dict[str, Any]], gate: dict[str, Any] | None) -> str | None:
    """One plain sentence for the findings card, assembled only from what the numbers support.

    Each clause is dropped or reworded when its number does not back it: the wave comparison is
    stated only when the live-vs-dead finding is stable, in whichever direction it came out, and
    the gate is named only when one was found. It never calls a dip of the wave a bottleneck.
    """
    live_n, dead_n = int(data["live_total"]), int(data["dead_total"])
    if not live_n or not dead_n:
        return None
    share = dead_n / (live_n + dead_n)
    spoken = {2: "One in two", 3: "One in three", 4: "One in four", 5: "One in five"}
    k = next((k for k in spoken if abs(share - 1 / k) < 0.02), None)
    lead = (f"{spoken[k]} declarations ({_thousands(dead_n)}, grey) is" if k
            else f"{share:.0%} of declarations ({_thousands(dead_n)}, grey) are") + " never used by the final theorem"
    versus = next((f for f in findings if f["id"] == "live-vs-dead"), None)
    ls, dsx = pops["live"]["stats"], pops["dead"]["stats"]
    kind = "are the live proof"
    if versus and versus["status"] == "stable" and ls.get("n1") and dsx.get("n1") and ls["n1"] != dsx["n1"]:
        kind = "make the richer wave" if ls["n1"] > dsx["n1"] else "make the more concentrated wave"
    rest = f"the {_thousands(live_n)} that are (colour) {kind}"
    if gate:
        return f"{lead}; {rest}, and a single theorem gates {gate['share']:.1f}% of them."
    return f"{lead}; {rest}."


def _findings(data, pops, observed, surrogate, ensemble, zeros, scale, sig: Signatures, p: Params) -> list[dict[str, Any]]:
    """Plain statements generated from the numbers, written for engineers rather than number
    theorists. Each says how far it can be trusted: `exact` (a property of the graph), `stable`
    (holds however the proof is numbered), `encoding-specific` (a landmark of this numbering
    only) or `null` (a negative result)."""
    out: list[dict[str, Any]] = []
    live, dead = pops["live"], pops["dead"]
    gate = data["gate"]
    if gate and data["live_total"]:
        share = 100.0 * gate["dom_mass"] / data["live_total"]
        out.append({"id": "bottleneck", "status": "exact",
                    "headline": f"One theorem gates {share:.1f}% of the live proof",
                    "detail": f"Every route from the final theorem to {gate['dom_mass']:,} of the {data['live_total']:,} live declarations "
                              f"runs through `{gate['full_name']}`. This is how a bottleneck is measured here: on the dependency "
                              "graph (dominators), never read off the wave."})
    if live["stats"]["present"]:
        zl = live["zeros_by_band"][-1]
        near = sum(1 for z in zl if abs(z["sigma"] - 0.5) <= NEAR_LINE)
        enc = [r["live"]["zeros"] for r in ensemble]
        enc_txt = f" Number the proof {len(enc)} other ways and the count runs from {min(enc)} to {max(enc)}." if enc else ""
        sur = surrogate.get("live", {}).get("zeros")
        sur_txt = ""
        if sur:
            edge = " (this window is above all but a few of them)" if sur["rank"] >= 0.975 else (
                " (this window is below all but a few of them)" if sur["rank"] <= 0.025 else "")
            sur_txt = f" Shuffled stand-ins for the same wave give {sur['mean']:.1f} ± {sur['sd']:.1f}{edge}."
        spread = (f"from σ = {min(z['sigma'] for z in zl):.2f} to {max(z['sigma'] for z in zl):.2f}, {near} within {NEAR_LINE} of the line"
                  if zl else "none in this window")
        out.append({"id": "scatter", "status": "stable" if enc and min(enc) > 0 else "encoding-specific",
                    "headline": f"ζ keeps all {len(zeros)} zeros on one line; the proof wave scatters its {len(zl)}",
                    "detail": f"Up to t = {p.t_max:g} the live wave has {len(zl)} zeros, {spread}.{enc_txt}{sur_txt} "
                              "Where each one sits depends on the numbering. That they do not line up does not."})
    if live["stats"]["present"] and dead["stats"].get("present"):
        ls, dsx = live["stats"], dead["stats"]
        sm = size_matched(sig, p.subsamples)
        sm_txt = (f" A random sample of live modules as large as the dead code ({sm['modules']:,}) still spreads over "
                  f"{sm['n1_mean']:,.0f}, so size alone does not explain it." if sm.get("available") else "")
        enc_ok = all((r["dead"].get("r_inf_half") or 0) > (r["live"].get("r_inf_half") or 0) for r in ensemble) if ensemble else None
        richer = ("The live proof is the richer wave" if ls["n1"] > dsx["n1"] else "Dead code is the richer wave")
        level = ("Fewer kinds cancel less, so the dead wave's usual level on the line is higher "
                 f"({dsx['r_inf_half']:.4f} against {ls['r_inf_half']:.4f}), not lower: dead code is not flat."
                 if dsx["r_inf_half"] > ls["r_inf_half"] else
                 f"The dead wave's usual level on the line is {dsx['r_inf_half']:.4f} against {ls['r_inf_half']:.4f} for the live one.")
        out.append({"id": "live-vs-dead", "status": "stable" if enc_ok else ("encoding-specific" if enc_ok is False else "exact"),
                    "headline": f"{richer}: {ls['n1']:,.0f} effective kinds of declaration against {dsx['n1']:,.0f} for dead code",
                    "detail": "Each wave is a sum over structural kinds of declaration. The live one spreads its weight over "
                              f"{ls['n1']:,.0f} of them, the dead one over {dsx['n1']:,.0f}.{sm_txt} {level}"})
    for pop_name, pop in (("live", live), ("dead", dead)):
        al = pop.get("alignment")
        if al and al.get("available"):
            out.append({"id": f"alignment-{pop_name}", "status": "null" if al["p"] > 0.025 else "encoding-specific",
                        "headline": f"No echo of the Riemann zeros in the {pop_name} wave" if al["p"] > 0.025
                        else f"The {pop_name} wave dips near the Riemann zeros in this numbering (p = {al['p']:.3f})",
                        "detail": f"The {al['zeros']} zeros of ζ beyond t = {T_ALIGN:g} fall in dips of the {pop_name} wave no more often "
                                  f"than the same zeros slid along the line do (p = {al['p']:.2f}). The two t axes share no unit, "
                                  "so none was expected."})
    for pop_name, pop in (("live", live), ("dead", dead)):
        rms = surrogate.get(pop_name, {}).get("rms")
        level = pop["stats"].get("r_inf_half") if pop["stats"].get("present") else None
        if rms and level and (rms["rank"] <= 0.025 or rms["rank"] >= 0.975):
            ratios = [r[pop_name]["rms"] / r[pop_name]["r_inf_half"] for r in ensemble if r[pop_name].get("r_inf_half")]
            same = [x < 1 for x in ratios] if rms["rank"] <= 0.025 else [x > 1 for x in ratios]
            word = "quieter" if rms["rank"] <= 0.025 else "louder"
            out.append({"id": f"window-{pop_name}", "status": "stable" if ratios and all(same) else "encoding-specific",
                        "headline": f"Near the origin the {pop_name} wave is {word} than its usual level",
                        "detail": f"Beyond t = {min(T_ALIGN, 0.2 * p.t_max):g} the {pop_name} wave runs at {100 * rms['observed'] / level:.0f}% of its "
                                  f"exact long-run level on the line, outside the range of {surrogate['draws']} shuffled stand-ins "
                                  f"({100 * rms['lo'] / level:.0f}% to {100 * rms['hi'] / level:.0f}%). "
                                  f"{sum(same)} of {len(same)} other numberings agree in direction."})
    sharp = [m for m in live.get("minima", []) if m["sharp"]]
    if sharp:
        m = min(sharp, key=lambda x: x["r"])
        b = m["by_band"]
        zero_txt = f", the shadow of a zero at σ = {m['zero']['sigma']:.3f}" if m["zero"] else ""
        out.append({"id": "landmark", "status": "encoding-specific", "at": {"sigma": 0.5, "t": _round(m["t"], 4)},
                    "headline": f"A landmark dip at t = {m['t']:.2f}",
                    "detail": f"On the line the live wave falls to {100 * m['r'] / live['stats']['r_inf_half']:.1f}% of its usual level at "
                              f"t = {m['t']:.2f}{zero_txt}. {100 * b['within']:.0f}% of the height is lost inside single layers of the "
                              f"proof and {100 * b['between']:.0f}% cancels between layers. Number the proof differently and the dip "
                              "moves: it is a landmark of this numbering, not a bottleneck."})
    shares = live.get("variance_shares") or {}
    if shares:
        lead = max(shares, key=lambda k: shares[k])
        names = {"height": "how deep its dependencies run", "fan_in": "how many modules import it", "fan_out": "how many it imports",
                 "own_pillar": "its own chapter", "imported_pillars": "which chapters it imports"}
        out.append({"id": "drivers", "status": "exact",
                    "headline": f"Where a module lands on the wave is set mostly by {names[lead]} ({100 * shares[lead]:.0f}% of the variance)",
                    "detail": "Shares of the variance of a module's log-number, over live declarations: "
                              + ", ".join(f"{names[k]} {100 * v:.0f}%" for k, v in sorted(shares.items(), key=lambda kv: -kv[1])) + "."})
    return out
