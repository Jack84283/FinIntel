"""AL-01 contracts using only in-memory, synthetic research evidence."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finintel.main import app
from finintel.models.contracts import (
    AnalysisEvidence,
    AnalysisRequest,
    AnalysisResult,
    DateRange,
    ModelIdentity,
    ModelKey,
    ModelMetadata,
    ObservationRange,
    OutputField,
    QualityEvidence,
    SourceEvidence,
    SourceIdentity,
    SourcePin,
    canonical_sha256,
)
from finintel.models.registry import (
    MODEL_ID,
    MODEL_VERSION,
    SMA_DEFINITION,
    ModelDefinition,
    ModelParameters,
    _index_definitions,
    analysis_model_registry,
    get_model_definition,
    model_registry,
)

START = date(2024, 1, 2)
END = date(2024, 2, 2)
TIMESTAMP = datetime(2024, 2, 3, tzinfo=UTC)


def _request(*, pins=None, parameters=None, quality_policy="require_complete"):
    return AnalysisRequest(
        model_id=MODEL_ID,
        model_version=MODEL_VERSION,
        window=DateRange(start=START, end=END),
        sources=pins
        or (
            SourcePin(
                identity=SMA_DEFINITION.metadata.supported_sources[0], snapshot_sha256="a" * 64
            ),
        ),
        parameters=parameters or {},
        quality_policy=quality_policy,
    )


def _evidence(request, metadata=SMA_DEFINITION.metadata, **overrides):
    fields = {
        "model": ModelIdentity(
            model_id=metadata.model_id,
            model_version=metadata.model_version,
            implementation_revision=metadata.implementation_revision,
        ),
        "calculation_version": metadata.calculation_version,
        "sources": tuple(
            SourceEvidence(
                **pin.model_dump(),
                observed=ObservationRange(start=START, end=END, count=23),
                aligned_count=1,
                excluded_count=22,
                quality=QualityEvidence(status="verified"),
            )
            for pin in request.sources
        ),
        "requested": request.window,
        "observed_sessions": (END,),
        "parameters": ModelParameters.model_validate(request.parameters).model_dump(),
        "output_fields": metadata.output_fields,
        "quality_policy": request.quality_policy,
    }
    return AnalysisEvidence(**{**fields, **overrides})


def _result(evidence, now=TIMESTAMP, **point_overrides):
    point = {
        "session_date": END.isoformat(),
        "close": "4500.25",
        "fast_sma": "4498.2",
        "slow_sma": None,
        "signal_after_close": None,
    }
    return AnalysisResult.from_evaluation(
        evidence=evidence, points=({**point, **point_overrides},), evaluated_at_utc=now
    )


def test_registry_lookup_metadata_and_legacy_discovery():
    definition = get_model_definition(ModelKey(model_id=MODEL_ID, model_version=MODEL_VERSION))
    metadata = definition.metadata
    entry = next(
        item for item in analysis_model_registry()["models"] if item["model_id"] == MODEL_ID
    )
    (legacy,) = model_registry()["models"]
    assert entry["model_id"] == legacy["model_id"] == MODEL_ID
    assert entry["parameter_schema"] == legacy["parameter_schema"]
    assert entry["implementation_revision"] == legacy["implementation_revision"]
    assert entry["availability"] == "released"
    assert entry["research_only"] is True
    assert entry["execution"]["network_access"] is False
    assert entry["observations"]["maximum"] == legacy["input_contract"]["maximum_observations"]
    assert set(entry["parameter_schema"]["properties"]) == {"fast_window", "slow_window"}
    assert {source["code"] for source in entry["supported_sources"]} == set(
        legacy["input_contract"]["allowed_codes"]
    )
    assert {field["name"] for field in entry["output_fields"] if field["strictly_positive"]} == {
        "close",
        "fast_sma",
        "slow_sma",
    }
    entry["supported_sources"].clear()
    assert len(analysis_model_registry()["models"][0]["supported_sources"]) == 4
    with pytest.raises(ValidationError):
        metadata.research_only = False
    with pytest.raises(ValidationError):
        ModelMetadata(**{**metadata.model_dump(), "research_only": 1})
    with pytest.raises(ValidationError, match="Duplicate output field"):
        ModelMetadata(**{**metadata.model_dump(), "output_fields": metadata.output_fields * 2})


@pytest.mark.parametrize(
    ("identifier", "version"),
    [
        ("builtin.unknown", "1.0.0"),
        (MODEL_ID, "2.0.0"),
    ],
)
def test_unknown_model_version_is_rejected(identifier, version):
    with pytest.raises(ValueError, match="Unknown analysis model ID or version"):
        get_model_definition(ModelKey(model_id=identifier, model_version=version))


def test_model_version_and_duplicate_registration_are_rejected():
    with pytest.raises(ValidationError):
        ModelKey(model_id=MODEL_ID, model_version="01.0.0")
    with pytest.raises(ValueError, match="Duplicate analysis model ID and version"):
        _index_definitions(SMA_DEFINITION, SMA_DEFINITION)


@pytest.mark.parametrize(
    "parameters",
    [
        {"fast_window": "5"},
        {"fast_window": True},
        {"fast_window": 1},
        {"fast_window": 20},
        {"slow_window": 253},
        {"python_module": "test"},
    ],
)
def test_registry_uses_typed_parameter_schema(parameters):
    with pytest.raises(ValidationError):
        SMA_DEFINITION.validate_request(_request(parameters=parameters))
    assert SMA_DEFINITION.validate_request(_request()) == ModelParameters()


@pytest.mark.parametrize(
    "invalid",
    [
        {"code": "VIX"},
        {"provider": "other"},
        {"return_semantics": "total_return"},
        {"source_url": "https://example.com"},
        {"instrument_kind": "equity"},
    ],
)
def test_incompatible_source_identities_are_rejected(invalid):
    with pytest.raises(ValidationError):
        SourceIdentity(**{**SMA_DEFINITION.metadata.supported_sources[0].model_dump(), **invalid})


def test_request_validation_is_strict_and_snapshot_pinned():
    request = _request()
    assert AnalysisRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError, match="Duplicate"):
        _request(pins=request.sources * 2)
    with pytest.raises(ValidationError):
        SourcePin(identity=request.sources[0].identity, snapshot_sha256="A" * 64)
    with pytest.raises(ValidationError):
        _request(quality_policy="ignore")
    with pytest.raises(ValidationError):
        AnalysisRequest(**request.model_dump(), filesystem_path="/tmp/state")
    with pytest.raises(ValidationError):
        DateRange(start=END, end=START)


def test_result_round_trip_and_stable_hashes():
    request = _request()
    evidence = _evidence(request)
    result = _result(evidence)
    later = _result(evidence, now=TIMESTAMP + timedelta(seconds=60))
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result
    assert later.evaluated_at_utc != result.evaluated_at_utc
    assert later.reproducibility_sha256 == result.reproducibility_sha256
    assert later.output_sha256 == result.output_sha256
    SMA_DEFINITION.validate_result(request, result)
    assert _result(evidence, close="4500.26").output_sha256 != result.output_sha256
    assert canonical_sha256({"a": 1, "z": "中"}) == canonical_sha256({"z": "中", "a": 1})
    assert (
        result.output_sha256 == "09389bbb7033e28a4a76978bdcff14b1c6470c92a37147e8e9c5ff2e21721fa0"
    )
    assert (
        result.reproducibility_sha256
        == "fdc479377d1437a0bef5f3553ab3e20a2ff5a19f7e3940064537ed96a766f089"
    )
    with pytest.raises(ValidationError, match="Output hash|output hash"):
        AnalysisResult(**{**result.model_dump(), "output_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="UTC"):
        _result(evidence, now=TIMESTAMP.replace(tzinfo=None))
    result.points[0]["close"] = "4500.99"
    with pytest.raises(ValidationError, match="output hash"):
        SMA_DEFINITION.validate_result(request, result)


@pytest.mark.parametrize(
    "point",
    [
        {"close": "NaN"},
        {"close": 4500},
        {"close": None},
        {"signal_after_close": "buy"},
        {"new_field": "unknown"},
        {"session_date": START.isoformat()},
    ],
)
def test_result_points_match_schema_and_observed_dates(point):
    with pytest.raises(ValidationError):
        _result(_evidence(_request()), **point)


@pytest.mark.parametrize("field", ["close", "fast_sma", "slow_sma"])
@pytest.mark.parametrize("value", ["0", "-1", "-0.00"])
def test_sma_result_rejects_nonpositive_prices(field, value):
    with pytest.raises(ValidationError, match="Point does not match the declared output schema"):
        _result(_evidence(_request()), **{field: value})


def test_decimal_output_positivity_is_opt_in():
    field = OutputField(name="drawdown", kind="decimal", units="percent", nullable=False)
    assert field.accepts("-1.25")
    assert field.accepts("0")
    with pytest.raises(ValidationError, match="only supported for decimal"):
        OutputField(
            name="session_date",
            kind="date",
            units="iso_date",
            nullable=False,
            strictly_positive=True,
        )


def test_result_binds_revision_and_parameter_dependent_range():
    request = _request(parameters={"slow_window": 30})
    with pytest.raises(ValueError, match="observation count"):
        SMA_DEFINITION.validate_result(request, _result(_evidence(request)))
    request = _request()
    model = _evidence(request).model.model_copy(update={"implementation_revision": "old-v1"})
    result = _result(_evidence(request, model=model))
    with pytest.raises(ValueError, match="registered model evidence contract"):
        SMA_DEFINITION.validate_result(request, result)


def test_quality_policy_contract():
    request = _request(quality_policy="allow_unverified")
    with pytest.raises(ValidationError, match="override"):
        _evidence(request)
    evidence = _evidence(request, warnings=("quality_override",))
    SMA_DEFINITION.validate_result(request, _result(evidence))
    with pytest.raises(ValidationError, match="explicit warning"):
        QualityEvidence(status="stale")
    stale = QualityEvidence(status="stale", warnings=("stale_quality_scan",))
    source = evidence.sources[0].model_copy(update={"quality": stale})
    with pytest.raises(ValidationError, match="verified source evidence"):
        _evidence(_request(), sources=(source,))


def test_proposed_metadata_is_discoverable_without_executability(monkeypatch):
    from finintel.models import registry

    definition = ModelDefinition(
        metadata=ModelMetadata(
            **{
                **SMA_DEFINITION.metadata.model_dump(),
                "model_id": "builtin.fixture_only",
                "max_sources": 2,
            }
        ),
        parameter_type=ModelParameters,
        availability="proposed",
        parameter_minimum=SMA_DEFINITION.parameter_minimum,
    )
    monkeypatch.setattr(registry, "_DEFINITIONS", _index_definitions(SMA_DEFINITION, definition))
    pins = tuple(
        SourcePin(identity=identity, snapshot_sha256=hash_)
        for identity, hash_ in zip(definition.metadata.supported_sources[:2], ("a" * 64, "b" * 64))
    )
    request = AnalysisRequest(
        **{**_request(pins=pins).model_dump(), "model_id": "builtin.fixture_only"}
    )
    assert registry.get_model_definition(request) == definition
    result = _result(_evidence(request, definition.metadata))
    definition.validate_result(request, result)
    changed_pin = pins[1].model_copy(update={"snapshot_sha256": "c" * 64})
    changed_request = _request(pins=(pins[0], changed_pin))
    other = _result(_evidence(changed_request, definition.metadata))
    assert other.reproducibility_sha256 != result.reproducibility_sha256
    assert other.output_sha256 == result.output_sha256
    with pytest.raises(ValueError, match="pinned"):
        definition.validate_result(request, other)

    token = "analysis-registry-contract-test-token-32bytes"
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    headers = {"x-finintel-process-token": token}
    with TestClient(app) as client:
        assert client.get("/api/v1/models/registry/v2").status_code == 403
        legacy = client.get("/api/v1/models/registry", headers=headers)
        catalog = client.get("/api/v1/models/registry/v2", headers=headers)
        assert legacy.json() == model_registry()
        assert catalog.status_code == 200
        assert catalog.json()["models"][1]["availability"] == "proposed"
        for route in ("/api/v1/models/evaluate", "/api/v1/models/jobs"):
            denied = client.post(
                route,
                headers=headers,
                json={
                    "model_id": request.model_id,
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                },
            )
            assert denied.status_code == 422


def test_source_alignment_counts_cover_reported_endpoints():
    request = _request()
    source = _evidence(request).sources[0]
    with pytest.raises(ValidationError, match="must sum"):
        SourceEvidence(**{**source.model_dump(), "excluded_count": 21})
    interior = START + timedelta(days=1)
    only_endpoints = SourceEvidence(
        **{
            **source.model_dump(),
            "observed": ObservationRange(start=START, end=END, count=2),
            "excluded_count": 1,
        }
    )
    # The aligned date is inside the source range but neither of its two known sessions.
    with pytest.raises(ValidationError, match="observed endpoints and aligned sessions"):
        _evidence(request, sources=(only_endpoints,), observed_sessions=(interior,))
    evidence = _evidence(request, sources=(only_endpoints,), observed_sessions=(END,))
    assert evidence.sources[0].aligned_count == 1
    assert evidence.sources[0].excluded_count == 1
    with pytest.raises(ValidationError, match="alignment count must match"):
        _evidence(request, sources=(only_endpoints,), observed_sessions=(START, END))


def test_declared_reason_codes_round_trip_and_hash_as_ordered_array():
    evidence = _evidence(
        _request(),
        output_fields=(
            *SMA_DEFINITION.metadata.output_fields,
            OutputField(
                name="reason_codes",
                kind="reason_codes",
                units="code",
                nullable=False,
                allowed_values=("insufficient_history", "volatility_unavailable"),
            ),
        ),
    )
    reasons = ("insufficient_history", "volatility_unavailable")
    result = _result(evidence, reason_codes=reasons)
    assert result.model_dump(mode="json")["points"][0]["reason_codes"] == list(reasons)
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result
    assert _result(evidence, reason_codes=reasons).output_sha256 == result.output_sha256
    assert _result(evidence, reason_codes=reasons[::-1]).output_sha256 != result.output_sha256
    assert _result(evidence, reason_codes=()).points[0]["reason_codes"] == ()
    for invalid in (reasons[0], ("unknown",), (reasons[0], reasons[0]), (1,), None):
        with pytest.raises(ValidationError):
            _result(evidence, reason_codes=invalid)
