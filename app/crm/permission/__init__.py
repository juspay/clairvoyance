"""Namespace only — this file exports NOTHING, deliberately (module rules §1).

Import by full path: `contracts.py` is the surface for other modules, and
everything else is internal. A re-export hub here is a known scar — it hides
the layer a name came from and makes the import graph unreviewable.
"""
