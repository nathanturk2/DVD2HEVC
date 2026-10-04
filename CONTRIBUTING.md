# Contributing

DVD2HEVC is an unusual format experiment: a change can decode cleanly in a
linear sample while breaking a menu jump, shared cell, branch, or still frame.
Keep patches focused and preserve the existing validation gates.

Before submitting a change:

```powershell
python -m compileall -q dvd2hevc.py dvd2hevc_app
python -m unittest discover -s tests -v
python dvd2hevc.py --help
python dvd2hevc.py tools
```

For playback or transport changes, test at least a title start and menu start.
Changes affecting clocks, random access, VOBU packing, navigation relocation,
or the VLC patch also need an authored jump/seek transition. Document which
fixture and route were tested.

Bug reports should attach a bundle from `python dvd2hevc.py diagnose JOB_ID`.
Do not upload DVD ISOs, VOBs, keys, copyrighted assets, or decryption logs.

Contributions must be compatible with the repository's GPL-3.0-only license.
Third-party code must retain its notices and have a compatible license.
