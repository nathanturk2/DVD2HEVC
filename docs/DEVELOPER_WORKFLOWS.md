# Developer workflows

Start with the current README for normal conversion. Public workflows use `gui`, `tools`, `auto`, `start`, `queue`, `jobs`, `status` and the documented playback/verification commands. Older phase/prototype commands remain available for engineering investigations; they are not the first-run path.

## Structure and ownership

The public CLI facades remain compatible with existing scripts and tests. Focused workflow modules receive an explicit `services` context, so their filesystem/process boundaries remain injectable without duplicating converter logic.

- `queue_dispatcher.py`: exclusive work admission, dispatcher startup and worker loop.
- `watched_batches.py`: intake stability, durable discovery and rename handling.
- `command_generation.py`: full-disc runner command construction.
- `job_progress.py`: bounded/incremental progress events and duration-weighted reporting.
- `frontend.py`: public job/configuration facade, command generation and progress.
- `pipeline.py`, physical/compact authoring modules: conversion and navigation invariants.
- `quality.py`: native-duration bitrate planning and bounded FFmpeg measurement.
- `setup_tools.py` / `vlc_setup.py`: per-user inspector/player preparation.

`convert-title`, `convert-menu`, `stage-*`, `relocate-*`, physical/interleaved prototype commands and the `docs/PHASE*.md` guides describe engineering stages. The public `auto` runner composes these stages with validation gates; do not substitute an isolated prototype for a complete passing conversion.

## Shared modules

`gui_support.py`, `udf_validation.py` and `runtime_support.py` are deliberately shipped independently in both projects. Change both copies together. `SHARED_MODULES.json` records normalized AST hashes; the GUI's platform import adapter is the only excluded difference. Run `python tools/check-shared.py PATH_TO_SIBLING_REPO` before updating the manifests. No shared package installation is required.

Raw progress output is bounded. An incremental reader scans new bytes and retains compact task events across the raw tail boundary, including UTF-16 PowerShell logs. Rotation/truncation resets its cache. Subprocess output and diagnostic tails must remain bounded, and cancellation/error paths must reap their children.

## Local checks

```powershell
python -m unittest discover -s tests
python tools/check-shared.py
python tools/check-provenance.py
python tools/build-release.py --require-clean
```

The release workflow tests exported artifacts, not just the checkout. See the release guide for installed-resource checks and the private playback-matrix template.

`tools/make-demo.py` creates a 16-second FFmpeg-generated disc and can run the real converter with `--convert`. It refuses existing destinations. `tools/demo-ui.py` is solely a labelled screenshot harness with isolated state and simulated jobs. It must not be used as evidence of a real conversion.
