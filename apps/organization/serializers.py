from rest_framework import serializers
from django.db import transaction
from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee, EmployeeAssignment
)
from apps.organization.services import generate_next_employee_id


class BusinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = [
            'id', 'name', 'legal_name', 'email', 'phone',
            'address_line_1', 'address_line_2', 'city', 'state', 'postal_code', 'country',
            'timezone', 'currency',
            'employee_id_enabled', 'employee_id_prefix', 'employee_id_next_number',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class BranchSerializer(serializers.ModelSerializer):
    allocated_capacity = serializers.IntegerField(read_only=True)
    active_employees_count = serializers.IntegerField(read_only=True)
    is_active = serializers.BooleanField(default=True, required=False)

    class Meta:
        model = Branch
        fields = [
            'id', 'business', 'name', 'code', 'address', 'city', 'state', 'postal_code',
            'country', 'timezone', 'is_active', 'allocated_capacity', 'active_employees_count',
            'created_at'
        ]
        read_only_fields = ['id', 'business', 'allocated_capacity', 'active_employees_count', 'created_at']

    def create(self, validated_data):
        business = validated_data['business']
        biz_id = getattr(business, 'id', business)
        # Enforce subscription centre limit
        from apps.subscriptions.models import Subscription
        sub = Subscription.objects.filter(business_id=biz_id).select_related('plan').first()
        if sub:
            current_centres = Branch.objects.filter(business_id=biz_id, is_active=True).count()
            if current_centres >= sub.plan.max_centres:
                raise serializers.ValidationError({
                    'code': 'PLAN_CENTRE_LIMIT_EXCEEDED',
                    'detail': f"Maximum centre limit ({sub.plan.max_centres}) reached for current plan '{sub.plan.name}'. "
                              f"Upgrade your subscription to add more centres."
                })
        return super().create(validated_data)


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'business', 'name', 'code', 'description', 'is_active', 'created_at']
        read_only_fields = ['id', 'business', 'created_at']


class EmployeeListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    manager_name = serializers.CharField(source='manager.full_name', read_only=True, default='')

    class Meta:
        model = Employee
        fields = [
            'id', 'employee_id', 'first_name', 'last_name', 'full_name',
            'email', 'phone', 'designation', 'employment_status',
            'joining_date', 'department_name', 'branch_name', 'manager_name',
            'department', 'branch', 'manager'
        ]


class EmployeeDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    manager_name = serializers.CharField(source='manager.full_name', read_only=True, default='')

    class Meta:
        model = Employee
        fields = [
            'id', 'business', 'employee_id', 'first_name', 'last_name', 'full_name',
            'email', 'phone', 'designation', 'employment_status',
            'joining_date', 'date_of_exit',
            'department', 'branch', 'manager',
            'department_name', 'branch_name', 'manager_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'employee_id', 'created_at', 'updated_at']


class EmployeeCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    create_user_account = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Employee
        fields = [
            'id', 'first_name', 'last_name', 'email', 'phone',
            'designation', 'joining_date',
            'department', 'branch', 'manager',
            'password', 'create_user_account'
        ]

    def to_internal_value(self, data):
        data_dict = {}
        for k in data:
            data_dict[k] = data.get(k)
        if 'branch_id' in data_dict and 'branch' not in data_dict:
            data_dict['branch'] = data_dict.pop('branch_id')
        if 'department_id' in data_dict and 'department' not in data_dict:
            data_dict['department'] = data_dict.pop('department_id')
        if 'manager_id' in data_dict and 'manager' not in data_dict:
            data_dict['manager'] = data_dict.pop('manager_id')
        return super().to_internal_value(data_dict)

    def create(self, validated_data):
        business = self.context['business']
        password = validated_data.pop('password', None)
        create_user_account = validated_data.pop('create_user_account', False)
        branch = validated_data.get('branch')

        with transaction.atomic():
            # 1. Enforce individual centre capacity if assigned to a centre
            if branch:
                from apps.subscriptions.models import CentreCapacityAllocation
                alloc = CentreCapacityAllocation.objects.select_for_update().filter(centre=branch).first()
                if alloc:
                    current_active_in_centre = branch.employees.filter(employment_status='ACTIVE').count()
                    if current_active_in_centre >= alloc.allocated_capacity:
                        raise serializers.ValidationError({
                            'code': 'EMPLOYEE_CAPACITY_EXCEEDED',
                            'detail': f"Centre '{branch.name}' has reached its allocated capacity limit of {alloc.allocated_capacity} employees."
                        })

            # 2. Enforce total enterprise subscription capacity if subscribed
            from apps.subscriptions.models import Subscription
            sub = Subscription.objects.filter(business=business).select_related('plan').first()
            if sub:
                total_active = business.employees.filter(employment_status='ACTIVE').count()
                if total_active >= sub.plan.total_employee_capacity:
                    raise serializers.ValidationError({
                        'code': 'EMPLOYEE_CAPACITY_EXCEEDED',
                        'detail': f"Enterprise has reached total subscription plan capacity of {sub.plan.total_employee_capacity} employees."
                    })

            # Concurrency-safe employee ID generation
            generated_id = generate_next_employee_id(business)

            user_obj = None
            email = validated_data.get('email', '').strip().lower()
            if create_user_account and email:
                user_obj, _ = User.objects.get_or_create(
                    email=email,
                    defaults={
                        'first_name': validated_data.get('first_name', ''),
                        'last_name': validated_data.get('last_name', ''),
                        'phone': validated_data.get('phone', ''),
                    }
                )
                if password:
                    user_obj.set_password(password)
                    user_obj.save()

                BusinessMembership.objects.get_or_create(
                    business=business,
                    user=user_obj,
                    defaults={'role': BusinessRole.STAFF}
                )

            employee = Employee.objects.create(
                business=business,
                employee_id=generated_id,
                user=user_obj,
                **validated_data
            )

            # Record initial assignment
            EmployeeAssignment.objects.create(
                business=business,
                employee=employee,
                manager=employee.manager,
                branch=employee.branch,
                department=employee.department,
                designation=employee.designation,
                effective_from=employee.joining_date,
                notes='Initial assignment on joining'
            )

            return employee



class ManagerCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)
    designation = serializers.CharField(max_length=100, default='Manager')
    joining_date = serializers.DateField()
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all(), required=False, allow_null=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return email

    def create(self, validated_data):
        business = self.context['business']
        email = validated_data['email'].strip().lower()
        password = validated_data['password']

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=validated_data['first_name'],
                last_name=validated_data.get('last_name', ''),
                phone=validated_data.get('phone', '')
            )

            BusinessMembership.objects.create(
                business=business,
                user=user,
                role=BusinessRole.MANAGER
            )

            generated_id = generate_next_employee_id(business)

            employee = Employee.objects.create(
                business=business,
                user=user,
                employee_id=generated_id,
                first_name=validated_data['first_name'],
                last_name=validated_data.get('last_name', ''),
                email=email,
                phone=validated_data.get('phone', ''),
                designation=validated_data.get('designation', 'Manager'),
                joining_date=validated_data['joining_date'],
                department=validated_data.get('department'),
                branch=validated_data.get('branch'),
            )

            EmployeeAssignment.objects.create(
                business=business,
                employee=employee,
                branch=employee.branch,
                department=employee.department,
                designation=employee.designation,
                effective_from=employee.joining_date,
                notes='Initial manager appointment'
            )

            return employee


class BusinessCreateAdminSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return email

    def create(self, validated_data):
        business = self.context['business']
        email = validated_data['email'].strip().lower()
        password = validated_data['password']

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=validated_data['first_name'],
                last_name=validated_data.get('last_name', ''),
                phone=validated_data.get('phone', '')
            )
            membership = BusinessMembership.objects.create(
                business=business,
                user=user,
                role=BusinessRole.BUSINESS_ADMIN
            )
            return membership
