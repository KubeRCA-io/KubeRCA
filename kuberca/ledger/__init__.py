"""Change Ledger for KubeRCA.

Maintains an in-memory ring buffer of resource spec snapshots to power
deployment diff queries.  The ledger is the authoritative source for
"what changed and when" in the rule engine's correlation phase.

Submodules:
    diff            -- Recursive JSON diff producing FieldChange lists.
    change_ledger   -- In-memory ring buffer with tiered memory enforcement.
"""
