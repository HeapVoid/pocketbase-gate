# PocketBase Gate

Follow Fedor's preferences in `~/.codex/AGENTS.md` when available.

- This package owns planning, proof validation, resource admission, processes and fixture cleanup. Consumers declare checks and fixture requirements.
- Keep project names, application routes, business schema and deployment policy out of the engine. Preserve conservative dependency coverage when a complete graph is unavailable.
- Evolve the extracted resource/content owners; avoid competing caches, schedulers or process supervisors.
- Verify concrete risks with the relevant planner, execution, client or native fixture tests. Use disposable databases only.
- Commands run as argv, never through a shell. Disposable credentials belong in private runtime context or stopped-fixture metadata, never reports or verification receipts.
- Publishing is authorized through version tags: pushing `v<package.json version>` runs the verification workflow and publishes through npm Trusted Publishing. Branch pushes run checks only.
