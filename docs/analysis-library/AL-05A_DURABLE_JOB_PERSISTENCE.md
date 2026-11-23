# AL-05A · Versioned durable model job persistence

## Migration design

The job ledger has a separate SQLite file and schema version. Version 2 adds an explicit request format discriminator and permits the six initial Analysis Library model identities alongside the legacy SMA identity. The legacy format remains unchanged. A legacy-format row continues to hold the original request and result JSON, hashes, state, attempt generation, and timestamps.

SQLite cannot widen a table-level constraint in place. On the first write, the ledger takes a write reservation, reads the schema version under that reservation, and builds the replacement table in the same transaction. It copies every existing column, marks each copied row as legacy, and compares row counts and every copied field by job ID before replacing the old table. It then reinstates the existing indexes and updates the version within that transaction. Failure rolls back the reconstruction. The next write can retry. Reads still work against version 1 before migration and never create or upgrade a ledger. Unknown schema versions fail closed. Migration leaves the ledger file and its permissions in place.

## Request and result integrity

Analysis Library persistence accepts only the six version 1.0.0 identities listed for AL-05A and checks the exact registered definition. The normalized request includes its format version, resolved parameters, quality policy, requested dates, and ordered source identities and snapshot pins. Its SHA-256 covers the canonical JSON, independent of object-key ordering. The stored request format selects the parser, and both formats keep their existing result schema.

Before an analysis result is written, the ledger reparses the complete result, verifies its hashes and size, and binds its schema, lineage, model revision, and values to the stored request and registered contract. Writes require a matching running attempt with no pending cancellation. A completed row is immutable through the ledger lifecycle. A private digest secures the complete stored analysis envelope, including the UTC evaluation timestamp. Public reproducibility and output hashes retain their AL-01 meaning. Reads repeat the integrity and binding checks against stored evidence and never open the market-data database. Source revisions and current quality state do not change the meaning of historical evidence. The existing summary digest is populated for a single source. Multi-source analysis retains individual pins in the request and result evidence instead of inventing a combined snapshot digest.

## Execution and rollout

Production queue creation, claim, worker, HTTP, and native commands continue to use the legacy SMA contract. Analysis result storage can be exercised with internal synthetic rows. Discovery, storage acceptance, and execution approval are separate. AL-05B supplies the worker dispatch and AL-05C supplies the new managed API. Keep prior registry definitions available by version so previously stored results can still be verified. These later integrations must recheck current source evidence before execution and persistence. Existing queue, observation, result, attempt, and worker ceilings still apply.
