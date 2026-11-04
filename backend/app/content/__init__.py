"""Phase 3A — content intelligence: platform specifications and planning."""

from app.content.platforms import (
    CONTENT_TYPES,
    ContentType,
    PlatformSpec,
    available_platforms,
    get_spec,
    spec_dict,
    validate_against_spec,
)

__all__ = [
    "CONTENT_TYPES",
    "ContentType",
    "PlatformSpec",
    "available_platforms",
    "get_spec",
    "spec_dict",
    "validate_against_spec",
]
