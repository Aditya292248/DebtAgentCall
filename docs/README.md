# Documentation

Plain-language documentation for DebtAgentCall. Nothing here is legal advice, and none of it has been reviewed by counsel.

| Page | Read it to |
| --- | --- |
| [architecture.md](architecture.md) | See what the components are, how one call goes from the operator's request to the summary, what each table holds and what writes it, and where each kind of memory is stored. Has a component diagram and a call-flow diagram. |
| [compliance.md](compliance.md) | See every rule the code enforces: its source, its rule id in the audit log, where it is enforced, the file and function, the test that proves it, and its limits. Also lists what depends on unconfirmed AgentPhone behavior, what is not handled in this version, what needs a lawyer, and what the owner-only test-number list can and cannot skip. |
| [demo.md](demo.md) | Run a 6 to 7 minute founders' demo with exact commands, what the audience should see at each step, and answers to the questions people ask. |
| [reference.md](reference.md) | Look up one feature: model settings, the agent loop and tool outcomes, the database and sample people, each rule, the audit log, the webhook, sessions, the web UI, the dialer, and the full command list. |
| [adding-people-by-chat.md](adding-people-by-chat.md) | See how the operator chat adds a person, with example conversations. |
| [latency.md](latency.md) | See what each live-call turn's timing measures and how to read the report. |

For setup and the local demo, see the project's [README](../README.md). To contribute, see [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
