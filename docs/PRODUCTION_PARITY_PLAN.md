# DF-VPN-Bot: Production Parity Plan

Target branch: `feature/production-parity`

This program extends the standalone DF-VPN-Bot without changing the active YadrenoVPN production bot. Changes are delivered in independently testable stages. Existing installations must remain upgradeable and the default single-panel configuration must remain compatible.

## Non-negotiable safety rules

- Never commit secrets, production databases, client configs, private keys, payment tokens, or panel credentials.
- Every database migration is additive, idempotent, and backed by a dry-run/backup path.
- Payment confirmation and fulfillment are separate idempotent states.
- A provider/network error must never be interpreted as payment cancellation.
- Production is not modified by this branch.
- New features remain disabled unless configured.

## Stages

### P0 — Foundation and regression baseline

- [x] Create isolated branch.
- [x] Confirm current self-test baseline: 63 passed, 0 failed.
- [x] Add CI for compile, self-test, integration test, and secret scanning.
- [ ] Add schema migration journal and migration runner.
- [ ] Split health checks from Telegram handlers.

### P1 — Reliable payment lifecycle

- [ ] Add immutable payment intents and provider-order snapshots.
- [ ] Separate `provider_status` and `fulfillment_status`.
- [ ] Add atomic/idempotent fulfillment claims.
- [ ] Add retry queue with bounded backoff.
- [ ] Treat transport/API failures as retryable, never canceled.
- [ ] Add admin recovery for confirmed-but-unfulfilled orders.
- [ ] Add user/admin notifications and audit log.
- [ ] Cover YooMoney, YooKassa, Stars, balance, and manual payments.

### P2 — Multi-server and routing

- [ ] Add `servers`, `server_groups`, and subscription binding tables.
- [ ] Support multiple 3x-ui and Marzban servers simultaneously.
- [ ] Route by tariff/group/capacity/health.
- [ ] Add server health, retry, circuit breaking, and admin status.
- [ ] Preserve legacy `VPN_PANEL` single-server mode.

### P3 — AmneziaWG

- [ ] Add Amnezia server adapter.
- [ ] Generate peers/configs without shell interpolation.
- [ ] Store peer ownership, expiry, config path, and lifecycle state.
- [ ] Show clearly marked Amnezia keys in user and admin key lists.
- [ ] Download, renew, revoke, and permanently delete with confirmation.
- [ ] Add expiry worker and ownership checks.
- [ ] Add MTU configuration and health diagnostics.

### P4 — HTTPS subscriptions and monitoring

- [ ] Add configurable HTTPS subscription base URL per server.
- [ ] Validate scheme, certificate, status, payload, and node count.
- [ ] Add watchdog command and scheduled alerts.
- [ ] Keep legacy links during migration.
- [ ] Add access-log-compatible usage reporting without storing secrets.

### P5 — Admin and messaging parity

- [ ] Rich user card with standard and Amnezia keys.
- [ ] Filtered broadcasts with preview, confirmation, stop, and report.
- [ ] Delivery/block status tracking and deduplication.
- [ ] Editable user-facing text/pages with safe placeholders.
- [ ] Manual server reconciliation and dry-run reports.
- [ ] Detailed payment/key lifecycle audit views.

### P6 — Backup and disaster recovery

- [ ] Atomic SQLite backup and integrity check.
- [ ] Restore drill to a temporary database.
- [ ] Retention and checksums.
- [ ] Optional remote rsync/SFTP target with remote checksum validation.
- [ ] Scheduler, status command, and failure alerts.

### P7 — Migration tooling

- [ ] Import users, tariffs, balances, promos, keys, payments, and Amnezia records from supported exports.
- [ ] Dry-run only by default.
- [ ] Mapping/conflict report and deterministic idempotency keys.
- [ ] No migration from the live production DB in the current project scope; tooling targets explicit export files only.

### P8 — Release gate

- [ ] Full unit/integration suite.
- [ ] Fresh install test and upgrade test.
- [ ] Failure injection for panel, Telegram, and payment APIs.
- [ ] Security review and secret scan.
- [ ] Documentation and rollback guide.
- [ ] Merge only after review; no automatic production deployment.

## Acceptance criteria

The project is complete only when all enabled features have tests, a documented configuration, a migration/rollback path, and no regression in the legacy single-server setup.
