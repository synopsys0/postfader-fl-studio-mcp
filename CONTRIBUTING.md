# Contributing

Contributions to Postfader should keep the public repository
reproducible, content-neutral, and safe to clone on a machine that has never
seen the maintainer's projects.

## Development setup

The deterministic safe suite does not require FL Studio or a MIDI device:

```bash
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
cd postfader-fl-studio-mcp
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python scripts/run_safe_tests.py
```

Use Python 3.10 or newer on macOS. The production bridge is currently
macOS-only because it uses CoreMIDI/IAC.

## Public-content rule

Never commit material from a real creative project, even when it appears
harmless or is intended only as test evidence. Prohibited content includes:

- FL Studio project files or backups;
- recorded audio, stems, samples, bounces, reference tracks, and waveforms;
- mixer states, plug-in presets, session exports, screenshots, and UI captures;
- project titles, artist names, lyrics, track layouts, musical settings, or
  measurements copied from a user's session;
- absolute home-directory paths, usernames, machine identifiers, process
  dumps, or unredacted logs;
- credentials, tokens, cookies, private keys, and client-local configuration;
  and
- generated caches, build products, or test output containing any of the
  above.

Public test audio must be deterministic and generated entirely by
`scripts/generate_audio_fixtures.py`. The expected fixture names and hashes are
recorded under `tests/fixtures/`. Do not replace those files with recorded or
copyrighted material.

Use the fake FL API for project-state tests. A fixture should describe generic
behavior such as a mixer track, native effect, third-party parameter map, or
vocal/instrument analysis input without reproducing a real user's session.

Before submitting a change, run the public-tree check when present:

```bash
./.venv/bin/python scripts/check_public_tree.py
```

That checker is a release boundary, not a substitute for reviewing the diff.

## Plug-in validation reports

The compatibility matrix is evidence, not a runtime allowlist. A report can
add confidence and document an exception; it never teaches Postfader a product
identity or enables that product in code.

Generate a read-only report with the installed command:

```bash
postfader-plugin-report \
  --track 3 --slot 1 \
  --plugin-version 2.1 \
  --plugin-origin third-party \
  --plugin-format VST3 \
  --fl-edition Producer
```

Submit the generated text through the repository's **Plug-in validation**
issue form. Generated reports always label their source `community candidate`:
the reporter records evidence but cannot approve its own submission or speak
for the maintainer. Maintainers curate accepted evidence into
`docs/plugin-matrix.md`; the resulting row records `community` or `maintainer`
as its source while the evidence level remains `detected`, `read-profiled`, or
`write-validated`.

For write evidence, use only a new blank disposable project. The command must
be given both `--validate-write PARAMETER_INDEX` and
`--confirm-disposable-project`. It refuses Master, playback, recording, and a
bridge without verified writes. A report qualifies as write-validated only if
the move verifies and a separate read confirms the captured original
normalized value after restoration. Close the disposable project without
saving even after a passing report.

Never attach the saved scan, raw JSON, logs, screenshots, presets, FLP files,
audio, or project/session descriptions. The generated report removes current
values, display strings, parameter names, option strings, track/slot locations,
paths, project metadata, and timestamps; nevertheless, read it before posting.
GitHub issue text is not covered by `scripts/check_public_tree.py`, so reviewer
judgment is still the final privacy boundary.

## Testing

The required test command is:

```bash
./.venv/bin/python scripts/run_safe_tests.py
```

It runs an explicit allowlist against fake transports, a fake FL API, and
synthetic audio. A test that exits successfully without reporting any checks
is treated as a failure.

`tests/test_midi_transport.py` is excluded because it touches the real shared
IAC bus. Live FL Studio validation is optional, must use a new blank disposable
project, and must never commit the project, audio, screenshots, paths, or raw
output it produces.

If a live result changes a documented constraint, reduce it to the smallest
generic fake-API regression test and describe only the technical behavior in
the documentation.

## Bridge requirements

The bridge lives at `fl_studio_mcp/_bridge/device_UniversalBridge.py` and ships
as package data so an installed copy can deploy it without a clone. It must
never gain an `__init__.py`. Python can expose the directory as a namespace,
but the controller module itself depends on FL Studio-only modules and must run
only after deployment into FL Studio. The MCP process reads and copies its
bytes; it does not import or execute the controller module.

Changes to it must preserve these properties:

- ASCII-only source;
- no exception escaping an FL Studio callback;
- no background-thread access to the FL Studio API;
- bounded work per idle tick;
- read-only mode by default;
- separate allowlists for read and verified-write commands;
- no `saveProject` call;
- no automatic replay of a write after an ambiguous transport failure; and
- later-tick readback for every public write.

Run `tests/test_bridge.py` and `tests/test_tick_budget.py` while iterating, then
run the complete safe suite.

## MCP surface changes

The package command is `fl-studio-mcp`, the configured MCP server ID is
`fl-studio`, and the current public surface contains 36 tools. Preserve
existing names and response contracts unless a deliberate compatibility change
has been discussed.

For a new or changed tool:

- use a strict argument schema with bounds;
- choose accurate read-only, destructive, idempotent, and open-world hints;
- add a typed immutable response contract;
- define its exact bridge allowlist boundary;
- state whether it reads a local file or mutates FL Studio;
- add deterministic success, refusal, and malformed-response tests; and
- update `docs/tool-contracts.md`, safety guidance, and the tool count when
  appropriate.

Do not expose a generic bridge dispatcher or an unrestricted filesystem
search.

## Pull-request checklist

- [ ] The change contains no user project, user audio, private path, secret, or
      session-derived material.
- [ ] The safe suite passes from a clean clone.
- [ ] The public-tree check passes.
- [ ] Bridge changes remain ASCII-only and within the tick budget.
- [ ] New mutations are off by default, narrowly allowlisted, and verified by
      later readback.
- [ ] Documentation describes current behavior without host-specific paths or
      private-session examples.
- [ ] Security-sensitive changes include refusal and adversarial tests.

## Security reports

Do not open a public pull request or issue containing an unreleased
vulnerability. Follow [SECURITY.md](SECURITY.md) and use GitHub's private
vulnerability-reporting flow.
