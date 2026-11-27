"""Bounded 120-second motion transport and continuous retargeting contract."""
FPS = 30
MAX_SECONDS = 120
MAX_FRAMES = FPS * MAX_SECONDS
# Four float64 arrays at 3600 frames need about 9 MiB including NPY headers.
MAX_NPZ_BYTES = 12 * 1024 * 1024
MAX_IMPORT_BODY_BYTES = 17 * 1024 * 1024

def capabilities():
    return dict(fps=FPS,min_frames=2,max_frames=MAX_FRAMES,duration_max_seconds=MAX_SECONDS,
                import_max_bytes=MAX_NPZ_BYTES,processing='continuous; no clipping or independent chunks')
