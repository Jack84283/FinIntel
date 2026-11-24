# AL-08B · Verified backtest visualization and local export

Open a completed backtest in Model Jobs, then choose **View result**. FinIntel verifies the retained request, result and hashes before displaying the dashboard. Historical inspection uses the saved evidence. Current data quality and sources still determine whether a new run can be queued.

## Presentation

- Performance shares the original report metrics. Returns, drawdown, volatility, relative performance and turnover display as percentages. Sharpe is a dimensionless ratio, and an unavailable Sharpe stays unavailable. Equity and costs are expressed in modeled initial-capital units.
- Strategy equity after costs and frictionless benchmark equity share the same observed-session axis. Drawdown, modeled allocation and costs have separate axes. Allocation changes at the next observed close, shown as a step with markers on hypothetical fill sessions from the stored transaction history. The charts plot representable values from the full backtest decimal format. Values outside the safe plotting range leave a gap with a visible notice, and the tables retain their exact values.
- The keyboard session inspector reads the exact stored values. Both tables paginate ten records at a time. Transactions remain chronological and can be filtered by date and direction. The worst drawdown date is identified by exact equality with the saved maximum drawdown, without recalculating the equity curve or drawdown.
- Evidence includes source identity, assumptions, the original quality rule, and copyable request, input, output, snapshot and lineage hashes. Price-index returns exclude dividends. Frictionless benchmark returns exclude transaction costs.

## Export

Choose verified JSON, equity CSV or hypothetical-transaction CSV, then acknowledge the plaintext warning and choose a destination in the native save dialog. Cancellation leaves the filesystem untouched.

The desktop command accepts only the format, UI locale, job ID and expected request/output SHA-256 values. It retrieves the completed ledger result through the managed AL-07 transport and validates all identities and hashes again. Webview paths and serialized report content never cross the write boundary. The dialog runs from Rust and requires no webview filesystem or dialog capability.

JSON uses the `finintel-backtest-export-v1` wrapper and retains the complete verified job and its unchanged result envelope. CSV uses stable column order and line endings, preserves canonical decimals, and guards formula-like text. Transaction rows explicitly carry a `hypothetical` flag. A private temporary file is written and synced next to the selected destination, then atomically persisted on supported local filesystems. The save command rejects Windows UNC, device and mapped remote drives. macOS requires the destination volume to carry the local-mount flag. Linux allows known local filesystem types and rejects network, unknown and ambiguous types such as FUSE. A separate sync application may still sync a file saved on a local filesystem. Error cleanup removes the temporary file and retains any prior destination content.

Exported reports are unencrypted and intended for review. FinIntel has no automatic export, upload or report import through this feature. Research data remains hypothetical, and these reports do not represent actual fills or at-market performance.

## Validation

Synthetic hosted checks cover hash validation, cancellation, fail-closed writes, deterministic CSV, exact dashboard values, observed-session limits, localized presentation and stale-result behavior. Installed-desktop verification with private historical data remains a separate owner acceptance check.
