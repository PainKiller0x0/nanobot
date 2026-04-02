"""Configuration validator for startup-time validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationError:
    field: str
    message: str


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid


# Built-in schemas for common config sections
AGENT_DEFAULTS_SCHEMA = {
    "model": {"type": "str", "required": False},
    "temperature": {"type": "float", "min": 0.0, "max": 2.0},
    "max_tokens": {"type": "int", "min": 1, "max": 200_000},
    "max_tool_iterations": {"type": "int", "min": 1, "max": 1000},
    "context_window_tokens": {"type": "int", "min": 1},
}


class ConfigValidator:
    """Validates configuration at startup to prevent runtime errors.

    Usage:
        validator = ConfigValidator(schema=MY_SCHEMA)
        result = validator.validate(config)
        if not result:
            for err in result.errors:
                print(f"Error: {err.field} - {err.message}")
    """

    # Common field validators
    TYPE_VALIDATORS = {
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int),
        "float": lambda v: isinstance(v, (int, float)),
        "bool": lambda v: isinstance(v, bool),
        "list": lambda v: isinstance(v, list),
        "dict": lambda v: isinstance(v, dict),
    }

    def __init__(self, schema: dict[str, dict] | None = None):
        """Initialize with an optional schema.

        Schema format:
            {
                "field_name": {
                    "type": "str|int|float|bool|list|dict",
                    "required": bool,
                    "min": number (for int/float),
                    "max": number (for int/float),
                    "choices": list (enum),
                    "validator": callable,
                }
            }
        """
        self._schema = schema or {}

    def validate(self, config: dict[str, Any]) -> ValidationResult:
        """Validate a config dict against the schema."""
        errors: list[ValidationError] = []

        for field_name, field_schema in self._schema.items():
            value = config.get(field_name)
            expected_type = field_schema.get("type")

            # Required check
            if field_schema.get("required", False):
                if value is None:
                    errors.append(ValidationError(field_name, "required but missing"))
                    continue
                # Non-empty string required
                if expected_type == "str" and not value:
                    errors.append(ValidationError(field_name, "required but empty"))
                    continue

            if value is None:
                continue

            # Type check
            if expected_type and not self.TYPE_VALIDATORS.get(expected_type, lambda _: True)(value):
                errors.append(ValidationError(
                    field_name,
                    f"expected {expected_type}, got {type(value).__name__}"
                ))
                continue

            # Min/max for numbers
            if expected_type in ("int", "float"):
                if "min" in field_schema and value < field_schema["min"]:
                    errors.append(ValidationError(
                        field_name,
                        f"value {value} is below minimum {field_schema['min']}"
                    ))
                if "max" in field_schema and value > field_schema["max"]:
                    errors.append(ValidationError(
                        field_name,
                        f"value {value} exceeds maximum {field_schema['max']}"
                    ))

            # Enum choices
            if "choices" in field_schema and value not in field_schema["choices"]:
                errors.append(ValidationError(
                    field_name,
                    f"value '{value}' not in allowed choices: {field_schema['choices']}"
                ))

            # Custom validator
            custom = field_schema.get("validator")
            if custom and callable(custom):
                try:
                    if not custom(value):
                        errors.append(ValidationError(field_name, "custom validation failed"))
                except Exception as e:
                    errors.append(ValidationError(field_name, f"validator error: {e}"))

        return ValidationResult(valid=len(errors) == 0, errors=errors)
