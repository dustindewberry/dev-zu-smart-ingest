"""Cross-cutting shared types, branded identifiers, constants, and common DTOs.

This package holds types and constants that are used across multiple layers
without belonging to any single one (e.g. branded IDs, pagination result
wrappers, auth context, service-wide constants). It must have no imports from
api, services, domain, or infrastructure.
"""
