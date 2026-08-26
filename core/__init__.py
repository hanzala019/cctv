"""
CCTV viewer.

The OpenCV/FFmpeg capture options below MUST be set before cv2 is first
imported anywhere in the process, which is why they live at package
import time rather than inside capture/video_stream.py.

main.py sets the same variable before importing this package at all;
this is the safety net for tests, scripts and tools that import cctv.*
directly. setdefault makes both idempotent, and means an operator who
exports the variable in their environment still wins.

  rtsp_transport;tcp   TCP instead of UDP -- far fewer corrupt frames
                       on a congested network, at a little latency
  fflags;nobuffer      don't accumulate a decode buffer
  flags;low_delay      minimise decoder reordering delay
  max_delay;500000     cap demuxer delay at 0.5s
"""

import os

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000",
)
