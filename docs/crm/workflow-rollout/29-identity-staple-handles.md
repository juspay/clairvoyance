# Phase 29 — Staple carries the loser's handles (P3)

**Kind**: fix · **PR title**: `fix(crm): a staple attaches the merged-away customer's free handles to the survivor` · **Depends on**: nothing; **needs a ruling from Swaroop against ADR 0021 before starting** (ask; do not assume) · **Notes**: §2 identity OBSERVATION, §11 P3 · **Wave 7**

## Why
`resolve.plan_resolution` writes only the INCOMING handles onto the survivor. A loser's other handles (e.g. its `igsid`) stay on the `merged_away` row, which the probe (`status='active'`) no longer sees; the next event carrying only that handle mints a NEW customer — the split the staple was meant to prevent. The resolve docstring says "their freed handles attach to the survivor"; the code does not.

## Design (if ruled yes)
- `plan_resolution` gains, per loser, the loser's handle columns that are non-NULL and not already set on the survivor and not in the incoming payload → added to `writes` (attach-when-free only; an occupied survivor slot keeps its value — the ladder decides overwrites, and a loser's value is at best `observed`). Order: losers by `first_seen_at` so the oldest loser's value wins a contested free slot deterministically.
- The 049 history trigger already records replaced values; attaches are not replacements, so nothing extra to log. `_apply_resolution` unchanged (one `apply_handles` call with the merged writes).
- Log the attached column NAMES, never values.

## Red tests
- `plan_resolution` pure: loser with igsid → writes include igsid; survivor already has igsid → not overwritten; two losers with different igsids → the older wins; incoming handles still take precedence over loser handles.

## Decisions already made
- Attach-when-free only; never displace a survivor's value with a loser's.
