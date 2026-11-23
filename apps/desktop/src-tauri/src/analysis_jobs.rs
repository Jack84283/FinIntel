//! Fixed managed Analysis Library transport and independent wire validation.

use std::collections::{BTreeMap, HashSet};
use std::sync::OnceLock;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use time::{format_description::well_known::Rfc3339, Date, OffsetDateTime};

use super::{valid_index_history_date, valid_model_job_hash, valid_model_job_id};

const PATH: &str = "/api/v1/models/analysis/jobs";
const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024 + 8192;
const MALFORMED: &str = "invalid_response";
const INVALID: &str = "invalid_input";
pub(super) const STATES: [&str; 5] = ["queued", "running", "completed", "failed", "cancelled"];
pub(super) const WORKER_ERRORS: [&str; 7] = [
    "interrupted",
    "invalid_input",
    "quality_blocked",
    "source_changed",
    "storage_unavailable",
    "resource_limit",
    "worker_failure",
];

pub(super) type Checked<T> = Result<T, &'static str>;

#[derive(Deserialize)]
struct ModelContract {
    version: String,
    revision: String,
    calculation: String,
    min_sources: usize,
    max_sources: usize,
    allows_unverified: bool,
    parameters: BTreeMap<String, ParameterRule>,
    output_fields: Vec<OutputField>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ParameterRule {
    default: Value,
    minimum: Option<i64>,
    maximum: Option<i64>,
    exclusive_maximum: Option<i64>,
    #[serde(rename = "enum")]
    allowed: Option<Vec<i64>>,
}

#[derive(Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct OutputField {
    name: String,
    kind: String,
    units: String,
    nullable: bool,
    strictly_positive: bool,
    allowed_values: Vec<String>,
}

fn models() -> &'static BTreeMap<String, ModelContract> {
    static MODELS: OnceLock<BTreeMap<String, ModelContract>> = OnceLock::new();
    MODELS.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../contracts/analysis-jobs.json"))
            .expect("valid checked-in analysis contract")
    })
}

pub(super) fn exact<'a>(value: &'a Value, keys: &[&str]) -> Checked<&'a Map<String, Value>> {
    let object = value.as_object().ok_or(MALFORMED)?;
    if object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key)) {
        return Err(MALFORMED);
    }
    Ok(object)
}

pub(super) fn string(value: &Value) -> Checked<&str> {
    value.as_str().ok_or(MALFORMED)
}

pub(super) fn integer(value: &Value, min: u64, max: u64) -> bool {
    value.as_u64().is_some_and(|n| (min..=max).contains(&n))
}

pub(super) fn timestamp(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        text.len() <= 40
            && OffsetDateTime::parse(text, &Rfc3339).is_ok_and(|date| date.offset().is_utc())
    })
}

fn date(value: &Value) -> Checked<Date> {
    let text = string(value)?;
    if !valid_index_history_date(text) {
        return Err(MALFORMED);
    }
    Date::from_calendar_date(
        text[..4].parse().map_err(|_| MALFORMED)?,
        text[5..7]
            .parse::<u8>()
            .ok()
            .and_then(|m| m.try_into().ok())
            .ok_or(MALFORMED)?,
        text[8..].parse().map_err(|_| MALFORMED)?,
    )
    .map_err(|_| MALFORMED)
}

pub(super) fn hash(value: &Value) -> bool {
    value.as_str().is_some_and(valid_model_job_hash)
}

pub(super) fn canonical_hash(value: &Value) -> String {
    // Validation admits only ASCII strings and safe integers in hashed fields.
    let mut sorted = value.clone();
    sorted.sort_all_objects();
    format!("{:x}", Sha256::digest(sorted.to_string().as_bytes()))
}

pub(super) fn decimal(text: &str, positive: bool) -> bool {
    let magnitude = text.strip_prefix('-').unwrap_or(text);
    let (whole, fraction) = magnitude.split_once('.').unwrap_or((magnitude, ""));
    !whole.is_empty()
        && (whole.len() == 1 || !whole.starts_with('0'))
        && whole.bytes().all(|byte| byte.is_ascii_digit())
        && (!magnitude.contains('.')
            || (!fraction.is_empty() && fraction.bytes().all(|byte| byte.is_ascii_digit())))
        && (!positive
            || (magnitude == text && magnitude.bytes().any(|byte| matches!(byte, b'1'..=b'9'))))
}

fn neutrality_band(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        let (whole, fraction) = text.split_once('.').unwrap_or((text, ""));
        matches!(whole, "0" | "1")
            && (text == whole || (1..=6).contains(&fraction.len()))
            && fraction
                .bytes()
                .all(|b| b.is_ascii_digit() && (whole == "0" || b == b'0'))
    })
}

fn parameters(id: &str, model: &ModelContract, value: &Value) -> Checked<Value> {
    let provided = value.as_object().ok_or(MALFORMED)?;
    if provided
        .keys()
        .any(|key| !model.parameters.contains_key(key))
    {
        return Err(MALFORMED);
    }
    let mut normalized = Map::new();
    for (name, rule) in &model.parameters {
        let value = provided.get(name).unwrap_or(&rule.default);
        let valid = if name == "neutrality_band" {
            neutrality_band(value)
        } else {
            value.as_i64().is_some_and(|n| {
                rule.minimum.is_none_or(|min| n >= min)
                    && rule.maximum.is_none_or(|max| n <= max)
                    && rule.exclusive_maximum.is_none_or(|max| n < max)
                    && rule
                        .allowed
                        .as_ref()
                        .is_none_or(|allowed| allowed.contains(&n))
            })
        };
        if !valid {
            return Err(MALFORMED);
        }
        normalized.insert(name.clone(), value.clone());
    }
    let n = |key: &str| normalized[key].as_i64().unwrap_or_default();
    let valid = match id {
        "builtin.ema_trend" => n("fast_window") < n("slow_window"),
        "builtin.rsi_state" => n("lower_threshold") < n("upper_threshold"),
        "builtin.market_regime" => {
            n("short_window") < n("long_window")
                && n("volatility_window") + n("calibration_window") < 2000
        }
        _ => true,
    };
    valid.then_some(Value::Object(normalized)).ok_or(MALFORMED)
}

fn source_code(identity: &Value) -> Checked<&str> {
    exact(
        identity,
        &[
            "instrument_kind",
            "code",
            "provider",
            "adjustment",
            "return_semantics",
            "frequency",
            "units",
        ],
    )?;
    let code = string(&identity["code"])?;
    if !matches!(code, "SP500" | "DJIA" | "NASDAQCOM" | "NDX")
        || identity["instrument_kind"] != "index"
        || identity["provider"] != "fred"
        || identity["adjustment"] != "price_index"
        || identity["return_semantics"] != "price_index"
        || identity["frequency"] != "daily"
        || identity["units"] != "index_points"
    {
        return Err(MALFORMED);
    }
    Ok(code)
}

fn request(value: &Value, revision: &Value) -> Checked<Value> {
    exact(
        value,
        &[
            "contract_version",
            "model_id",
            "model_version",
            "sources",
            "window",
            "parameters",
            "quality_policy",
        ],
    )?;
    let id = string(&value["model_id"])?;
    let model = models().get(id).ok_or(MALFORMED)?;
    let policy = string(&value["quality_policy"])?;
    if value["contract_version"] != "analysis-model-request-v1"
        || value["model_version"] != model.version
        || revision != &model.revision
        || !(policy == "require_complete"
            || (policy == "allow_unverified" && model.allows_unverified))
    {
        return Err(MALFORMED);
    }
    let window = &value["window"];
    exact(window, &["start", "end"])?;
    let end = date(&window["end"])?;
    if date(&window["start"])? > end || end > OffsetDateTime::now_utc().date() {
        return Err(MALFORMED);
    }
    let sources = value["sources"].as_array().ok_or(MALFORMED)?;
    let mut codes = HashSet::new();
    if !(model.min_sources..=model.max_sources).contains(&sources.len()) {
        return Err(MALFORMED);
    }
    for source in sources {
        exact(source, &["identity", "snapshot_sha256"])?;
        if !codes.insert(source_code(&source["identity"])?) || !hash(&source["snapshot_sha256"]) {
            return Err(MALFORMED);
        }
    }
    let mut normalized = value.clone();
    normalized["parameters"] = parameters(id, model, &value["parameters"])?;
    Ok(normalized)
}

pub(super) const SUMMARY_KEYS: &[&str] = &[
    "id",
    "state",
    "status",
    "model_id",
    "model_version",
    "implementation_revision",
    "request_sha256",
    "request",
    "attempts",
    "progress",
    "cancel_requested",
    "output_sha256",
    "source_snapshot_sha256",
    "error_code",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
];

fn summary(value: &Value, expected_id: Option<&str>, include_result: bool) -> Checked<()> {
    let mut keys = SUMMARY_KEYS.to_vec();
    if include_result {
        keys.push("result");
    }
    exact(value, &keys)?;
    let id = string(&value["id"])?;
    let state = string(&value["state"])?;
    let original = &value["request"];
    if !valid_model_job_id(id)
        || expected_id.is_some_and(|expected| id != expected)
        || !STATES.contains(&state)
        || !integer(&value["cancel_requested"], 0, 1)
        || value["status"]
            != if state == "running" && value["cancel_requested"] == 1 {
                "cancel_requested"
            } else {
                state
            }
        || !integer(&value["attempts"], 0, 3)
        || !integer(&value["progress"], 0, 100)
        || !timestamp(&value["created_at"])
        || !timestamp(&value["updated_at"])
        || (!value["started_at"].is_null() && !timestamp(&value["started_at"]))
        || (!value["finished_at"].is_null() && !timestamp(&value["finished_at"]))
        || (!value["error_code"].is_null()
            && !WORKER_ERRORS.contains(&string(&value["error_code"])?))
        || (state == "failed") != !value["error_code"].is_null()
        || request(original, &value["implementation_revision"])? != *original
        || value["model_id"] != original["model_id"]
        || value["model_version"] != original["model_version"]
        || value["request_sha256"] != canonical_hash(original)
    {
        return Err(MALFORMED);
    }
    let sources = original["sources"].as_array().ok_or(MALFORMED)?;
    if state == "completed" {
        let snapshot = if sources.len() == 1 {
            &sources[0]["snapshot_sha256"]
        } else {
            &Value::Null
        };
        if value["progress"] != 100
            || !hash(&value["output_sha256"])
            || &value["source_snapshot_sha256"] != snapshot
        {
            return Err(MALFORMED);
        }
        if include_result {
            validate_result(&value["result"], value)?;
        }
    } else if !value["output_sha256"].is_null()
        || !value["source_snapshot_sha256"].is_null()
        || !value["result"].is_null()
    {
        return Err(MALFORMED);
    }
    Ok(())
}

fn warnings(value: &Value) -> Checked<&Vec<Value>> {
    let codes = value.as_array().ok_or(MALFORMED)?;
    if !codes.iter().all(|entry| {
        entry.as_str().is_some_and(|code| {
            let bytes = code.as_bytes();
            (1..=128).contains(&bytes.len())
                && bytes[0].is_ascii_alphanumeric()
                && !bytes[0].is_ascii_uppercase()
                && bytes
                    .iter()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(b))
        })
    }) {
        return Err(MALFORMED);
    }
    Ok(codes)
}

fn validate_source(
    source: &Value,
    pin: &Value,
    first: Date,
    last: Date,
    requested: &Value,
    count: usize,
    minimum: u64,
    policy: &Value,
) -> Checked<()> {
    exact(
        source,
        &[
            "identity",
            "snapshot_sha256",
            "observed",
            "aligned_count",
            "excluded_count",
            "quality",
        ],
    )?;
    let observed = &source["observed"];
    exact(observed, &["start", "end", "count"])?;
    let start = date(&observed["start"])?;
    let end = date(&observed["end"])?;
    let observed_count = observed["count"].as_u64().ok_or(MALFORMED)?;
    let excluded = source["excluded_count"].as_u64().ok_or(MALFORMED)?;
    let quality = &source["quality"];
    exact(quality, &["status", "warnings"])?;
    let status = string(&quality["status"])?;
    let codes = warnings(&quality["warnings"])?;
    if source["identity"] != pin["identity"]
        || source["snapshot_sha256"] != pin["snapshot_sha256"]
        || start < date(&requested["start"])?
        || end > date(&requested["end"])?
        || start > first
        || end < last
        || !(minimum..=2000).contains(&observed_count)
        || observed_count as i64 > (end - start).whole_days() + 1
        || source["aligned_count"] != count
        || excluded.checked_add(count as u64) != Some(observed_count)
        || count as u64 + u64::from(start < first) + u64::from(last < end) > observed_count
        || !matches!(
            status,
            "verified" | "not_run" | "partial" | "stale" | "blocked" | "failed"
        )
        || (status != "verified" && (codes.is_empty() || policy == "require_complete"))
    {
        return Err(MALFORMED);
    }
    Ok(())
}

fn accepts(field: &OutputField, value: &Value) -> bool {
    if value.is_null() {
        return field.nullable;
    }
    match field.kind.as_str() {
        "date" => date(value).is_ok(),
        "decimal" => value
            .as_str()
            .is_some_and(|text| decimal(text, field.strictly_positive)),
        "integer" => value
            .as_i64()
            .is_some_and(|n| (-9_007_199_254_740_991..=9_007_199_254_740_991).contains(&n)),
        "boolean" => value.is_boolean(),
        "enum" => value
            .as_str()
            .is_some_and(|text| field.allowed_values.iter().any(|item| item == text)),
        "reason_codes" => value.as_array().is_some_and(|values| {
            let mut seen = HashSet::new();
            values.iter().all(|v| {
                v.as_str().is_some_and(|text| {
                    field.allowed_values.iter().any(|item| item == text) && seen.insert(text)
                })
            })
        }),
        _ => false,
    }
}

fn validate_result(result: &Value, job: &Value) -> Checked<()> {
    exact(
        result,
        &[
            "contract_version",
            "evidence",
            "points",
            "reproducibility_sha256",
            "output_sha256",
            "evaluated_at_utc",
        ],
    )?;
    let request = &job["request"];
    let id = string(&request["model_id"])?;
    let model = models().get(id).ok_or(MALFORMED)?;
    let evidence = &result["evidence"];
    exact(
        evidence,
        &[
            "contract_version",
            "model",
            "calculation_version",
            "sources",
            "requested",
            "observed_sessions",
            "parameters",
            "output_fields",
            "quality_policy",
            "warnings",
            "research_only",
        ],
    )?;
    let identity = json!({
        "model_id": id,
        "model_version": model.version,
        "implementation_revision": model.revision,
    });
    let fields: Vec<OutputField> =
        serde_json::from_value(evidence["output_fields"].clone()).map_err(|_| MALFORMED)?;
    let codes = warnings(&evidence["warnings"])?;
    if result["contract_version"] != "analysis-model-result-v1"
        || !timestamp(&result["evaluated_at_utc"])
        || evidence["contract_version"] != "analysis-model-evidence-v1"
        || evidence["model"] != identity
        || evidence["calculation_version"] != model.calculation
        || evidence["requested"] != request["window"]
        || evidence["parameters"] != request["parameters"]
        || evidence["quality_policy"] != request["quality_policy"]
        || evidence["research_only"] != true
        || fields != model.output_fields
        || (request["quality_policy"] == "allow_unverified"
            && !codes.iter().any(|code| code == "quality_override"))
    {
        return Err(MALFORMED);
    }
    let sessions = evidence["observed_sessions"].as_array().ok_or(MALFORMED)?;
    let points = result["points"].as_array().ok_or(MALFORMED)?;
    let p = &request["parameters"];
    let minimum = match id {
        "builtin.ema_trend" => p["slow_window"].as_u64().ok_or(MALFORMED)?,
        "builtin.momentum" => p["lookback_sessions"].as_u64().ok_or(MALFORMED)? + 1,
        "builtin.relative_strength" => 2,
        _ => 1,
    };
    if !(minimum as usize..=2000).contains(&sessions.len()) || points.len() != sessions.len() {
        return Err(MALFORMED);
    }
    let pins = request["sources"].as_array().ok_or(MALFORMED)?;
    let sources = evidence["sources"].as_array().ok_or(MALFORMED)?;
    if sources.len() != pins.len() {
        return Err(MALFORMED);
    }
    let first = date(&sessions[0])?;
    let last = date(&sessions[sessions.len() - 1])?;
    for (source, pin) in sources.iter().zip(pins) {
        validate_source(
            source,
            pin,
            first,
            last,
            &request["window"],
            sessions.len(),
            minimum,
            &request["quality_policy"],
        )?;
    }
    let names: Vec<_> = fields.iter().map(|field| field.name.as_str()).collect();
    let mut previous = None;
    let benchmark = source_code(&pins[0]["identity"])?.to_ascii_lowercase();
    let relative_fields = if id == "builtin.relative_strength" {
        let selected = pins
            .iter()
            .map(|pin| source_code(&pin["identity"]).map(str::to_ascii_lowercase))
            .collect::<Checked<HashSet<_>>>()?;
        fields
            .iter()
            .filter(|field| field.kind == "decimal")
            .map(|field| {
                let (code, _) = field.name.split_once('_').ok_or(MALFORMED)?;
                Ok((field.name.as_str(), selected.contains(code)))
            })
            .collect::<Checked<Vec<_>>>()?
    } else {
        Vec::new()
    };
    let single_source_risk = id == "builtin.risk_profile" && pins.len() == 1;
    for (point, session) in points.iter().zip(sessions) {
        let day = date(session)?;
        exact(point, &names)?;
        if previous.is_some_and(|prev| prev >= day)
            || &point["session_date"] != session
            || fields
                .iter()
                .any(|field| !accepts(field, &point[&field.name]))
            || (id == "builtin.relative_strength" && point["benchmark"] != benchmark)
            || relative_fields
                .iter()
                .any(|(name, selected)| point[*name].is_null() == *selected)
            || (single_source_risk
                && (!point["comparison_return"].is_null() || !point["correlation"].is_null()))
            || (id == "builtin.momentum" && point["lookback_sessions"] != p["lookback_sessions"])
            || (id == "builtin.rsi_state"
                && (point["lower_threshold"] != p["lower_threshold"]
                    || point["upper_threshold"] != p["upper_threshold"]))
        {
            return Err(MALFORMED);
        }
        previous = Some(day);
    }
    if result["reproducibility_sha256"] != canonical_hash(evidence)
        || result["output_sha256"] != canonical_hash(&result["points"])
        || result["output_sha256"] != job["output_sha256"]
    {
        return Err(MALFORMED);
    }
    Ok(())
}

pub(super) fn valid_status(status: &str) -> bool {
    status == "cancel_requested" || STATES.contains(&status)
}

pub(super) enum Operation {
    Create(Value),
    List {
        limit: u8,
        offset: u32,
        status: Option<String>,
    },
    Get {
        id: String,
        include_result: bool,
    },
    Cancel(String),
    Retry(String),
}

impl Operation {
    fn validate(&self) -> Checked<Option<Value>> {
        match self {
            Self::Create(payload) => {
                let mut original = payload.clone();
                let revision = original
                    .as_object_mut()
                    .ok_or(INVALID)?
                    .remove("implementation_revision")
                    .ok_or(INVALID)?;
                request(&original, &revision).map(Some).map_err(|_| INVALID)
            }
            Self::List {
                limit,
                offset,
                status,
            } => {
                if (1..=100).contains(limit)
                    && *offset <= 10_000
                    && status.as_deref().is_none_or(valid_status)
                {
                    Ok(None)
                } else {
                    Err(INVALID)
                }
            }
            Self::Get { id, .. } | Self::Cancel(id) | Self::Retry(id) => {
                if valid_model_job_id(id) {
                    Ok(None)
                } else {
                    Err(INVALID)
                }
            }
        }
    }

    pub(super) fn http_request(
        &self,
        client: &reqwest::Client,
        base: &str,
        token: &str,
        path: &str,
    ) -> reqwest::RequestBuilder {
        let url = format!("{base}{path}");
        let request = match self {
            Self::Create(payload) => client.post(url).json(payload),
            Self::List {
                limit,
                offset,
                status,
            } => {
                let request = client
                    .get(url)
                    .query(&[("limit", limit.to_string()), ("offset", offset.to_string())]);
                match status {
                    Some(status) => request.query(&[("status", status)]),
                    None => request,
                }
            }
            Self::Get { id, include_result } => client
                .get(format!("{url}/{id}"))
                .query(&[("include_result", include_result)]),
            Self::Cancel(id) => client.post(format!("{url}/{id}/cancel")),
            Self::Retry(id) => client.post(format!("{url}/{id}/retry")),
        };
        request.header("x-finintel-process-token", token)
    }

    fn response(&self, payload: &Value, original: Option<Value>) -> Checked<()> {
        match self {
            Self::Create(submitted) => {
                summary(payload, None, false)?;
                if original.as_ref() != Some(&payload["request"])
                    || payload["implementation_revision"] != submitted["implementation_revision"]
                    || payload["state"] != "queued"
                    || payload["attempts"] != 0
                {
                    return Err(MALFORMED);
                }
                Ok(())
            }
            Self::List {
                limit,
                offset,
                status,
            } => {
                exact(payload, &["total", "items"])?;
                let items = payload["items"].as_array().ok_or(MALFORMED)?;
                let total = payload["total"]
                    .as_u64()
                    .filter(|total| *total <= 9_007_199_254_740_991)
                    .ok_or(MALFORMED)?;
                if items.len() as u64
                    != total
                        .saturating_sub(u64::from(*offset))
                        .min(u64::from(*limit))
                {
                    return Err(MALFORMED);
                }
                let mut ids = HashSet::new();
                for item in items {
                    summary(item, None, false)?;
                    if !ids.insert(string(&item["id"])?)
                        || status
                            .as_ref()
                            .is_some_and(|status| &item["status"] != status)
                    {
                        return Err(MALFORMED);
                    }
                }
                Ok(())
            }
            Self::Get { id, include_result } => summary(payload, Some(id), *include_result),
            Self::Retry(id) => summary(payload, Some(id), false),
            Self::Cancel(_) => {
                exact(payload, &["state"])?;
                valid_status(string(&payload["state"])?)
                    .then_some(())
                    .ok_or(MALFORMED)
            }
        }
    }
}

fn safe_error(status: u16, payload: &Value) -> &'static str {
    match (status, payload["detail"].as_str()) {
        (403, Some("managed_required")) => "managed_required",
        (404, Some("job_not_found")) => "job_not_found",
        (409, Some("queue_full")) => "queue_full",
        (409, Some("job_conflict")) => "job_conflict",
        (409, Some("source_changed")) => "source_changed",
        (422, Some("invalid_input")) => "invalid_input",
        (422, Some("quality_blocked")) => "quality_blocked",
        (422, Some("resource_limit")) => "resource_limit",
        (503, Some("storage_unavailable")) => "storage_unavailable",
        _ => MALFORMED,
    }
}

async fn execute(operation: Operation) -> Result<Value, String> {
    if super::backend_token().is_empty() {
        return Err("managed_required".into());
    }
    let original = operation.validate()?;
    let payload = managed_job_response(&operation, PATH).await?;
    operation.response(&payload, original)?;
    Ok(payload)
}

pub(super) async fn managed_job_response(
    operation: &Operation,
    path: &str,
) -> Result<Value, String> {
    let token = super::backend_token();
    if token.is_empty() {
        return Err("managed_required".into());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(35))
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "backend_unavailable")?;
    let mut response = operation
        .http_request(&client, &super::backend_base(), token, path)
        .send()
        .await
        .map_err(|_| "backend_unavailable")?;
    let status = response.status().as_u16();
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "backend_unavailable")? {
        if body.len() + chunk.len() > MAX_RESPONSE_BYTES {
            return Err(MALFORMED.into());
        }
        body.extend_from_slice(&chunk);
    }
    let payload = serde_json::from_slice(&body).map_err(|_| MALFORMED)?;
    let success = if matches!(&operation, Operation::Create(_)) {
        202
    } else {
        200
    };
    if status != success {
        return Err(safe_error(status, &payload).into());
    }
    Ok(payload)
}

#[tauri::command]
pub async fn analysis_jobs_create(request: Value) -> Result<Value, String> {
    execute(Operation::Create(request)).await
}

#[tauri::command]
pub async fn analysis_jobs_list(
    limit: u8,
    offset: u32,
    status: Option<String>,
) -> Result<Value, String> {
    execute(Operation::List {
        limit,
        offset,
        status,
    })
    .await
}

#[tauri::command]
pub async fn analysis_jobs_get(job_id: String, include_result: bool) -> Result<Value, String> {
    execute(Operation::Get {
        id: job_id,
        include_result,
    })
    .await
}

#[tauri::command]
pub async fn analysis_jobs_cancel(job_id: String) -> Result<Value, String> {
    execute(Operation::Cancel(job_id)).await
}

#[tauri::command]
pub async fn analysis_jobs_retry(job_id: String) -> Result<Value, String> {
    execute(Operation::Retry(job_id)).await
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "123e4567-e89b-12d3-a456-426614174000";

    fn fixture() -> Value {
        serde_json::from_str(include_str!(
            "../../../../contracts/test-fixtures/analysis-relative-strength.json"
        ))
        .unwrap()
    }

    fn input() -> Value {
        let fixture = fixture();
        let mut request = fixture["request"].clone();
        request["implementation_revision"] = fixture["implementation_revision"].clone();
        request
    }

    #[test]
    fn fixed_routes_only_use_managed_loopback_and_token() {
        let client = reqwest::Client::new();
        let base = "http://127.0.0.1:1234";
        let operations = [
            (Operation::Create(input()), "POST", PATH.to_owned()),
            (
                Operation::List {
                    limit: 10,
                    offset: 20,
                    status: Some("completed".into()),
                },
                "GET",
                format!("{PATH}?limit=10&offset=20&status=completed"),
            ),
            (
                Operation::Get {
                    id: ID.into(),
                    include_result: true,
                },
                "GET",
                format!("{PATH}/{ID}?include_result=true"),
            ),
            (
                Operation::Cancel(ID.into()),
                "POST",
                format!("{PATH}/{ID}/cancel"),
            ),
            (
                Operation::Retry(ID.into()),
                "POST",
                format!("{PATH}/{ID}/retry"),
            ),
        ];
        for (operation, method, url) in operations {
            assert!(operation.validate().is_ok());
            let request = operation
                .http_request(&client, base, "test-process-token", PATH)
                .build()
                .unwrap();
            assert_eq!(request.method().as_str(), method);
            assert_eq!(request.url().as_str(), format!("{base}{url}"));
            assert_eq!(
                request.headers()["x-finintel-process-token"],
                "test-process-token"
            );
        }
        assert_eq!(
            Operation::Cancel("../../anything".into()).validate(),
            Err(INVALID)
        );
        assert_eq!(
            Operation::List {
                limit: 101,
                offset: 0,
                status: None
            }
            .validate(),
            Err(INVALID)
        );
        assert_eq!(
            safe_error(409, &json!({"detail": "source_changed"})),
            "source_changed"
        );
        assert_eq!(
            safe_error(409, &json!({"detail": "private database path"})),
            MALFORMED
        );
    }

    fn rehash_output(response: &mut Value) {
        let hash = json!(canonical_hash(&response["result"]["points"]));
        response["result"]["output_sha256"] = hash.clone();
        response["output_sha256"] = hash;
    }

    #[test]
    fn rejects_output_that_disagrees_with_pinned_source_membership() {
        let operation = Operation::Get {
            id: ID.into(),
            include_result: true,
        };
        let relative = fixture();
        for (field, value) in [
            ("ndx_normalized_100", Value::Null),
            ("djia_relative_strength", json!("1")),
        ] {
            let mut malformed = relative.clone();
            malformed["result"]["points"][1][field] = value;
            rehash_output(&mut malformed);
            assert_eq!(operation.response(&malformed, None), Err(MALFORMED));
        }

        let id = "builtin.risk_profile";
        let model = &models()[id];
        let schema: Value =
            serde_json::from_str(include_str!("../../../../contracts/analysis-jobs.json")).unwrap();
        let mut risk = relative;
        risk["model_id"] = json!(id);
        risk["implementation_revision"] = json!(model.revision);
        risk["request"]["model_id"] = json!(id);
        risk["request"]["parameters"] = parameters(id, model, &json!({})).unwrap();
        risk["request"]["sources"]
            .as_array_mut()
            .unwrap()
            .truncate(1);
        risk["request_sha256"] = json!(canonical_hash(&risk["request"]));
        risk["source_snapshot_sha256"] = risk["request"]["sources"][0]["snapshot_sha256"].clone();

        let resolved_parameters = risk["request"]["parameters"].clone();
        let evidence = &mut risk["result"]["evidence"];
        evidence["model"]["model_id"] = json!(id);
        evidence["model"]["implementation_revision"] = json!(model.revision);
        evidence["calculation_version"] = json!(model.calculation);
        evidence["parameters"] = resolved_parameters;
        evidence["sources"].as_array_mut().unwrap().truncate(1);
        evidence["output_fields"] = schema[id]["output_fields"].clone();
        let points: Vec<Value> = evidence["observed_sessions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|session| {
                Value::Object(
                    model
                        .output_fields
                        .iter()
                        .map(|field| {
                            let value = match field.kind.as_str() {
                                _ if field.nullable => Value::Null,
                                "date" => session.clone(),
                                "decimal" => json!("1"),
                                "integer" => json!(0),
                                "enum" => json!(field.allowed_values[0]),
                                _ => unreachable!("unexpected fixture field"),
                            };
                            (field.name.clone(), value)
                        })
                        .collect(),
                )
            })
            .collect();
        risk["result"]["reproducibility_sha256"] = json!(canonical_hash(evidence));
        risk["result"]["points"] = json!(points);
        rehash_output(&mut risk);
        assert!(operation.response(&risk, None).is_ok());

        for field in ["comparison_return", "correlation"] {
            let mut malformed = risk.clone();
            malformed["result"]["points"][1][field] = json!("0.1");
            rehash_output(&mut malformed);
            assert_eq!(operation.response(&malformed, None), Err(MALFORMED));
        }
    }

    #[test]
    fn rejects_bad_request_and_malformed_multi_source_result() {
        let mut wrong = input();
        wrong["sources"][1] = wrong["sources"][0].clone();
        assert_eq!(Operation::Create(wrong).validate(), Err(INVALID));
        let mut wrong = input();
        wrong["implementation_revision"] = json!("unknown");
        assert_eq!(Operation::Create(wrong).validate(), Err(INVALID));
        let mut wrong = input();
        wrong["parameters"]["lookback_sessions"] = json!(22);
        assert_eq!(Operation::Create(wrong).validate(), Err(INVALID));

        let operation = Operation::Get {
            id: ID.into(),
            include_result: true,
        };
        let response = fixture();
        assert!(operation.response(&response, None).is_ok());
        let mut wrong = response.clone();
        wrong["result"]["evidence"]["sources"]
            .as_array_mut()
            .unwrap()
            .reverse();
        wrong["result"]["reproducibility_sha256"] =
            json!(canonical_hash(&wrong["result"]["evidence"]));
        assert_eq!(operation.response(&wrong, None), Err(MALFORMED));
        let mut wrong = response.clone();
        wrong["source_snapshot_sha256"] = wrong["request"]["sources"][0]["snapshot_sha256"].clone();
        assert_eq!(operation.response(&wrong, None), Err(MALFORMED));
        let mut wrong = response.clone();
        wrong["result"]["points"][0]["benchmark"] = json!("sp500");
        wrong["result"]["output_sha256"] = json!(canonical_hash(&wrong["result"]["points"]));
        wrong["output_sha256"] = wrong["result"]["output_sha256"].clone();
        assert_eq!(operation.response(&wrong, None), Err(MALFORMED));
        let mut wrong = response.clone();
        wrong["result"]["output_sha256"] = json!("0".repeat(64));
        assert_eq!(operation.response(&wrong, None), Err(MALFORMED));
        let mut wrong = response;
        wrong["private"] = json!("sensitive backend field");
        assert_eq!(operation.response(&wrong, None), Err(MALFORMED));
    }
}
