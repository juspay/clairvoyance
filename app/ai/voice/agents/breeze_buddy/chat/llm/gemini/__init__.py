"""Gemini/Vertex-specific chat support: the adjacency-constrained adapter
patch, the thought-signature persistence codec, and explicit Vertex
context caching for the static prompt prefix. Everything in this package
exists because of documented Gemini/Vertex behavior — other providers
get their own package here if/when they need one."""
