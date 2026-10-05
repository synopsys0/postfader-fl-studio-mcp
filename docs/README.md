# PostFader documentation

PostFader is an FL Studio MCP server: it lets Claude, Codex, Cursor, and other
MCP clients read and edit the FL Studio project you have open. Start with the
[README](../README.md) for the overview; these pages go deeper.

## Start here

| Page | Read it when |
| --- | --- |
| [Setup and troubleshooting](setup.md) | You're installing, connecting a client, upgrading, or something isn't connecting |
| [FAQ](faq.md) | You want a quick answer about requirements, safety, or a common problem |
| [Tool reference](tools.md) | You want to see every tool and resource, grouped by task |
| [Comparison](comparison.md) | You're choosing between FL Studio MCP servers |
| [V12 release notes](releases/v12.0.0.md) | You're upgrading from V11; every tool has a new name |

## Workflows

| Page | What it covers |
| --- | --- |
| [Production Runs](production-runs.md) | Multi-step jobs: planning, running, resuming, and stopping |
| [Creation pipeline](creation-pipeline.md) | Readiness checks, sound-aware composition, and processing inside a run |
| [Creation Review](creation-review.md) | Reviewing a draft bounce, planning one revision, comparing, and delivery |
| [Sound Selection](sound-selection.md) | Choosing presets from the instruments already loaded |
| [Plug-in support](plugin-support.md) | Reading and setting plug-in parameters, presets, and compatibility reports |
| [Plugin Atlas](plugin-atlas.md) | Offline knowledge about Image-Line and selected third-party plug-ins |

## Reference

| Page | What it covers |
| --- | --- |
| [Tool contracts](tool-contracts.md) | Exact arguments, results, refusals, and evidence for every tool |
| [FL Studio limits](fl-constraints.md) | What FL Studio's scripting API allows, and where PostFader stops |
| [Architecture](architecture.md) | Components, the MIDI transport, the bridge, and trust boundaries |
| [Plug-in matrix](plugin-matrix.md) | Compatibility evidence levels and validated plug-in reports |
| [Security policy](../SECURITY.md) | Threat model, privacy, and reporting vulnerabilities |

## Contributing and maintaining

| Page | What it covers |
| --- | --- |
| [Contributing](../CONTRIBUTING.md) | Development setup, tests, and the public-content rules |
| [Releasing](../RELEASING.md) | How a version is built, verified, and published |
| [Code quality](code-quality.md) | Lint, type, and coverage gates |
| [Supply chain](supply-chain.md) | Dependencies, SBOM, and build provenance |
| [Distribution and listings](distribution.md) | Canonical descriptions for package indexes and MCP directories |
| [Discussions](discussions.md) | How GitHub Discussions are organized |
| [Early access testing](early-access-testing.md) | A privacy-safe first-session checklist for testers |
| [Tool-surface evaluation](tool-surface-evaluation.md) | Reporting how AI clients pick and use tools |
| [External review scope](external-review-scope.md) | What an outside security review should cover |
| [Security hardening options](future-security-hardening-options.md) | Hardening ideas that are not implemented yet |
| [Maintainability plan](maintainability-plan.md) | Planned internal refactoring |
| [Website copy](website-copy.md) | Product copy for a future website |

## Release notes

- [V12.0.3](releases/v12.0.3.md)
- [V12.0.2](releases/v12.0.2.md)
- [V12.0.1](releases/v12.0.1.md)
- [V12.0.0](releases/v12.0.0.md)
- [V11.0.0](releases/v11.0.0.md)
- [V10.0.0](releases/v10.0.0.md) and [V10 development notes](releases/dev-v10.md)
- [v0.20.0](releases/v0.20.0.md)
