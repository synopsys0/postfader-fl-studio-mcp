# PostFader website copy

This is the canonical product-copy deck for the PostFader website. The
repository does not currently contain a website implementation, so these
sections are ready to move into the website project without inventing new
capabilities or duplicating the full technical manuals.

## Information architecture

1. Hero and downloads
2. Category statement
3. Four workflow stories
4. One complete production workflow
5. Feature depth
6. Generic category comparison
7. Plug-in compatibility
8. How PostFader connects
9. Workflow proof cards
10. Short installation and client compatibility
11. Compact trust section
12. Documentation and final download call to action

The navigation should emphasize **What it can do**, **Workflows**, **Features**,
**Install**, and **Docs**. Detailed setup, evidence semantics, architecture,
security boundaries, and qualification records should remain in their linked
documents instead of expanding the homepage.

---

## Hero

**Eyebrow:** PostFader — the AI copilot for FL Studio

# Your AI can finally work inside FL Studio.

Inspect your real project, diagnose mixes, control supported parameters on
loaded plug-ins, build MIDI parts, organize arrangements, and make supported
changes from Claude, Codex, Cursor, or another local MCP client.

**Primary actions**

- [Download for Windows](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-Windows.zip)
- [Download for macOS](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-macOS.zip)

**Secondary action:** Explore what PostFader can do

**Proof strip:** 95 tools · 8 live resources · Windows and macOS · Open source ·
No PostFader account

Starts read-only and never saves your project automatically.

---

## Not just another note sender

PostFader is a production layer for FL Studio—not only transport commands,
isolated mixer controls, or generated MIDI notes.

It gives your AI useful context from the project that is open now. Inspect the
mixer and routing. Understand channels, patterns, Playlist tracks, undo/redo
history position, steps, and loaded plug-ins. Diagnose exported audio. Control
the session and supported plug-in parameters. Compose complete musical parts.
Transform Piano Roll material. Prepare patterns and markers that make an
arrangement easier to navigate.

The result is one connected workflow across project understanding, mixing,
plug-in control, composition, editing, arrangement, and audio analysis.

---

## Workflow A — Understand the project

### You ask

> “Show me every instrument that is not routed to the mixer.”
>
> “Which effects are loaded on my lead vocal?”
>
> “Where are my drums routed, and which mixer inserts are peaking too high?”

### PostFader inspects

- mixer inserts and routing;
- loaded mixer effects;
- Channel Rack generators and mixer assignments;
- patterns and Playlist track state;
- transport and undo/redo history bounds;
- the current step sequence; and
- parameter names and values that loaded plug-ins expose to FL Studio.

### What it does

PostFader combines those reads into a useful view of the open project. The AI
can trace a channel to its mixer destination, inspect the effects already on a
track, identify current level or routing concerns, and explain the session in
producer language.

### You receive

An answer grounded in the real FL Studio session instead of a project
description typed into chat.

---

## Workflow B — Diagnose and improve a mix

### You ask

> “What are the three highest-impact problems in this bounce?”
>
> “Compare this mix with my reference and explain the biggest differences.”
>
> “Check these synchronized vocal and instrumental exports for likely
> masking.”

### PostFader uses

- Mix Doctor;
- exported-audio peak, loudness, dynamics, tonal-balance, and stereo
  measurements;
- reference comparison across aligned audio;
- masking analysis for synchronized vocal and instrumental renders;
- persistent mixer peak watches;
- gain-staging plans;
- finish assessment; and
- reviewable one-shot mix plans.

### What it does

PostFader measures the audio you exported and reports technical findings with
severity, confidence, or scores where the workflow supports them. Your AI can
use that evidence to prioritize practical next moves. When the inputs align and
pass readiness checks, PostFader can compare loudness and tonal balance with a
reference. It can also highlight possible spectral overlap between synchronized
vocal and instrumental renders and separate a technical observation from a
creative opinion.

For level decisions, start a peak watch for a chosen observation window.
PostFader samples the mixer inserts included in the watch, keeps the highest
value it observed for each, then can build a gain-staging plan around those
results rather than one moment on the meters. These are sampled, process-local
observations, not proof that every transient was captured.

### You receive

Reported findings and the evidence behind them, plus a plan your AI can build
from supported operations for you to review before proceeding to application.

---

## Workflow C — Control the session and loaded plug-ins

### You ask

> “Rename insert 4 to Lead Vocal, color it purple, pan it 10% left, and confirm
> the changes.”
>
> “Mute the backing-vocal tracks and lower the send to the reverb bus.”
>
> “Find the feedback parameter on the delay that is already loaded and reduce
> it.”

### PostFader uses

- mixer volume, pan, mute, solo, arm, selection, color, and name controls;
- sends, send levels, stereo separation, and routing;
- tempo, playback, loop mode, recording state, and song position;
- Channel Rack, pattern, Playlist track, undo/redo history, and step-sequence
  controls;
- runtime plug-in parameter discovery and bounded scans; and
- normalized, displayed-value, and named-option parameter controls.

### What it does

PostFader asks FL Studio which parameters each loaded mixer effect or Channel
Rack generator exposes. The AI can search those names and values, identify a
supported exposed control, and set a known value, target the number a plug-in
displays, or choose an exact named option. Bundled profiles for selected FL
Studio stock effects provide known parameter roles for supported workflows
without requiring every plug-in to use the same parameter layout.

For those selected profiles, the AI can turn supported goals such as “tame
harshness,” “control dynamics,” “limit peaks,” “shorten the reverb,” or “create
a rhythmic echo” into matching parameter roles. Intent resolution is read-only;
choosing values and applying a change remain separate steps.

### You receive

A cleaner session or an attempted change to a supported exposed plug-in
parameter after you explicitly confirm that you are present and enable writes
for the current session. The result says whether FL Studio's readback matched,
refused the request, or could not confirm it. PostFader works with plug-ins
already in the project; it does not insert, remove, or reorder them, and FL
Studio does not expose reliable slot bypass or wet/dry control here.

---

## Workflow D — Compose and arrange music

### You ask

> “Create an eight-bar D Dorian melody with a bassline and drum pattern.”
>
> “Transpose this Piano Roll part up an octave and humanize the velocities.”
>
> “Estimate the tempo and key of this sample.”
>
> “Transcribe this monophonic melody into a note sequence I can review.”

### PostFader uses

- chord, melody, bass, and drum generation;
- deterministic outputs, with seed controls for melody, bass, and drums;
- multi-track Type-1 MIDI export and file verification;
- tempo and global major/minor key estimation;
- monophonic transcription;
- finding and preparing FL-reported empty patterns, plus section markers;
- Playlist track organization and automation-value recording while playback
  and recording are active; and
- Piano Roll append, replace, quantize, transpose, humanize, duplicate, delete,
  and clear operations.

### What it does

PostFader can build separate musical parts from one brief. Chord progressions
are deterministic from their inputs, while melody, bass, and drums also accept
seeds for reproducible results. It writes the parts into a Type-1 MIDI file and
checks the file structure and content digest. It can also analyze an audio file
for tempo and key, produce a reviewable monophonic transcription, and prepare
or transform material through FL Studio's separate Piano Roll script workflow.
That workflow requires a one-time manual **Postfader Apply** handshake in each
MCP process and does not provide controller-side note readback.

### You receive

Reproducible musical parts, an importable multi-track MIDI file, or a prepared
editing and arrangement starting point—not merely a stream of disconnected
notes.

---

## One request, a complete production workflow

### You ask

> “The vocal feels buried. Find the most likely cause, show me what you would
> change, and fix only the highest-confidence problem.”

### PostFader workflow

1. Reads the mixer, routing, and relevant loaded plug-ins.
2. Analyzes the bounce or synchronized renders you provide.
3. Reports possible level, tonal, dynamics, or synchronized-input masking
   findings.
4. Your AI prioritizes the reported evidence.
5. Builds a separate, reviewable plan from supported operations.
6. Keeps that plan separate until you choose to proceed.
7. After you explicitly confirm that you are present and enable session writes,
   sends the separate apply request.
8. Reports the observed result, including a refusal or unverified outcome when
   FL Studio does not confirm the requested state.

**Read the project → analyze the bounce → identify the problem → propose a plan
→ choose the change → make a separate apply request → report the result**

A narrow FL Studio integration can act like a remote control and stop at
individual commands. PostFader connects them into a production workflow.

---

## Feature depth

### Mix and finish

- Diagnose an exported bounce with Mix Doctor.
- Track the highest sampled mixer peak during a chosen observation window.
- Build a gain-staging plan from those observations.
- Compare loudness and tonal balance when candidate and reference inputs align
  and pass readiness checks.
- Find possible spectral-overlap regions in synchronized vocal and instrumental
  renders.
- Measure peaks, loudness, dynamics, tonal balance, and stereo behavior.
- Run a read-only finish assessment, then separately use your AI to build and
  apply a reviewable one-shot plan from selected recommendations.

### Control the session

- Adjust mixer levels, pan, mute, solo, arm, selection, color, and names.
- Manage sends, routing, stereo separation, and transport.
- Inspect and organize Channel Rack generators, patterns, and Playlist tracks.
- Read undo/redo history bounds and edit the current step sequence.
- Discover and control supported parameters on loaded effects and generators.

### Create music

- Generate deterministic chords, melody, bass, and drums.
- Reproduce melody, bass, and drum results with a seed.
- Export multi-track Type-1 MIDI and verify the written content.
- Estimate tempo and a global major or minor key from audio.
- Turn monophonic audio into a reviewable note sequence.

### Edit and arrange

- Append, replace, quantize, transpose, humanize, duplicate, delete, or clear
  Piano Roll content through the separate script workflow.
- Find and prepare FL-reported empty patterns and add section markers.
- Organize Playlist tracks and record supported automation values.

### Bring your own AI

- Claude Desktop and Claude Code
- Codex CLI, IDE extension, and desktop Codex
- Cursor IDE and Cursor CLI
- OpenCode
- Grok Build
- T3 Code through an MCP-capable provider
- Other compatible local `stdio` MCP hosts

---

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
| Monitor levels through playback | Point-in-time meter reads | Process-local per-insert peak watches sample across a chosen observation window |
| Move from diagnosis to a separate apply request | Not part of the baseline | Diagnose → propose → review → apply → report |
| Work with loaded plug-ins | Predefined controls | Runtime discovery, exact controls, named options, and selected stock-effect profiles |
| Generate musical parts | Individual note dispatch | Chords, melody, bass, drums, transcription, and Type-1 MIDI export |
| Transform Piano Roll content | Not part of the baseline | Append, replace, quantize, transpose, humanize, duplicate, delete, and clear |
| Help organize an arrangement | Not part of the baseline | Pattern preparation, markers, Playlist track tools, and automation helpers |
| Install on Windows and macOS | Not part of the baseline | Guided platform packages for both |
| Work across AI clients | Single-host setup | Claude, Codex, Cursor, OpenCode, Grok Build, and other local MCP hosts |
| Check supported changes | Command dispatch only | Reads supported controls back from FL Studio after the change |

---

## Plug-in compatibility follows what FL Studio exposes

PostFader does not need a custom profile simply to inspect an unfamiliar loaded
effect or generator. It asks FL Studio for the exposed parameter surface at
runtime. Bundled profiles add richer parameter roles for selected FL Studio
stock effects; they do not gate basic discovery or imply universal plug-in
support.

For those selected profiles, supported outcome-level requests can resolve to
known parameter roles without changing the project. The AI still chooses any
values and makes a separate apply request.

The community matrix separates three useful evidence levels: **detected** means
FL Studio reported the plug-in, **read-profiled** means a bounded parameter
sample was captured, and **write-validated** means one representative control
was changed, read back, and restored in a disposable project. A representative
result is not proof for every parameter or workstation.

[Explore the plug-in matrix](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/plugin-matrix.md)

---

## How PostFader connects

**Your AI client → local PostFader server → virtual MIDI endpoint → Universal
Bridge → the project open in FL Studio**

The AI client launches PostFader on the same computer as FL Studio. Project
reads and supported controls travel through the Universal Bridge. Audio tools
analyze exported files you choose because FL Studio's scripting API does not
expose its live audio buffer.

---

## Workflow proof cards

### Inspect a project you did not describe first

**You ask:** “Which channels are not assigned to the mixer, and what is loaded
on the vocal track?”

**PostFader uses:** Channel Rack assignments, mixer tracks, routing, loaded
effects, and exposed plug-in parameters.

**You get:** A session-grounded routing list and the actual effect chain FL
Studio reports.

### Find why a vocal is getting buried

**You ask:** “Why does this vocal disappear when the chorus gets busy?”

**PostFader uses:** Live mixer and routing context, loaded plug-in inspection,
the synchronized vocal and instrumental exports you provide, masking analysis,
and reviewable planning.

**You get:** Possible spectral-overlap regions, evidence behind each finding,
and bounded suggestions that your AI can prioritize and turn into a plan for a
supported change you choose.

### Adjust a loaded delay without guessing parameter numbers

**You ask:** “Find the delay feedback control on this insert and reduce it.”

**PostFader uses:** The loaded effect list, runtime parameter names and values,
bounded parameter search, and an exact supported setter.

**You get:** The matching supported exposed control and proposed value. After
you explicitly confirm that you are present, enable session writes, and request
the change, PostFader reports whether FL Studio confirmed it, refused it, or
left it unverified.

### Clean up a working session

**You ask:** “Find unassigned instruments, label the vocal tracks, and show me
the routing changes before applying them.”

**PostFader uses:** Channel-to-mixer assignments, mixer and Playlist track
names, colors, routing reads, and a reviewable plan of supported operations.

**You get:** A concrete cleanup list first. Names, colors, or routing operations
are attempted only after you explicitly confirm that you are present, enable
session writes, and make a separate apply call.

### Build a reproducible musical idea

**You ask:** “Create an eight-bar D Dorian idea with melody, bass, and drums.”

**PostFader uses:** Deterministic melody, bass, and drum generation, Type-1 MIDI
export, file verification, and preparation of a pattern FL reports as empty.

**You get:** Separate reproducible parts, an importable multi-track MIDI file,
and a prepared pattern as an arrangement starting point.

### Transform Piano Roll material

**You ask:** “Transpose this part up an octave, tighten the timing, and humanize
the velocities.”

**PostFader uses:** Piano Roll transpose, quantize, and humanize transforms
through the generated FL Studio script workflow after its one-time manual
**Postfader Apply** handshake.

**You get:** A prepared transform and an honest dispatch result. Because FL
Studio does not expose controller-side note readback, PostFader does not claim
the resulting notes were independently verified.

### Prepare an arrangement scaffold

**You ask:** “Find empty patterns for the main sections, prepare them one at a
time, label the Playlist tracks, and add section markers.”

**PostFader uses:** Discovery and preparation of FL-reported empty patterns,
Playlist track naming and color, and section-marker creation.

**You get:** A clearer starting structure for arranging. Marker creation can
read back the name but not the marker time. PostFader does not claim to create,
move, or delete Playlist clips through the current backend.

---

## Install PostFader

Download PostFader, extract it to a stable writable folder, run the guided
installer, select the virtual MIDI endpoint you created, complete one FL Studio
MIDI setup stage—Input, Output, Universal Bridge, matching port, and script
reload—and connect your AI client.

- [Download for Windows](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-Windows.zip)
- [Download for macOS](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-macOS.zip)
- [Read the complete setup guide](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/setup.md)

Dedicated Codex packages add guided Codex registration. The Claude Desktop
MCPB is an optional wrapper after platform setup. Advanced users can install
the Python wheel or source archive. The packages do not bundle FL Studio,
Python, or a virtual MIDI provider. Every path still requires the matching
Universal Bridge, a user-created virtual MIDI endpoint, and FL Studio MIDI
configuration.

PostFader v0.20 supports Python 3.10–3.14, FL Studio 2026 version 26.1.3 build
5336 or newer, and MIDI scripting API 44 or newer. Live qualification is
limited to macOS 27.0 arm64 with FL Studio Producer Edition 26.1.3 build 5336
and Windows 11 x64 with FL Studio Producer Edition 26.1.4 build 5589; it is not
a claim about every build, plug-in, or workstation.

---

## Use the AI client you already prefer

PostFader works with Claude Desktop, Claude Code, Codex, Cursor, OpenCode, Grok
Build, T3 Code through an MCP-capable provider, and other compatible local
`stdio` MCP hosts. There is no PostFader account and no hosted PostFader
service.

Grok on the web and Grok Bot require a remote HTTP MCP server and cannot use
the current local PostFader packages directly.

PostFader itself runs locally, but the AI client is a separate trust boundary
and may send tool arguments and results to its model provider. Users should
review their AI client's privacy policy before opening sensitive projects.

---

## Built for real projects without pretending FL Studio exposes more than it does

- Starts read-only.
- Write access lasts only for the current session.
- Never saves the project automatically.
- Reads supported direct changes back from FL Studio.
- Labels workflows that cannot be fully confirmed.
- Runs locally with no PostFader account or PostFader telemetry.

When PostFader says a supported change landed, it checks what FL Studio reports
afterward. That proves the observed control state—not artistic quality,
guaranteed rollback, or a guaranteed undo point.

**Technical details:** [What “verified” means](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/tool-contracts.md#write-response-contract)
· [Security policy](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/SECURITY.md)
· [FL Studio API limitations](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/fl-constraints.md)
· [Tool contracts](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/tool-contracts.md)

---

## Final call to action

### Give your AI a place in the FL Studio workflow.

Inspect the project. Diagnose the mix. Build the parts. Review the plan. Make
the change.

[Download for Windows](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-Windows.zip)
·
[Download for macOS](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest/download/PostFader-v0.20.0-macOS.zip)
·
[Read the setup guide](https://github.com/synopsys0/postfader-fl-studio-mcp/blob/main/docs/setup.md)

PostFader is an unofficial community project and is not made by, endorsed by,
or affiliated with Image-Line.
