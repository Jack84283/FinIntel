use std::time::Duration;

const BACKEND: &str = "http://127.0.0.1:8765";

async fn read_backend(path: &str, params: &[(&str, String)]) -> Result<serde_json::Value, String> {
    // Fixed loopback host, strictly bounded routes and parameters.
    // No generic proxy, client-selected host or credentials in the webview.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
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
) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) {
        return Err("Invalid ticker symbol.".to_string());
    }
    if outputsize != "compact" && outputsize != "full" {
        return Err("Output size must be compact or full.".to_string());
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(45))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not initialize the local HTTP client.".to_string())?;
    let response = client
        .post(format!("{BACKEND}/api/v1/market-data/acquisition"))
        .header("x-finintel-client", "local-workstation")
        .json(&serde_json::json!({
            "symbol": symbol.to_ascii_uppercase(),
            "outputsize": outputsize
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
) -> Result<serde_json::Value, String> {
    if !valid_symbol(&symbol) {
        return Err("Invalid ticker symbol.".to_string());
    }
    if limit == 0 || limit > 1000 {
        return Err("Daily history limit must be 1–1000.".to_string());
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
    let mut params = vec![("limit", limit.to_string()), ("latest", latest.to_string())];
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

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            backend_health,
            market_instruments,
            market_daily,
            market_imports,
            market_acquisition_status,
            market_acquire_daily
        ])
        .run(tauri::generate_context!())
        .expect("failed to run FinIntel");
}
