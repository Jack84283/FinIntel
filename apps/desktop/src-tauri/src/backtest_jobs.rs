//! Fixed desktop transport for durable, source-pinned SMA backtests.
use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};

use super::analysis_jobs::{
    canonical_hash, decimal, exact, hash, integer, managed_job_response, string, timestamp,
    valid_status, Checked, Operation, STATES, SUMMARY_KEYS, WORKER_ERRORS,
};
use super::{valid_index_history_date, valid_model_job_id};

const PATH: &str = "/api/v1/models/backtests/jobs";
const INVALID: &str = "invalid_input";
const MALFORMED: &str = "invalid_response";
const REVISION: &str = "sma-next-observed-close-v1";
const SMA: &str = "builtin.sma_crossover";

fn request(value: &Value) -> Checked<()> {
    exact(
        value,
        &[
            "contract_version",
            "source_job_id",
            "source_request_sha256",
            "source_output_sha256",
            "source_lineage_sha256",
            "source_implementation_revision",
            "engine_revision",
            "simulation",
        ],
    )?;
    let simulation = &value["simulation"];
    exact(
        simulation,
        &[
            "model_id",
            "model_version",
            "index_code",
            "expected_snapshot_sha256",
            "training_end",
            "test_start",
            "test_end",
            "source_semantics",
            "point_in_time_membership",
            "assumptions",
        ],
    )?;
    if value["contract_version"] != "backtest-job-request-v1"
        || !valid_model_job_id(string(&value["source_job_id"])?)
        || value["source_implementation_revision"] != "sma-crossover-observed-close-v1"
        || value["engine_revision"] != REVISION
        || ![
            "source_request_sha256",
            "source_output_sha256",
            "source_lineage_sha256",
        ]
        .iter()
        .all(|key| hash(&value[key]))
        || simulation["model_id"] != SMA
        || simulation["model_version"] != "1.0.0"
        || !["SP500", "DJIA", "NASDAQCOM", "NDX"].contains(&string(&simulation["index_code"])?)
        || !hash(&simulation["expected_snapshot_sha256"])
        || simulation["source_semantics"] != "fred_price_index_level"
        || simulation["point_in_time_membership"] != "not_used"
    {
        return Err(MALFORMED);
    }
    let training = day(&simulation["training_end"])?;
    let first = day(&simulation["test_start"])?;
    let last = day(&simulation["test_end"])?;
    if !(training < first && first < last) {
        return Err(MALFORMED);
    }
    let assumptions = &simulation["assumptions"];
    exact(
        assumptions,
        &[
            "initial_capital",
            "position_fraction",
            "commission_bps",
            "slippage_bps",
            "risk_free_rate_annual",
            "execution",
            "sizing",
            "benchmark",
            "rebalance",
            "annualization_sessions",
            "no_orders",
        ],
    )?;
    // Bounds in millionths, matching Python's exact Decimal validation.
    for (key, min, max) in [
        ("initial_capital", 1, 1_000_000_000_000_000_000_i128),
        ("position_fraction", 1, 1_000_000),
        ("commission_bps", 0, 1_000_000_000),
        ("slippage_bps", 0, 1_000_000_000),
        ("risk_free_rate_annual", -990_000, 1_000_000),
    ] {
        let text = string(&assumptions[key])?;
        if !decimal(text, false) || text.len() > 20 {
            return Err(MALFORMED);
        }
        let unsigned = text.strip_prefix('-').unwrap_or(text);
        let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
        if whole.len() > 13 || fraction.len() > 6 {
            return Err(MALFORMED);
        }
        let scaled = format!("{whole}{fraction:0<6}")
            .parse::<i128>()
            .map_err(|_| MALFORMED)?;
        let signed = if text.starts_with('-') {
            -scaled
        } else {
            scaled
        };
        if !(min..=max).contains(&signed) {
            return Err(MALFORMED);
        }
    }
    if assumptions["execution"] != "next_observed_session_close"
        || assumptions["sizing"] != "fixed_fraction_long_or_flat"
        || assumptions["benchmark"] != "frictionless_same_index_buy_hold"
        || assumptions["rebalance"] != "on_signal_change"
        || assumptions["annualization_sessions"] != 252
        || assumptions["no_orders"] != true
    {
        return Err(MALFORMED);
    }
    Ok(())
}

fn day(value: &Value) -> Checked<&str> {
    let text = string(value)?;
    valid_index_history_date(text)
        .then_some(text)
        .ok_or(MALFORMED)
}

fn decimals(value: &Value, keys: &[&str]) -> Checked<()> {
    for key in keys {
        let text = string(&value[key])?;
        if text.len() > 512 || !decimal(text, false) {
            return Err(MALFORMED);
        }
    }
    Ok(())
}

fn report(value: &Value, job: &Value) -> Checked<()> {
    exact(
        value,
        &[
            "contract_version",
            "request_sha256",
            "evaluated_at_utc",
            "report",
        ],
    )?;
    let r = &value["report"];
    exact(
        r,
        &[
            "contract",
            "engine_revision",
            "input_sha256",
            "output_sha256",
            "source_snapshot_sha256",
            "model_id",
            "model_version",
            "source_semantics",
            "point_in_time_membership",
            "signal_timing",
            "execution_assumption",
            "tradeable_instrument",
            "research_only",
            "no_network",
            "no_orders",
            "persisted",
            "test_sessions",
            "rows",
            "transactions",
            "metrics",
            "caution",
        ],
    )?;
    let request = &job["request"];
    let simulation = &request["simulation"];
    let input = json!({
        "backtest_contract": "local-backtest-research-v1",
        "engine_revision": REVISION,
        "request": simulation,
        "model_result_sha256": request["source_output_sha256"],
        "model_lineage_sha256": request["source_lineage_sha256"]
    });
    let mut output = r.clone();
    output
        .as_object_mut()
        .ok_or(MALFORMED)?
        .remove("output_sha256");
    if value["contract_version"] != "backtest-job-result-v1"
        || value["request_sha256"] != job["request_sha256"]
        || !timestamp(&value["evaluated_at_utc"])
        || r["contract"] != "local-backtest-research-v1"
        || r["engine_revision"] != REVISION
        || r["input_sha256"] != canonical_hash(&input)
        || r["output_sha256"] != job["output_sha256"]
        || r["output_sha256"] != canonical_hash(&output)
        || r["source_snapshot_sha256"] != job["source_snapshot_sha256"]
        || r["model_id"] != SMA
        || r["model_version"] != "1.0.0"
        || r["source_semantics"] != "fred_price_index_level"
        || r["point_in_time_membership"] != "not_used"
        || r["signal_timing"] != "after_observed_close"
        || r["execution_assumption"] != "next_observed_session_close"
        || r["tradeable_instrument"] != false
        || r["research_only"] != true
        || r["no_network"] != true
        || r["no_orders"] != true
        || r["persisted"] != false
        || r["caution"]
            != concat!(
                "Hypothetical FRED index-price-level exposure, not a tradable fill or ",
                "dividend-inclusive return; benchmark ignores all transaction costs."
            )
    {
        return Err(MALFORMED);
    }
    let rows = r["rows"].as_array().ok_or(MALFORMED)?;
    let transactions = r["transactions"].as_array().ok_or(MALFORMED)?;
    if !(2..=2000).contains(&rows.len())
        || r["test_sessions"] != rows.len()
        || transactions.len() > rows.len()
    {
        return Err(MALFORMED);
    }
    let mut fills = HashMap::new();
    let mut previous = day(&simulation["training_end"])?;
    for (i, row) in rows.iter().enumerate() {
        exact(
            row,
            &[
                "session_date",
                "equity_after_cost",
                "frictionless_benchmark_equity",
                "exposure_after_close",
                "transaction_cost",
                "drawdown",
                "executed_from_prior_signal",
            ],
        )?;
        let session = day(&row["session_date"])?;
        if session <= previous
            || row["executed_from_prior_signal"] != previous
            || (i == 0 && row["session_date"] != simulation["test_start"])
        {
            return Err(MALFORMED);
        }
        decimals(
            row,
            &[
                "equity_after_cost",
                "frictionless_benchmark_equity",
                "exposure_after_close",
                "transaction_cost",
                "drawdown",
            ],
        )?;
        fills.insert(session, previous);
        previous = session;
    }
    if simulation["test_end"] != previous {
        return Err(MALFORMED);
    }
    let mut previous = "";
    for tx in transactions {
        exact(
            tx,
            &[
                "signal_as_of_session",
                "hypothetical_fill_session",
                "exposure_before",
                "exposure_after",
                "turnover_fraction",
                "cost",
            ],
        )?;
        let fill = day(&tx["hypothetical_fill_session"])?;
        if fill <= previous || fills.get(fill).copied() != Some(day(&tx["signal_as_of_session"])?) {
            return Err(MALFORMED);
        }
        decimals(
            tx,
            &[
                "exposure_before",
                "exposure_after",
                "turnover_fraction",
                "cost",
            ],
        )?;
        previous = fill;
    }
    let metrics = &r["metrics"];
    let decimals_keys = [
        "cumulative_return",
        "annualized_return",
        "max_drawdown",
        "annualized_volatility",
        "risk_free_rate_annual",
        "turnover_fraction",
        "transaction_cost_total",
        "frictionless_benchmark_cumulative_return",
        "relative_to_frictionless_benchmark",
    ];
    let mut keys = decimals_keys.to_vec();
    keys.extend(["sharpe", "transactions"]);
    exact(metrics, &keys)?;
    decimals(metrics, &decimals_keys)?;
    if !metrics["sharpe"].is_null() {
        decimals(metrics, &["sharpe"])?;
    }
    if metrics["transactions"] != transactions.len() {
        return Err(MALFORMED);
    }
    Ok(())
}

pub(super) fn summary(value: &Value, expected_id: Option<&str>, include: bool) -> Checked<()> {
    let mut keys: Vec<_> = SUMMARY_KEYS
        .iter()
        .copied()
        .filter(|key| *key != "implementation_revision")
        .collect();
    if include {
        keys.push("result");
    }
    exact(value, &keys)?;
    request(&value["request"])?;
    let id = string(&value["id"])?;
    let state = string(&value["state"])?;
    if !valid_model_job_id(id)
        || expected_id.is_some_and(|expected| expected != id)
        || value["model_id"] != SMA
        || value["model_version"] != "1.0.0"
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
        || value["request_sha256"] != canonical_hash(&value["request"])
    {
        return Err(MALFORMED);
    }
    if state == "completed" {
        if value["progress"] != 100
            || !hash(&value["output_sha256"])
            || value["source_snapshot_sha256"]
                != value["request"]["simulation"]["expected_snapshot_sha256"]
        {
            return Err(MALFORMED);
        }
        if include {
            report(&value["result"], value)?;
        }
    } else if !value["output_sha256"].is_null()
        || !value["source_snapshot_sha256"].is_null()
        || !value["result"].is_null()
    {
        return Err(MALFORMED);
    }
    Ok(())
}

async fn execute(operation: Operation) -> Result<Value, String> {
    match &operation {
        Operation::Create(value) => request(value).map_err(|_| INVALID)?,
        Operation::List {
            limit,
            offset,
            status,
        } => {
            if !(1..=100).contains(limit)
                || *offset > 10_000
                || status
                    .as_deref()
                    .is_some_and(|status| !valid_status(status))
            {
                return Err(INVALID.into());
            }
        }
        Operation::Get { id, .. } | Operation::Cancel(id) | Operation::Retry(id) => {
            if !valid_model_job_id(id) {
                return Err(INVALID.into());
            }
        }
    }
    let value = managed_job_response(&operation, PATH).await?;
    match operation {
        Operation::Create(original) => {
            summary(&value, None, false)?;
            if value["request"] != original || value["state"] != "queued" || value["attempts"] != 0
            {
                return Err(MALFORMED.into());
            }
        }
        Operation::Get { id, include_result } => summary(&value, Some(&id), include_result)?,
        Operation::Retry(id) => summary(&value, Some(&id), false)?,
        Operation::Cancel(_) => {
            exact(&value, &["state"])?;
            if !valid_status(string(&value["state"])?) {
                return Err(MALFORMED.into());
            }
        }
        Operation::List {
            limit,
            offset,
            status,
        } => {
            exact(&value, &["total", "items"])?;
            let total = value["total"].as_u64().ok_or(MALFORMED)?;
            let items = value["items"].as_array().ok_or(MALFORMED)?;
            if items.len() as u64
                != total
                    .saturating_sub(u64::from(offset))
                    .min(u64::from(limit))
            {
                return Err(MALFORMED.into());
            }
            let mut ids = HashSet::new();
            for item in items {
                summary(item, None, false)?;
                if !ids.insert(string(&item["id"])?)
                    || status
                        .as_ref()
                        .is_some_and(|state| item["status"] != *state)
                {
                    return Err(MALFORMED.into());
                }
            }
        }
    }
    Ok(value)
}

#[tauri::command]
pub async fn backtest_jobs_create(request: Value) -> Result<Value, String> {
    execute(Operation::Create(request)).await
}

#[tauri::command]
pub async fn backtest_jobs_list(
    limit: Option<u8>,
    offset: Option<u32>,
    status: Option<String>,
) -> Result<Value, String> {
    execute(Operation::List {
        limit: limit.unwrap_or(10),
        offset: offset.unwrap_or(0),
        status,
    })
    .await
}

#[tauri::command]
pub async fn backtest_jobs_get(job_id: String, include_result: bool) -> Result<Value, String> {
    execute(Operation::Get {
        id: job_id,
        include_result,
    })
    .await
}

#[tauri::command]
pub async fn backtest_jobs_cancel(job_id: String) -> Result<Value, String> {
    execute(Operation::Cancel(job_id)).await
}

#[tauri::command]
pub async fn backtest_jobs_retry(job_id: String) -> Result<Value, String> {
    execute(Operation::Retry(job_id)).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Value {
        serde_json::from_str(include_str!(
            "../../../../contracts/test-fixtures/durable-backtest.json"
        ))
        .unwrap()
    }

    #[test]
    fn accepts_engine_golden_and_rejects_resigned_same_close_and_hash_mismatch() {
        let mut value = fixture();
        summary(&value, string(&value["id"]).ok(), true).unwrap();

        value["result"]["report"]["rows"][0]["equity_after_cost"] = json!("9999");
        assert_eq!(summary(&value, None, true), Err(MALFORMED));

        let mut value = fixture();
        let r = &mut value["result"]["report"];
        r["transactions"][0]["signal_as_of_session"] = r["rows"][0]["session_date"].clone();
        let mut without_hash = r.clone();
        without_hash
            .as_object_mut()
            .unwrap()
            .remove("output_sha256");
        let output = canonical_hash(&without_hash);
        r["output_sha256"] = json!(output);
        value["output_sha256"] = json!(output);
        assert_eq!(summary(&value, None, true), Err(MALFORMED));
    }

    #[test]
    fn routes_only_managed_backtest_operations_and_rejects_invalid_inputs() {
        let archived = fixture();
        let mut bad = archived["request"].clone();
        bad["simulation"]["index_code"] = json!("VIX");
        assert!(request(&bad).is_err());
        let mut bad = archived["request"].clone();
        bad["simulation"]["assumptions"]["initial_capital"] = json!("1e3");
        assert!(request(&bad).is_err());
        let id = string(&archived["id"]).unwrap().to_string();
        let client = reqwest::Client::new();
        let base = "http://127.0.0.1:8765";
        let operation = Operation::Retry(id.clone());
        let http = operation
            .http_request(&client, base, "synthetic-token", PATH)
            .build()
            .unwrap();
        assert_eq!(http.method(), reqwest::Method::POST);
        assert_eq!(http.url().path(), format!("{PATH}/{id}/retry"));
        assert_eq!(
            http.headers()["x-finintel-process-token"],
            "synthetic-token"
        );
    }
}
