use std::sync::{atomic::{AtomicBool, AtomicU16, Ordering}, Mutex, OnceLock};
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};

static BACKEND_PORT: AtomicU16 = AtomicU16::new(8765);
static BACKEND_TOKEN: OnceLock<String> = OnceLock::new();

struct ManagedBackend {
    child: Mutex<Option<CommandChild>>,
    stopping: AtomicBool,
}

fn backend_base() -> String {
    format!("http://127.0.0.1:{}", BACKEND_PORT.load(Ordering::Relaxed))
}

fn available_backend_port() -> std::io::Result<u16> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    listener
        .local_addr()
        .map(|address| address.port())
}

async fn wait_for_backend(
    port: u16,
    token: &str,
    attempts: usize,
    retry_delay: Duration,
) -> bool {
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_millis(750))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    else {
        return false;
    };
    let attempts = attempts.max(1);
    for attempt in 0..attempts {
        let response = client
            .get(format!("http://127.0.0.1:{port}/api/v1/health"))
            .header("x-finintel-process-token", token)
            .send()
            .await;
        if let Ok(response) = response {
            if response.status().is_success() {
                if let Ok(payload) = response.json::<serde_json::Value>().await {
                    let healthy = payload.get("status").and_then(serde_json::Value::as_str)
                        == Some("ok")
                        && payload.get("service").and_then(serde_json::Value::as_str)
                            == Some("finintel-backend")
                        && payload.get("version").and_then(serde_json::Value::as_str)
                            .is_some_and(|version| !version.is_empty());
                    if healthy {
                        return true;
                    }
                }
            }
        }
        if attempt + 1 < attempts {
            tokio::time::sleep(retry_delay).await;
        }
    }
    false
}

fn backend_token() -> &'static str {
    BACKEND_TOKEN.get().map(String::as_str).unwrap_or("")
}

async fn read_backend(path: &str, params: &[(&str, String)]) -> Result<serde_json::Value, String> {
    // Fixed loopback host, strictly bounded routes and parameters.
    // No generic proxy, client-selected host or credentials in the webview.
    let client = reqwest::Client::builder()
        .timeout(if path.starts_with("/api/v1/settings/backups/") {
            Duration::from_secs(180)
        } else {
            Duration::from_secs(30)
        })
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local HTTP client.".to_string())?;
    let mut url = reqwest::Url::parse(&format!("{}{path}", backend_base()))
        .map_err(|_| "Invalid local backend route.".to_string())?;
    if !params.is_empty() {
        url.query_pairs_mut().extend_pairs(params.iter().map(|(k, v)| (*k, v.as_str())));
    }
    let response = client
        .get(url)
        .header("x-finintel-process-token", backend_token())
        .send()
        .await
        .map_err(|error| {
            if error.is_timeout() {
                "The local Python backend request timed out. Retry after the database operation finishes."
                    .to_string()
            } else if error.is_connect() && cfg!(debug_assertions) {
                "Local Python backend is unreachable. Start FinIntel with npm run dev:desktop."
                    .to_string()
            } else if error.is_connect() {
                "The managed Python backend is not running. Restart FinIntel.".to_string()
            } else {
                "The local Python backend request failed. Recheck the connection.".to_string()
            }
        })?;

    if response.status() == reqwest::StatusCode::FORBIDDEN {
        return Err(
            "The Python backend requires matching desktop authorization. Close old FinIntel sessions and launch with npm run dev:desktop."
                .to_string(),
        );
    }
    if matches!(path, "/api/v1/analytics/series" | "/api/v1/analytics/basket")
        && response.status() == reqwest::StatusCode::UNPROCESSABLE_ENTITY
    {
        // Fixed analytics endpoint reports safe validation/quality failures.
        // Never forward arbitrary provider payloads or credentials to the WebView.
        let detail = response.json::<serde_json::Value>().await.ok()
            .and_then(|payload| payload.get("detail").and_then(serde_json::Value::as_str).map(str::to_string));
        return Err(detail.filter(|message| !message.is_empty() && message.len() <= 300)
            .unwrap_or_else(|| "Index analytics selection or quality policy was rejected.".to_string()));
    }
    if !response.status().is_success() {
        return Err(format!("Local backend returned HTTP {}.", response.status()));
    }
    response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Local backend returned malformed JSON.".to_string())
}

/// Restricted loopback write: the webview cannot choose the URL or headers.
#[tauri::command]
async fn market_acquisition_status() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/market-data/acquisition/status", &[]).await
}

#[tauri::command]
async fn market_acquire_daily(
    symbol: String,
    outputsize: String,
    provider: String,
    period: String,
) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) {
        return Err("Invalid ticker symbol.".to_string());
    }
    if outputsize != "compact" && outputsize != "full" {
        return Err("Output size must be compact or full.".to_string());
    }
    if provider != "alpha_vantage" && provider != "alpaca_sip" && provider != "tiingo" {
        return Err("Unsupported acquisition provider.".to_string());
    }
    if !matches!(period.as_str(), "5y" | "10y" | "max") {
        return Err("Invalid Alpaca history period.".to_string());
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(90))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local HTTP client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/market-data/acquisition", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .header("x-finintel-client", "local-workstation")
        .json(&serde_json::json!({
            "symbol": symbol.to_ascii_uppercase(),
            "outputsize": outputsize,
            "provider": provider,
            "period": period
        }))
        .send()
        .await
        .map_err(|_| "Local acquisition backend is unreachable or timed out.".to_string())?;
    let status = response.status();
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Acquisition backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload
            .get("detail")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("Market data request did not succeed.");
        return Err(format!("HTTP {}: {detail}", status.as_u16()));
    }
    Ok(payload)
}

#[tauri::command]
async fn market_symbols(
    q: String,
    asset_type: Option<String>,
    limit: u8,
) -> Result<serde_json::Value, String> {
    if q.len() > 80 || q.chars().any(|ch| ch.is_control()) {
        return Err("Symbol search query is invalid.".to_string());
    }
    if limit == 0 || limit > 100 {
        return Err("Invalid symbol result limit.".to_string());
    }
    if asset_type.as_deref().is_some_and(|value| !matches!(value, "Stock" | "ETF" | "Index" | "Mutual Fund")) {
        return Err("Asset category must be Stock or ETF.".to_string());
    }
    let mut params = vec![("q", q), ("limit", limit.to_string())];
    if let Some(kind) = asset_type {
        params.push(("asset_type", kind));
    }
    read_backend("/api/v1/market-data/symbols", &params).await
}

#[tauri::command]
async fn market_sync_symbols() -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(50))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize local HTTP client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/market-data/symbols/sync", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .header("x-finintel-client", "local-workstation")
        .send()
        .await
        .map_err(|_| "Local listing sync backend is unreachable or timed out.".to_string())?;
    let status = response.status();
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Listing sync returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload
            .get("detail")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("Listing sync failed.");
        return Err(format!("HTTP {}: {detail}", status.as_u16()));
    }
    Ok(payload)
}

fn valid_symbol(symbol: &str) -> bool {
    (1..=15).contains(&symbol.len())
        && symbol.bytes().next().is_some_and(|b| b.is_ascii_alphanumeric())
        && symbol
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
}

fn valid_date(date: &str) -> bool {
    date.len() == 10
        && date.bytes().enumerate().all(|(index, value)| match index {
            4 | 7 => value == b'-',
            _ => value.is_ascii_digit(),
        })
}

#[tauri::command]
async fn backend_health() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/health", &[]).await
}

#[tauri::command]
async fn market_instrument_directory() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/market-data/instruments/directory", &[]).await
}

#[tauri::command]
async fn market_instruments() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/market-data/instruments", &[]).await
}

#[tauri::command]
async fn market_daily(
    symbol: String,
    start_date: Option<String>,
    end_date: Option<String>,
    limit: u16,
    latest: bool,
    provider: String,
) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) {
        return Err("Invalid ticker symbol.".to_string());
    }
    if provider != "alpha_vantage" && provider != "alpaca_sip" && provider != "tiingo" {
        return Err("Unsupported price-history provider.".to_string());
    }
    if limit == 0 || limit > 4000 {
        return Err("Daily history limit must be 1–4000.".to_string());
    }
    if start_date.as_deref().is_some_and(|value| !valid_date(value))
        || end_date.as_deref().is_some_and(|value| !valid_date(value))
    {
        return Err("Invalid ISO trading date.".to_string());
    }
    if let (Some(start), Some(end)) = (&start_date, &end_date) {
        if start > end {
            return Err("Start date must not exceed end date.".to_string());
        }
    }
    let mut params = vec![
        ("limit", limit.to_string()),
        ("latest", latest.to_string()),
        ("provider", provider),
    ];
    if let Some(start) = start_date {
        params.push(("start_date", start));
    }
    if let Some(end) = end_date {
        params.push(("end_date", end));
    }
    read_backend(
        &format!("/api/v1/market-data/{}/daily", symbol.to_ascii_uppercase()),
        &params,
    )
    .await
}

#[tauri::command]
async fn market_imports(limit: u8) -> Result<serde_json::Value, String> {
    if limit == 0 || limit > 100 {
        return Err("Import history limit must be 1–100.".to_string());
    }
    read_backend(
        "/api/v1/market-data/imports",
        &[("limit", limit.to_string())],
    )
    .await
}

#[tauri::command]
async fn market_imports_search(
    q: String, provider: String, limit: u8, offset: u32,
) -> Result<serde_json::Value, String> {
    if q.len() > 80 || provider.len() > 40 || limit == 0 || limit > 100 {
        return Err("Invalid import history search.".to_string());
    }
    read_backend(
        "/api/v1/market-data/imports/search",
        &[("q", q), ("provider", provider), ("limit", limit.to_string()), ("offset", offset.to_string())],
    ).await
}


// Restricted Tauri bridge for data-quality investigation. The webview cannot
// provide a host, token, arbitrary HTTP route or query parameter name.
fn valid_quality_id(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

#[tauri::command]
async fn quality_read(
    route: String,
    query: serde_json::Value,
    id: Option<String>,
) -> Result<serde_json::Value, String> {
    let (path, permitted): (String, &[&str]) = match route.as_str() {
        "overview" => ("/api/v1/quality/overview".into(), &["code"]),
        "findings" => (
            "/api/v1/quality/findings".into(),
            &["code", "finding_type", "provider", "review_state", "limit", "offset"],
        ),
        "report" => ("/api/v1/quality/report".into(), &["limit"]),
        "corrections" => (
            "/api/v1/quality/corrections".into(),
            &["code", "state", "q", "limit", "offset"],
        ),
        "corporate-actions" => (
            "/api/v1/quality/corporate-actions".into(),
            &["code", "limit"],
        ),
        "reconciliation" => (
            "/api/v1/quality/reconciliation/summary".into(),
            &["code"],
        ),
        "history" | "correction" | "preview" => {
            let key = id.ok_or("Missing record identifier.")?;
            if route == "history" {
                if key.parse::<u64>().ok().filter(|value| *value > 0).is_none() {
                    return Err("Invalid finding ID.".into());
                }
                (format!("/api/v1/quality/findings/{key}/history"), &[])
            } else {
                if !valid_quality_id(&key) {
                    return Err("Invalid correction ID.".into());
                }
                (
                    if route == "preview" {
                        format!("/api/v1/quality/corrections/{key}/preview")
                    } else {
                        format!("/api/v1/quality/corrections/{key}")
                    },
                    &[],
                )
            }
        }
        _ => return Err("Unsupported data-quality read route.".into()),
    };
    let map = query.as_object().ok_or("Invalid data-quality filter object.")?;
    if map.len() > permitted.len() {
        return Err("Unexpected data-quality filters.".into());
    }
    let mut params: Vec<(&str, String)> = Vec::new();
    for (key, value) in map {
        if !permitted.contains(&key.as_str()) {
            return Err("Unrecognized data-quality filter.".into());
        }
        let val = match value {
            serde_json::Value::String(s) => s.clone(),
            serde_json::Value::Number(n) => n.to_string(),
            _ => return Err("Invalid data-quality filter value.".into()),
        };
        if val.len() > 80 {
            return Err("Data-quality filter is too long.".into());
        }
        params.push((key.as_str(), val));
    }
    read_backend(&path, &params).await
}

#[tauri::command]
async fn quality_write(
    route: String,
    body: serde_json::Value,
    id: Option<String>,
    action: Option<String>,
) -> Result<serde_json::Value, String> {
    if backend_token().is_empty() {
        return Err("Data-quality changes require the managed desktop.".into());
    }
    let path = match route.as_str() {
        "scan" => "/api/v1/quality/scan".to_string(),
        "propose" => "/api/v1/quality/corrections".to_string(),
        "corporate-verify" => "/api/v1/quality/corporate-actions/verify".to_string(),
        "review" => {
            let key = id.ok_or("Missing finding identifier.")?;
            if key.parse::<u64>().ok().filter(|value| *value > 0).is_none() {
                return Err("Invalid finding ID.".into());
            }
            format!("/api/v1/quality/findings/{key}/review")
        }
        "transition" => {
            let key = id.ok_or("Missing correction identifier.")?;
            if !valid_quality_id(&key) {
                return Err("Invalid correction ID.".into());
            }
            let operation = action.ok_or("Missing correction action.")?;
            if !["validate", "approve", "reject", "reverse"].contains(&operation.as_str()) {
                return Err("Unsupported correction action.".into());
            }
            format!("/api/v1/quality/corrections/{key}/{operation}")
        }
        _ => return Err("Unsupported data-quality write route.".into()),
    };
    if !body.is_object() || body.to_string().len() > 8192 {
        return Err("Invalid data-quality request body.".into());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(if route == "scan" { 600 } else { 90 }))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local quality client.".to_string())?;
    let response = client
        .post(format!("{}{}", backend_base(), path))
        .header("x-finintel-process-token", backend_token())
        .json(&body)
        .send()
        .await
        .map_err(|_| "Could not reach the managed local data-quality backend.".to_string())?;
    let status = response.status();
    let result = response.json::<serde_json::Value>().await
        .map_err(|_| "The local quality backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        return Err(format!(
            "HTTP {}: {}",
            status.as_u16(),
            result["detail"].as_str().unwrap_or("Data-quality request failed."),
        ));
    }
    Ok(result)
}

#[tauri::command]
async fn validation_status() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/validation/status", &[]).await
}

#[tauri::command]
async fn validation_start(checks: Vec<String>) -> Result<serde_json::Value, String> {
    const ALLOWED: [&str; 6] = ["coverage", "integrity", "corporate", "provider", "performance", "quality"];
    if checks.is_empty() || checks.len() > 6
        || checks.iter().any(|check| !ALLOWED.contains(&check.as_str()))
    {
        return Err("Invalid validation checks.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize local HTTP client.".to_string())?;
    let response = client.post(format!("{}/api/v1/validation/run", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .json(&serde_json::json!({"checks": checks}))
        .send().await
        .map_err(|_| "Could not reach local validation backend.".to_string())?;
    let status = response.status();
    let payload = response.json::<serde_json::Value>().await
        .map_err(|_| "Validation backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {}: {}", status.as_u16(), payload["detail"].as_str().unwrap_or("Validation request failed.")));
    }
    Ok(payload)
}

#[tauri::command]
async fn validation_corporate_reference(symbol: String) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) {
        return Err("Invalid equity ticker.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(75))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize local HTTP client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/validation/corporate-reference", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .json(&serde_json::json!({"symbol": symbol}))
        .send().await
        .map_err(|_| "Could not reach local validation backend.".to_string())?;
    let status = response.status();
    let payload = response.json::<serde_json::Value>().await
        .map_err(|_| "Corporate-action backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {}: {}", status.as_u16(), payload["detail"].as_str().unwrap_or("Reference verification failed.")));
    }
    Ok(payload)
}


#[tauri::command]
async fn index_acquire(code: String) -> Result<serde_json::Value, String> {
    if !valid_index(&code) {
        return Err("Invalid index code.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(180))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize local HTTP client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/market-data/indexes/{code}/acquire", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .header("x-finintel-client", "local-workstation")
        .send()
        .await
        .map_err(|_| "Could not reach local index acquisition backend.".to_string())?;
    let status = response.status();
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Index acquisition returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload
            .get("detail")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("Index acquisition failed.");
        return Err(format!("HTTP {}: {detail}", status.as_u16()));
    }
    Ok(payload)
}

fn valid_index(code: &str) -> bool {
    matches!(code, "SP500" | "DJIA" | "NDX" | "RUT")
}

#[tauri::command]
async fn index_catalog(as_of: Option<String>) -> Result<serde_json::Value, String> {
    if as_of.as_deref().is_some_and(|date| !valid_date(date)) {
        return Err("Invalid as-of date.".to_string());
    }
    let mut params = vec![];
    if let Some(date) = as_of { params.push(("as_of", date)); }
    read_backend("/api/v1/market-data/indexes", &params).await
}

#[tauri::command]
async fn index_constituents(
    code: String,
    as_of: Option<String>,
    q: String,
    limit: u8,
    offset: u32,
) -> Result<serde_json::Value, String> {
    if !valid_index(&code) || q.len() > 80 || q.chars().any(|c| c.is_control()) ||
       limit == 0 || limit > 100 || as_of.as_deref().is_some_and(|d| !valid_date(d)) {
        return Err("Invalid index query.".to_string());
    }
    let mut params = vec![
        ("q", q),
        ("limit", limit.to_string()),
        ("offset", offset.to_string()),
    ];
    if let Some(date) = as_of { params.push(("as_of", date)); }
    read_backend(
        &format!("/api/v1/market-data/indexes/{code}/constituents"),
        &params,
    ).await
}

/// All three layers limit Unicode scalar characters (not UTF-8 bytes) to 80.
fn valid_overlap_search(query: &str) -> bool {
    query.chars().count() <= 80 && !query.chars().any(|ch| ch.is_control())
}

#[cfg(test)]
mod overlap_search_tests {
    use super::valid_overlap_search;

    #[test]
    fn chinese_search_counts_characters_not_utf8_bytes() {
        assert!(valid_overlap_search(&"中".repeat(80)));
        assert!(!valid_overlap_search(&"中".repeat(81)));
        assert!(valid_overlap_search(&"📈".repeat(80)));
        assert!(!valid_overlap_search(&"📈".repeat(81)));
        assert!(!valid_overlap_search("newline\n"));
    }
}

/// Dated membership overlap is a fixed read-only local route, not a provider lookup.
#[tauri::command]
async fn index_membership_overlap(
    left: String,
    right: String,
    as_of: String,
    q: String,
    limit: u8,
    offset: u32,
) -> Result<serde_json::Value, String> {
    if !valid_index(&left) || !valid_index(&right) || left == right
        || !valid_date(&as_of)
        || !valid_overlap_search(&q)
        || limit == 0 || limit > 100 || offset > 1_000_000
    {
        return Err("Invalid dated membership overlap query.".to_string());
    }
    read_backend(
        "/api/v1/market-data/indexes/overlap",
        &[
            ("left", left), ("right", right), ("as_of", as_of),
            ("q", q), ("limit", limit.to_string()), ("offset", offset.to_string()),
        ],
    ).await
}

#[tauri::command]
async fn index_membership_trend(
    code: String,
    from_as_of: String,
    to_as_of: String,
    limit: u8,
    offset: u32,
    evidence_status: Option<String>,
) -> Result<serde_json::Value, String> {
    if !valid_index(&code) || !valid_date(&from_as_of) || !valid_date(&to_as_of)
        || from_as_of > to_as_of || limit == 0 || limit > 100 || offset > 1_000_000
        || evidence_status.as_deref().is_some_and(|status| !matches!(
            status, "verified_snapshot" | "source_date_mismatch" | "proxy_only" |
            "incomplete" | "unverified"
        ))
    {
        return Err("Invalid dated membership trend query.".to_string());
    }
    let mut params = vec![
        ("code", code),
        ("from_as_of", from_as_of),
        ("to_as_of", to_as_of),
        ("limit", limit.to_string()),
        ("offset", offset.to_string()),
    ];
    if let Some(status) = evidence_status {
        params.push(("evidence_status", status));
    }
    read_backend("/api/v1/market-data/indexes/snapshot-trend", &params).await
}

#[tauri::command]
async fn index_stock_memberships(
    symbol: String,
    as_of: Option<String>,
) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) || as_of.as_deref().is_some_and(|d| !valid_date(d)) {
        return Err("Invalid stock membership query.".to_string());
    }
    let mut params = vec![];
    if let Some(date) = as_of { params.push(("as_of", date)); }
    read_backend(
        &format!("/api/v1/market-data/indexes/symbol/{}/memberships", symbol.to_ascii_uppercase()),
        &params,
    ).await
}

#[tauri::command]
async fn index_membership_changes(
    code: String,
    from_as_of: String,
    to_as_of: String,
    q: String,
    limit: u8,
    offset: u32,
) -> Result<serde_json::Value, String> {
    if !valid_index(&code) || !valid_date(&from_as_of) || !valid_date(&to_as_of) ||
       q.len() > 80 || q.chars().any(|c| c.is_control()) || limit == 0 || limit > 100 {
        return Err("Invalid index change query.".to_string());
    }
    let params = vec![
        ("from_as_of", from_as_of),
        ("to_as_of", to_as_of),
        ("q", q),
        ("limit", limit.to_string()),
        ("offset", offset.to_string()),
    ];
    read_backend(&format!("/api/v1/market-data/indexes/{code}/changes"), &params).await
}

fn valid_credential_path(provider: &str, field: &str) -> bool {
    matches!(
        (provider, field),
        ("alpha_vantage", "api_key")
            | ("alpaca_sip", "key_id")
            | ("alpaca_sip", "secret_key")
            | ("tiingo", "api_key")
            | ("equibles", "api_key")
    )
}

#[tauri::command]
async fn credential_status() -> Result<serde_json::Value, String> {
    if backend_token().is_empty() {
        return Err("Provider credential Settings require the managed desktop build.".to_string());
    }
    read_backend("/api/v1/settings/providers", &[]).await
}

#[tauri::command]
async fn credential_set(
    provider: String, field: String, secret: String
) -> Result<serde_json::Value, String> {
    if !valid_credential_path(&provider, &field) || secret.is_empty()
        || secret.len() > 4096 || secret.chars().any(char::is_control)
        || backend_token().is_empty()
    {
        return Err("Invalid secure provider credential request.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize secure credential client.".to_string())?;
    let response = client
        .put(format!("{}/api/v1/settings/providers/{provider}/{field}", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .json(&serde_json::json!({ "value": secret }))
        .send().await
        .map_err(|_| "Secure provider settings service unavailable.".to_string())?;
    if !response.status().is_success() {
        return Err(format!("Credential save unsuccessful (HTTP {}).", response.status()));
    }
    Ok(serde_json::json!({"configured": true}))
}

#[tauri::command]
async fn credential_delete(
    provider: String, field: String
) -> Result<serde_json::Value, String> {
    if !valid_credential_path(&provider, &field) || backend_token().is_empty() {
        return Err("Invalid native credential request.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize secure credential client.".to_string())?;
    let response = client
        .delete(format!("{}/api/v1/settings/providers/{provider}/{field}", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .send().await
        .map_err(|_| "Secure provider settings service unavailable.".to_string())?;
    if !response.status().is_success() {
        return Err(format!("Credential deletion unsuccessful (HTTP {}).", response.status()));
    }
    Ok(serde_json::json!({"configured": false}))
}

#[tauri::command]
async fn credential_test(provider: String) -> Result<serde_json::Value, String> {
    if !matches!(provider.as_str(), "alpha_vantage" | "alpaca_sip" | "tiingo" | "equibles")
        || backend_token().is_empty()
    {
        return Err("Invalid provider connection test request.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(16))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize connection test client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/settings/providers/{provider}/test", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .send().await
        .map_err(|_| "Local connection test service unavailable.".to_string())?;
    if !response.status().is_success() {
        return Err(format!("Provider test could not run (HTTP {}).", response.status()));
    }
    response.json::<serde_json::Value>().await
        .map_err(|_| "Provider connection test returned malformed JSON.".to_string())
}

async fn backup_write(path: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
    if backend_token().is_empty() {
        return Err("Database backups require the managed desktop.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(600))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not create backup service client.".to_string())?;
    let response = client
        .post(format!("{}{}", backend_base(), path))
        .header("x-finintel-process-token", backend_token())
        .json(&body)
        .send()
        .await
        .map_err(|_| "Local backup service unavailable.".to_string())?;
    if !response.status().is_success() {
        return Err(format!("Database backup operation failed (HTTP {}).", response.status()));
    }
    response.json::<serde_json::Value>().await
        .map_err(|_| "Database backup service returned malformed JSON.".to_string())
}

#[tauri::command]
async fn database_backup_list() -> Result<serde_json::Value, String> {
    if backend_token().is_empty() {
        return Err("Database backups require the managed desktop.".to_string());
    }
    read_backend("/api/v1/settings/backups", &[]).await
}

#[tauri::command]
async fn database_backup_create() -> Result<serde_json::Value, String> {
    backup_write("/api/v1/settings/backups", serde_json::json!({})).await
}

#[tauri::command]
async fn database_backup_preview(backup_id: String) -> Result<serde_json::Value, String> {
    if backend_token().is_empty()
        || backup_id.len() > 120
        || !backup_id.starts_with("finintel-")
        || !backup_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '.')
    {
        return Err("Invalid local database backup ID.".to_string());
    }
    read_backend(&format!("/api/v1/settings/backups/{backup_id}"), &[]).await
}

#[tauri::command]
async fn database_backup_restore(backup_id: String, mode: String) -> Result<serde_json::Value, String> {
    if backup_id.len() > 120
        || !backup_id.starts_with("finintel-")
        || !backup_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '.')
        || !matches!(mode.as_str(), "merge" | "replace")
    {
        return Err("Invalid database restore choice.".to_string());
    }
    backup_write(
        "/api/v1/settings/backups/restore",
        serde_json::json!({"backup_id": backup_id, "mode": mode}),
    ).await
}


fn valid_index_history_code(code: &str) -> bool {
    matches!(code, "SP500" | "DJIA" | "NASDAQCOM" | "NDX" | "VIX")
}

fn valid_index_history_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    if !(bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit()))
    {
        return false;
    }
    let Ok(year) = value[0..4].parse::<u32>() else { return false };
    let Ok(month) = value[5..7].parse::<u32>() else { return false };
    let Ok(day) = value[8..10].parse::<u32>() else { return false };
    if year == 0 || !(1..=12).contains(&month) || day == 0 {
        return false;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    day <= days_in_month
}

/// Fixed-shape read-only local analytics query; no arbitrary paths or providers.
#[tauri::command]
async fn analytics_series(
    code: String,
    comparison_code: Option<String>,
    start: String,
    end: String,
    window: u16,
    quality_policy: String,
) -> Result<serde_json::Value, String> {
    if !valid_index_history_code(&code)
        || comparison_code.as_deref().is_some_and(|value| {
            !valid_index_history_code(value) || value == code
                || code == "VIX" || value == "VIX"
        })
        || !valid_index_history_date(&start)
        || !valid_index_history_date(&end)
        || start > end
        || !matches!(window, 5 | 10 | 14 | 20 | 21 | 50 | 63 | 126 | 200 | 252)
        || !matches!(quality_policy.as_str(), "require_complete" | "allow_unverified")
    {
        return Err("Invalid index analytics query.".to_string());
    }
    let mut parameters = vec![
        ("kind", "index".to_string()),
        ("code", code),
        ("provider", "fred".to_string()),
        ("start", start),
        ("end", end),
        ("window", window.to_string()),
        ("quality_policy", quality_policy),
    ];
    if let Some(benchmark) = comparison_code {
        parameters.push(("comparison_code", benchmark));
        parameters.push(("comparison_kind", "index".to_string()));
        parameters.push(("comparison_provider", "fred".to_string()));
    }
    read_backend("/api/v1/analytics/series", &parameters).await
}

/// A fixed, allowlisted research indicator: no dynamic code, arbitrary URL or orders.
#[tauri::command]
async fn model_research_evaluate(
    code: String,
    start: String,
    end: String,
    quality_policy: String,
    fast_window: u16,
    slow_window: u16,
    expected_snapshot: Option<String>,
) -> Result<serde_json::Value, String> {
    if backend_token().is_empty()
        || !valid_index_history_code(&code) || code == "VIX"
        || !valid_index_history_date(&start)
        || !valid_index_history_date(&end) || start > end
        || !matches!(quality_policy.as_str(), "require_complete" | "allow_unverified")
        || !(2..=100).contains(&fast_window) || !(3..=252).contains(&slow_window)
        || fast_window >= slow_window
        || expected_snapshot.as_ref().is_some_and(|hash| {
            hash.len() != 64 || !hash.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        })
    {
        return Err("Invalid or unauthorized local model evaluation.".to_string());
    }
    let body = serde_json::json!({
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "code": code,
        "start": start,
        "end": end,
        "quality_policy": quality_policy,
        "parameters": {"fast_window": fast_window, "slow_window": slow_window},
        "expected_snapshot_sha256": expected_snapshot,
    });
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local research client.".to_string())?;
    let response = client
        .post(format!("{}/api/v1/models/evaluate", backend_base()))
        .header("x-finintel-process-token", backend_token())
        .json(&body)
        .send()
        .await
        .map_err(|_| "Could not reach the managed local model backend.".to_string())?;
    let status = response.status();
    let payload = response.json::<serde_json::Value>().await
        .map_err(|_| "Local model backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload.get("detail").and_then(serde_json::Value::as_str)
            .filter(|msg| msg.len() <= 300)
            .unwrap_or("Local model execution was refused.");
        return Err(format!("HTTP {}: {}", status.as_u16(), detail));
    }
    Ok(payload)
}

/// Fixed managed-only routes for durable local model jobs. No URL or command string
/// is accepted from the webview and no proprietary model source is executable.
fn valid_model_job_id(value: &str) -> bool {
    uuid::Uuid::parse_str(value)
        .ok()
        .is_some_and(|parsed| parsed.to_string() == value)
}

fn valid_model_job_hash(hash: &str) -> bool {
    hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

async fn model_jobs_write(path: &str, body: Option<serde_json::Value>) -> Result<serde_json::Value, String> {
    if backend_token().is_empty() {
        return Err("Model jobs require the managed local desktop.".into());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(35))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local model-jobs client.".to_string())?;
    let mut request = client.post(format!("{}{}", backend_base(), path))
        .header("x-finintel-process-token", backend_token());
    if let Some(ref value) = body { request = request.json(value); }
    let response = request.send().await
        .map_err(|_| "Local model-jobs backend is unavailable.".to_string())?;
    let status = response.status();
    let payload = response.json::<serde_json::Value>().await
        .map_err(|_| "Model-jobs backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload.get("detail").and_then(serde_json::Value::as_str)
            .filter(|value| value.len() <= 160)
            .unwrap_or("Model job request was rejected.");
        return Err(format!("HTTP {}: {}", status.as_u16(), detail));
    }
    Ok(payload)
}

#[tauri::command]
async fn model_jobs_create(
    code: String,
    start: String,
    end: String,
    quality_policy: String,
    fast_window: u16,
    slow_window: u16,
    expected_snapshot: Option<String>,
) -> Result<serde_json::Value, String> {
    if !valid_index_history_code(&code) || code == "VIX"
        || !valid_index_history_date(&start) || !valid_index_history_date(&end)
        || start > end
        || !matches!(quality_policy.as_str(), "require_complete" | "allow_unverified")
        || !(2..=100).contains(&fast_window) || !(3..=252).contains(&slow_window)
        || fast_window >= slow_window
        || expected_snapshot.as_deref().is_some_and(|value| !valid_model_job_hash(value))
    {
        return Err("Invalid local model-job request.".into());
    }
    model_jobs_write("/api/v1/models/jobs", Some(serde_json::json!({
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "code": code,
        "start": start,
        "end": end,
        "quality_policy": quality_policy,
        "parameters": {"fast_window": fast_window, "slow_window": slow_window},
        "expected_snapshot_sha256": expected_snapshot,
    }))).await
}

#[tauri::command]
async fn model_jobs_list(limit: u8, offset: u32) -> Result<serde_json::Value, String> {
    if backend_token().is_empty() || limit == 0 || limit > 100 || offset > 10_000 {
        return Err("Invalid managed model-job pagination.".into());
    }
    read_backend("/api/v1/models/jobs", &[
        ("limit", limit.to_string()), ("offset", offset.to_string()),
    ]).await
}

#[tauri::command]
async fn model_jobs_get(job_id: String, include_result: bool) -> Result<serde_json::Value, String> {
    if backend_token().is_empty() || !valid_model_job_id(&job_id) {
        return Err("Invalid managed model-job identifier.".into());
    }
    read_backend(&format!("/api/v1/models/jobs/{job_id}"), &[
        ("include_result", include_result.to_string()),
    ]).await
}

#[tauri::command]
async fn model_jobs_cancel(job_id: String) -> Result<serde_json::Value, String> {
    if !valid_model_job_id(&job_id) {
        return Err("Invalid local model-job identifier.".into());
    }
    model_jobs_write(&format!("/api/v1/models/jobs/{job_id}/cancel"), None).await
}

#[tauri::command]
async fn model_jobs_retry(job_id: String) -> Result<serde_json::Value, String> {
    if !valid_model_job_id(&job_id) {
        return Err("Invalid local model-job identifier.".into());
    }
    model_jobs_write(&format!("/api/v1/models/jobs/{job_id}/retry"), None).await
}

/// Read-only, bounded multi-index query with fixed local FRED series.
#[tauri::command]
async fn analytics_basket(
    codes: Vec<String>,
    start: String,
    end: String,
    window: u16,
    quality_policy: String,
) -> Result<serde_json::Value, String> {
    if !(2..=4).contains(&codes.len())
        || codes.iter().any(|code| !valid_index_history_code(code) || code == "VIX")
        || codes.iter().collect::<std::collections::HashSet<_>>().len() != codes.len()
        || !valid_index_history_date(&start)
        || !valid_index_history_date(&end)
        || start > end
        || !matches!(window, 5 | 10 | 14 | 20 | 21 | 50 | 63 | 126 | 200 | 252)
        || !matches!(quality_policy.as_str(), "require_complete" | "allow_unverified")
    {
        return Err("Invalid multi-index comparison selection.".to_string());
    }
    read_backend(
        "/api/v1/analytics/basket",
        &[
            ("codes", codes.join(",")),
            ("start", start),
            ("end", end),
            ("window", window.to_string()),
            ("quality_policy", quality_policy),
        ],
    ).await
}

#[tauri::command]
async fn index_history_catalog() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/market-data/index-history", &[]).await
}

#[tauri::command]
async fn index_history_daily(code: String, limit: u16) -> Result<serde_json::Value, String> {
    if !valid_index_history_code(&code) || !(1..=10000).contains(&limit) {
        return Err("Invalid index history query.".to_string());
    }
    read_backend(
        &format!("/api/v1/market-data/index-history/{code}/daily"),
        &[("limit", limit.to_string())],
    )
    .await
}

#[tauri::command]
async fn index_history_acquire(
    code: String,
    start: Option<String>,
    end: Option<String>,
) -> Result<serde_json::Value, String> {
    if !valid_index_history_code(&code)
        || backend_token().is_empty()
        || start.as_deref().is_some_and(|value| !valid_index_history_date(value))
        || end.as_deref().is_some_and(|value| !valid_index_history_date(value))
        || start.as_deref().zip(end.as_deref()).is_some_and(|(first, last)| first > last)
    {
        return Err("Index acquisition requires a managed desktop and valid index.".to_string());
    }
    // First write may perform a verified backup of the large local database
    // before the v6 -> v7 migration. Do not abandon that write after 120s.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(900))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Unable to initialize the index history client.".to_string())?;
    let mut url = reqwest::Url::parse(&format!(
        "{}/api/v1/market-data/index-history/{code}/acquire",
        backend_base()
    ))
    .map_err(|_| "Unable to initialize the index history URL.".to_string())?;
    {
        let mut query = url.query_pairs_mut();
        if let Some(value) = start.as_deref() {
            query.append_pair("start", value);
        }
        if let Some(value) = end.as_deref() {
            query.append_pair("end", value);
        }
    }
    let response = client
        .post(url)
        .header("x-finintel-process-token", backend_token())
        .send()
        .await
        .map_err(|error| {
            if error.is_timeout() {
                "Index acquisition timed out. The backend may still be finishing a database backup or migration; check its status before retrying.".to_string()
            } else if error.is_connect() {
                "Index history backend is unreachable. Restart FinIntel with npm run dev:desktop and verify the Python backend is running.".to_string()
            } else {
                format!("Index history request failed: {error}")
            }
        })?;
    let status = response.status();
    let payload = response.json::<serde_json::Value>().await
        .map_err(|_| "Index history backend returned invalid JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload.get("detail").and_then(serde_json::Value::as_str)
            .unwrap_or("Index history request failed.");
        return Err(format!("HTTP {}: {detail}", status.as_u16()));
    }
    Ok(payload)
}

async fn daily_write(
    path: &str,
    method: reqwest::Method,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the daily acquisition client.".to_string())?;
    let mut request = client
        .request(method, format!("{}{}", backend_base(), path))
        .header("x-finintel-process-token", backend_token())
        .header("x-finintel-client", "local-workstation");
    if let Some(payload) = body {
        request = request.json(&payload);
    }
    let response = request
        .send()
        .await
        .map_err(|_| "Daily acquisition backend is unreachable or timed out.".to_string())?;
    let status = response.status();
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Daily acquisition backend returned malformed JSON.".to_string())?;
    if !status.is_success() {
        let detail = payload
            .get("detail")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("Daily acquisition request failed.");
        return Err(format!("HTTP {}: {detail}", status.as_u16()));
    }
    Ok(payload)
}

#[tauri::command]
async fn daily_acquisition_status() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/daily-acquisition/status", &[]).await
}

#[tauri::command]
async fn daily_acquisition_settings() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/daily-acquisition/settings", &[]).await
}

#[tauri::command]
async fn daily_acquisition_job(job_id: String) -> Result<serde_json::Value, String> {
    if job_id.len() != 32 || !job_id.chars().all(|value| value.is_ascii_hexdigit()) {
        return Err("Invalid daily acquisition job ID.".to_string());
    }
    read_backend(
        &format!("/api/v1/daily-acquisition/jobs/{job_id}"),
        &[],
    )
    .await
}

#[tauri::command]
async fn daily_acquisition_save_settings(
    settings: serde_json::Value,
) -> Result<serde_json::Value, String> {
    if !settings.is_object() {
        return Err("Invalid daily acquisition settings.".to_string());
    }
    daily_write(
        "/api/v1/daily-acquisition/settings",
        reqwest::Method::PUT,
        Some(settings),
    )
    .await
}

#[tauri::command]
async fn daily_acquisition_run() -> Result<serde_json::Value, String> {
    daily_write("/api/v1/daily-acquisition/run", reqwest::Method::POST, None).await
}

async fn daily_control(job_id: String, action: &str) -> Result<serde_json::Value, String> {
    if job_id.len() != 32 || !job_id.chars().all(|value| value.is_ascii_hexdigit()) {
        return Err("Invalid daily acquisition job ID.".to_string());
    }
    daily_write(
        &format!("/api/v1/daily-acquisition/jobs/{job_id}/{action}"),
        reqwest::Method::POST,
        None,
    )
    .await
}

#[tauri::command]
async fn daily_acquisition_pause(job_id: String) -> Result<serde_json::Value, String> {
    daily_control(job_id, "pause").await
}

#[tauri::command]
async fn daily_acquisition_resume(job_id: String) -> Result<serde_json::Value, String> {
    daily_control(job_id, "resume").await
}

#[tauri::command]
async fn daily_acquisition_cancel(job_id: String) -> Result<serde_json::Value, String> {
    daily_control(job_id, "cancel").await
}

#[tauri::command]
async fn daily_acquisition_retry_failed(job_id: String) -> Result<serde_json::Value, String> {
    daily_control(job_id, "retry-failed").await
}

#[tauri::command]
async fn daily_acquisition_history(
    q: String,
    provider: String,
    status: String,
    kind: String,
    execution_date: String,
    limit: u8,
    offset: u32,
) -> Result<serde_json::Value, String> {
    if q.len() > 80 || provider.len() > 40 || status.len() > 20 || kind.len() > 20
        || execution_date.len() > 10
        || limit == 0 || limit > 100
    {
        return Err("Invalid daily acquisition history query.".to_string());
    }
    read_backend(
        "/api/v1/daily-acquisition/history",
        &[
            ("q", q),
            ("provider", provider),
            ("status", status),
            ("kind", kind),
            ("execution_date", execution_date),
            ("limit", limit.to_string()),
            ("offset", offset.to_string()),
        ],
    )
    .await
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Native development can keep using the standard developer backend.
            // Production bundles launch an app-owned backend with a private port.
            if cfg!(debug_assertions) {
                // Combined developer launcher starts Python and provides a shared random token.
                // Plain `tauri dev` without that launcher retains read-only legacy mode.
                if let Ok(token) = std::env::var("FININTEL_PROCESS_TOKEN") {
                    if token.len() >= 32 {
                        BACKEND_TOKEN.set(token).map_err(|_| "Token already initialized")?;
                    }
                }
                return Ok(());
            }
            let port = available_backend_port()?;
            let token = uuid::Uuid::new_v4().to_string();
            let app_data = app.path().app_data_dir()?;
            std::fs::create_dir_all(&app_data)?;
            let db_path = app_data.join("finintel.sqlite3");
            BACKEND_TOKEN.set(token.clone()).map_err(|_| "Token already initialized")?;
            let (receiver, child) = app.shell()
                .sidecar("finintel-backend")?
                .env("FININTEL_BACKEND_PORT", port.to_string())
                .env("FININTEL_PROCESS_TOKEN", token.clone())
                .env("FININTEL_DB_PATH", db_path.to_string_lossy().into_owned())
                .spawn()?;
            if !tauri::async_runtime::block_on(wait_for_backend(
                port,
                &token,
                20,
                Duration::from_millis(250),
            )) {
                let _ = child.kill();
                return Err("Managed Python backend did not become healthy.".into());
            }
            BACKEND_PORT.store(port, Ordering::Release);
            app.manage(ManagedBackend {
                child: Mutex::new(Some(child)),
                stopping: AtomicBool::new(false),
            });
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut receiver = receiver;
                loop {
                    loop {
                        match receiver.recv().await {
                            Some(CommandEvent::Terminated(_)) | None => break,
                            Some(_) => {}
                        }
                    }
                    let should_stop = handle
                        .try_state::<ManagedBackend>()
                        .map(|state| state.stopping.load(Ordering::Acquire))
                        .unwrap_or(true);
                    if should_stop { break; }

                    let mut restarted = false;
                    for _ in 0..3 {
                        tokio::time::sleep(Duration::from_millis(500)).await;
                        let should_stop = handle
                            .try_state::<ManagedBackend>()
                            .map(|state| state.stopping.load(Ordering::Acquire))
                            .unwrap_or(true);
                        if should_stop { break; }
                        let Ok(next_port) = available_backend_port() else { continue; };
                        let spawned = handle.shell()
                            .sidecar("finintel-backend")
                            .and_then(|command| command
                                .env("FININTEL_BACKEND_PORT", next_port.to_string())
                                .env("FININTEL_PROCESS_TOKEN", token.clone())
                                .env("FININTEL_DB_PATH", db_path.to_string_lossy().into_owned())
                                .spawn());
                        if let Ok((next_receiver, next_child)) = spawned {
                            if !wait_for_backend(
                                next_port,
                                &token,
                                10,
                                Duration::from_millis(250),
                            )
                            .await
                            {
                                let _ = next_child.kill();
                                continue;
                            }
                            let Some(state) = handle.try_state::<ManagedBackend>() else {
                                let _ = next_child.kill();
                                break;
                            };
                            if state.stopping.load(Ordering::Acquire) {
                                let _ = next_child.kill();
                                break;
                            }
                            if let Ok(mut guard) = state.child.lock() {
                                *guard = Some(next_child);
                            }
                            BACKEND_PORT.store(next_port, Ordering::Release);
                            receiver = next_receiver;
                            restarted = true;
                            break;
                        }
                    }
                    if !restarted {
                        eprintln!("Managed Python backend restart attempts exhausted.");
                        BACKEND_PORT.store(0, Ordering::Release);
                        break;
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_health,
            credential_status,
            credential_set,
            credential_delete,
            credential_test,
            database_backup_list,
            database_backup_create,
            database_backup_preview,
            database_backup_restore,
            quality_read,
            quality_write,
            validation_status,
            validation_start,
            validation_corporate_reference,
            market_instruments,
            market_instrument_directory,
            market_daily,
            market_imports,
            market_imports_search,
            market_acquisition_status,
            market_acquire_daily,
            market_symbols,
            market_sync_symbols,
            index_acquire,
            index_history_catalog,
            index_history_daily,
            analytics_series,
            analytics_basket,
            model_research_evaluate,
            model_jobs_create,
            model_jobs_list,
            model_jobs_get,
            model_jobs_cancel,
            model_jobs_retry,
            index_history_acquire,
            daily_acquisition_status,
            daily_acquisition_settings,
            daily_acquisition_job,
            daily_acquisition_save_settings,
            daily_acquisition_run,
            daily_acquisition_pause,
            daily_acquisition_resume,
            daily_acquisition_cancel,
            daily_acquisition_retry_failed,
            daily_acquisition_history,
            index_catalog,
            index_constituents,
            index_membership_overlap,
            index_membership_trend,
            index_stock_memberships,
            index_membership_changes
        ])
        .build(tauri::generate_context!())
        .expect("failed to build FinIntel")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Some(owned) = app.try_state::<ManagedBackend>() {
                    owned.stopping.store(true, Ordering::Release);
                    if let Ok(mut guard) = owned.child.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{valid_model_job_hash, valid_model_job_id, wait_for_backend};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    fn response(status: u16, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    fn health_server(statuses: Vec<u16>) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test health server");
        let port = listener.local_addr().expect("test server address").port();
        let thread = thread::spawn(move || {
            for status in statuses {
                let Ok((mut stream, _)) = listener.accept() else { return; };
                let mut request = [0_u8; 2048];
                let _ = stream.read(&mut request);
                let body = if status == 200 {
                    r#"{"status":"ok","service":"finintel-backend","version":"test"}"#
                } else {
                    r#"{"status":"starting"}"#
                };
                let _ = stream.write_all(response(status, body).as_bytes());
            }
        });
        (port, thread)
    }

    #[test]
    fn model_job_native_bridge_rejects_noncanonical_ids_and_unpinned_revisions() {
        assert!(valid_model_job_id("123e4567-e89b-12d3-a456-426614174000"));
        assert!(!valid_model_job_id("../../local-db"));
        assert!(!valid_model_job_id("123E4567-E89B-12D3-A456-426614174000"));
        assert!(!valid_model_job_id("not-a-uuid"));
        assert!(valid_model_job_hash(&"a".repeat(64)));
        assert!(!valid_model_job_hash(&"A".repeat(64)));
        assert!(!valid_model_job_hash(&"g".repeat(64)));
        assert!(!valid_model_job_hash("token"));
    }

    #[test]
    fn restart_health_check_accepts_a_ready_sidecar() {
        let (port, server) = health_server(vec![200]);
        let ready = tauri::async_runtime::block_on(wait_for_backend(
            port,
            "test-token",
            1,
            Duration::ZERO,
        ));
        assert!(ready);
        server.join().expect("health server thread");
    }

    #[test]
    fn restart_health_check_rejects_a_failed_spawn_port() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("reserve test port");
        let port = listener.local_addr().expect("reserved port").port();
        drop(listener);
        let ready = tauri::async_runtime::block_on(wait_for_backend(
            port,
            "test-token",
            2,
            Duration::ZERO,
        ));
        assert!(!ready);
    }

    #[test]
    fn restart_health_check_recovers_after_an_initial_failure() {
        let (port, server) = health_server(vec![503, 200]);
        let ready = tauri::async_runtime::block_on(wait_for_backend(
            port,
            "test-token",
            2,
            Duration::ZERO,
        ));
        assert!(ready);
        server.join().expect("health server thread");
    }
}
