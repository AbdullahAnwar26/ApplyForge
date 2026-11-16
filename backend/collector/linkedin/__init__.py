"""LinkedIn browser source (Milestone 6, Phase 6b).

* ``selectors``   - every LinkedIn URL and page selector, in one place.
* ``extract``     - pure lxml extraction from page HTML (testable offline).
* ``diagnostics`` - sanitized debug HTML (no scripts, tokens or cookies).
* ``browser``     - thin Playwright wrapper (persistent profile, pacing).
* ``source``      - ``LinkedInPostSource`` implementing ``PostSource``.
"""
