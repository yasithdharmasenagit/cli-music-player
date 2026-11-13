"""ASCII Music Player."""

import os

# Keep OpenCV/FFmpeg decoder chatter off the terminal (must be set before cv2 is imported).
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
# Network streams (YouTube) drop connections mid-playback: reconnect, and never hang on a dead socket.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "reconnect;1|reconnect_streamed;1|reconnect_at_eof;1|reconnect_delay_max;4|rw_timeout;8000000",
)
