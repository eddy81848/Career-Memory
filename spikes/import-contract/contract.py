from pathlib import Path

from jsonschema import Draft202012Validator
from yaml import safe_load

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts/import-v1/openapi.yaml"
CONTRACT = safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def validator_for(schema):
    # Keep the components at the document root for OpenAPI JSON pointers.
    return Draft202012Validator(
        {"allOf": [schema], "components": CONTRACT["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
