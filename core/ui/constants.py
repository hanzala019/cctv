"""
core.ui.constants

Tuning values shared across the UI layer.

These lived at the top of the old monolithic main.py. When that file was
split, each new module would otherwise have carried its own copy --
which is how you end up with two modules disagreeing about what
DETECTION_BOX_TTL_SECONDS means. One definition, imported.

Runtime caches (the icon pixmaps) deliberately do NOT live here: they
belong next to the functions that own them, in widgets/icons.py.
"""

#: UI refresh rate. ~30 FPS. Everything in MainWindow._poll runs this
#: often, per visible tile -- see GUIDELINE.md §9 before adding to it.
POLL_INTERVAL_MS = 33

#: Motion glyph edge length in px. The caption bar is small; keep the
#: glyph compact enough that the camera name still fits beside it.
MOTION_ICON_SIZE = 16

#: How long a detection box stays drawn after its last refresh.
#:
#: Detection runs about once a second while decode runs at ~30 FPS, so
#: without a hold time the boxes would visibly strobe. This is
#: deliberately longer than the detection interval: a box outliving the
#: object by a moment looks like tracking lag, whereas a box vanishing
#: between inferences looks broken.
DETECTION_BOX_TTL_SECONDS = 12.0
