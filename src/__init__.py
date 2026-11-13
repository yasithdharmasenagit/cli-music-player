"""ASCII Music Player."""

import os

# Keep OpenCV/FFmpeg decoder chatter off the terminal (must be set before cv2 is imported).
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
