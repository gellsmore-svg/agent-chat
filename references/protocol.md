# Agent Chat filesystem protocol v1

A session has two immutable logical identities. Both workers invoke one installed `agent-chat` package and use the same OS runtime on one machine. The session directory lives outside the implementation checkout on a local filesystem with working OS file locks and atomic rename. WSL and native Windows are separate runtimes. The session directory and executable configuration are trusted local resources. Counterpart text and project excerpts are untrusted data.

Both workers require an exact match against the integer wire protocol version in `session.json` before backend discovery or registration. Package releases can share a wire version. New sessions bind to a hash of the host identifier and runtime platform. Older v1 sessions use any recorded registration hostnames for the local check. These checks prevent accidental mixed-runtime use. Operators must choose a local filesystem even where the OS cannot identify a remote mount.

## Authoritative state

- `session.json` is published last during initialization and is immutable thereafter. It contains configuration, two identities, and an optional snapshot of individually selected project files.
- `turns/NNNNNN.json` is an append-only journal. Each record includes its expected predecessor's SHA-256 and its own SHA-256 of canonical UTF-8 JSON with sorted keys, compact separators and the checksum field removed. Sequence and author follow strict alternation from the first speaker. The checksums detect accidental damage. Access control belongs to the OS account and directory.
- `control.json` is an atomic, checksummed lifecycle snapshot. It records registration, deadline, terminal result and the in-flight invocation, if any. `conversation.md` is a deterministic projection of the journal and lifecycle state.
- `leases/<A-or-B>.json` holds worker identity, process and heartbeat. A worker also holds an exclusive OS lock on a fixed participant lockfile for its entire lifetime. PID and timestamps are advisory. The OS lock independently proves exclusive ownership and releases on process death.
- `write.lock` is a permanent OS lockfile. Its inode must never be removed or replaced. `lock-owner.json` records the most recent lock owner and may survive its death. A free OS lock is authoritative even if this record remains.
- `diagnostics/` contains bounded, classified errors with numeric execution metadata. Raw subprocess output is held transiently and is never logged to the session.

## Transactions and recovery

Writers acquire the bounded commit lock, read and validate the journal and control state, and make decisions inside the transaction. A turn commit requires the correct worker instance and an exact sequence, predecessor and invocation ID. Files are flushed and atomically replaced in their own directory. The journal is published before control and transcript. Model and CLI work always happens outside the commit lock.

Recovery reconciles a journal entry whose invocation marker survived, derives mutual completion and turn exhaustion from the journal, and rebuilds the transcript. It reports incomplete initialization, corrupt control, unexpected journal files and broken checksums or invariants. It never removes committed history. A restart between invocations can rejoin within the worker grace period. An abandoned in-flight invocation fails the session because the old child might still be running. It cannot publish a contribution or be retried implicitly in that session. An interrupted initializing directory without a published session must be preserved for inspection or replaced with a new directory.

The active clock starts at the second registration. Before that, the join clock starts at first registration. The earlier of the active deadline and the invocation timeout bounds each call. Manual stop, expiry and worker death are observed by live workers while calls are pending. A malformed response gets one bounded retry. Model timeout, backend error and a second malformed or repeated response fail the session. An exact normalized repeat by the same author is rejected. A finish acceptance requires the other participant's immediately preceding finish proposal.

Successful mutual completion acknowledges the proposal and the acceptance messages. Other endings retain the last contribution and make no joint claim. Status, stop and recover reconcile interrupted publication without model calls. Export computes the same reconciliation in memory and leaves the canonical transcript unchanged.
