"""UI detector package: OmniParser YOLO icon detection and SoM visualization."""

from ui_detector.visualizer import visualize_som
from ui_detector.yolo_detector import YoloDetector

__all__ = ["YoloDetector", "visualize_som"]
