"""The eval engine: validator, adapter, pluggable engines and providers.

Generic by design — any Buddy service may judge anything through it, and
the evaluation type is the caller's: the row handed to the adapter names
it. Finished conversations are the first caller.
"""
