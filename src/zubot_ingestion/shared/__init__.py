"""Cross-cutting shared types, branded identifiers, and common DTOs.

This package holds types that are used across multiple layers without
belonging to any single one (e.g. branded IDs, pagination result
wrappers, auth context). It must have no imports from api, services,
domain, or infrastructure.
"""
