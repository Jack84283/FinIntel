//! Fixed local comparison transport. The webview supplies data, never a route.
use std::{collections::HashSet, time::Duration};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use time::{Date, Month, OffsetDateTime};

const PATH: &str = "/api/v1/market-data/comparison";
const CONTRACT: &str = "market-comparison-v1";
const LIMIT: usize = 4 * 1024 * 1024;

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Source {
    kind: String,
    code: String,
    provider: String,
}

impl Source {
    fn valid(&self) -> bool {
        match self.kind.as_str() {
            "index" => {
                self.provider == "fred"
                    && matches!(self.code.as_str(), "SP500" | "DJIA" | "NASDAQCOM" | "NDX")
            }
            "equity" => {
                matches!(
                    self.provider.as_str(),
                    "alpha_vantage" | "alpaca_sip" | "tiingo"
                ) && (1..=15).contains(&self.code.len())
                    && self.code.bytes().enumerate().all(|(index, byte)| {
                        byte.is_ascii_uppercase()
                            || byte.is_ascii_digit()
                            || index > 0 && matches!(byte, b'.' | b'-')
                    })
            }
            _ => false,
        }
    }

    fn key(&self) -> String {
        format!("{}:{}", self.provider, self.code)
    }
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct ComparisonRequest {
    contract_version: String,
    sources: Vec<Source>,
    start: String,
    end: String,
}

fn date(value: &str) -> Option<Date> {
    if !super::valid_index_history_date(value) {
        return None;
    }
    Date::from_calendar_date(
        value[..4].parse().ok()?,
        Month::try_from(value[5..7].parse::<u8>().ok()?).ok()?,
        value[8..].parse().ok()?,
    )
    .ok()
}

impl ComparisonRequest {
    fn validate(&self) -> Result<(), &'static str> {
        if self
            .sources
            .iter()
            .any(|s| s.kind == "index" && s.code == "VIX")
        {
            return Err("unsupported_vix");
        }
        if self.contract_version != CONTRACT
            || !(2..=4).contains(&self.sources.len())
            || !self.sources.iter().all(Source::valid)
            || self
                .sources
                .iter()
                .map(Source::key)
                .collect::<HashSet<_>>()
                .len()
                != self.sources.len()
        {
            return Err("invalid_input");
        }
        let (Some(start), Some(end)) = (date(&self.start), date(&self.end)) else {
            return Err("invalid_input");
        };
        if start > end || end > OffsetDateTime::now_utc().date() {
            return Err("invalid_input");
        }
        if (end - start).whole_days() > 3660 {
            return Err("resource_limit");
        }
        Ok(())
    }
}

fn checked(payload: &Value, request: &ComparisonRequest) -> bool {
    let Some(points) = payload["points"].as_array() else {
        return false;
    };
    let Some(sources) = payload["sources"].as_array() else {
        return false;
    };
    let Some(baseline) = payload["baseline_date"].as_str() else {
        return false;
    };
    let Some(common_end) = payload["last_common_date"].as_str() else {
        return false;
    };
    if payload["contract_version"] != CONTRACT
        || payload["calculation_revision"] != "market-comparison-decimal-v1"
        || payload["request"] != serde_json::to_value(request).unwrap_or(Value::Null)
        || ["read_only", "no_network", "no_synthetic_sessions"]
            .iter()
            .any(|key| payload[*key] != true)
        || !(2..=3661).contains(&points.len())
        || sources.len() != request.sources.len()
        || !sources.iter().zip(&request.sources).all(|(item, source)| {
            item["key"] == source.key()
                && item["identity"] == serde_json::to_value(source).unwrap_or(Value::Null)
                && item["snapshot_sha256"]
                    .as_str()
                    .is_some_and(super::valid_model_job_hash)
        })
        || !payload["fingerprint_sha256"]
            .as_str()
            .is_some_and(super::valid_model_job_hash)
    {
        return false;
    }
    let mut previous = "";
    let mut shared = 0;
    let mut last_shared = "";
    for point in points {
        let (Some(day), Some(values)) =
            (point["session_date"].as_str(), point["values"].as_array())
        else {
            return false;
        };
        if date(day).is_none()
            || day <= previous
            || day < request.start.as_str()
            || day > request.end.as_str()
            || values.len() != sources.len()
            || values.iter().all(Value::is_null)
        {
            return false;
        }
        for cell in values.iter().filter(|value| !value.is_null()) {
            if ["close", "normalized_100", "cumulative_return_pct"]
                .iter()
                .any(|key| {
                    cell[*key]
                        .as_str()
                        .filter(|value| value.len() <= 110)
                        .and_then(|value| value.parse::<f64>().ok())
                        .is_none_or(|value| !value.is_finite() || *key == "close" && value <= 0.0)
                })
            {
                return false;
            }
        }
        if previous.is_empty()
            && (day != baseline
                || values.iter().any(|item| {
                    item["normalized_100"] != "100" || item["cumulative_return_pct"] != "0"
                }))
        {
            return false;
        }
        previous = day;
        if values.iter().all(|value| !value.is_null()) {
            shared += 1;
            last_shared = day;
        }
    }
    if shared < 2 || last_shared != common_end || payload["shared_sessions"] != shared {
        return false;
    }
    let Some(hash) = payload["output_sha256"].as_str() else {
        return false;
    };
    serde_json::to_vec(points)
        .ok()
        .is_some_and(|bytes| format!("{:x}", Sha256::digest(bytes)) == hash)
}

fn request_builder(
    client: &reqwest::Client,
    port_base: &str,
    token: &str,
    request: &ComparisonRequest,
) -> reqwest::RequestBuilder {
    client
        .post(format!("{port_base}{PATH}"))
        .header("x-finintel-process-token", token)
        .json(request)
}

#[tauri::command]
pub(super) async fn market_comparison(request: ComparisonRequest) -> Result<Value, String> {
    request.validate()?;
    let token = super::backend_token();
    if token.is_empty() && !cfg!(debug_assertions) {
        return Err("backend_unavailable".into());
    }
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "backend_unavailable")?;
    let mut response = request_builder(&client, &super::backend_base(), token, &request)
        .send()
        .await
        .map_err(|_| "backend_unavailable")?;
    let status = response.status().as_u16();
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "backend_unavailable")? {
        if chunk.len() > LIMIT - body.len() {
            return Err("invalid_response".into());
        }
        body.extend_from_slice(&chunk);
    }
    let value: Value = serde_json::from_slice(&body).map_err(|_| "invalid_response")?;
    if status != 200 {
        let code = match value["detail"].as_str() {
            Some("invalid_input") => "invalid_input",
            Some("invalid_source") => "invalid_source",
            Some("unsupported_vix") => "unsupported_vix",
            Some("missing_history") => "missing_history",
            Some("insufficient_overlap") => "insufficient_overlap",
            Some("incompatible_source") => "incompatible_source",
            Some("invalid_close") => "invalid_close",
            Some("resource_limit") => "resource_limit",
            _ => "backend_unavailable",
        };
        return Err(code.into());
    }
    if !checked(&value, &request) {
        return Err("invalid_response".into());
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture() -> ComparisonRequest {
        serde_json::from_value(json!({
            "contract_version": CONTRACT, "start": "2024-01-02", "end": "2024-01-09",
            "sources": [{"kind":"equity","code":"AAPL","provider":"tiingo"},{"kind":"index","code":"SP500","provider":"fred"}]
        })).unwrap()
    }

    #[test]
    fn fixed_route_and_validated_source_identity() {
        let mut request = fixture();
        assert!(request.validate().is_ok());
        let call = request_builder(
            &reqwest::Client::new(),
            "http://127.0.0.1:8765",
            "fixture-token",
            &request,
        )
        .build()
        .unwrap();
        assert_eq!(call.url().as_str(), format!("http://127.0.0.1:8765{PATH}"));
        assert_eq!(call.method(), reqwest::Method::POST);
        assert_eq!(call.headers()["x-finintel-process-token"], "fixture-token");
        request.sources[1].code = "VIX".into();
        assert_eq!(request.validate(), Err("unsupported_vix"));
        request.sources[1].code = "SP500".into();
        request.sources[1].provider = "https://invalid".into();
        assert_eq!(request.validate(), Err("invalid_input"));
    }

    #[test]
    fn refuses_wrong_identity_and_invalid_aligned_points() {
        let request = fixture();
        let mut response: Value = serde_json::from_str(include_str!(
            "../../../../contracts/test-fixtures/market-comparison.json"
        ))
        .unwrap();
        assert!(checked(&response, &request));
        let cell = response["points"][2]["values"][0].clone();
        response["points"][2]["values"][0]["close"] = json!("NaN");
        assert!(!checked(&response, &request));
        response["points"][2]["values"][0] = cell;
        response["points"][2]["values"][0]["normalized_100"] = json!("104");
        assert!(!checked(&response, &request));
        response["sources"][1]["key"] = json!("fred:VIX");
        assert!(!checked(&response, &request));
    }
}
