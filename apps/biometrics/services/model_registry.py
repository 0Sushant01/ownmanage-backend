"""
OWNManage Production Biometrics — Centralized Model Registry & Contract Manifest.
Enforces model versioning, canonical contracts, preprocessing rules, and threshold policies.
"""
import os
import hashlib
from typing import Dict, Any, Tuple


class ModelRegistry:
    # Official Model Manifest
    FACE_MODEL_ID = "OWNMANAGE-MOBILEFACENET-ARCFACE-128-V1"
    FACE_MODEL_VERSION = "1.0.0"
    BACKBONE = "MobileFaceNet"
    LOSS_METHODOLOGY = "ArcFace"
    EMBEDDING_DIMENSION = 128
    INPUT_WIDTH = 112
    INPUT_HEIGHT = 112
    COLOR_FORMAT = "RGB"
    NORMALIZATION = "L2"
    DISTANCE_METRIC = "COSINE"
    MODEL_FORMAT = "ONNX"
    PREPROCESSING_VERSION = "1.0.0"

    # Detector Manifest
    DETECTOR_MODEL_ID = "ULTRAFACE-RFB-320-V1"
    DETECTOR_VERSION = "1.0.0"
    DETECTOR_INPUT_WIDTH = 320
    DETECTOR_INPUT_HEIGHT = 240

    # Model file artifacts
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RECOGNITION_MODEL_FILENAME = "mobilefacenet_ownmanage_v1.onnx"
    RECOGNITION_MODEL_SHA256 = "f4a2bdfc9a97e8f186a3ba747337424ffa4211ec56b25449bfa96de1a276a241"

    DETECTOR_MODEL_FILENAME = "ultraface_rfb_320_v1.onnx"
    DETECTOR_MODEL_SHA256 = "34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017"

    # Validation & Threshold Policy V1
    # For L2-normalized 128-D ArcFace embeddings, cosine distance threshold:
    # Cosine Similarity = A . B (range -1.0 to 1.0)
    # Cosine >= 0.68 provides high confidence (>99.5% accuracy on LFW benchmark)
    MATCH_POLICY_V1 = {
        'similarity_metric': 'COSINE',
        'accept_threshold': 0.68,
        'review_threshold': 0.58,
        'reject_threshold': 0.50,
    }

    # Enrollment Image Quality Requirements
    QUALITY_REQUIREMENTS = {
        'min_face_width': 70,
        'min_face_height': 70,
        'min_brightness': 35.0,     # Out of 255 (reject severely dark images)
        'max_brightness': 235.0,    # Out of 255 (reject severely overexposed images)
        'min_sharpness_var': 40.0,  # Laplacian variance (reject excessively blurry images)
    }

    @classmethod
    def get_recognition_model_path(cls) -> str:
        return os.path.join(cls.BASE_DIR, "models", cls.RECOGNITION_MODEL_FILENAME)

    @classmethod
    def get_detector_model_path(cls) -> str:
        return os.path.join(cls.BASE_DIR, "models", cls.DETECTOR_MODEL_FILENAME)

    @classmethod
    def verify_recognition_model_integrity(cls) -> Tuple[bool, str]:
        path = cls.get_recognition_model_path()
        if not os.path.exists(path):
            return False, f"Model file not found at {path}"
        with open(path, "rb") as f:
            calculated_sha = hashlib.sha256(f.read()).hexdigest()
        if calculated_sha != cls.RECOGNITION_MODEL_SHA256:
            return False, f"Checksum mismatch: expected {cls.RECOGNITION_MODEL_SHA256}, got {calculated_sha}"
        return True, "Integrity verified"

    @classmethod
    def get_manifest_dict(cls) -> Dict[str, Any]:
        return {
            'model_id': cls.FACE_MODEL_ID,
            'model_version': cls.FACE_MODEL_VERSION,
            'backbone': cls.BACKBONE,
            'loss_methodology': cls.LOSS_METHODOLOGY,
            'embedding_dimension': cls.EMBEDDING_DIMENSION,
            'input_width': cls.INPUT_WIDTH,
            'input_height': cls.INPUT_HEIGHT,
            'color_format': cls.COLOR_FORMAT,
            'normalization': cls.NORMALIZATION,
            'distance_metric': cls.DISTANCE_METRIC,
            'model_format': cls.MODEL_FORMAT,
            'preprocessing_version': cls.PREPROCESSING_VERSION,
            'threshold_policy': cls.MATCH_POLICY_V1,
            'sha256': cls.RECOGNITION_MODEL_SHA256,
        }
