"""Rows -> domain shapes, one module per table. Exports nothing.

The layer looks mechanical enough to skip, and does not get skipped: it is
where a jsonb blob becomes total (shared/decode.py) instead of raising in
the middle of a claimed batch and stranding every row beside it.
"""
