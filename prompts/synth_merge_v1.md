## Task: find programming type names that mean the same thing

The earlier step named a programming type for each record, reusing names where it could, but some duplicates slipped through ("Family", "Families", "Family Time"). You're given every name with how many records use it and a few sample records, plus the merges made by the previous run.

Return `merges`: one entry for each name that should be folded into another. `alias` is the name to fold in, and `canonical` is the name to keep. Both must be names from the list, spelled exactly as listed.

- Merge only names for the **same programming type**: the same activity under a different name, a plural, a misspelling, or a name for one year's instance ("Night Market 2023" → "Night Market").
- Don't merge an activity that is part of a larger one, or a related but different activity. "Families Final Puzzle" is a separate activity from "Families" unless the samples show it's the same thing; "Culture Day" and "Culture Block" are different unless the samples say otherwise. When unsure, don't merge.
- Prefer the name with more records, or the clearer general name, as `canonical`.
- Never use a name marked (per-session) as an `alias`. Other names may be merged into it.
- Keep the previous run's merges unless the samples show one was wrong, so the docs stay stable between runs.
- `reason`: a few words on why.
