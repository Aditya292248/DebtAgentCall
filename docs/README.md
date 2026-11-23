# Documentation

Plain-language documentation for DebtAgentCall. Nothing here is legal advice, and none of it has been reviewed by counsel.

| Page | Read it to |
| --- | --- |
| [architecture.md](architecture.md) | See what the components are, how one call goes from the operator's request to the summary, what each table holds and what writes it, and where each kind of memory is stored. Has a component diagram and a call-flow diagram. |
| [compliance.md](compliance.md) | See every rule the code enforces: its source, its rule id in the audit log, where it is enforced, the file and function, the test that proves it, and its limits. Also lists what depends on unconfirmed AgentPhone behavior, what is not handled in this version, what needs a lawyer, and what the owner-only test-number list can and cannot skip. |
| [demo.md](demo.md) | Run a 6 to 7 minute founders' demo with exact commands, what the audience should see at each step, and answers to the questions people ask. |
| [design/event-log.md](design/event-log.md) | Read a proposal, for the owner to decide, to keep an append-only event log as the single source of truth: what is wrong with the current store (checked against the code), the event table and types, how everything is rebuilt from it, a step-by-step migration, and a straight answer on whether it is worth doing now. |

For setup, the full command list and the feature-by-feature reference, see the project's [README](../README.md). To contribute, see [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
