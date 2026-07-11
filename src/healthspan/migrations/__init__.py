"""Numbered plain-SQL migration files, shipped as package data.

The files (``NNNN_*.sql``) are the source of truth for the database schema
(ADR-0009). They live inside the package rather than a repo-root ``sql/``
directory so the installed CLI can locate them at runtime via
:mod:`importlib.resources`; the runner in :mod:`healthspan.migrate`
discovers and applies them.
"""
