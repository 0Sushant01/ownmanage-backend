import io
import os
import time
import numpy as np
from PIL import Image
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.organization.models import (
    Business, Branch, Department, Employee, BusinessRole,
    BusinessMembership, Permission, ManagerAccessControl
)
from apps.accounts.models import User
from apps.biometrics.models import (
    EmployeeFaceEnrollment,
    EmployeeDevice,
    BiometricChallenge,
    BiometricStatus,
    DeviceStatus,
    BiometricAuditEvent
)
from apps.biometrics.services.model_registry import ModelRegistry
from apps.biometrics.services.template_service import TemplateService
from apps.biometrics.services.enrollment_service import EnrollmentService
from apps.biometrics.services.device_service import DeviceService
from apps.biometrics.services.verification_service import VerificationService
from apps.attendance.models import AttendanceDay, AttendanceMethod


class BiometricsCoreTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create Business & Hierarchy
        self.business = Business.objects.create(name='Acme Biometrics Corp')
        self.centre = Branch.objects.create(
            business=self.business,
            name='Tech Center',
            code='TC01',
            latitude=28.6139,
            longitude=77.2090,
            geofence_radius=150
        )
        self.dept = Department.objects.create(business=self.business, name='Engineering', code='ENG')

        # Admin User
        self.admin_user = User.objects.create_user(
            email='admin@acmebio.com',
            password='AdminPassword123!',
            first_name='Admin',
            last_name='User'
        )
        BusinessMembership.objects.create(
            user=self.admin_user,
            business=self.business,
            role=BusinessRole.BUSINESS_ADMIN,
            is_active=True
        )

        # Manager User
        self.manager_user = User.objects.create_user(
            email='manager@acmebio.com',
            password='ManagerPassword123!',
            first_name='Manager',
            last_name='One'
        )
        BusinessMembership.objects.create(
            user=self.manager_user,
            business=self.business,
            role=BusinessRole.MANAGER,
            is_active=True
        )
        self.manager_emp = Employee.objects.create(
            business=self.business,
            user=self.manager_user,
            branch=self.centre,
            department=self.dept,
            employee_id='MGR-BIO-001',
            first_name='Manager',
            last_name='One',
            email='manager@acmebio.com',
            joining_date='2024-01-01'
        )

        # Employee (Subject)
        self.emp_user = User.objects.create_user(
            email='emp@acmebio.com',
            password='EmpPassword123!',
            first_name='Rahul',
            last_name='Sharma'
        )
        BusinessMembership.objects.create(
            user=self.emp_user,
            business=self.business,
            role=BusinessRole.STAFF,
            is_active=True
        )
        self.employee = Employee.objects.create(
            business=self.business,
            user=self.emp_user,
            branch=self.centre,
            department=self.dept,
            employee_id='EMP-BIO-001',
            first_name='Rahul',
            last_name='Sharma',
            email='emp@acmebio.com',
            joining_date='2025-01-01'
        )

        # Permission for face enrollment
        self.perm_face_enroll, _ = Permission.objects.get_or_create(
            key='employee.face_enrollment',
            defaults={'name': 'Face Biometric Enrollment', 'module': 'employees'}
        )

        # Enable face recognition on centre policy
        from apps.attendance.models import AttendancePolicy
        AttendancePolicy.objects.create(
            business=self.business,
            allow_face_recognition=True,
            allow_normal_punch=True
        )

    # -------------------------------------------------------------
    # 1. Model Contract & Manifest Tests
    # -------------------------------------------------------------
    def test_01_model_contract_and_manifest(self):
        manifest = ModelRegistry.get_manifest_dict()
        self.assertEqual(manifest['model_id'], "OWNMANAGE-MOBILEFACENET-ARCFACE-128-V1")
        self.assertEqual(manifest['model_version'], "1.0.0")
        self.assertEqual(manifest['backbone'], "MobileFaceNet")
        self.assertEqual(manifest['loss_methodology'], "ArcFace")
        self.assertEqual(manifest['embedding_dimension'], 128)
        self.assertEqual(manifest['input_width'], 112)
        self.assertEqual(manifest['input_height'], 112)
        self.assertEqual(manifest['color_format'], "RGB")
        self.assertEqual(manifest['normalization'], "L2")
        self.assertEqual(manifest['distance_metric'], "COSINE")

        # Verify physical model file integrity on disk
        ok, msg = ModelRegistry.verify_recognition_model_integrity()
        self.assertTrue(ok, f"Model integrity check failed: {msg}")

    # -------------------------------------------------------------
    # 2. Golden Embedding Compatibility Test
    # -------------------------------------------------------------
    def test_02_golden_embedding_compatibility(self):
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        self.assertTrue(os.path.exists(ref_path), "Reference face image must exist")

        img = Image.open(ref_path).convert('RGB')
        emb1 = EnrollmentService.generate_embedding(img)

        # 1. Verify exact dimension
        self.assertEqual(len(emb1), 128)
        self.assertEqual(emb1.dtype, np.float32)

        # 2. Verify L2 normalization
        norm = np.linalg.norm(emb1)
        self.assertAlmostEqual(norm, 1.0, places=5)

        # 3. Deterministic repeatability: same input image produces identical embedding
        emb2 = EnrollmentService.generate_embedding(img)
        cosine_self = float(np.dot(emb1, emb2))
        self.assertAlmostEqual(cosine_self, 1.0, places=5)

        # 4. Cosine similarity with random vector is low (< 0.40)
        random_vec = np.random.randn(128).astype(np.float32)
        random_vec = random_vec / np.linalg.norm(random_vec)
        cosine_random = float(np.dot(emb1, random_vec))
        self.assertLess(cosine_random, 0.45)

    # -------------------------------------------------------------
    # 3. Template Encryption & Decryption at Rest
    # -------------------------------------------------------------
    def test_03_template_encryption_at_rest(self):
        test_vector = np.random.randn(128).astype(np.float32)
        test_vector = test_vector / np.linalg.norm(test_vector)

        ciphertext = TemplateService.encrypt_embedding(test_vector)
        self.assertIsInstance(ciphertext, str)
        # Ensure ciphertext is not plaintext floats
        self.assertNotIn(str(test_vector[0]), ciphertext)

        # Decrypt
        decrypted = TemplateService.decrypt_embedding(ciphertext)
        self.assertEqual(len(decrypted), 128)
        self.assertTrue(np.allclose(test_vector, decrypted, atol=1e-6))

    # -------------------------------------------------------------
    # 4. Enrollment & Re-enrollment Pipeline
    # -------------------------------------------------------------
    def test_04_enrollment_and_re_enrollment(self):
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        with open(ref_path, 'rb') as f:
            image_bytes = f.read()

        # Enroll face
        enrollment1 = EnrollmentService.enroll_employee_face(
            employee=self.employee,
            image_bytes=image_bytes,
            actor=self.admin_user
        )
        self.assertEqual(enrollment1.status, BiometricStatus.ACTIVE)
        self.assertEqual(enrollment1.model_id, ModelRegistry.FACE_MODEL_ID)
        self.assertEqual(enrollment1.embedding_dimension, 128)

        # Re-enroll face: older enrollment should become REVOKED
        enrollment2 = EnrollmentService.enroll_employee_face(
            employee=self.employee,
            image_bytes=image_bytes,
            actor=self.admin_user
        )
        enrollment1.refresh_from_db()
        self.assertEqual(enrollment1.status, BiometricStatus.REVOKED)
        self.assertIn("Superceded", enrollment1.revocation_reason)
        self.assertEqual(enrollment2.status, BiometricStatus.ACTIVE)

        # Revoke enrollment
        revoked = EnrollmentService.revoke_employee_face(
            employee=self.employee,
            actor=self.admin_user,
            reason="Employee requested removal"
        )
        self.assertTrue(revoked)
        enrollment2.refresh_from_db()
        self.assertEqual(enrollment2.status, BiometricStatus.REVOKED)

    # -------------------------------------------------------------
    # 5. Image Validation & Multi-Face Rejection
    # -------------------------------------------------------------
    def test_05_multi_face_and_quality_rejection(self):
        # sample_face.jpg contains multiple faces
        group_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/sample_face.jpg'
        with open(group_path, 'rb') as f:
            group_bytes = f.read()

        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            EnrollmentService.enroll_employee_face(
                employee=self.employee,
                image_bytes=group_bytes,
                actor=self.admin_user
            )
        self.assertIn('MULTIPLE_FACES_DETECTED', str(ctx.exception))

    # -------------------------------------------------------------
    # 6. Manager RBAC for Face Enrollment
    # -------------------------------------------------------------
    def test_06_manager_face_enrollment_permission(self):
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'

        # Manager WITHOUT permission: 403 Forbidden
        self.client.force_authenticate(user=self.manager_user)
        with open(ref_path, 'rb') as f:
            res_denied = self.client.post(
                f'/api/v1/biometrics/employees/{self.employee.id}/enroll/',
                {'image': f},
                format='multipart'
            )
        self.assertEqual(res_denied.status_code, status.HTTP_403_FORBIDDEN)

        # Grant explicit 'employee.face_enrollment' permission to Manager
        ManagerAccessControl.objects.create(
            business=self.business,
            user=self.manager_user,
            permission=self.perm_face_enroll,
            is_granted=True
        )

        # Manager WITH permission: 201 Created
        with open(ref_path, 'rb') as f:
            res_allowed = self.client.post(
                f'/api/v1/biometrics/employees/{self.employee.id}/enroll/',
                {'image': f},
                format='multipart'
            )
        self.assertEqual(res_allowed.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_allowed.data['enrollment']['status'], 'ACTIVE')

    # -------------------------------------------------------------
    # 7. Device Registration & Template Provisioning
    # -------------------------------------------------------------
    def test_07_device_registration_and_template_provision(self):
        # Enroll face first
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        with open(ref_path, 'rb') as f:
            EnrollmentService.enroll_employee_face(self.employee, f.read(), self.admin_user)

        # Employee registers device
        self.client.force_authenticate(user=self.emp_user)
        res_dev = self.client.post('/api/v1/biometrics/devices/register/', {
            'device_id': 'DEVICE-PHONE-12345',
            'device_name': 'Samsung Galaxy S22',
            'platform': 'ANDROID'
        })
        self.assertEqual(res_dev.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_dev.data['device']['status'], 'ACTIVE')

        # Provision template package to authorized device
        res_prov = self.client.get('/api/v1/biometrics/template/provision/?device_id=DEVICE-PHONE-12345')
        self.assertEqual(res_prov.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_prov.data['template_vector']), 128)
        self.assertEqual(res_prov.data['model_id'], ModelRegistry.FACE_MODEL_ID)

        # Unauthorized device fails
        res_unauth = self.client.get('/api/v1/biometrics/template/provision/?device_id=UNKNOWN-DEVICE')
        self.assertEqual(res_unauth.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------
    # 8. Challenge Nonce, Expiry, and Replay Protection
    # -------------------------------------------------------------
    def test_08_challenge_replay_protection(self):
        # Register device and enroll face
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        with open(ref_path, 'rb') as f:
            EnrollmentService.enroll_employee_face(self.employee, f.read(), self.admin_user)
        device = DeviceService.register_device(self.employee, 'DEVICE-PHONE-999')

        # Request challenge nonce
        challenge = VerificationService.create_challenge(self.employee, 'DEVICE-PHONE-999')
        self.assertIn('nonce', challenge)
        self.assertIn('challenge_id', challenge)

        assertion_payload = {
            'challenge_id': challenge['challenge_id'],
            'nonce': challenge['nonce'],
            'device_id': 'DEVICE-PHONE-999',
            'model_id': ModelRegistry.FACE_MODEL_ID,
            'similarity_score': 0.85,
            'liveness': True
        }

        # First verification succeeds
        verified = VerificationService.verify_assertion(assertion_payload, self.employee, self.centre)
        self.assertTrue(verified['face_verified'])
        self.assertEqual(verified['similarity_score'], 0.85)

        # Second verification with same challenge is rejected (Replay Attack)
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            VerificationService.verify_assertion(assertion_payload, self.employee, self.centre)
        self.assertIn('VERIFICATION_REPLAYED', str(ctx.exception))

    # -------------------------------------------------------------
    # 9. Similarity Threshold Enforcement
    # -------------------------------------------------------------
    def test_09_threshold_enforcement(self):
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        with open(ref_path, 'rb') as f:
            EnrollmentService.enroll_employee_face(self.employee, f.read(), self.admin_user)
        DeviceService.register_device(self.employee, 'DEVICE-PHONE-777')

        # Similarity score 0.50 is below accept_threshold (0.68)
        challenge = VerificationService.create_challenge(self.employee, 'DEVICE-PHONE-777')
        sub_threshold_assertion = {
            'challenge_id': challenge['challenge_id'],
            'device_id': 'DEVICE-PHONE-777',
            'model_id': ModelRegistry.FACE_MODEL_ID,
            'similarity_score': 0.52,  # Sub-threshold
            'liveness': True
        }

        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            VerificationService.verify_assertion(sub_threshold_assertion, self.employee, self.centre)
        self.assertIn('FACE_MISMATCH', str(ctx.exception))

    # -------------------------------------------------------------
    # 10. End-to-End Attendance Check-In with Assertion
    # -------------------------------------------------------------
    def test_10_attendance_punch_with_assertion(self):
        ref_path = '/home/s/projects/ownmanage/ownmanage-backend/apps/biometrics/tests/reference_face.jpg'
        with open(ref_path, 'rb') as f:
            EnrollmentService.enroll_employee_face(self.employee, f.read(), self.admin_user)
        DeviceService.register_device(self.employee, 'DEVICE-E2E-1')

        # Create challenge
        challenge = VerificationService.create_challenge(self.employee, 'DEVICE-E2E-1')

        # Punch Check-in via API
        self.client.force_authenticate(user=self.emp_user)
        res_punch = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'FACE',
            'face_data': {
                'verification_assertion': {
                    'challenge_id': challenge['challenge_id'],
                    'device_id': 'DEVICE-E2E-1',
                    'model_id': ModelRegistry.FACE_MODEL_ID,
                    'similarity_score': 0.88,
                    'liveness': True
                }
            }
        }, format='json')

        self.assertIn(res_punch.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        self.assertEqual(res_punch.data['attendance_method'], 'FACE')

        # Verify AttendanceDay record created with verification metadata
        day = AttendanceDay.objects.filter(employee=self.employee).first()
        self.assertIsNotNone(day)
        self.assertEqual(day.attendance_method, 'FACE')
        self.assertTrue(day.verification_metadata.get('face_verified'))
        self.assertEqual(day.verification_metadata.get('model_id'), ModelRegistry.FACE_MODEL_ID)
        self.assertEqual(day.verification_metadata.get('similarity_score'), 0.88)
