# Agent Chat IOMR proposal

The intent agreed in the product review is to help two reviewers reach a traceable technical conclusion from shared evidence. The SCL proposal models two outcomes that support that intent. Codex implemented the changes and Grok was assigned independent verification.

## Recover a failed contribution

A correctable response failure gives the backend enough information to try again before a turn is committed. The participants in this interaction are the runner and the CLI backend. Empty, non-text and oversized replies must produce distinct diagnostics. For a size failure, the diagnostic and retry prompt carry the measured character count and the cap. A corrected retry commits one contribution. A second failure terminates the session. Exact normalized repeats keep the same one-retry limit. [Issue #1](https://github.com/gellsmore-svg/agent-chat/issues/1)

## Share citable evidence

An operator can select exact inclusive line ranges from a larger UTF-8 project document. The runner snapshots the text and gives both participants the original path and line numbers. Acceptance checks compare the selected bytes and provenance, exercise the existing whole-file input, and verify rejection of out-of-file, conflicting, and over-budget selections. The provisional scan limit permits up to 1 MiB before the first selected line. [Issue #2](https://github.com/gellsmore-svg/agent-chat/issues/2)

## Product constraints

Discussion uses two participants, bounded turns and time, read-only backends, and explicitly selected evidence. Retry feedback excludes rejected text. Range selection uses the existing path checks and snapshot and prompt budgets. A saved protocol v1 session can load without source provenance.

## Model status

SCL’s `iomr-evolved` 2.0.0 metamodel connects Requirements to Models, Models to measurable Outcomes, and Outcomes to the Intent. The SCL service stores the candidate under `.scl/proposals/`. Use `scl --root . api validate_proposal` with its proposal ID to check the graph. Integration into the canonical model requires a trusted human signature. The application has no SCL runtime dependency.
