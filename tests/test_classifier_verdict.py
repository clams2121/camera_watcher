from clip_classifier.detector import Detection
from clip_classifier.verdict import evaluate_verdict

THRESHOLDS = {
    "high_confidence": 0.5,
    "review_large_object_area_frac": 0.05,
    "review_persistent_detection_frac": 0.6,
    "review_persistent_motion_detection_size": 0.05,
    "review_persistent_motion_frame_ratio": 0.6,
}


def _det(label, confidence, box=(0.1, 0.1, 0.1, 0.1)):
    return Detection(label=label, confidence=confidence, box_norm=box)


def test_high_verdict_for_a_confident_person_detection():
    frame_detections = [(2.0, [_det("person", 0.7)])]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"
    assert result.reason == "person>=0.5"


def test_high_verdict_for_a_confident_vehicle_detection():
    frame_detections = [(2.0, [_det("car", 0.6)])]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"
    assert result.reason == "car>=0.5"


def test_high_verdict_for_a_confident_animal_detection():
    frame_detections = [(2.0, [_det("dog", 0.55)])]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"
    assert result.reason == "dog>=0.5"


def test_high_verdict_triggers_from_any_sampled_frame_not_just_the_first():
    frame_detections = [
        (1.0, [_det("bird", 0.3, box=(0, 0, 0.01, 0.01))]),  # small, non-target-irrelevant, low conf
        (5.0, [_det("person", 0.9)]),  # this one qualifies
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"


def test_target_class_below_confidence_threshold_does_not_trigger_high():
    frame_detections = [(2.0, [_det("person", 0.4)])]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict != "high"


def test_review_for_a_large_non_target_object():
    frame_detections = [(2.0, [_det("kite", 0.6, box=(0.1, 0.1, 0.3, 0.3))])]  # 0.09 area >= 0.05
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "review"
    assert result.reason == "large_other:kite"


def test_small_non_target_object_does_not_trigger_large_other_review():
    # Spread across enough frames (only 1 of 5 has a detection at all) that
    # the persistent_detection rule doesn't also independently fire --
    # isolates "small box -> no large_other" from the other review rule.
    frame_detections = [
        (1.0, [_det("umbrella", 0.6, box=(0.1, 0.1, 0.05, 0.05))]),  # 0.0025 area < 0.05
        (2.0, []),
        (3.0, []),
        (4.0, []),
        (5.0, []),
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "low"


def test_review_for_a_persistent_detection_across_most_frames():
    frame_detections = [
        (1.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (2.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (3.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (4.0, []),  # 3 of 4 frames have a detection -> 0.75 >= 0.6
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "review"
    assert result.reason == "persistent_detection"


def test_persistent_detection_below_the_frame_ratio_threshold_is_not_review():
    frame_detections = [
        (1.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (2.0, []),
        (3.0, []),
        (4.0, []),  # only 1 of 4 -> 0.25 < 0.6
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "low"


def test_review_for_persistent_motion_with_no_detections_at_all():
    frame_detections = [(1.0, []), (2.0, []), (3.0, [])]
    recorder_metadata = {"detection_size": 0.1, "motion_confidence": {"motion_frame_ratio": 0.8}}
    result = evaluate_verdict(frame_detections, recorder_metadata, THRESHOLDS)
    assert result.verdict == "review"
    assert result.reason == "persistent_motion_no_detection"


def test_persistent_motion_below_thresholds_with_no_detections_is_low():
    frame_detections = [(1.0, []), (2.0, [])]
    recorder_metadata = {"detection_size": 0.01, "motion_confidence": {"motion_frame_ratio": 0.2}}
    result = evaluate_verdict(frame_detections, recorder_metadata, THRESHOLDS)
    assert result.verdict == "low"


def test_persistent_motion_rule_requires_both_size_and_ratio_thresholds():
    # detection_size alone clears the bar, motion_frame_ratio doesn't
    frame_detections = [(1.0, [])]
    recorder_metadata = {"detection_size": 0.2, "motion_confidence": {"motion_frame_ratio": 0.1}}
    result = evaluate_verdict(frame_detections, recorder_metadata, THRESHOLDS)
    assert result.verdict == "low"


def test_low_verdict_when_nothing_notable_at_all():
    frame_detections = [(1.0, []), (2.0, [])]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "low"
    assert result.reason == "no_target_or_notable_detections"


def test_high_takes_priority_over_review_conditions_present_in_the_same_clip():
    frame_detections = [
        (1.0, [_det("kite", 0.6, box=(0.1, 0.1, 0.3, 0.3))]),  # would independently trigger review
        (2.0, [_det("person", 0.9)]),  # but this triggers high, which is checked first
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"


def test_large_other_review_rule_takes_priority_over_persistent_detection_rule():
    # every frame has a small, non-target detection (would trigger
    # persistent_detection) AND one frame also has a large non-target
    # detection (large_other) -- large_other is checked first.
    frame_detections = [
        (1.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (2.0, [_det("kite", 0.6, box=(0.1, 0.1, 0.3, 0.3))]),
        (3.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.reason == "large_other:kite"


def test_labels_are_recorded_regardless_of_verdict():
    frame_detections = [
        (1.0, [_det("bird", 0.3, box=(0, 0, 0.02, 0.02))]),
        (2.0, []),
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "low"
    assert len(result.labels) == 1
    assert result.labels[0]["label"] == "bird"
    assert result.labels[0]["frame_offset"] == 1.0
    assert result.labels[0]["box"] == [0, 0, 0.02, 0.02]


def test_labels_include_every_detection_across_every_frame_for_high_verdict():
    frame_detections = [
        (1.0, [_det("bird", 0.3), _det("person", 0.9)]),
        (2.0, [_det("car", 0.55)]),
    ]
    result = evaluate_verdict(frame_detections, {}, THRESHOLDS)
    assert result.verdict == "high"
    assert len(result.labels) == 3


def test_empty_frame_detections_list_falls_back_to_low_without_metadata():
    result = evaluate_verdict([], {}, THRESHOLDS)
    assert result.verdict == "low"


def test_empty_frame_detections_list_with_persistent_motion_metadata_is_review():
    recorder_metadata = {"detection_size": 0.2, "motion_confidence": {"motion_frame_ratio": 0.9}}
    result = evaluate_verdict([], recorder_metadata, THRESHOLDS)
    assert result.verdict == "review"
    assert result.reason == "persistent_motion_no_detection"


def test_custom_high_confidence_threshold_is_reflected_in_the_reason_string():
    thresholds = dict(THRESHOLDS, high_confidence=0.7)
    frame_detections = [(1.0, [_det("person", 0.75)])]
    result = evaluate_verdict(frame_detections, {}, thresholds)
    assert result.reason == "person>=0.7"
