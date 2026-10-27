use std::time::Duration;
use std::sync::{Mutex, OnceLock};
use tauri::Manager;
use tauri_plugin_shell::{process::CommandChild, ShellExt};

static BACKEND_PORT: OnceLock<u16> = OnceLock::new();
static BACKEND_TOKEN: OnceLock<String> = OnceLock::new();

struct ManagedBackend(Mutex<Option<CommandChild>>);

fn backend_base() -> String {
    format!("http://127.0.0.1:{}", BACKEND_PORT.get().copied().unwrap_or(8765))
}

fn backend_token() -> &'static str {
    BACKEND_TOKEN.get().map(String::as_str).unwrap_or("")
}

async fn read_backend(path: &str, params: &[(&str, String)]) -> Result<serde_json::Value, String> {
    // Fixed loopback host, strictly bounded routes and parameters.
    // No generic proxy, client-selected host or credentials in the webview.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
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
            let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            drop(listener);
            let token = uuid::Uuid::new_v4().to_string();
            let app_data = app.path().app_data_dir()?;
            std::fs::create_dir_all(&app_data)?;
            let db_path = app_data.join("finintel.sqlite3");
            BACKEND_PORT.set(port).map_err(|_| "Port already initialized")?;
            BACKEND_TOKEN.set(token.clone()).map_err(|_| "Token already initialized")?;
            let (mut receiver, child) = app.shell()
                .sidecar("finintel-backend")?
                .env("FININTEL_BACKEND_PORT", port.to_string())
                .env("FININTEL_PROCESS_TOKEN", token)
                .env("FININTEL_DB_PATH", db_path.to_string_lossy().into_owned())
                .spawn()?;
            tauri::async_runtime::spawn(async move {
                while receiver.recv().await.is_some() {}
            });
            app.manage(ManagedBackend(Mutex::new(Some(child))));
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
            index_catalog,
            index_constituents,
            index_stock_memberships
        ])
        .build(tauri::generate_context!())
        .expect("failed to build FinIntel")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Some(owned) = app.try_state::<ManagedBackend>() {
                    if let Ok(mut guard) = owned.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
