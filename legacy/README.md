# legacy

The first prototype, kept for reference only (not used by osv360):

- `decode_dji.py`: extracted quaternions from a `djmd` dump, assuming 800 Hz (actually 1000 Hz).
- `horizon_lock.py`: yaw-only Euler horizon lock on an ffmpeg dual-fisheye stitch.
- `README_old_pipeline.md`: its original notes.

What was wrong with it, and how osv360 fixes it, is in `docs/HOW_IT_WORKS.md`.
