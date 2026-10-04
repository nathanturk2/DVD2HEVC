> UHD-BD is the current default and uses stock VLC with Java; no VLC patch is needed. This document describes the legacy HEVC DVD output or the intermediate DVD encoding stage. See [UHD_BD.md](UHD_BD.md) and the [current README](../README.md).

# Compatibility invariants and repair policy

DVD2HEVC compatibility changes are expressed as DVD-wide invariants, never as
disc-title checks. No production branch may select behavior because an input is
named *Taken*, *Bourne*, *Terminator*, or any other fixture.

## Required invariants

1. The source must be a readable, decrypted DVD backup. Every referenced sector
   lies inside its authored domain and every source VOB sector passes the full
   structural scan.
2. The physical cell graph is authoritative. Repeated logical references reuse
   one physical encode; alternate/interleaved extents are never duplicated or
   written into another branch.
3. Every video-bearing VOBU is independently reachable at its DVD random-access
   boundary and carries valid HEVC signalling, headers, timestamps and NAV
   relationships.
4. Compaction may move VOBUs and domains, but all affected VMGI/VTSI, PGC,
   C_ADT, VOBU_ADMAP, title-set start, NAV forward/backward and UDF references
   must be relocated together and revalidated.
5. Non-video data is byte-preserved unless an explicit audio policy rebuilds a
   selected title stream. Audio ordinals, languages, timing grids and IFO
   descriptors remain internally consistent.
6. Menu still guards are selected by independently measured cell/picture
   structure. They are never enabled for a particular disc name.
7. A final ISO passes exact UDF-file comparison, physical-graph comparison,
   complete HEVC decode/random-access gates and CSS-safe PSM audit before its job
   can be `passed`.

## Repair boundary

An in-place repair is allowed only when all of the following are true:

- the defect is confined to deterministic metadata or signalling bytes;
- the repair has an explicit byte-change allowlist;
- video/audio elementary payloads and ISO size remain unchanged;
- the repaired image repeats the structural, graph, decode and signalling gates;
- the original is retained until the repaired temporary image passes.

The current CSS-safe PSM repair satisfies this boundary. IFO/NAV layout changes,
timestamp-grid changes, missing access units and audio repacketization generally
require rebuilding from retained intermediates or reconverting; they must not be
marketed as safe in-place repairs merely to save time.

Every new compatibility fix requires a synthetic/unit regression where
possible, a media-free compatibility-history entry, and a difficult-disc gate
when private media is still available.
