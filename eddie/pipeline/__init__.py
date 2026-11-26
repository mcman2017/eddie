"""The Day 3 decompilation pipeline: `python -m eddie.cli run-ember`.

Four brain regions on `muse-ember-1.0`, every call through the existing governed gateway:

    R1.triage        plain-English glosses for batches of the proof's top declarations
    R1.worker        a sandboxed Python script that draws a numerical witness
    R2.critic        looks at the rendered PNG and accepts or rejects it, with exact defects
    R3.arbiter       audits the gate theorems' intuitions against their Lean statements
    R4.consolidator  one plain-English Proof Atlas chapter per pillar

The orchestrator is deterministic and resumable: every unit of work is a row in
`pipeline_steps`, every call is tagged with its step in the ledger, and every 2xx payload is
journaled before it is settled, so a crash never pays for the same output twice.
"""
