<div align="center">

# PostFader

**The AI copilot for FL Studio**

## Your AI can finally work inside FL Studio.

PostFader connects Claude, Codex, Cursor, and other local MCP-compatible AI
clients to the FL Studio project you already have open. Ask it to inspect your
session, diagnose a mix, control loaded plug-ins, clean up routing, build MIDI
parts, organize patterns and Playlist tracks, add section markers, or make
supported changes from natural language.

[**Download for Windows**](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-Windows.zip)
·
[**Download for macOS**](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-macOS.zip)
·
[Explore what PostFader can do](#not-just-another-note-sender)

**90 tools · 8 live resources · Windows and macOS · Open source · No PostFader account**

Starts read-only and never saves your project automatically.

</div>

> [!NOTE]
> PostFader is an unofficial community project. It is not made by or affiliated
> with Image-Line.

## Not just another note sender

PostFader is a production layer for FL Studio—not only a way to send notes or
change isolated controls. It gives your AI useful context from the project that
is open now, plus tools to analyze exported audio, work with loaded plug-ins,
compose musical parts, and carry separately requested supported changes back
into the session.

- **Understand the project.** Inspect mixer inserts and routing, Channel Rack
  generators, loaded effects, patterns, Playlist tracks, transport, undo/redo
  history position, step sequences, and plug-in parameters exposed by FL
  Studio.
- **Diagnose the mix.** Measure an exported bounce, compare it with a reference,
  examine vocal-versus-instrument masking, monitor peaks during playback, and
  surface severity, confidence, and scored evidence so your AI can prioritize
  the next moves.
- **Control the session.** Rename and color tracks, adjust levels and panning,
  manage sends and routing, control transport, organize channels and patterns,
  edit steps, and change supported parameters on plug-ins already loaded.
- **Create and transform music.** Generate chords, melody, bass, and drums;
  export multi-track Type-1 MIDI; estimate tempo and key; transcribe monophonic
  audio; and prepare or transform Piano Roll material.

## Four ways to work with PostFader

### Understand the project already open

> “Show me every instrument that is not routed to the mixer.”
>
> “Which effects are loaded on my lead vocal?”
>
> “Where are my drums routed, and which mixer inserts are peaking too high?”

PostFader reads FL Studio's current project state: mixer inserts and sends,
loaded mixer effects, Channel Rack generators, patterns, Playlist track state,
transport, undo/redo history bounds, the current step grid, and exposed plug-in
parameters. Your AI can answer from the actual session instead of relying on a
project description pasted into chat.

### Diagnose an exported mix and decide what to improve

> “What are the three highest-impact problems in this bounce?”
>
> “Compare this mix with my reference and explain the biggest differences.”
>
> “Check these vocal and instrumental exports for likely masking.”

Mix Doctor turns an exported bounce into producer-readable technical findings
about level, dynamics, tonal balance, stereo behavior, and export readiness.
Reference analysis compares loudness and tonal balance across aligned audio.
Masking analysis uses synchronized vocal and instrumental renders to identify
frequency regions that may be competing.

During playback, a peak watch samples the mixer inserts included in the watch
and remembers the highest level it observed for each. Play the song through,
then use those results to build a gain-staging plan based on the track rather
than a single instant. Your AI can use other reported findings to create a
separate, reviewable mix plan and apply only the operations you choose.

### Control the session and the plug-ins you already use

> “Rename insert 4 to Lead Vocal, color it purple, pan it 10% left, and confirm
> the changes.”
>
> “Mute the backing-vocal tracks and lower the send to the reverb bus.”
>
> “Find the feedback parameter on the delay that is already loaded and reduce
> it.”

PostFader can control mixer volume, pan, mute, solo, arm, selection, stereo
separation, sends, and routing. It can change tempo, playback, loop mode,
recording state, and song position; organize channels, patterns, and Playlist
tracks; and edit step sequences.

For loaded effects and Channel Rack generators, PostFader asks FL Studio which
parameters the plug-in exposes. It can inspect names and values, search or scan
large parameter surfaces within explicit limits, set a known value, target the
number a plug-in displays, or choose an exact named option. Bundled profiles
for selected FL Studio stock effects add known parameter roles for supported
workflows without pretending every plug-in has the same controls.

For those selected profiles, your AI can turn supported goals such as “tame
harshness,” “control dynamics,” “limit peaks,” “shorten the reverb,” or “create
a rhythmic echo” into matching parameter roles. Intent resolution is read-only;
choosing values and applying a change remain separate steps.

PostFader works with the chain already in the session. It does not currently
insert, remove, or reorder plug-ins.

### Compose, transform, and organize musical ideas

> “Create an eight-bar D Dorian melody with a bassline and drum pattern.”
>
> “Transpose this Piano Roll part up an octave and humanize the velocities.”
>
> “Estimate the tempo and key of this sample.”
>
> “Transcribe this monophonic melody into a reviewable note sequence.”

Generate deterministic chord progressions, melodies, bass parts, and drums from
a musical brief; melody, bass, and drum generation also accept reproducible
seeds. Export separate parts in one Type-1 MIDI file and verify the written
file's structure and content digest. Audio tools can estimate tempo and a global
major or minor key, while monophonic transcription creates a note sequence that
can be reviewed and exported in a separate step.

PostFader can also find and prepare a pattern FL Studio reports as empty, add
section markers, organize Playlist tracks, record a supported automation value,
and prepare Piano Roll append, replace, quantize, transpose, humanize,
duplicate, delete, or clear operations. Piano Roll application uses FL Studio's
separate script workflow, so PostFader reports the evidence it actually has
instead of claiming controller-side note readback.

## From one request to a complete production workflow

### You ask

> “The vocal feels buried. Find the most likely cause, show me what you would
> change, and fix only the highest-confidence problem.”

### PostFader workflow

1. Reads the mixer, routing, and relevant loaded plug-ins.
2. Analyzes the bounce or synchronized renders you provide.
3. Identifies likely level, tonal, dynamics, or masking problems.
4. Your AI prioritizes the reported evidence and builds a reviewable plan from
   supported operations.
5. Keeps the plan separate until you choose to proceed with the apply call.
6. Applies the selected supported operation.
7. Reports the observed result and any evidence limitation.

**Read the project → analyze the bounce → identify the problem → propose a plan
→ choose the change → make a separate apply request → report the result**

A narrow remote control stops at individual commands. PostFader connects those
commands into a production workflow.

## Feature depth

### Mix and finish

- Run Mix Doctor on an exported bounce.
- Compare loudness and tonal balance with a reference.
- Examine synchronized vocal and instrumental renders for likely masking.
- Measure peaks, loudness, dynamics, tonal balance, and stereo behavior.
- Watch mixer peaks across playback and build gain-staging plans.
- Run a finish assessment and use your AI to turn selected recommendations into
  reviewable one-shot plans.

### Control the session

- Work with mixer inserts, sends, routing, and transport.
- Inspect and organize Channel Rack generators, patterns, and Playlist tracks.
- Read undo/redo history bounds and edit the current step sequence.
- Discover and control supported parameters exposed by loaded effects and
  generators.

### Create music

- Generate deterministic chord progressions; melody, bass, and drums also
  accept reproducible seeds.
- Export multi-track Type-1 MIDI and verify the written content.
- Estimate tempo and global major or minor key from an audio file.
- Turn monophonic audio into a reviewable note sequence.

### Edit and arrange

- Append, replace, quantize, transpose, humanize, duplicate, delete, or clear
  Piano Roll material through the separate FL Studio script workflow.
- Find and prepare patterns FL Studio reports as empty, add section markers,
  organize Playlist tracks, and record supported automation values.

### Bring your own AI

- Claude Desktop and Claude Code
- Codex CLI, IDE extension, and desktop Codex
- Cursor IDE and Cursor CLI
- OpenCode
- Grok Build
- T3 Code through an MCP-capable provider
- Other compatible local `stdio` MCP hosts

## More than a basic FL Studio MCP

To make the distinction concrete, the baseline below is deliberately defined
as a local MCP with transport commands, individual controls, point-in-time
reads, predefined parameter mappings, and note dispatch. It is not a survey of
every other project.

| Capability | Narrow baseline used here | PostFader |
| --- | --- | --- |
| Play, stop, and change individual controls | Transport and individual controls | Yes, plus wider session workflows |
| Read the open project | Selected state only | Mixer, channels, loaded plug-ins, patterns, Playlist tracks, undo/redo history, steps, and transport |
| Diagnose exported audio | Not part of the baseline | Mix Doctor, peaks, loudness, tonal balance, stereo analysis, masking, and references |
| Monitor levels through playback | Point-in-time meter reads | Persistent per-insert peak watches sample across a playback pass |
| Move from diagnosis to a separate apply request | Not part of the baseline | Diagnose → propose → review → apply → report |
| Work with loaded plug-ins | Predefined controls | Runtime parameter discovery, exact controls, named options, and selected stock-effect profiles |
| Generate musical parts | Individual note dispatch | Chords, melody, bass, drums, transcription, and Type-1 MIDI export |
| Transform Piano Roll content | Not part of the baseline | Append, replace, quantize, transpose, humanize, duplicate, delete, and clear |
| Help organize an arrangement | Not part of the baseline | Pattern preparation, markers, Playlist track tools, and automation helpers |
| Install on Windows and macOS | Not part of the baseline | Guided platform packages for both |
| Work across AI clients | Single-host setup | Claude, Codex, Cursor, OpenCode, Grok Build, and other local MCP hosts |
| Check supported changes | Command dispatch only | Reads supported controls back from FL Studio after the change |

## Quick installation

Download the Windows or macOS package, extract it to a stable writable folder,
run the guided installer, select your virtual MIDI endpoint, complete one FL
Studio MIDI Settings step, and connect your AI client.

- [Download PostFader for Windows](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-Windows.zip)
- [Download PostFader for macOS](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-macOS.zip)
- [Open the complete setup and troubleshooting guide](docs/setup.md)
- [See every v0.20.0 release asset](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/tag/v0.20.0)

Codex users can choose the dedicated Codex ZIP for guided `codex mcp add`
registration. Claude Desktop users can add the `.mcpb` after completing the
same platform setup. Advanced users can install the wheel or source archive.
None of those formats replaces the Universal Bridge, virtual MIDI endpoint, or
FL Studio MIDI configuration described in the setup guide.

PostFader does not install virtual MIDI software. Python 3.10–3.14 is required,
and Python 3.13/3.14 or Windows ARM64 may require a native compiler for
`python-rtmidi`.

## Supported AI clients

PostFader runs as a local `stdio` MCP server, so the AI host must be able to
launch it on the same computer as FL Studio.

| Client or host | v0.20 setup path |
| --- | --- |
| Claude Desktop | Use the Windows/macOS package and generated `claude-json`; the optional `.mcpb` is an additional Claude Desktop wrapper, not the platform setup. |
| Claude Code | Use the Windows/macOS package and adapt the generated `claude-json` server values to Claude Code's MCP configuration. |
| Codex CLI, IDE extension, and desktop Codex | Use a Codex ZIP, or run `postfader setup --client codex-toml --register-codex` from a Python/source install. |
| Cursor IDE and Cursor CLI | Put the resolved executable, arguments, and environment values in Cursor's `mcp.json`. |
| OpenCode | Adapt the resolved values to `opencode.json` or `opencode.jsonc`. |
| T3 Code | Configure PostFader in the MCP-capable provider T3 Code is using; no T3-specific package is shipped. |
| Grok Build | Configure the local `stdio` server in Grok Build's MCP settings; no Grok-specific package is shipped. |
| Other local MCP hosts | Adapt the generated server values to the host's local `stdio` schema. |

Grok on the web and Grok Bot require a publicly reachable remote HTTP MCP
server and cannot use PostFader's current local packages directly.

## Supported systems

| Component | v0.20 support |
| --- | --- |
| PostFader | 0.20.0 |
| FL Studio | FL Studio 2026, version 26.1.3 build 5336 or newer; live evidence is limited to the tested systems below. |
| FL MIDI scripting API | Version 44 or newer |
| Python | 3.10 through 3.14 |
| macOS | Qualified on macOS 27.0 arm64 with FL Studio Producer Edition 26.1.3 build 5336 and the built-in IAC bus. |
| Windows | Qualified on Windows 11 x64 with FL Studio Producer Edition 26.1.4 build 5589. |

The [v0.20.0 release notes](docs/releases/v0.20.0.md#qualified-environments)
record the exact qualification revision, acceptance counts, restoration
evidence, and boundaries.

## How it works

~~~mermaid
flowchart LR
    A["Your AI client<br/>(MCP)"] --> B["PostFader<br/>runs locally"]
    B -->|"Virtual MIDI"| C["Universal Bridge<br/>inside FL Studio"]
    C --> D["Your open project"]
    B -->|"Files you choose"| E["Exported audio<br/>analysis"]
    B --> F["Mix and creative<br/>workflows"]
    F -->|"Type-1 MIDI"| G["Generated MIDI files"]
    F -->|"Optional script"| H["FL Piano Roll"]
~~~

The AI client calls PostFader's named MCP tools. Live FL Studio communication
travels over a local virtual MIDI endpoint to the Universal Bridge controller
script. Audio tools analyze files you select because FL Studio's scripting API
does not expose its live audio buffer.

## Built for real projects without pretending FL Studio exposes more than it does

- PostFader starts read-only; write access lasts only for the current session.
- It never saves the project automatically.
- Supported direct changes are read back from FL Studio after they are made.
- Workflows with narrower evidence say so instead of reporting full
  verification.
- PostFader runs locally, requires no PostFader account, and has no PostFader
  telemetry.

For the exact boundaries, read [What “verified” means](#what-verified-means),
the [write response contract](docs/tool-contracts.md#write-response-contract),
the [security policy](SECURITY.md), and
[FL Studio API limitations](docs/fl-constraints.md).

### What “verified” means

For a supported direct change, PostFader checks the target and current session,
asks FL Studio to make the change, waits for a later controller update, and
reads the control again. `verified: true` means the requested control state was
observed afterward. It does not prove the choice sounds good or guarantee an
undo point or rollback.

Writes affect the open project immediately. Ask the AI client to enable write
mode only when you want changes, use a blank or disposable project for the
first write test, and disable write mode when you are done.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Setup and troubleshooting](docs/setup.md) | Full installation, virtual MIDI, bridge, client configuration, upgrades, and diagnostics |
| [Tool contracts](docs/tool-contracts.md) | All 90 tools and 8 resources, exact arguments, results, refusals, and evidence boundaries |
| [Plug-in support](docs/plugin-support.md) | Parameter discovery, option controls, scan limits, troubleshooting, and compatibility evidence |
| [Plug-in matrix](docs/plugin-matrix.md) | Evidence definitions, validated reports, and the contributor target backlog |
| [FL Studio constraints](docs/fl-constraints.md) | What FL Studio's scripting API allows and where PostFader stops |
| [Architecture](docs/architecture.md) | Components, transport, bridge behavior, resources, and trust boundaries |
| [Security](SECURITY.md) | Threat model, local trust boundaries, privacy, and vulnerability reporting |
| [Early-user activation](docs/early-access-testing.md) | A privacy-safe first-session and return-session checklist |
| [Contributing](CONTRIBUTING.md) | Development workflow and contribution guidelines |
| [GitHub Discussions](https://github.com/synopsys0/postfader-fl-studio-mcp/discussions) | Setup help, workflow sharing, ideas, and plug-in compatibility conversations |

## Detailed limitations

PostFader does not currently:

- guarantee rollback or an FL Studio undo point;
- save, render, or export an FL Studio project;
- hear or capture FL Studio's live audio output;
- insert, remove, or reorder plug-ins;
- reliably control an effect slot's bypass or wet/dry mix;
- create, move, or delete Playlist clips through the public scripting API;
- read Piano Roll notes back after the focus-sensitive script workflow;
- read section-marker times or recorded automation points back from FL Studio;
- infer named competing project tracks from a full-mix masking measurement; or
- turn technical measurements into objective artistic truth.

Batch application is bounded but not atomic: earlier changes are not rolled
back if a later operation cannot be verified. A lost or ambiguous mutation
response is never replayed automatically. The local virtual MIDI bus is shared
and unauthenticated, so use PostFader on a trusted, single-user workstation.

Audio and mix tools analyze files you explicitly select or recent bounces found
in bounded FL Studio folders. Results can include paths, hashes, and
measurements, but never audio samples. Your AI client is separate software and
may send tool arguments and results to its model provider; review that client's
privacy policy before using sensitive projects.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. To help
expand real-world plug-in evidence, review the definitions and target backlog
in the [plug-in matrix](docs/plugin-matrix.md), then submit a privacy-safe
[plug-in validation report](https://github.com/synopsys0/postfader-fl-studio-mcp/issues/new?template=plugin-validation.yml).

Community setup help, workflow notes, feature ideas, and compatibility results
belong in [GitHub Discussions](https://github.com/synopsys0/postfader-fl-studio-mcp/discussions).

## Security reporting

Do not open a public issue for a suspected vulnerability. Follow the private
reporting instructions in [SECURITY.md](SECURITY.md#reporting-a-vulnerability).

## License

PostFader is available under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution details.

<!-- mcp-name: io.github.synopsys0/postfader-fl-studio-mcp -->
