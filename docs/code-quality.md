# Code quality and hermetic coverage

PostFader's first post-release quality gate is intentionally small and
reviewable. It catches obvious Python errors and import drift, checks a
high-value typed baseline, and measures the safe suite without contacting FL
Studio or a physical MIDI endpoint.

## Local commands

Install the development extras in a fresh environment:

```bash
python -m pip install --editable ".[dev]"
```

Run the static checks:

```bash
python -m ruff check fl_studio_mcp scripts
python -m pyright --project pyrightconfig.json
```

Run the hermetic suite with coverage. The optional artifact directory receives
both a machine-readable JSON report and a readable text report:

```bash
python scripts/run_coverage.py --artifact-dir coverage-artifacts
```

The existing safe runner remains the shortest test-only command:

```bash
python scripts/run_safe_tests.py
```

## Ruff boundary

Ruff is configured in `pyproject.toml` for the actionable syntax/error
subsets (`E4`, `E7`, `E9`), Pyflakes (`F`), and import sorting (`I`). Ruff's
formatter is not enabled. The import configuration preserves the existing
case-sensitive contract-name ordering and two-line separation; a small,
file-scoped list records pre-existing one-line import layouts. New modules do
not inherit those exceptions.

The FL-only controller at
`fl_studio_mcp/_bridge/device_UniversalBridge.py` is excluded because its
imports are provided only by FL Studio's embedded interpreter. Test harnesses
are excluded from Ruff because they deliberately use dynamic module injection,
late imports, and fake FL modules. Their behavior is still exercised by the
safe suite and its coverage job.

## Pyright baseline

Pyright was compared with mypy on the current tree. On the required high-value
modules (setup, client configuration, bridge installation, contracts,
evidence, workflows, and release-bundle generation), the initial comparison
reported 45 Pyright diagnostics and 98 mypy diagnostics. Pyright gave the
useful signal with the least disruptive configuration, so it is the enforced
checker. After the small protocol/keyword typing fixes below, the same mypy
probe still reports 94 diagnostics while the enforced Pyright baseline is
clean; this difference is why mypy is documented as exploratory rather than a
second failing CI gate.

`pyrightconfig.json` uses basic checking and includes exactly those seven
entry modules. Their imported application modules are analyzed normally.
The FL-only controller and test harnesses are excluded for the same runtime
reasons described above. The only source-level compatibility adjustment is a
targeted `reportMissingImports` comment on the Python 3.10-only `tomli`
fallback; Python 3.10 installs that development dependency, while newer
interpreters use the standard-library `tomllib` branch. There is no blanket
`ignore_errors` setting.

Mypy remains a useful exploratory tool, but is not a CI gate until its
platform-specific stubs and Pydantic model-inheritance diagnostics can be
resolved without weakening the safety boundary.

## Coverage expectations

`run_coverage.py` starts every allowlisted test file in an isolated child,
combines the parallel data, prints a terminal report, and optionally writes
`coverage.json` and `coverage.txt`. The package-wide combined statement/branch
floor is 45%. The script also enforces separate statement floors so a broad
percentage cannot hide an untested safety boundary:

| Hermetic boundary | Floor |
| --- | ---: |
| Policy and response contracts | 55% |
| Transport and framing | 45% |
| Setup and packaging | 45% |
| Fake-FL bridge behavior | 45% |

The CI job derives and prints the current totals from the complete allowlist;
it runs on macOS because guided setup deliberately rejects Linux as an
unsupported desktop target. This host choice does not start or emulate FL
Studio and does not turn hermetic coverage into live qualification. Local
probes made while the public-documentation allowlist was in flight are
not release or qualification results. Coverage is evidence about fake
transports, synthetic audio, setup logic, contracts, and fake-FL behavior only;
it is not live FL Studio qualification. Live writes, real FL callbacks,
physical MIDI, plug-in behavior, and platform installer interaction remain
separately qualified on the documented Windows/macOS environments.

No telemetry or coverage upload service is used. CI retains the optional
report as a build artifact only.
