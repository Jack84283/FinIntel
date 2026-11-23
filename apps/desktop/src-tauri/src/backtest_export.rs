//! Export only a verified completed ledger entry to a native-dialog destination.

use std::{
    fs,
    io::{self, Write},
    path::Path,
    sync::atomic::{AtomicBool, Ordering},
};

use serde::Deserialize;
use serde_json::{json, Value};
use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;

use super::backtest_jobs;

const EQUITY: &[&str] = &[
    "session_date",
    "equity_after_cost",
    "frictionless_benchmark_equity",
    "exposure_after_close",
    "drawdown",
    "transaction_cost",
    "executed_from_prior_signal",
];
const TRANSACTIONS: &[&str] = &[
    "signal_as_of_session",
    "hypothetical_fill_session",
    "exposure_before",
    "exposure_after",
    "turnover_fraction",
    "cost",
];
static EXPORTING: AtomicBool = AtomicBool::new(false);

struct ExportPermit;

impl Drop for ExportPermit {
    fn drop(&mut self) {
        EXPORTING.store(false, Ordering::Release);
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportFormat {
    Json,
    Equity,
    Transactions,
}

#[derive(Clone, Copy, Deserialize)]
pub enum ExportLocale {
    #[serde(rename = "en")]
    English,
    #[serde(rename = "zh-CN")]
    Chinese,
}

impl ExportLocale {
    fn title(self) -> &'static str {
        match self {
            Self::English => "Save backtest report",
            Self::Chinese => "保存回测报告",
        }
    }
}

impl ExportFormat {
    fn extension(self) -> &'static str {
        match self {
            Self::Json => "json",
            _ => "csv",
        }
    }

    fn filename(self) -> &'static str {
        match self {
            Self::Json => "finintel-backtest.json",
            Self::Equity => "finintel-backtest-equity.csv",
            Self::Transactions => "finintel-backtest-hypothetical-transactions.csv",
        }
    }
}

fn csv_cell(value: &str) -> String {
    let significant = value.trim_start_matches(|c: char| c.is_whitespace() || c.is_control());
    let formula = significant.starts_with(['=', '+', '-', '@'])
        && !super::analysis_jobs::decimal(value, false);
    let control = value.starts_with(['\t', '\r', '\n']);
    let safe = if formula || control {
        format!("'{value}")
    } else {
        value.to_owned()
    };
    if safe.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", safe.replace('"', "\"\""))
    } else {
        safe
    }
}

fn export_bytes(verified: &Value, format: ExportFormat) -> Result<Vec<u8>, String> {
    let result = &verified["result"];
    let report = &result["report"];
    match format {
        ExportFormat::Json => {
            let mut bytes = serde_json::to_vec_pretty(&json!({
                "contract_version": "finintel-backtest-export-v1",
                "research_only": true,
                "hypothetical_transactions": true,
                "source_provider": "fred",
                "original_quality_policy": "require_complete",
                "price_index_returns_exclude_dividends": true,
                "benchmark_excludes_transaction_costs": true,
                "job": verified,
            }))
            .map_err(|_| "invalid_response")?;
            bytes.push(b'\n');
            Ok(bytes)
        }
        ExportFormat::Equity | ExportFormat::Transactions => {
            let (key, columns) = if matches!(format, ExportFormat::Equity) {
                ("rows", EQUITY)
            } else {
                ("transactions", TRANSACTIONS)
            };
            let mut csv = columns.join(",");
            if key == "transactions" {
                csv.push_str(",hypothetical");
            }
            csv.push_str("\r\n");
            for row in report[key].as_array().ok_or("invalid_response")? {
                for (index, column) in columns.iter().enumerate() {
                    if index != 0 {
                        csv.push(',');
                    }
                    csv.push_str(&csv_cell(row[column].as_str().ok_or("invalid_response")?));
                }
                if key == "transactions" {
                    csv.push_str(",true");
                }
                csv.push_str("\r\n");
            }
            Ok(csv.into_bytes())
        }
    }
}

fn write_export(path: &Path, extension: &str, bytes: &[u8]) -> Result<(), &'static str> {
    write_export_with(path, extension, |file| file.write_all(bytes))
}

#[cfg(windows)]
fn windows_drive_root(path: &Path) -> Option<[u16; 4]> {
    use std::path::{Component, Prefix};
    let Some(Component::Prefix(prefix)) = path.components().next() else {
        return None;
    };
    match prefix.kind() {
        Prefix::Disk(drive) | Prefix::VerbatimDisk(drive) => {
            Some([u16::from(drive), b':' as u16, b'\\' as u16, 0])
        }
        _ => None,
    }
}

#[cfg(windows)]
fn is_local_windows_drive(path: &Path) -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetDriveTypeW(root: *const u16) -> u32;
    }

    let Some(root) = windows_drive_root(path) else {
        return false;
    };
    // SAFETY: the drive root is a null-terminated UTF-16 string. The OS only reads it.
    // Allow removable, fixed, and RAM drives. Mapped shares are DRIVE_REMOTE (4).
    matches!(unsafe { GetDriveTypeW(root.as_ptr()) }, 2 | 3 | 6)
}

fn write_export_with(
    path: &Path,
    extension: &str,
    write: impl FnOnce(&mut fs::File) -> io::Result<()>,
) -> Result<(), &'static str> {
    if !path.is_absolute()
        || !path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case(extension))
    {
        return Err("invalid_destination");
    }
    #[cfg(windows)]
    if !is_local_windows_drive(path) {
        return Err("invalid_destination");
    }
    let parent = path
        .parent()
        .filter(|dir| dir.is_dir())
        .ok_or("invalid_destination")?;
    #[cfg(windows)]
    if !parent
        .canonicalize()
        .is_ok_and(|resolved| is_local_windows_drive(&resolved))
    {
        return Err("invalid_destination");
    }
    match fs::symlink_metadata(path) {
        Ok(metadata) if !metadata.is_file() => return Err("invalid_destination"),
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) => return Err("export_write_failed"),
    }
    // Stage next to the target, so persistence is a same-filesystem atomic replacement.
    // NamedTempFile owns cleanup on failure and creates private files on Unix.
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|_| "export_write_failed")?;
    write(temp.as_file_mut()).map_err(|_| "export_write_failed")?;
    temp.as_file()
        .sync_all()
        .map_err(|_| "export_write_failed")?;
    temp.persist(path).map_err(|_| "export_write_failed")?;
    Ok(())
}

#[tauri::command]
pub async fn backtest_export(
    app: AppHandle,
    job_id: String,
    request_sha256: String,
    output_sha256: String,
    format: ExportFormat,
    locale: ExportLocale,
) -> Result<&'static str, String> {
    if !super::valid_model_job_hash(&request_sha256) || !super::valid_model_job_hash(&output_sha256)
    {
        return Err("invalid_input".into());
    }
    if EXPORTING
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("export_busy".into());
    }
    let permit = ExportPermit;
    let verified = backtest_jobs::backtest_jobs_get(job_id, true).await?;
    let bytes = verified_bytes(&verified, &request_sha256, &output_sha256, format)?;

    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        let selected = app
            .dialog()
            .file()
            .set_title(locale.title())
            .set_file_name(format.filename())
            .add_filter(format.extension().to_uppercase(), &[format.extension()])
            .blocking_save_file();
        let selected = selected
            .map(|path| path.into_path().map_err(|_| "invalid_destination"))
            .transpose()?;
        save_selected(selected.as_deref(), format, &bytes)
    })
    .await
    .map_err(|_| "export_write_failed")?
}

fn save_selected(
    path: Option<&Path>,
    format: ExportFormat,
    bytes: &[u8],
) -> Result<&'static str, String> {
    let Some(path) = path else {
        return Ok("cancelled");
    };
    write_export(path, format.extension(), bytes)?;
    Ok("saved")
}

fn verified_bytes(
    verified: &Value,
    request: &str,
    output: &str,
    format: ExportFormat,
) -> Result<Vec<u8>, String> {
    if verified["state"] != "completed"
        || verified["request_sha256"] != request
        || verified["output_sha256"] != output
    {
        return Err("invalid_response".into());
    }
    backtest_jobs::summary(verified, None, true)?;
    export_bytes(verified, format)
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

    #[cfg(windows)]
    #[test]
    fn windows_rejects_unc_and_device_namespaces_without_a_filesystem_probe() {
        for path in [
            r"\\server\share\report.json",
            r"\\?\UNC\server\share\report.json",
            r"\\.\HarddiskVolume1\report.json",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\report.json",
        ] {
            assert!(windows_drive_root(Path::new(path)).is_none());
            assert_eq!(
                write_export(Path::new(path), "json", b"report"),
                Err("invalid_destination")
            );
        }
        assert_eq!(
            windows_drive_root(Path::new(r"\\?\C:\report.json")),
            Some([67, 58, 92, 0])
        );
    }

    #[test]
    fn exports_verified_job_and_rejects_modified_hashes_or_report() {
        let mut job = fixture();
        let request = job["request_sha256"].as_str().unwrap().to_owned();
        let output = job["output_sha256"].as_str().unwrap().to_owned();
        let bytes = verified_bytes(&job, &request, &output, ExportFormat::Json).unwrap();
        let exported: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(exported["contract_version"], "finintel-backtest-export-v1");
        assert_eq!(exported["research_only"], true);
        assert_eq!(exported["job"], job);
        assert!(verified_bytes(&job, &output, &request, ExportFormat::Json).is_err());
        job["result"]["report"]["rows"][0]["equity_after_cost"] = json!("10");
        assert!(verified_bytes(&job, &request, &output, ExportFormat::Json).is_err());
        job["state"] = json!("running");
        assert!(verified_bytes(&job, &request, &output, ExportFormat::Equity).is_err());
    }

    #[test]
    fn csv_is_deterministic_and_quotes_untrusted_cells() {
        let job = fixture();
        for (format, key, columns) in [
            (ExportFormat::Equity, "rows", EQUITY),
            (ExportFormat::Transactions, "transactions", TRANSACTIONS),
        ] {
            let actual = String::from_utf8(export_bytes(&job, format).unwrap()).unwrap();
            let suffix = if key == "transactions" {
                ",hypothetical"
            } else {
                ""
            };
            let mut lines = actual.split_terminator("\r\n");
            assert_eq!(
                lines.next().unwrap(),
                format!("{}{suffix}", columns.join(","))
            );
            for row in job["result"]["report"][key].as_array().unwrap() {
                let expected = columns
                    .iter()
                    .map(|key| row[key].as_str().unwrap())
                    .collect::<Vec<_>>()
                    .join(",");
                assert_eq!(
                    lines.next().unwrap(),
                    if suffix.is_empty() {
                        expected
                    } else {
                        format!("{expected},true")
                    }
                );
            }
            assert!(lines.next().is_none());
        }
        assert_eq!(csv_cell("-0.03120"), "-0.03120");
        assert_eq!(csv_cell("  =1+1"), "'  =1+1");
        assert_eq!(csv_cell("@SUM(1,2)"), "\"'@SUM(1,2)\"");
        assert_eq!(csv_cell("\tA"), "'\tA");
        assert_eq!(csv_cell("hi\"\nthere"), "\"hi\"\"\nthere\"");
    }

    #[test]
    fn cancellation_and_invalid_destinations_do_not_write() {
        let temp = tempfile::tempdir().unwrap();
        assert_eq!(
            save_selected(None, ExportFormat::Json, b"data").unwrap(),
            "cancelled"
        );
        assert_eq!(
            write_export(Path::new("relative.json"), "json", b"data"),
            Err("invalid_destination")
        );
        assert_eq!(
            write_export(&temp.path().join("data.csv"), "json", b"data"),
            Err("invalid_destination")
        );
        assert_eq!(
            write_export(
                &temp.path().join("missing").join("data.json"),
                "json",
                b"data"
            ),
            Err("invalid_destination")
        );
        assert_eq!(fs::read_dir(temp.path()).unwrap().count(), 0);
    }

    #[test]
    fn atomic_write_keeps_old_file_and_cleans_up_on_failure() {
        let temp = tempfile::tempdir().unwrap();
        let destination = temp.path().join("report.json");
        write_export(&destination, "json", b"original").unwrap();
        for kind in [io::ErrorKind::PermissionDenied, io::ErrorKind::WriteZero] {
            let failure = write_export_with(&destination, "json", |staged| {
                staged.write_all(b"sensitive partial data")?;
                Err(io::Error::from(kind))
            });
            assert_eq!(failure, Err("export_write_failed"));
            assert_eq!(fs::read(&destination).unwrap(), b"original");
            assert_eq!(fs::read_dir(temp.path()).unwrap().count(), 1);
        }
        write_export(&destination, "json", b"replacement").unwrap();
        assert_eq!(fs::read(&destination).unwrap(), b"replacement");
        assert_eq!(fs::read_dir(temp.path()).unwrap().count(), 1);
    }
}
