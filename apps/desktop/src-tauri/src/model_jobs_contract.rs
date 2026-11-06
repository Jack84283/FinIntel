//! Strict, allowlisted model-job responses at the desktop IPC boundary.
//! Never forward unknown backend properties or unverified model result shapes.

use serde::Deserialize;
use serde_json::Value;

const MAX_RESULT_POINTS: usize = 2_000;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JobSummary {
    id: String,
    state: String,
    status: String,
    model_id: String,
    model_version: String,
    request_sha256: String,
    attempts: u8,
    progress: u8,
    cancel_requested: u8,
    output_sha256: Option<String>,
    source_snapshot_sha256: Option<String>,
    error_code: Option<String>,
    created_at: String,
    updated_at: String,
    started_at: Option<String>,
    finished_at: Option<String>,
}

impl JobSummary {
    fn valid(&self) -> bool {
        uuid::Uuid::parse_str(&self.id).ok().is_some_and(|id| id.to_string() == self.id)
            && matches!(self.state.as_str(), "queued" | "running" | "completed" | "failed" | "cancelled")
            && self.status == if self.state == "running" && self.cancel_requested == 1 {
                "cancel_requested"
            } else {
                self.state.as_str()
            }
            && self.model_id == "builtin.sma_crossover"
            && self.model_version == "1.0.0"
            && valid_hash(&self.request_sha256)
            && self.attempts <= 3
            && self.progress <= 100
            && self.cancel_requested <= 1
            && self.output_sha256.as_deref().is_none_or(valid_hash)
            && self.source_snapshot_sha256.as_deref().is_none_or(valid_hash)
            && self.error_code.as_deref().is_none_or(|code| {
                matches!(code, "interrupted" | "invalid_input" | "quality_blocked"
                    | "source_changed" | "storage_unavailable" | "resource_limit"
                    | "worker_failure")
            })
            && !self.created_at.is_empty()
            && !self.updated_at.is_empty()
            && self.started_at.as_ref().is_none_or(|s| !s.is_empty())
            && self.finished_at.as_ref().is_none_or(|s| !s.is_empty())
            && (self.state != "completed"
                || (self.progress == 100 && self.output_sha256.is_some()
                    && self.source_snapshot_sha256.is_some()))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JobList {
    total: u64,
    items: Vec<JobSummary>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JobState {
    id: String,
    status: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Parameters {
    fast_window: u16,
    slow_window: u16,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceIdentity {
    instrument_kind: String,
    code: String,
    provider: String,
    adjustment: String,
    return_semantics: String,
    frequency: String,
    units: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Lineage {
    model_id: String,
    model_version: String,
    implementation_revision: String,
    parameters: Parameters,
    source_identity: SourceIdentity,
    source_snapshot_sha256: String,
    analysis_calculation_version: String,
    requested_start: String,
    requested_end: String,
    first_observed_session: String,
    last_observed_session: String,
    quality_policy: String,
    quality_status: String,
    quality_verified: bool,
    point_in_time_membership: String,
    dividends_included: bool,
    corporate_action_adjustment: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResearchPoint {
    session_date: String,
    close: String,
    fast_sma: Option<String>,
    slow_sma: Option<String>,
    signal_after_close: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResearchResult {
    execution_contract: String,
    lineage: Lineage,
    reproducibility_sha256: String,
    output_sha256: String,
    evaluated_at_utc: String,
    status: String,
    observations: u16,
    points: Vec<ResearchPoint>,
    quality_warning: Option<String>,
    read_only: bool,
    no_network: bool,
    no_synthetic_sessions: bool,
    signal_after_close_only: bool,
    backtest_validated: bool,
    persistent_job: bool,
}

fn valid_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn valid_index(value: &str) -> bool {
    matches!(value, "SP500" | "DJIA" | "NASDAQCOM" | "NDX")
}

fn valid_decimal(value: &str) -> bool {
    !value.is_empty() && value.len() <= 80
        && value.parse::<f64>().is_ok_and(f64::is_finite)
}

fn valid_result(result: &ResearchResult, summary: &JobSummary) -> bool {
    let lineage = &result.lineage;
    let identity = &lineage.source_identity;
    result.execution_contract == "model-research-evaluation-v1"
        && result.status == "completed"
        && summary.state == "completed"
        && result.read_only
        && result.no_network
        && result.no_synthetic_sessions
        && result.signal_after_close_only
        && !result.backtest_validated
        && !result.persistent_job
        && valid_hash(&result.output_sha256)
        && valid_hash(&result.reproducibility_sha256)
        && summary.output_sha256.as_deref() == Some(result.output_sha256.as_str())
        && summary.source_snapshot_sha256.as_deref() == Some(lineage.source_snapshot_sha256.as_str())
        && lineage.model_id == summary.model_id
        && lineage.model_version == summary.model_version
        && !lineage.implementation_revision.is_empty()
        && !lineage.analysis_calculation_version.is_empty()
        && valid_hash(&lineage.source_snapshot_sha256)
        && valid_index(&identity.code)
        && identity.instrument_kind == "index"
        && identity.provider == "fred"
        && identity.adjustment == "price_index"
        && identity.return_semantics == "price_index"
        && identity.frequency == "daily"
        && identity.units == "index_points"
        && (2..=100).contains(&lineage.parameters.fast_window)
        && (3..=252).contains(&lineage.parameters.slow_window)
        && lineage.parameters.fast_window < lineage.parameters.slow_window
        && matches!(lineage.quality_policy.as_str(), "require_complete" | "allow_unverified")
        && !lineage.quality_status.is_empty()
        && lineage.quality_status.len() <= 40
        && (lineage.quality_verified || !lineage.quality_verified)
        && !lineage.requested_start.is_empty()
        && !lineage.requested_end.is_empty()
        && !lineage.first_observed_session.is_empty()
        && !lineage.last_observed_session.is_empty()
        && lineage.point_in_time_membership == "not_used"
        && !lineage.dividends_included
        && lineage.corporate_action_adjustment == "none_index_price_level"
        && !result.evaluated_at_utc.is_empty()
        && result.quality_warning.as_ref().is_none_or(|warning| warning.len() <= 200)
        && result.points.len() == usize::from(result.observations)
        && !result.points.is_empty()
        && result.points.len() <= MAX_RESULT_POINTS
        && result.points.iter().all(|point| {
            point.session_date.len() == 10
                && valid_decimal(&point.close)
                && point.fast_sma.as_deref().is_none_or(valid_decimal)
                && point.slow_sma.as_deref().is_none_or(valid_decimal)
                && point.signal_after_close.as_deref().is_none_or(|value| matches!(value, "long" | "flat"))
        })
}

fn read_summary(value: &Value) -> Result<JobSummary, String> {
    let summary: JobSummary = serde_json::from_value(value.clone())
        .map_err(|_| "Model-jobs backend returned an invalid job schema.".to_string())?;
    if !summary.valid() {
        return Err("Model-jobs backend returned an invalid job contract.".to_string());
    }
    Ok(summary)
}

pub fn validate_summary(value: Value) -> Result<Value, String> {
    read_summary(&value)?;
    Ok(value)
}

pub fn validate_list(value: Value) -> Result<Value, String> {
    let list: JobList = serde_json::from_value(value.clone())
        .map_err(|_| "Model-jobs backend returned an invalid list schema.".to_string())?;
    if list.items.len() > 100 || list.total < list.items.len() as u64
        || list.items.iter().any(|item| !item.valid())
    {
        return Err("Model-jobs backend returned an invalid list contract.".to_string());
    }
    Ok(value)
}

pub fn validate_detail(mut value: Value, include_result: bool) -> Result<Value, String> {
    let object = value.as_object_mut()
        .ok_or_else(|| "Model-jobs backend returned an invalid detail schema.".to_string())?;
    let result = object.remove("result");
    let summary = read_summary(&Value::Object(object.clone()))?;
    if include_result {
        let result = result.ok_or_else(|| "Model-jobs backend omitted the requested result.".to_string())?;
        if !result.is_null() {
            let typed: ResearchResult = serde_json::from_value(result.clone())
                .map_err(|_| "Model-jobs backend returned an invalid result schema.".to_string())?;
            if !valid_result(&typed, &summary) {
                return Err("Model-jobs backend returned an invalid result contract.".to_string());
            }
        } else if summary.state == "completed" {
            return Err("Completed model job omitted its result.".to_string());
        }
        object.insert("result".to_string(), result);
    } else if result.is_some() {
        return Err("Model-jobs backend unexpectedly exposed a result.".to_string());
    }
    Ok(value)
}

pub fn validate_cancel(value: Value, requested_id: &str) -> Result<Value, String> {
    let state: JobState = serde_json::from_value(value.clone())
        .map_err(|_| "Model-jobs backend returned an invalid cancel schema.".to_string())?;
    if state.id != requested_id
        || !matches!(state.status.as_str(),
            "queued" | "running" | "completed" | "failed" | "cancelled" | "cancel_requested")
    {
        return Err("Model-jobs backend returned an invalid cancel state.".to_string());
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn summary() -> Value {
        json!({
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "state": "queued", "status": "queued",
            "model_id": "builtin.sma_crossover",
            "model_version": "1.0.0", "request_sha256": "a".repeat(64),
            "attempts": 0, "progress": 0, "cancel_requested": 0,
            "output_sha256": null, "source_snapshot_sha256": null,
            "error_code": null, "created_at": "2026-11-06T01:00:00Z",
            "updated_at": "2026-11-06T01:00:00Z",
            "started_at": null, "finished_at": null
        })
    }

    #[test]
    fn reject_unknown_job_properties_or_invalid_status() {
        assert!(validate_summary(summary()).is_ok());
        let mut extra = summary();
        extra["module_path"] = json!("/private/secret.py");
        assert!(validate_summary(extra).is_err());
        let mut bad = summary();
        bad["state"] = json!("synthetic_completed");
        assert!(validate_summary(bad).is_err());
    }

    #[test]
    fn reject_unrequested_or_missing_job_result_and_bad_list() {
        assert!(validate_detail(summary(), false).is_ok());
        assert!(validate_detail(summary(), true).is_err());
        let mut detail = summary();
        detail["result"] = Value::Null;
        assert!(validate_detail(detail, true).is_ok());
        let mut leaked = summary();
        leaked["result"] = json!({"secret": "token"});
        assert!(validate_detail(leaked, false).is_err());
        assert!(validate_list(json!({"total": 1, "items": [summary()]})).is_ok());
        assert!(validate_list(json!({"total": 0, "items": [summary()]})).is_err());
    }

    #[test]
    fn require_explicit_pending_cancellation_status() {
        let mut pending = summary();
        pending["state"] = json!("running");
        pending["cancel_requested"] = json!(1);
        pending["status"] = json!("cancel_requested");
        assert!(validate_summary(pending.clone()).is_ok());
        assert!(validate_detail(pending.clone(), false).is_ok());
        assert!(validate_list(json!({"total": 1, "items": [pending.clone()]})).is_ok());
        pending["status"] = json!("running");
        assert!(validate_summary(pending).is_err());
    }

    #[test]
    fn reject_forged_or_unspecified_cancel_state() {
        let id = "123e4567-e89b-12d3-a456-426614174000";
        assert!(validate_cancel(json!({"id": id, "status": "cancel_requested"}), id).is_ok());
        assert!(validate_cancel(json!({"id": id, "status": "unknown"}), id).is_err());
        assert!(validate_cancel(json!({"id": id, "status": "cancelled", "hidden": 123}), id).is_err());
        assert!(validate_cancel(json!({"id": "not-the-job", "status": "cancelled"}), id).is_err());
    }
}
