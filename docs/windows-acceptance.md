# Windows supervised acceptance checklist

This checklist is the remaining evidence gate for Postfader 0.13.0 Windows
support. Run it only with the user present, a disposable unsaved FL Studio
project, and an intentionally configured virtual MIDI endpoint. Do not infer
support for a particular third-party MIDI provider from this procedure.

## Evidence header

Record, without user media or secrets:

- Windows edition/build and architecture;
- Python and Postfader versions;
- FL Studio edition/version/build and MIDI scripting API;
- virtual endpoint name and provider/version (evidence only, not endorsement);
- repository commit and cumulative patch SHA-256;
- deployed and running bridge SHA-256;
- test time and operator.

## 1. Clean bootstrap and configuration

1. Close FL Studio and all MCP clients.
2. Preview the downloaded PowerShell installer with an explicit process-level
   execution-policy override (this does not change machine policy):

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -DryRun -Python (Get-Command python.exe).Source -UserDataDir "C:\ABSOLUTE\FL Studio"
   ```

3. Run the installer with the discovered or explicit absolute user-data path.
   It refuses bridge replacement while FL Studio is running. Close FL Studio;
   do not use `-AllowBridgeReplacementWhileFLStudioRunning` during acceptance.
   Then select the checkout interpreter and create the ignored evidence folder;
   every later Python command in this checklist uses that interpreter:

   ```powershell
   $RepoPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
   New-Item -ItemType Directory -Force .private | Out-Null
   ```

4. Generate Codex or Claude configuration with absolute paths and the exact
   endpoint name. Confirm `FL_BRIDGE_ENABLE_WRITES` is absent.
5. Register the server using the client's supported user-scope mechanism.
6. Run the doctor in human and JSON modes with the terminal's own exact MIDI
   endpoint and custom FL path when needed; do not assume MCP-client environment
   variables are inherited:

   ```powershell
   $Port = "REPLACE_WITH_EXACT_VIRTUAL_MIDI_ENDPOINT"
   $UserData = "C:\ABSOLUTE\Image-Line\FL Studio"
   $FL = "C:\ABSOLUTE\Image-Line\FL Studio 2026\FL64.exe"
   .\.venv\Scripts\postfader-doctor.exe --user-data-dir $UserData --midi-port $Port --fl-executable $FL
   .\.venv\Scripts\postfader-doctor.exe --json --user-data-dir $UserData --midi-port $Port --fl-executable $FL > .private\windows-doctor.json
   ```

Pass requires resolved paths, matching source/deployed hashes, deterministic
input/output selection, no ambiguity, and no unsupported provider claim.

## 2. Read-only FL and real MCP session

1. Start FL normally through `launch_fl_studio.ps1` (no `-EnableWrites`).
2. Configure matching FL input/output Port numbers, select `Universal Bridge`,
   reload the script, and confirm `ready: MIDI SysEx`.
3. Run the doctor JSON live. Require compatible FL/build/API/protocol, matching
   running bridge hash, a session fingerprint, `bridge_mode=read_only`, and
   `verified_writes_enabled=false`.
4. Start the actual MCP client and inspect its server list/status.
5. In a real client conversation, call project summary and one bounded mixer
   read. Retain tool names, outcome, transport, session fingerprint, and hashes.
6. Run the read harness with reviewed target indices, the terminal's exact
   endpoint, and a new output file. Require every authoritative read tool to
   pass, including the bounded full mixer and large plug-in scans:

   ```powershell
   & $RepoPython .\scripts\live_read_acceptance.py --midi-port $Port --mixer-track 1 --plugin-track 1 --plugin-slot 0 --pattern 1 --channel 0 --per-tool-timeout-seconds 180 --overall-timeout-seconds 1200 --output .private\windows-read.json
   ```

   Each read runs once in its own tracked worker. The parent atomically records
   the exact in-flight tool before contact, terminates and reaps only that
   worker on timeout, and skips every later read because the timed-out FL-side
   outcome is uncertain. Preserve the resulting timeout checkpoint; do not
   retry the tool or reduce coverage during the evidence run.

### Codex-specific evidence

1. Generate `codex-toml` and `codex-command` from the same absolute facts.
2. Review the command, then add it explicitly with `codex mcp add`; do not have
   the installer edit active configuration.
3. Run `codex mcp list` and retain the `fl-studio` registration/status without
   recording unrelated servers or secrets.
4. Start a fresh Codex task after registration so the host reloads its tool
   surface. Confirm all 36 expected Postfader tools are present and no generic
   bridge-command tool exists.
5. Ask the fresh task to call `fl_get_project_summary`,
   `fl_get_transport_state`, and a bounded `fl_list_mixer_tracks` read.
6. Retain redacted task/tool-call evidence identifying Codex as the MCP host,
   the called tool names, selected transport, session fingerprint, protocol,
   and bridge hash. A standalone Python harness result does not satisfy this
   Codex-session gate.
7. Do not request writes through Codex until the disposable write-mode setup in
   section 4 is complete. Then use only the reviewed scenario targets and
   retain each tool result plus restoration evidence.

## 3. Ownership, release, and restart behavior

1. While the first MCP process owns the endpoint, start a second configured
   process. Require refusal before endpoint open and retain the owner PID.
2. Stop the first client cleanly, then start the successor. Require successful
   ownership acquisition and a new live handshake.
3. Reload the FL script and prove a fresh session fingerprint is observed.
4. Quit and cold-restart FL in read-only mode. Require doctor and MCP reads to
   recover without deleting lock files or killing processes.

## 4. Supervised persistent-write restoration

1. Close every MCP process and FL Studio.
2. Start FL with `launch_fl_studio.ps1 -EnableWrites`.
3. Open a blank disposable unsaved project and stop playback/recording.
4. Run doctor JSON. Require matching provenance, session fingerprint,
   `bridge_mode=write_test`, and `verified_writes_enabled=true`.
5. Build the disposable fixture below and review
   `docs/windows-write-scenario-v1.json`. Replace its clearly marked plug-in
   option and any parameter indices/values that differ from the read-only scan.
   For every plug-in replacement, update the mutation parameter/index, the
   identity in each `$select` before-state selector, every restore
   `parameter_index`, and every verification selector together. A logical
   parameter index is not assumed to equal its list position.
   Copy it to `.private\windows-write-scenario-reviewed.json`, make the reviewed
   replacements there, keep `scenario_version` exactly `1` and
   `safe_to_edit` exactly `true`, and change `fixture_status` to
   `REVIEWED_FOR_THIS_DISPOSABLE_PROJECT`. The public template itself is
   deliberately refused in live mode. It covers all 19 authoritative
   persistent-write tools exactly once.
6. Prove full no-I/O resolution first, then run with all confirmations, the
   exact endpoint, and a new evidence file. A Master target also requires that
   operation's explicit acknowledgement; this fixture intentionally has none.

   ```powershell
   Copy-Item .\docs\windows-write-scenario-v1.json .private\windows-write-scenario-reviewed.json
   # Edit only the private copy as described above, then validate it:
   & $RepoPython .\scripts\live_write_acceptance.py --plan --midi-port $Port --scenario .private\windows-write-scenario-reviewed.json
   & $RepoPython .\scripts\live_write_acceptance.py --scenario .private\windows-write-scenario-reviewed.json --midi-port $Port --confirm-user-present --confirm-disposable-project --confirm-safe-to-edit --output .private\windows-write.json
   ```
7. Require every mutation to be attempted at most once, every requested state
   to be verified, every captured state to be restored, and every restoration
   to pass an independent reread. The evidence file must contain durable
   checkpoints for preflight, before-state reads, mutation attempt/result,
   each restore attempt/result, and the independent reread. Stop immediately
   if restoration fails; an ambiguous mutation is never replayed.
8. Confirm FL remains unsaved. Close without saving.

## 5. Separate ephemeral note test

In a new disposable session, review the channel, note, velocity, and duration.
Run `live_note_acceptance.py` with its user-present/disposable/audition
confirmations. Retain the dispatch receipt. Do not describe it as persistent
state verification.

```powershell
& $RepoPython .\scripts\live_note_acceptance.py --plan --midi-port $Port --channel 0
& $RepoPython .\scripts\live_note_acceptance.py --midi-port $Port --channel 0 --note 60 --velocity 80 --duration-ms 250 --confirm-user-present --confirm-disposable-project --confirm-live-note-dispatch --output .private\windows-note.json
```

## Disposable fixture recipe

Create a brand-new unsaved project and never use Master (mixer index 0) as a
source, destination, or plug-in target. Set Song mode, tempo 120 BPM, stop
playback/recording, and place the playhead at roughly 25% so the stop/rewind
case has reversible state.

1. Name mixer insert 1 `PF Source` and insert 2 `PF Return`. Create and leave
   enabled a send from insert 1 to insert 2 at normalized level `0.8`. Insert 1
   begins at volume `0.8`, pan `0.0`, unmuted, and flat built-in EQ band 0.
   The read may also return the ordinary Master route (destination 0) first.
   The scenario deliberately resolves destination 2 by its unique
   `destination_track_index`; never replace those selectors with a positional
   route-list path.
2. Load the stock native **Fruity Limiter** in insert 1, effect slot 0. It is a
   specifically read-profiled effect in `docs/plugin-matrix.md`; the recipe
   does not generalize that evidence to other plug-ins.
3. Run `plugins_scan_parameters` read-only. Record one ordinary normalized
   control, one numeric-display control, and one enumerated text control. In
   the scenario, replace parameter indices `0`, `1`, and `2`, the `-12.0`
   display target, and `REPLACE_WITH_REVIEWED_ALTERNATE_OPTION` with observed,
   reversible values. Change each mutation index, `$select` identity, restore
   index, and verification identity as one unit. If Fruity Limiter on this
   exact FL build does not expose all three safely, stop: do not substitute an
   unreviewed plug-in.
4. Use global Channel Rack channel 0, named `PF Fixture Channel`, routed to
   mixer insert 1, at volume `0.8`, pan `0.0`, and unmuted. Use current pattern
   1 with exactly 16 blank sixteenth-note cells.
5. Run the scenario's `--plan` command. Eligibility requires
   `fully_resolved_operation_count: 19`; any placeholder, schema error, missing
   before path, Master target, or non-restorable control is a stop condition.

The embedded `fixture_before` objects are no-I/O validation exemplars. During a
live run the harness discards them, captures every real before-state first,
resolves all mutations/restores against those reads, and only then permits the
first mutation. The special `$after_step_digest` value is computed from the
captured grid and requested absolute update before mutation; it supplies the
real conflict digest needed for the one-step restore.

`$select` resolves one list member by logical identity and fails unless exactly
one member matches. Mixer-route restoration captures destination 2's own route
presence (its enabled state) and level, then verifies destination 2 again by
identity. The `fl_set_playing` case captures both `playing` and
`song_position_normalized`; it restores stopped playback first, restores the
captured position with `fl_set_song_position`, and independently rereads both.
Its fixture models playback advancing from `0.25` to another position, so a
playing-only restoration cannot pass.

## 6. Return to read-only and final gate

1. Quit FL and restart normally without the write flag.
2. Require the doctor and a real MCP read to report read-only mode again.
3. Run the full hermetic safe suite with hostile ambient transport variables;
   child isolation must still disable MIDI.
4. Build wheel/sdist, verify archives, install the wheel in a clean venv, and
   run all console `--help` smoke checks.
5. Retain command exits, total check count, package hashes, `git diff --check`,
   public-tree result, status, patch SHA-256, and the known limitations.

Windows may be promoted from release candidate to validated only when every
section passes on the release artifact. A failure stays visible; it is not
converted into a capability claim.
