use std::time::Duration;

const BACKEND: &str = "http://127.0.0.1:8765";

async fn read_backend(path: &str, params: &[(&str, String)]) -> Result<serde_json::Value, String> {
    // Fixed loopback host, strictly bounded routes and parameters.
    // No generic proxy, client-selected host or credentials in the webview.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local HTTP client.".to_string())?;
    let mut url = reqwest::Url::parse(&format!("{BACKEND}{path}"))
        .map_err(|_| "Invalid local backend route.".to_string())?;
    if !params.is_empty() {
        url.query_pairs_mut().extend_pairs(params.iter().map(|(k, v)| (*k, v.as_str())));
    }
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|_| "Could not reach the local Python backend.".to_string())?;

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
        .post(format!("{BACKEND}/api/v1/market-data/acquisition"))
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
        .post(format!("{BACKEND}/api/v1/market-data/symbols/sync"))
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

#[tauri::command]
async fn validation_status() -> Result<serde_json::Value, String> {
    read_backend("/api/v1/validation/status", &[]).await
}

#[tauri::command]
async fn validation_start(checks: Vec<String>) -> Result<serde_json::Value, String> {
    const ALLOWED: [&str; 5] = ["coverage", "integrity", "corporate", "provider", "performance"];
    if checks.is_empty() || checks.len() > 5
        || checks.iter().any(|check| !ALLOWED.contains(&check.as_str()))
    {
        return Err("Invalid validation checks.".to_string());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize local HTTP client.".to_string())?;
    let response = client.post(format!("{BACKEND}/api/v1/validation/run"))
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

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            backend_health,
            validation_status,
            validation_start,
            market_instruments,
            market_instrument_directory,
            market_daily,
            market_imports,
            market_imports_search,
            market_acquisition_status,
            market_acquire_daily,
            market_symbols,
            market_sync_symbols
        ])
        .run(tauri::generate_context!())
        .expect("failed to run FinIntel");
}
