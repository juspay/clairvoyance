"""The Assist build-time engine: platform-agnostic, adapters on top.

Given a store URL the engine probes, classifies, researches, writes the
merchant slots, builds a template from the reseller blueprint, gates it,
persists it in place and hands the widget / mirror bindings to the
surfaces that asked (install-time onboarding, the console stream, the
instant preview, regenerate, re-sync). Nothing under ``engine/`` names a
platform: platform facts live in ``assist/platforms/<name>/`` and reach the
engine only through ``platforms.base.PlatformAdapter``. A test greps for
that (``tests/assist/engine/test_no_platform_forks.py``).

Design: workspace ``ASSIST-ENGINE-DESIGN.md``.
"""
