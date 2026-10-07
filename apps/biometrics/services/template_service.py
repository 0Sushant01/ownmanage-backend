"""
Template Encryption & Provisioning Service.
Secures 128-D biometric embeddings at rest and packages device-bound templates.
Embeddings are never stored as plain text and never exposed through public APIs.
"""
import base64
import json
import numpy as np
from typing import List
from django.conf import settings
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class TemplateService:
    @staticmethod
    def _get_fernet() -> Fernet:
        """Derives a deterministic 32-byte key from SECRET_KEY using PBKDF2."""
        raw_key = getattr(settings, 'BIOMETRIC_ENCRYPTION_KEY', settings.SECRET_KEY)
        salt = b'ownmanage_biometric_salt_v1'
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
        )
        derived = base64.urlsafe_b64encode(kdf.derive(raw_key.encode('utf-8')))
        return Fernet(derived)

    @classmethod
    def encrypt_embedding(cls, embedding_vector: np.ndarray) -> str:
        """
        Encrypts a 128-D float vector into a secure ciphertext string.
        """
        if not isinstance(embedding_vector, np.ndarray):
            embedding_vector = np.array(embedding_vector, dtype=np.float32)
        raw_bytes = embedding_vector.astype(np.float32).tobytes()
        fernet = cls._get_fernet()
        ciphertext = fernet.encrypt(raw_bytes)
        return ciphertext.decode('utf-8')

    @classmethod
    def decrypt_embedding(cls, ciphertext: str) -> np.ndarray:
        """
        Decrypts an encrypted ciphertext into a 128-D numpy float32 vector.
        """
        fernet = cls._get_fernet()
        raw_bytes = fernet.decrypt(ciphertext.encode('utf-8'))
        vector = np.frombuffer(raw_bytes, dtype=np.float32)
        return vector

    @classmethod
    def prepare_device_template_payload(cls, enrollment, device) -> dict:
        """
        Prepares biometric template package provisioned specifically to an authorized device.
        """
        vector = cls.decrypt_embedding(enrollment.encrypted_embedding)
        # Format vector as list of floats for authorized device local matcher
        return {
            'enrollment_id': str(enrollment.id),
            'employee_id': str(enrollment.employee.id),
            'model_id': enrollment.model_id,
            'model_version': enrollment.model_version,
            'embedding_dimension': enrollment.embedding_dimension,
            'embedding_format': enrollment.embedding_format,
            'template_vector': vector.tolist(),
            'provisioned_to_device_id': device.device_id,
            'enrolled_at': enrollment.enrolled_at.isoformat(),
        }
