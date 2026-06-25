# Architecture

## Runtime Lines

```mermaid
flowchart LR
  subgraph DTT["data_train_test"]
    DB["Board injectors"]
    DH["Host HDC collectors"]
    DS["Server dataset and training tools"]
    DB --> DH --> DS
  end
  subgraph DEMO["closed_loop_demo"]
    CB["Board collector/action runner"]
    CS["Server ingest/infer/actions"]
    CH["Host orchestration"]
    CH --> CB
    CB --> CS
    CS --> CB
  end
  DS -->|"exported model or adapter path"| CS
  DTT --> SH["shared/protocol"]
  DEMO --> SH
```

## Data/train/test Flow

```mermaid
flowchart LR
  A["Board net_fault.sh"] --> B["Host collector"]
  B --> C["inbox/runs run folder"]
  C --> D["NET validator"]
  D --> E["L1 canonical case"]
  E --> F["L2 task samples"]
  F --> G["SFT/eval dataset"]
  G --> H["model artifact"]
  H --> I["closed-loop demo config"]
```

## Closed-loop Demo Flow

```mermaid
sequenceDiagram
  participant Host
  participant Board
  participant Server
  Host->>Server: start ingest/actions/watcher
  Host->>Board: deploy demo scripts
  Board->>Server: upload bundle on trigger
  Server->>Server: ingest and run inference
  Server->>Board: publish actions
  Board->>Board: execute allowlisted action
  Board->>Server: upload action_result bundle
```

## Boundaries

- Board code must remain BusyBox/Toybox compatible and must not require `awk` or
  `tr`.
- Host code owns Windows PowerShell, HDC target resolution, and SSH/SCP
  orchestration.
- Server code owns Python ingest, inference orchestration, watcher loops, and
  model/runtime paths.
- `shared/` is only for stable protocols and common schemas used by both lines.
