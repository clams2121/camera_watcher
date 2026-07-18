"""The COCO-80 class list (in the standard index order every COCO-trained
YOLO model, including Ultralytics' YOLOv8n, uses) plus the label groupings
verdict.py needs -- shared between the CPU and Hailo backends so a
detection's label means the same thing regardless of which one produced it.
"""
from __future__ import annotations

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]

PERSON_LABELS = frozenset({"person"})
VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle", "bicycle"})
ANIMAL_LABELS = frozenset(
    {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
)

# person, vehicle, or animal -- the "high" verdict's target classes (see verdict.py).
TARGET_LABELS = PERSON_LABELS | VEHICLE_LABELS | ANIMAL_LABELS
