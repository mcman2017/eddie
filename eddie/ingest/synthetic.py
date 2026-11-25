"""A synthetic mini Lean 4 project used for tests and `fetch_riemann_archive.py --sample`.

Every line below was written for Eddie's tests. None of it comes from the real archive. The
files mimic the archive's layout (`src/Main.lean`, `src/Statements.lean`, `src/Formalism`,
`src/Development`, ...), and `BuildAid/Edge.lean` packs the syntax that trips naive parsers:
nested block comments, keywords inside comments and strings, default arguments containing
`:=`, Unicode binders, attributes, `open ... in`, `mutual`, `_root_`, universe parameters,
quoted names, pattern-matching definitions, axioms and a `sorry`.

Because `*.lean` is git-ignored, the sources live here as Python strings and are written
out on demand.
"""

from __future__ import annotations

from pathlib import Path

LEAN_TOOLCHAIN = "leanprover/lean4:v4.33.1\n"

LAKEFILE = """import Lake
open Lake DSL

package «riemann_sample» where

lean_lib «RiemannSample» where
  srcDir := "src"
  roots := #[`Main, `Statements]
"""

SAMPLE_FILES: dict[str, str] = {
    "src/Main.lean": """import Statements
import Formalism.Zeta
import Development.Spectral.Trace

/-!
# Main

The headline statements of the synthetic development.
-/

open Formalism

/-- The Riemann hypothesis, stated with the library's own definition. -/
theorem riemannHypothesis : RiemannHypothesis := by
  exact Development.Spectral.Trace.critical_line_of_trace

/-- Every nontrivial zero of `ζ` lies on the critical line. -/
theorem riemannZeta_eq_zero_re {s : ℂ} (hs : riemannZeta s = 0)
    (htriv : ¬∃ n : ℕ, s = -2 * (n + 1)) (hone : s ≠ 1) :
    s.re = 1 / 2 :=
  riemannHypothesis s hs htriv hone
""",
    "src/Statements.lean": """import Formalism.LFunction

/-! Dirichlet `L`-function analogues (synthetic). -/

namespace Statements

/-- The generalized Riemann hypothesis for a primitive character. -/
theorem LFunction_eq_zero_re {q : ℕ} [NeZero q] (χ : DirichletCharacter ℂ q)
    (hχ : χ.IsPrimitive) {s : ℂ} (hs : χ.LFunction s = 0)
    (hstrip : 0 < s.re ∧ s.re < 1) : s.re = 1 / 2 := by
  have key := Formalism.LFunction.zero_free_region χ hχ
  simpa using key s hs hstrip

/-- No zeros to the right of the critical line. -/
theorem LFunction_ne_zero_of_half_lt_re {q : ℕ} [NeZero q] (χ : DirichletCharacter ℂ q)
    {s : ℂ} (hs : 1 / 2 < s.re) : χ.LFunction s ≠ 0 := by
  intro h
  exact Formalism.LFunction.ne_zero_right χ hs h

end Statements
""",
    "src/Formalism/Basic.lean": """import Mathlib.Analysis.Complex.Basic
import Mathlib.Algebra.BigOperators.Basic

namespace Formalism

universe u

/-- A spectral datum: an operator together with its eigenvalue sequence. -/
structure SpectralDatum (α : Type u) [NormedAddCommGroup α] where
  eigen : ℕ → ℂ
  bounded : ∃ C : ℝ, ∀ n, ‖eigen n‖ ≤ C

/-- Functions all of whose strip zeros lie on the critical line. -/
class HasCriticalLine (f : ℂ → ℂ) : Prop where
  on_line : ∀ s, f s = 0 → 0 < s.re → s.re < 1 → s.re = 1 / 2

instance : Inhabited (SpectralDatum ℂ) :=
  ⟨⟨fun _ => 0, ⟨0, fun _ => by simp⟩⟩⟩

/-- The open critical strip. -/
abbrev CriticalStrip : Set ℂ := {s | 0 < s.re ∧ s.re < 1}

/-- A truncated explicit-formula sum. -/
noncomputable def explicitSum (x : ℝ) (T : ℝ) : ℂ :=
  ∑ n ∈ Finset.range ⌊T⌋₊, (x : ℂ) ^ ((n : ℂ) / 2)

end Formalism
""",
    "src/Formalism/Zeta.lean": """import Mathlib.NumberTheory.LSeries.RiemannZeta
import Formalism.Basic

/-! Zeta-function facts used by the development. -/

namespace Formalism

open Complex

/-- The completed zeta function is symmetric under `s ↦ 1 - s`. -/
theorem completedZeta_one_sub (s : ℂ) :
    completedRiemannZeta (1 - s) = completedRiemannZeta s := by
  simpa using completedRiemannZeta_one_sub s

/-- Nontrivial zeros of `ζ`. -/
def NontrivialZero (s : ℂ) : Prop :=
  riemannZeta s = 0 ∧ 0 < s.re ∧ s.re < 1

/-- Nontrivial zeros come in symmetric pairs. -/
lemma nontrivialZero_symm {s : ℂ} (h : NontrivialZero s) :
    NontrivialZero (1 - s) := by
  obtain ⟨h0, hl, hr⟩ := h
  exact ⟨zeta_one_sub_zero h0, by simpa using hr, by simpa using hl⟩

end Formalism
""",
    "src/Formalism/LFunction.lean": """import Formalism.Zeta

namespace Formalism.LFunction

/-- A zero-free region for primitive characters (synthetic). -/
theorem zero_free_region {q : ℕ} [NeZero q] (χ : DirichletCharacter ℂ q) (hχ : χ.IsPrimitive) :
    ∀ s : ℂ, χ.LFunction s = 0 → 0 < s.re ∧ s.re < 1 → s.re = 1 / 2 := by
  intro s hs hstrip
  exact GroundTheory.Harmonic.balance χ hχ s hs hstrip

theorem ne_zero_right {q : ℕ} [NeZero q] (χ : DirichletCharacter ℂ q) {s : ℂ}
    (hs : 1 / 2 < s.re) : χ.LFunction s = 0 → False :=
  fun h => absurd (zero_free_region χ sorryAx s h ⟨by linarith, by linarith⟩) (by linarith)

end Formalism.LFunction
""",
    "src/GroundTheory/Harmonic.lean": """import Formalism.Basic

namespace GroundTheory.Harmonic

/-- Prime harmonics: `∑ p ≤ x, log p · cos (t log p)`. -/
noncomputable def primeHarmonic (x t : ℝ) : ℝ :=
  ∑ p ∈ (Finset.range ⌊x⌋₊).filter Nat.Prime, Real.log p * Real.cos (t * Real.log p)

theorem balance {q : ℕ} [NeZero q] (χ : DirichletCharacter ℂ q) (hχ : χ.IsPrimitive)
    (s : ℂ) (hs : χ.LFunction s = 0) (hstrip : 0 < s.re ∧ s.re < 1) :
    s.re = 1 / 2 := by
  nlinarith [hstrip.1, hstrip.2]

end GroundTheory.Harmonic
""",
    "src/Development/Spectral/Operator.lean": """import Formalism.Basic
import GroundTheory.Harmonic

namespace Development.Spectral.Operator

open Formalism

/-- A self-adjoint model operator on the critical line. -/
structure ModelOperator where
  datum : SpectralDatum ℂ
  selfAdjoint : ∀ n, (datum.eigen n).im = 0

/-- Eigenvalues of a self-adjoint operator are real. -/
theorem eigen_real (T : ModelOperator) (n : ℕ) : (T.datum.eigen n).im = 0 :=
  T.selfAdjoint n

end Development.Spectral.Operator
""",
    "src/Development/Spectral/Trace.lean": """import Development.Spectral.Operator
import Development.Primes.Explicit

namespace Development.Spectral.Trace

open Development.Spectral.Operator

/-- A trace formula pairing spectral data with prime sums (synthetic). -/
theorem trace_formula (T : ModelOperator) (x : ℝ) (hx : 1 < x) :
    ∃ E : ℝ, |E| ≤ Real.sqrt x * Real.log x ^ 2 := by
  exact ⟨0, by positivity⟩

theorem critical_line_of_trace : RiemannHypothesis := by
  intro s hs htriv hone
  exact Development.Primes.Explicit.no_off_line_zero s hs htriv hone

end Development.Spectral.Trace
""",
    "src/Development/Primes/Explicit.lean": """import Lemmata.Bounds
import Formalism.Zeta

namespace Development.Primes.Explicit

/-- Off-line zeros would contradict the explicit-formula bound. -/
theorem no_off_line_zero (s : ℂ) (hs : riemannZeta s = 0)
    (htriv : ¬∃ n : ℕ, s = -2 * (n + 1)) (hone : s ≠ 1) : s.re = 1 / 2 := by
  by_contra hne
  exact Lemmata.Bounds.explicit_bound_contradiction s hs hne

end Development.Primes.Explicit
""",
    "src/Lemmata/Bounds.lean": """import Formalism.Basic

namespace Lemmata.Bounds

/-- A synthetic bound: `|ψ(x) - x| ≤ √x · log² x` for `x ≥ 2`. -/
theorem psi_bound (x : ℝ) (hx : 2 ≤ x) :
    |Chebyshev.psi x - x| ≤ Real.sqrt x * Real.log x ^ 2 := by
  sorryAx

theorem explicit_bound_contradiction (s : ℂ) (hs : riemannZeta s = 0) (hne : s.re ≠ 1 / 2) :
    False := by
  exact absurd hs (by simpa using hne)

end Lemmata.Bounds
""",
    "src/BuildAid/Edge.lean": """module

public import Formalism.Basic
import all Lemmata.Bounds
public meta import Lemmata.Bounds

/-! Edge cases for the structural parser. theorem fake : False := by trivial -/

/- A nested /- block comment -/ that mentions theorem hidden : 1 = 2 := rfl
   and keeps going after the inner close -/

-- line comment: lemma alsoHidden : True := trivial

namespace Edge

set_option maxHeartbeats 400000 in
/-- A docstring with `:=` inside and a { brace. -/
@[simp, norm_cast]
protected theorem tricky_signature {α : Type*} [Ring α] (f : α → α)
    (h₁ : ∀ x, f x = x) -- trailing comment with := inside
    (h₂ : ∀ ⦃y : α⦄, f (f y) = y := by intro y; simp [h₁])
    (n : ℕ := 3) :
    ∃ g : α → α, ∀ x, g (f x) = x ∧ (⟨n, n⟩ : ℕ × ℕ).1 = n := by
  refine ⟨id, fun x => ⟨?_, rfl⟩⟩
  simp [h₁]

private lemma string_with_assign : "a := b".length = 6 := by decide

theorem char_literal : '"'.toNat = 34 := rfl

theorem raw_string : r#"no "theorem" here := ok"#.length > 0 := by decide

theorem «weird name with spaces» : True := trivial

theorem universe_params.{u, v} (α : Sort u) (β : Sort v) : True := trivial

open Formalism in
theorem uses_open (s : ℂ) (h : s ∈ CriticalStrip) : 0 < s.re := h.1

def pattern_match : ℕ → ℕ
  | 0 => 1
  | n + 1 => 2 * pattern_match n

instance (priority := 100) instSpectral : Inhabited ℕ := ⟨0⟩

axiom synthetic_axiom (p : Prop) : p ∨ ¬p

opaque hidden_constant : ℕ

example : 1 + 1 = 2 := rfl

theorem with_sorry : 2 + 2 = 5 := by
  sorry

mutual
  def isEven : ℕ → Bool
    | 0 => true
    | n + 1 => isOdd n
  def isOdd : ℕ → Bool
    | 0 => false
    | n + 1 => isEven n
end

theorem _root_.rootLevel : True := trivial

noncomputable section

theorem in_noncomputable_section : True := trivial

end

end Edge
""",
}


def _generated_module(i: int, theorems: int) -> str:
    prev = f"Development.Generated.M{i - 1}" if i > 0 else "Formalism.Basic"
    lines = [f"import {prev}", "", f"namespace Development.Generated.M{i}", ""]
    for k in range(theorems):
        lines += [
            f"/-- Generated lemma {k} of module {i}. -/",
            f"theorem gen_{i}_{k} (x : ℝ) (hx : 0 < x) :",
            f"    x ^ {k + 1} > 0 ∧ ∀ ε > 0, ∃ δ > 0, δ < ε := by",
            "  exact ⟨by positivity, fun ε hε => ⟨ε / 2, by positivity, by linarith⟩⟩",
            "",
        ]
    lines += [f"end Development.Generated.M{i}", ""]
    return "\n".join(lines)


def write_sample_tree(dest: Path, *, scale: int = 0, theorems_per_module: int = 8) -> dict[str, int]:
    """Write the synthetic project under `dest` (lakefile, toolchain, src/...).

    `scale` adds a chain of generated modules (each importing the previous one) for
    multi-core and DAG-depth testing.
    """
    dest = Path(dest)
    files = dict(SAMPLE_FILES)
    for i in range(scale):
        files[f"src/Development/Generated/M{i}.lean"] = _generated_module(i, theorems_per_module)
    (dest / "src").mkdir(parents=True, exist_ok=True)
    (dest / "lean-toolchain").write_text(LEAN_TOOLCHAIN)
    (dest / "lakefile.lean").write_text(LAKEFILE)
    total = 0
    for rel, text in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        total += len(text.encode("utf-8"))
    return {"lean_files": len(files) + 1, "bytes": total}
