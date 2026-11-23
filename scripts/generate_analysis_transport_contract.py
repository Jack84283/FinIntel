"""Export the managed model registry subset shared by the native and web clients."""

import json
import re
from pathlib import Path

from finintel.models.contracts import ModelKey
from finintel.models.managed_approvals import MANAGED_ANALYSIS_REVISIONS
from finintel.models.registry import get_model_definition


def export_contracts() -> str:
    models = {}
    for (model_id, version), revision in sorted(MANAGED_ANALYSIS_REVISIONS.items()):
        definition = get_model_definition(
            ModelKey(model_id=model_id, model_version=version)
        )
        metadata = definition.metadata
        assert metadata.implementation_revision == revision
        properties = definition.parameter_type.model_json_schema()["properties"]
        models[model_id] = {
            "version": version,
            "revision": revision,
            "calculation": metadata.calculation_version,
            "min_sources": metadata.min_sources,
            "max_sources": metadata.max_sources,
            "allows_unverified": metadata.allows_unverified,
            "parameters": {
                name: {
                    key: value
                    for key, value in rule.items()
                    if key not in {"title", "description"}
                }
                for name, rule in properties.items()
            },
            "output_fields": [
                field.model_dump(mode="json") for field in metadata.output_fields
            ],
        }
    # Keep one complete output-field declaration per line for readable diffs.
    return (
        re.sub(
            r"      \{\n        \"name\":.*?\n      \}",
            lambda match: "      " + json.dumps(json.loads(match[0])),
            json.dumps(models, indent=2),
            flags=re.DOTALL,
        )
        + "\n"
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    (root / "contracts/analysis-jobs.json").write_text(
        export_contracts(), encoding="utf-8"
    )
