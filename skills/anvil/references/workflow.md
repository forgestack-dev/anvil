# Intended execution workflow

These are implementation requirements for later versions. Check the installed CLI before using any operational capability.

A supervisor owns durable task state, worker limits, and claims. Workers receive isolated workspaces and attempt IDs. A single integration lane verifies combined changes before releasing dependents. Store messages, decisions, evidence, and candidate revisions outside conversational memory.

Expose the complete pinned AI Hero catalog through explicit compatibility rules. Load only selected skills and their dependencies. Distinguish automatic skills from human-facing or explicitly invoked workflows, and report missing runtime tools. Do not assume desktop connectors are available inside a Codex subprocess.

Workers may propose completion but cannot record authoritative success. Completion requires acceptance evidence, review of the exact candidate, and successful checks after integration. A wide-refactor group remains staged until its final verification succeeds.

An empty ready queue can mean in-flight work, blocked dependencies, an unresolved decision, or failure. It is not by itself success. Retries, process count, and run duration must be bounded. Interrupted work must remain recoverable, and late results from expired attempts must not be accepted.
