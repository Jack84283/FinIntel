# AL-09 acceptance matrix

Baseline: main at `dfbc4d0` (#86), with #73–#85 in its history. Status applies to the automated check unless the row explicitly describes a manual gate. Local evidence is summarized in the [acceptance report](AL-09_ACCEPTANCE_REPORT.md). CI lists the job responsible for independent hosted evidence.

| ID | Component | Automated coverage and evidence | CI | Manual verification | Status | Gap or blocker |
| --- | --- | --- | --- | --- | --- | --- |
| REG-01 | Registry and approvals | Analysis model contracts and worker approval parity | Python | None | PASS | Covered by existing tests |
| TM-01 | EMA, Momentum, Relative Strength, RSI | Model contracts and real worker jobs | Python/native | Inspect result UI | PASS | 2,000 shared sessions exercised with four sources |
| RISK-01 | Volatility, drawdown, VaR/ES, correlation | Risk contracts and worker results | Python/native | Inspect metric units | PASS | Complete logical market digest unchanged |
| REGIME-01 | Trend and volatility classifications | Market Regime contracts | Python/native | Inspect unknown states | PASS | 2,000 sessions, default calibration verified |
| SRC-01 | Identity, pins, dates, quality | Managed analysis and SMA worker tests | Python/native | None | PASS | Archived and corrupted results covered |
| JOB-01 | Queue, cancellation, retry, restart | Job ledger and worker tests | Python/native | Installed restart and process cleanup | PASS | Installed confirmation |
| BT-01 | Eligibility, timing, assumptions, report hashes | Backtest contract golden fixture and durable worker | Python/native | Run through installed UI | PASS | Isolated SMA and backtest; frozen golden fixture verified |
| UI-01 | Charts, tables and provenance | Mounted Analysis Library and backtest tests | React | Keyboard, narrow layout, themes | PASS | Covered by existing tests |
| EXP-01 | JSON/CSV, atomic write, filesystem policy | Native backtest export tests and mounted result interactions | Native/React | Native dialog and live mounts | PASS | Live mounts require owner environment |
| DATA-01 | Observations, audit metadata and schema | Worker and storage regressions | Python | Private-data gate | PASS | Schema version and logical database digest preserved |
| DATA-02 | Backup, recovery and rollback | Database backup and migration tests | Python | Backup/recovery rehearsal | PASS | Ledger unchanged; abrupt process termination NOT_RUN |
| DESK-01 | Installed app, sidecar and lifecycle | Sidecar smoke and packaging | Native | Install and execute the checklist | NOT_RUN | Packaging alone is insufficient |
| SEC-01 | Managed IPC, offline workers and limits | Managed worker, transport and native command suites | Python/native | Disconnect external networking during installed run | PASS | Installed offline run |
| L10N-01 | English and Simplified Chinese | Mounted frontend regressions | React | Switch languages in installed UI | PASS | Covered by existing tests |
| PERF-01 | Bounded sessions, queues and rendering | Worker limits, contract and mounted frontend tests | Python/native/React | Responsiveness and resource measurements | PASS | 2,000-session sources exercised; installed memory and UI timing NOT_RUN |
| EXP-02 | Live local and remote filesystem save | Classifier tests cover policy inputs | None | Native save on local, SMB/NFS and platform-specific mounts | NOT_RUN | Requires disposable mounts on each platform |
| SEC-02 | Installed offline execution | Synthetic child workflows and packaged sidecar startup | None | Disconnect external networking; keep loopback | NOT_RUN | Requires installed walkthrough |
| DATA-03 | Private preservation and recovery | Synthetic backup and ledger isolation | None | Owner review and separate recovery rehearsal | NOT_RUN | Requires owner-controlled histories and backups |
