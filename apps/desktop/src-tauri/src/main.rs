use std::time::Duration;

#[tauri::command]
async fn backend_health() -> Result<serde_json::Value, String> {
    // PR #1 development-only transport. Authentication and sidecar lifecycle
    // are introduced in PR #5. The webview never receives a generic HTTP proxy.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .map_err(|_| "Could not initialize the local HTTP client.".to_string())?;

    let response = client
        .get("http://127.0.0.1:8765/api/v1/health")
        .send()
        .await
        .map_err(|_| "Could not reach the local Python backend.".to_string())?;

    if !response.status().is_success() {
        return Err(format!("Python backend returned HTTP {}.", response.status()));
    }

    response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Python backend returned malformed JSON.".to_string())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![backend_health])
        .run(tauri::generate_context!())
        .expect("failed to run FinIntel");
}
