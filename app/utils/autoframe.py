from __future__ import annotations

import subprocess
import json
from pathlib import Path

try:
    import cv2  # type: ignore[import-untyped]
except ImportError:
    cv2 = None  # type: ignore[assignment]

try:
    import numpy as np  # type: ignore[import-untyped]
except ImportError:
    np = None  # type: ignore[assignment]


def get_video_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise ValueError(f"No video stream in {path}")


def detect_crop_window(
    video_path: Path,
    target_aspect: float = 9 / 16,
    sample_fps: float = 1.0,
) -> tuple[int, int, int, int] | None:
    """
    Detect optimal crop window for 16:9 -> 9:16 conversion using MediaPipe face detection.
    Returns (x, y, w, h) crop rect or None if not needed (already vertical).
    """
    w, h = get_video_dimensions(video_path)
    src_aspect = w / h
    # If already vertical (~9:16), no crop needed
    if src_aspect <= 0.65:  # ~9:16 = 0.5625, allow tolerance
        return None
    # If not wide enough to crop, center
    # Target crop width = h * target_aspect
    crop_w = int(h * target_aspect)
    crop_h = h
    if crop_w >= w:
        return None  # no cropping space

    # Try MediaPipe
    try:
        if cv2 is None:
            raise ImportError("cv2 not available")
        import mediapipe as mp  # type: ignore[import-untyped]

        mp_face = mp.solutions.face_detection
        face_centers: list[float] = []

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return _center_crop(w, h, crop_w, crop_h)

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_interval = max(1, int(fps / sample_fps))
        idx = 0
        with mp_face.FaceDetection(model_selection=0, min_detection_confidence=0.5) as detector:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                if idx % frame_interval != 0:
                    idx += 1
                    continue
                # BGR to RGB
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = detector.process(rgb)
                if results.detections:
                    # Use first detection
                    det = results.detections[0]
                    bbox = det.location_data.relative_bounding_box
                    # Center x in relative coords
                    cx = bbox.xmin + bbox.width / 2.0
                    face_centers.append(cx * w)
                idx += 1
                if idx > 300:  # cap sampled frames
                    break
        cap.release()

        if not face_centers:
            return _center_crop(w, h, crop_w, crop_h)

        # EMA smoothing / average
        # Simple average for v1 (stable crop)
        if np is None:
            avg_cx = sum(face_centers) / len(face_centers)
        else:
            avg_cx = float(np.mean(face_centers))
        # Clamp crop_x
        crop_x = int(avg_cx - crop_w / 2)
        crop_x = max(0, min(crop_x, w - crop_w))
        return (crop_x, 0, crop_w, crop_h)

    except Exception:
        return _center_crop(w, h, crop_w, crop_h)


def _center_crop(w: int, h: int, crop_w: int, crop_h: int) -> tuple[int, int, int, int]:
    crop_x = (w - crop_w) // 2
    return (crop_x, 0, crop_w, crop_h)


def build_crop_filter(crop: tuple[int, int, int, int] | None) -> str | None:
    if crop is None:
        return None
    x, y, cw, ch = crop
    return f"crop={cw}:{ch}:{x}:{y}"
