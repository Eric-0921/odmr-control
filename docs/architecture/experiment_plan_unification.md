# Experiment Plan Unification

This document defines the boundary for the experiment-plan unification work.

## Core Lifecycle

Formal experiment configuration has one source of truth:

```text
ExperimentPlanDraft -> CompiledPlan -> Run
```

- `ExperimentPlanDraft` is mutable GUI/application state.
- `CompiledPlan` is schema-compatible JSON plus compile diagnostics and a stable hash.
- `Run` is an immutable execution attempt with a run id, a frozen compiled plan, artifacts, and event history.

Editing a draft after a run starts must not change the running plan.

## Page Responsibilities

- `Station` owns connection, identity, bindings, and health checks.
- `Device Workbench` owns manual device set/query/diagnostic operations.
- `Run Designer` owns formal experiment draft editing and compilation.
- `Run Console` owns loading, validation, preflight, and lifecycle control.
- `Live Data` observes current run/device streams only.
- `Review / Log` owns run artifacts, logs, manifests, and exports.

Device workbench pages do not maintain formal experiment truth. Manual diagnostic actions must not mutate the draft unless the user invokes an explicit copy-to-draft action.

## Schema Compatibility

The first implementation phase emits schema-v1-compatible plans. Global recording defaults live in the draft and may be mirrored under `metadata.recording_defaults`; the compiler must not emit a top-level `recording` field while targeting schema v1.

Future schema v2 work may add top-level `station`, `recording`, `safety`, and `procedure` sections.

## Runtime Boundary

`CommandService` remains the single command entrypoint. Experiment-specific compile, preflight, resource lease, safety policy, manifest, and runtime concerns should live in focused helper modules rather than growing `CommandService` indefinitely.

Formal runs follow this lifecycle:

```text
compile draft
validate compiled plan
preflight
load plan
start run
pause/resume/stop/query
write artifacts
```

## Manual Override

When a run owns a device, dangerous manual operations should be disabled or require an explicit override. Overrides must be recorded in run events so the device state and recorded plan do not silently diverge.
