"""
Face Enrollment & Image Validation Service.
Implements the canonical enrollment pipeline:
Image -> Face Detection -> Exactly 1 Face -> Quality Check -> 112x112 Preprocessing -> MobileFaceNet -> 128-D L2 Embedding -> Encrypted Template.
"""
import io
import os
import logging
from typing import Tuple, Dict, Any, List, Optional
import numpy as np
from PIL import Image
import onnxruntime as ort
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.biometrics.models import (
    EmployeeFaceEnrollment,
    BiometricStatus,
    BiometricAuditEvent,
    BiometricAuditEventType
)
from apps.biometrics.services.model_registry import ModelRegistry
from apps.biometrics.services.template_service import TemplateService

logger = logging.getLogger(__name__)


def _iou_of(boxes0: np.ndarray, boxes1: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    overlap_left_top = np.maximum(boxes0[..., :2], boxes1[..., :2])
    overlap_right_bottom = np.minimum(boxes0[..., 2:], boxes1[..., 2:])
    hw = np.clip(overlap_right_bottom - overlap_left_top, 0.0, None)
    overlap_area = hw[..., 0] * hw[..., 1]
    hw0 = np.clip(boxes0[..., 2:] - boxes0[..., :2], 0.0, None)
    area0 = hw0[..., 0] * hw0[..., 1]
    hw1 = np.clip(boxes1[..., 2:] - boxes1[..., :2], 0.0, None)
    area1 = hw1[..., 0] * hw1[..., 1]
    return overlap_area / (area0 + area1 - overlap_area + eps)


def _hard_nms(box_scores: np.ndarray, iou_threshold: float = 0.3, top_k: int = -1, candidate_size: int = 200) -> np.ndarray:
    scores = box_scores[:, -1]
    boxes = box_scores[:, :-1]
    picked = []
    indexes = np.argsort(scores)[-candidate_size:]
    while len(indexes) > 0:
        current = indexes[-1]
        picked.append(current)
        if 0 < top_k == len(picked) or len(indexes) == 1:
            break
        current_box = boxes[current, :]
        indexes = indexes[:-1]
        rest_boxes = boxes[indexes, :]
        iou = _iou_of(rest_boxes, np.expand_dims(current_box, axis=0))
        indexes = indexes[iou <= iou_threshold]
    return box_scores[picked, :]


class EnrollmentService:
    _detector_session: Optional[ort.InferenceSession] = None
    _recognition_session: Optional[ort.InferenceSession] = None

    @classmethod
    def _get_detector_session(cls) -> ort.InferenceSession:
        if cls._detector_session is None:
            path = ModelRegistry.get_detector_model_path()
            if not os.path.exists(path):
                raise RuntimeError(f"Detector model not found at {path}")
            # Suppress default onnxruntime verbose logging
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            cls._detector_session = ort.InferenceSession(path, sess_options=opts)
        return cls._detector_session

    @classmethod
    def _get_recognition_session(cls) -> ort.InferenceSession:
        if cls._recognition_session is None:
            path = ModelRegistry.get_recognition_model_path()
            if not os.path.exists(path):
                raise RuntimeError(f"Recognition model not found at {path}")
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            cls._recognition_session = ort.InferenceSession(path, sess_options=opts)
        return cls._recognition_session

    @classmethod
    def detect_faces(cls, pil_image: Image.Image) -> List[Tuple[float, float, float, float, float]]:
        """
        Runs UltraFace-RFB-320 detection.
        Returns list of (x1, y1, x2, y2, score) in original image coordinates.
        """
        sess = cls._get_detector_session()
        orig_w, orig_h = pil_image.size

        # Resize to 320x240 RGB
        resized = pil_image.resize((320, 240))
        arr = np.array(resized, dtype=np.float32)
        # Preprocessing: (x - 127.0) / 128.0
        arr = (arr - 127.0) / 128.0
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, 0)

        scores, boxes = sess.run(None, {'input': arr})
        scores = scores[0]
        boxes = boxes[0]

        # Class index 1 is face
        face_scores = scores[:, 1]
        mask = face_scores > 0.65
        face_scores = face_scores[mask]
        subset_boxes = boxes[mask]

        if len(face_scores) == 0:
            return []

        box_scores = np.concatenate([subset_boxes, face_scores.reshape(-1, 1)], axis=1)
        picked = _hard_nms(box_scores, iou_threshold=0.3)

        detected = []
        for b in picked:
            x1 = max(0.0, float(b[0] * orig_w))
            y1 = max(0.0, float(b[1] * orig_h))
            x2 = min(float(orig_w), float(b[2] * orig_w))
            y2 = min(float(orig_h), float(b[3] * orig_h))
            sc = float(b[4])
            detected.append((x1, y1, x2, y2, sc))
        return detected

    @classmethod
    def validate_and_crop_face(cls, pil_image: Image.Image) -> Tuple[Image.Image, float]:
        """
        Validates image quality:
        - Exactly one face
        - Minimum dimensions (70x70)
        - Brightness / lighting
        - Sharpness / blur (Laplacian variance)
        Returns cropped face and quality score.
        """
        faces = cls.detect_faces(pil_image)

        if len(faces) == 0:
            raise ValidationError({
                'code': 'FACE_NOT_DETECTED',
                'detail': 'No human face detected in the image. Please ensure your face is clearly visible.'
            })
        if len(faces) > 1:
            raise ValidationError({
                'code': 'MULTIPLE_FACES_DETECTED',
                'detail': f'Multiple faces detected ({len(faces)}). Enrollment requires exactly one person in the frame.'
            })

        x1, y1, x2, y2, det_score = faces[0]
        fw = x2 - x1
        fh = y2 - y1

        reqs = ModelRegistry.QUALITY_REQUIREMENTS
        if fw < reqs['min_face_width'] or fh < reqs['min_face_height']:
            raise ValidationError({
                'code': 'FACE_TOO_SMALL',
                'detail': f'Detected face is too small ({int(fw)}x{int(fh)}px). Please move closer to the camera.'
            })

        # Add 15% margin for natural boundary
        margin_x = fw * 0.15
        margin_y = fh * 0.15
        crop_x1 = max(0, x1 - margin_x)
        crop_y1 = max(0, y1 - margin_y)
        crop_x2 = min(pil_image.width, x2 + margin_x)
        crop_y2 = min(pil_image.height, y2 + margin_y)

        cropped_face = pil_image.crop((crop_x1, crop_y1, crop_x2, crop_y2))

        # Quality Check: Luminance
        gray_arr = np.array(cropped_face.convert('L'), dtype=np.float32)
        mean_brightness = float(np.mean(gray_arr))
        if mean_brightness < reqs['min_brightness']:
            raise ValidationError({
                'code': 'FACE_TOO_DARK',
                'detail': 'Image is too dark. Please enroll in a well-lit environment.'
            })
        if mean_brightness > reqs['max_brightness']:
            raise ValidationError({
                'code': 'FACE_OVEREXPOSED',
                'detail': 'Image is overexposed/too bright. Please avoid strong direct backlighting.'
            })

        # Quality Check: Sharpness via discrete Laplacian operator
        kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
        # Approximate 2D convolution for blur detection
        h, w = gray_arr.shape
        if h > 3 and w > 3:
            lap = (
                gray_arr[0:-2, 1:-1] +
                gray_arr[2:, 1:-1] +
                gray_arr[1:-1, 0:-2] +
                gray_arr[1:-1, 2:] -
                4 * gray_arr[1:-1, 1:-1]
            )
            variance = float(np.var(lap))
            if variance < reqs['min_sharpness_var']:
                raise ValidationError({
                    'code': 'FACE_BLURRY',
                    'detail': 'Image is blurry. Please hold steady and ensure camera focus.'
                })

        quality_score = round(float(det_score), 4)
        return cropped_face, quality_score

    @classmethod
    def generate_embedding(cls, face_crop: Image.Image) -> np.ndarray:
        """
        Executes MobileFaceNet ONNX inference on a 112x112 RGB face crop.
        Returns 128-dimensional L2-normalized float32 vector.
        """
        # Preprocessing Contract:
        # 1. Resize to exactly 112x112 Bilinear
        resized = face_crop.resize(
            (ModelRegistry.INPUT_WIDTH, ModelRegistry.INPUT_HEIGHT),
            Image.Resampling.BILINEAR
        ).convert('RGB')

        # 2. Pixel scaling: (x - 127.5) / 128.0
        arr = np.array(resized, dtype=np.float32)
        arr = (arr - 127.5) / 128.0

        # 3. Transpose to CHW: (1, 3, 112, 112)
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, 0)

        # 4. Inference
        sess = cls._get_recognition_session()
        outputs = sess.run(['embedding'], {'input': arr})
        raw_emb = outputs[0][0]

        # 5. Output L2 Normalization (norm = 1.0)
        norm = np.linalg.norm(raw_emb)
        if norm > 0:
            normalized_emb = raw_emb / norm
        else:
            normalized_emb = raw_emb

        return normalized_emb.astype(np.float32)

    @classmethod
    def enroll_employee_face(
        cls,
        employee,
        image_bytes: bytes,
        actor=None,
        ip_address: str = '',
        user_agent: str = ''
    ) -> EmployeeFaceEnrollment:
        """
        Full enrollment pipeline:
        1. Decodes and validates image
        2. Detects face and validates quality
        3. Generates L2-normalized 128-D MobileFaceNet embedding
        4. Encrypts embedding at rest
        5. Atomically revokes prior active enrollment (if any) and creates new enrollment record
        6. Logs immutable audit event
        """
        try:
            pil_image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        except Exception as e:
            raise ValidationError({'code': 'INVALID_IMAGE', 'detail': 'Uploaded file is not a valid image format.'})

        cropped_face, quality_score = cls.validate_and_crop_face(pil_image)
        embedding_vec = cls.generate_embedding(cropped_face)

        if len(embedding_vec) != ModelRegistry.EMBEDDING_DIMENSION:
            raise ValidationError({
                'code': 'MODEL_DIMENSION_ERROR',
                'detail': f'Generated embedding dimension {len(embedding_vec)} does not match contract {ModelRegistry.EMBEDDING_DIMENSION}.'
            })

        encrypted_ciphertext = TemplateService.encrypt_embedding(embedding_vec)

        # Check existing active enrollments
        existing = EmployeeFaceEnrollment.objects.filter(
            employee=employee,
            status=BiometricStatus.ACTIVE
        )
        is_re_enrollment = existing.exists()

        now = timezone.now()
        actor_name = actor.get_full_name() if actor else 'System'
        for prior in existing:
            prior.status = BiometricStatus.REVOKED
            prior.revoked_at = now
            prior.revocation_reason = f"Superceded by re-enrollment by {actor_name} on {now.strftime('%Y-%m-%d %H:%M')}"
            prior.save()

        # Create new enrollment
        enrollment = EmployeeFaceEnrollment.objects.create(
            employee=employee,
            model_id=ModelRegistry.FACE_MODEL_ID,
            model_version=ModelRegistry.FACE_MODEL_VERSION,
            detector_version=ModelRegistry.DETECTOR_VERSION,
            preprocessing_version=ModelRegistry.PREPROCESSING_VERSION,
            embedding_dimension=ModelRegistry.EMBEDDING_DIMENSION,
            embedding_format='FLOAT32_L2_NORMALIZED',
            encrypted_embedding=encrypted_ciphertext,
            status=BiometricStatus.ACTIVE,
            quality_score=quality_score,
            enrolled_by=actor
        )

        # Audit Event
        BiometricAuditEvent.objects.create(
            business=employee.business,
            employee=employee,
            event_type=(
                BiometricAuditEventType.RE_ENROLLMENT
                if is_re_enrollment
                else BiometricAuditEventType.ENROLLMENT
            ),
            actor=actor,
            details={
                'enrollment_id': str(enrollment.id),
                'model_id': ModelRegistry.FACE_MODEL_ID,
                'model_version': ModelRegistry.FACE_MODEL_VERSION,
                'quality_score': quality_score,
                'is_re_enrollment': is_re_enrollment,
            },
            ip_address=ip_address,
            user_agent=user_agent
        )

        return enrollment

    @classmethod
    def revoke_employee_face(
        cls,
        employee,
        actor=None,
        reason: str = '',
        ip_address: str = '',
        user_agent: str = ''
    ) -> bool:
        """
        Revokes active face enrollment for an employee.
        """
        active_enrollments = EmployeeFaceEnrollment.objects.filter(
            employee=employee,
            status=BiometricStatus.ACTIVE
        )
        if not active_enrollments.exists():
            return False

        now = timezone.now()
        for enr in active_enrollments:
            enr.status = BiometricStatus.REVOKED
            enr.revoked_at = now
            enr.revocation_reason = reason or f"Revoked by {actor.get_full_name() if actor else 'Admin'}"
            enr.save()

        BiometricAuditEvent.objects.create(
            business=employee.business,
            employee=employee,
            event_type=BiometricAuditEventType.REVOCATION,
            actor=actor,
            details={
                'reason': reason,
                'revoked_at': now.isoformat()
            },
            ip_address=ip_address,
            user_agent=user_agent
        )
        return True
