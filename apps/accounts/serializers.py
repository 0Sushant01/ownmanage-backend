from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken
from apps.accounts.models import User
from apps.organization.models import BusinessMembership, Employee


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'phone', 'full_name', 'is_superuser', 'created_at']
        read_only_fields = ['id', 'email', 'is_superuser', 'created_at']


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'phone']


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        email = attrs.get('email', '').strip().lower()
        password = attrs.get('password')

        user = authenticate(email=email, password=password)
        if not user:
            user_exists = User.objects.filter(email=email).first()
            if user_exists and not user_exists.is_active and user_exists.check_password(password):
                raise serializers.ValidationError({'detail': 'This user account is inactive.'})
            raise serializers.ValidationError({'detail': 'Invalid email or password.'})

        attrs['user'] = user
        return attrs
