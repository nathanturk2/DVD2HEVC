# Compatibility history without retaining source ISOs

DVD2HEVC does not require a permanent nightly library of copyrighted images.
At the end of each managed job it writes a local, ignored registry under
`reports/compatibility/registry-v1.json`. The record contains no VOB or ISO
payload. It retains:

- a sampled first/last-MiB identity and size for source and output;
- converter and format/player contract versions;
- plan counts, settings, duration and terminal outcome class;
- schemas and pass states of key retained verification reports;
- an optional qualitative playback review.

The sampled identity is for rename/deduplication and regression history, not a
claim that it cryptographically authenticates every sector. Once captured it is
not discarded merely because the source ISO is later deleted.

Rebuild or inspect the history from retained jobs:

```powershell
python dvd2hevc.py compatibility-history --rebuild
python dvd2hevc.py compatibility-history
python dvd2hevc.py compatibility-history --json
```

After a manual VLC check, attach the observation to the job without changing its
automated verification result:

```powershell
python dvd2hevc.py review-job JOB_ID --result passed --note "Menus, episode and subtitles checked"
python dvd2hevc.py review-job JOB_ID --result issues-found --note "Second bonus menu stalls"
```

For machines that retain private fixtures, setting `DVD2HEVC_FIXTURE_DIR` still
enables the multi-gigabyte integration test. Public CI uses procedural sector,
navigation, audio and state-machine fixtures and never contains copyrighted
disc assets.

## 2026-09-06: resume and ISO publication regression review

Validated title-encode attempts now carry the cell cache key (source identity,
physical extents, VOBU timing, conversion settings and pipeline revision) and
the encoded file's size/mtime. Interrupted cells reuse the attempt only when
both still match and random-access validation succeeded. A changed encoded
file also invalidates a completed compact-input cell report. Old incomplete
attempts without this identity are reencoded once; existing completed cell
caches retain their previous compatibility behavior.

ISO authoring now rejects destinations inside the staged source, rejects an
empty image even when the author exits successfully, and cleans partial images
when execution or log writing fails. It rechecks destination existence before
publication so an image created during authoring is retained.

Evidence: `tests/test_resume_and_authoring.py` uses synthetic source files,
graphs and interrupted authoring operations. It verifies valid resume reuse,
invalidation after source/timing/settings/artifact changes, original-file
preservation and partial-file cleanup. No format or player contract changed.
The private difficult-disc gate was not run; `DVD2HEVC_FIXTURE_DIR` was unset.
