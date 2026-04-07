"""Application layer.

Application services that orchestrate domain logic and coordinate
with infrastructure via protocol interfaces. This layer depends on
domain protocols/entities and must not import concrete infrastructure
implementations (except in dependency-injection composition roots).
"""
