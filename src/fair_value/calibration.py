"""
Model calibration pipeline.

Compares past fair_up predictions to actual market resolutions.
Computes Brier score, calibration bias, and updates the model's bias_correction.

Run periodically (e.g. daily) to keep the model in sync with market reality.
"""
import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    n_samples: int
    brier_score: float       # lower is better; random = 0.25, perfect = 0.0
    bias: float              # positive = model over-estimates P(UP)
    accuracy: float          # fraction of correct binary predictions
    calibration_bins: list[tuple[float, float, int]]  # (predicted, actual, count)


def calibrate(predictions: list[float], outcomes: list[int]) -> CalibrationResult:
    """
    Args:
        predictions: list of fair_up values at time of prediction (0–1)
        outcomes: list of 1 (UP resolved) or 0 (DOWN resolved)

    Returns CalibrationResult.
    """
    assert len(predictions) == len(outcomes), "Mismatched lengths"
    n = len(predictions)
    if n == 0:
        return CalibrationResult(0, 0.0, 0.0, 0.0, [])

    # Brier score: mean squared error
    brier = sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / n

    # Bias: mean predicted - mean actual
    bias = sum(predictions) / n - sum(outcomes) / n

    # Accuracy: treat >0.5 as UP prediction
    correct = sum(1 for p, o in zip(predictions, outcomes) if (p > 0.5) == bool(o))
    accuracy = correct / n

    # Calibration bins (10 buckets)
    bins: dict[int, list] = {i: [] for i in range(10)}
    for p, o in zip(predictions, outcomes):
        bucket = min(9, int(p * 10))
        bins[bucket].append((p, o))

    calibration_bins = []
    for i in range(10):
        items = bins[i]
        if not items:
            continue
        mean_pred = sum(p for p, _ in items) / len(items)
        mean_actual = sum(o for _, o in items) / len(items)
        calibration_bins.append((mean_pred, mean_actual, len(items)))

    return CalibrationResult(
        n_samples=n,
        brier_score=brier,
        bias=bias,
        accuracy=accuracy,
        calibration_bins=calibration_bins,
    )


def recommend_bias_correction(result: CalibrationResult) -> float:
    """
    Return an additive correction to apply to fair_up predictions.
    If the model over-estimates by 3%, returns -0.03.
    """
    # Cap at ±0.10 to avoid overcorrecting
    return max(-0.10, min(0.10, -result.bias))


def log_calibration_report(result: CalibrationResult) -> None:
    log.info(
        "Calibration: n=%d brier=%.4f bias=%+.3f accuracy=%.1f%%",
        result.n_samples, result.brier_score, result.bias, result.accuracy * 100,
    )
    if result.brier_score > 0.25:
        log.warning("Brier score > 0.25 (worse than random). Model may need refit.")
    if abs(result.bias) > 0.03:
        log.warning("Systematic bias of %.1f%% detected. Apply correction.", result.bias * 100)
    for pred, actual, count in result.calibration_bins:
        log.debug("  bin predicted=%.2f actual=%.2f n=%d", pred, actual, count)
