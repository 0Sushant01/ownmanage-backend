import os
from datetime import date, timedelta, time
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee, EmployeeAssignment, EmploymentStatus
)
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory,
    CentreCapacityAllocation, Broker, Referral, Commission,
    SubscriptionAction
)
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceStatus, AttendanceEventType,
    AttendanceEventSource, WorkSchedule, WorkScheduleDay, EmployeeScheduleAssignment
)
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus, LeavePolicy, LeaveBalance
from apps.payroll.models import SalaryStructure, SalaryComponent, Payroll, PayrollStatus
from apps.core.models import Announcement, Notification, NotificationType


class Command(BaseCommand):
    help = 'Seeds safe development data for testing platform with Broker, Plans, Flexible Centre Capacity, and Enterprise hierarchy.'

    def handle(self, *args, **options):
        self.stdout.write("Seeding development data...")
        dev_password = os.getenv('DEV_SEED_PASSWORD', 'Dev@123456')

        with transaction.atomic():
            # 1. SuperAdmin
            superadmin, _ = User.objects.get_or_create(
                email='superadmin@ownmanage.in',
                defaults={
                    'first_name': 'Global',
                    'last_name': 'SuperAdmin',
                    'is_staff': True,
                    'is_superuser': True,
                    'is_active': True,
                }
            )
            superadmin.set_password(dev_password)
            superadmin.is_staff = True
            superadmin.is_superuser = True
            superadmin.save()
            self.stdout.write(f"SuperAdmin ready: {superadmin.email}")

            # 2. Broker
            broker_user, _ = User.objects.get_or_create(
                email='broker@ownmanage.in',
                defaults={
                    'first_name': 'Prime',
                    'last_name': 'Brokers',
                    'is_active': True,
                }
            )
            broker_user.set_password(dev_password)
            broker_user.save()

            broker, _ = Broker.objects.get_or_create(
                user=broker_user,
                defaults={
                    'name': 'Prime Enterprise Advisors',
                    'referral_code': 'BROKER2026',
                    'commission_rate': Decimal('12.50'),
                    'is_active': True,
                }
            )
            self.stdout.write(f"Broker ready: {broker.name} ({broker.referral_code})")

            # 3. Commercial Subscription Plans
            starter_plan, _ = Plan.objects.get_or_create(
                name='Starter',
                defaults={
                    'monthly_charge': Decimal('4999.00'),
                    'max_centres': 2,
                    'total_employee_capacity': 50,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': False},
                    'is_active': True,
                }
            )
            pro_plan, _ = Plan.objects.get_or_create(
                name='Professional',
                defaults={
                    'monthly_charge': Decimal('12999.00'),
                    'max_centres': 5,
                    'total_employee_capacity': 300,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': True, 'advanced_payroll': True},
                    'is_active': True,
                }
            )
            ent_plan, _ = Plan.objects.get_or_create(
                name='Enterprise',
                defaults={
                    'monthly_charge': Decimal('29999.00'),
                    'max_centres': 15,
                    'total_employee_capacity': 1000,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': True, 'advanced_payroll': True, 'custom_workflows': True},
                    'is_active': True,
                }
            )
            self.stdout.write("Commercial plans ready: Starter, Professional, Enterprise")

            # 4. Enterprise (Acme Corp)
            biz, _ = Business.objects.get_or_create(
                name='Acme Enterprises',
                defaults={
                    'legal_name': 'Acme Enterprises Private Limited',
                    'email': 'contact@acme.com',
                    'phone': '+91 9876543210',
                    'city': 'Bengaluru',
                    'state': 'Karnataka',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'ACM',
                    'employee_id_next_number': 8,
                }
            )

            # 5. Permanent Broker Referral
            Referral.objects.get_or_create(
                business=biz,
                defaults={
                    'broker': broker,
                    'referral_code_used': 'BROKER2026',
                }
            )

            # 6. Active Subscription for Acme (Professional Plan)
            today = date.today()
            sub, _ = Subscription.objects.get_or_create(
                business=biz,
                defaults={
                    'plan': pro_plan,
                    'status': 'ACTIVE',
                    'start_date': today - timedelta(days=60),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )

            SubscriptionHistory.objects.get_or_create(
                business=biz,
                plan=pro_plan,
                action=SubscriptionAction.INITIAL,
                defaults={
                    'monthly_charge': pro_plan.monthly_charge,
                    'max_centres': pro_plan.max_centres,
                    'total_employee_capacity': pro_plan.total_employee_capacity,
                    'effective_from': today - timedelta(days=60),
                    'reason': 'Initial referred enterprise signup'
                }
            )

            # 7. Centres with Flexible Capacity Allocation (Total sum = 300)
            centres_config = [
                ('Headquarters Centre', 'HQ-01', 100),
                ('North Hub Centre', 'NH-02', 80),
                ('East Wing Centre', 'EW-03', 60),
                ('West District Centre', 'WD-04', 40),
                ('South Tech Centre', 'ST-05', 20),
            ]
            centres = []
            for name, code, cap in centres_config:
                centre, _ = Branch.objects.get_or_create(
                    business=biz,
                    code=code,
                    defaults={'name': name, 'city': 'Bengaluru'}
                )
                centres.append(centre)
                CentreCapacityAllocation.objects.get_or_create(
                    subscription=sub,
                    centre=centre,
                    defaults={'allocated_capacity': cap}
                )

            self.stdout.write("Centres created with flexible capacity allocations totaling 300")

            # 8. Commission Records for Broker
            Commission.objects.get_or_create(
                broker=broker,
                business=biz,
                period_start=today.replace(day=1) - timedelta(days=30),
                period_end=today.replace(day=1) - timedelta(days=1),
                defaults={
                    'subscription': sub,
                    'plan': pro_plan,
                    'commission_amount': Decimal('1624.88'),
                    'status': 'PAID',
                    'payment_reference': 'TXN_COMM_90881',
                    'paid_at': timezone.now() - timedelta(days=5),
                    'notes': 'Paid monthly commission via direct bank transfer'
                }
            )
            Commission.objects.get_or_create(
                broker=broker,
                business=biz,
                period_start=today.replace(day=1),
                period_end=(today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                defaults={
                    'subscription': sub,
                    'plan': pro_plan,
                    'commission_amount': Decimal('1624.88'),
                    'status': 'PENDING',
                    'notes': 'Current period commission pending month-end closing'
                }
            )

            # 9. Departments
            dept_eng, _ = Department.objects.get_or_create(
                business=biz,
                code='ENG',
                defaults={'name': 'Engineering'}
            )
            dept_prod, _ = Department.objects.get_or_create(
                business=biz,
                code='PROD',
                defaults={'name': 'Product & Design'}
            )

            # 10. Work Schedules (Standard & Overnight)
            general_sched, _ = WorkSchedule.objects.get_or_create(
                business=biz,
                name='General Day Shift',
                defaults={
                    'start_time': time(9, 0),
                    'end_time': time(18, 0),
                    'is_overnight': False,
                    'grace_period_minutes': 15,
                    'is_business_default': True,
                }
            )
            for day_idx in range(5):
                WorkScheduleDay.objects.get_or_create(
                    schedule=general_sched,
                    day_of_week=day_idx,
                    defaults={'is_work_day': True}
                )

            night_sched, _ = WorkSchedule.objects.get_or_create(
                business=biz,
                name='Overnight Operations Shift',
                defaults={
                    'start_time': time(22, 0),
                    'end_time': time(6, 0),
                    'is_overnight': True,
                    'grace_period_minutes': 10,
                    'is_business_default': False,
                }
            )

            # 11. Business Admin User
            admin_user, _ = User.objects.get_or_create(
                email='admin@acme.com',
                defaults={'first_name': 'Aarav', 'last_name': 'Mehta'}
            )
            admin_user.set_password(dev_password)
            admin_user.save()
            BusinessMembership.objects.get_or_create(
                business=biz,
                user=admin_user,
                defaults={'role': BusinessRole.BUSINESS_ADMIN}
            )

            # 12. Managers
            mgr1_user, _ = User.objects.get_or_create(
                email='manager1@acme.com',
                defaults={'first_name': 'Vikram', 'last_name': 'Singh'}
            )
            mgr1_user.set_password(dev_password)
            mgr1_user.save()
            BusinessMembership.objects.get_or_create(
                business=biz,
                user=mgr1_user,
                defaults={'role': BusinessRole.MANAGER}
            )
            mgr1_emp, _ = Employee.objects.get_or_create(
                business=biz,
                user=mgr1_user,
                defaults={
                    'first_name': 'Vikram',
                    'last_name': 'Singh',
                    'email': 'manager1@acme.com',
                    'designation': 'Engineering Manager',
                    'branch': centres[0],
                    'department': dept_eng,
                    'joining_date': date(2023, 1, 15),
                    'employee_id': 'ACM001',
                }
            )

            mgr2_user, _ = User.objects.get_or_create(
                email='manager2@acme.com',
                defaults={'first_name': 'Priya', 'last_name': 'Sharma'}
            )
            mgr2_user.set_password(dev_password)
            mgr2_user.save()
            BusinessMembership.objects.get_or_create(
                business=biz,
                user=mgr2_user,
                defaults={'role': BusinessRole.MANAGER}
            )
            mgr2_emp, _ = Employee.objects.get_or_create(
                business=biz,
                user=mgr2_user,
                defaults={
                    'first_name': 'Priya',
                    'last_name': 'Sharma',
                    'email': 'manager2@acme.com',
                    'designation': 'Product Lead',
                    'branch': centres[1],
                    'department': dept_prod,
                    'joining_date': date(2023, 2, 1),
                    'employee_id': 'ACM002',
                }
            )

            # 13. Staff
            staff_data = [
                ('staff1@acme.com', 'Rahul', 'Verma', 'Frontend Engineer', centres[0], dept_eng, mgr1_emp, 'ACM003'),
                ('staff2@acme.com', 'Ananya', 'Iyer', 'Backend Engineer', centres[0], dept_eng, mgr1_emp, 'ACM004'),
                ('staff3@acme.com', 'Karan', 'Patel', 'QA Engineer', centres[0], dept_eng, mgr1_emp, 'ACM005'),
                ('staff4@acme.com', 'Sneha', 'Reddy', 'UI/UX Designer', centres[1], dept_prod, mgr2_emp, 'ACM006'),
                ('staff5@acme.com', 'Rohan', 'Gupta', 'Product Analyst', centres[1], dept_prod, mgr2_emp, 'ACM007'),
            ]
            staff_employees = []
            for email, fname, lname, desig, centre, dept, manager, emp_code in staff_data:
                u, _ = User.objects.get_or_create(
                    email=email,
                    defaults={'first_name': fname, 'last_name': lname}
                )
                u.set_password(dev_password)
                u.save()
                BusinessMembership.objects.get_or_create(
                    business=biz,
                    user=u,
                    defaults={'role': BusinessRole.STAFF}
                )
                emp, _ = Employee.objects.get_or_create(
                    business=biz,
                    user=u,
                    defaults={
                        'first_name': fname,
                        'last_name': lname,
                        'email': email,
                        'designation': desig,
                        'branch': centre,
                        'department': dept,
                        'manager': manager,
                        'joining_date': date(2023, 3, 1),
                        'employee_id': emp_code,
                    }
                )
                staff_employees.append(emp)

            # 14. Leave Types & Policies
            casual_leave, _ = LeaveType.objects.get_or_create(
                business=biz,
                code='CL',
                defaults={'name': 'Casual Leave', 'is_paid': True}
            )
            sick_leave, _ = LeaveType.objects.get_or_create(
                business=biz,
                code='SL',
                defaults={'name': 'Sick Leave', 'is_paid': True}
            )
            LeavePolicy.objects.get_or_create(
                business=biz,
                leave_type=casual_leave,
                defaults={'annual_days': 12, 'carry_forward_days': 3, 'effective_from': date(2026, 1, 1)}
            )
            LeavePolicy.objects.get_or_create(
                business=biz,
                leave_type=sick_leave,
                defaults={'annual_days': 8, 'carry_forward_days': 0, 'effective_from': date(2026, 1, 1)}
            )

            # 15. Leave Balances for Staff
            for emp in staff_employees:
                LeaveBalance.objects.get_or_create(
                    employee=emp,
                    leave_type=casual_leave,
                    year=today.year,
                    defaults={'allocated_days': Decimal('12.0'), 'used_days': Decimal('2.0'), 'pending_days': Decimal('0.0')}
                )
                LeaveBalance.objects.get_or_create(
                    employee=emp,
                    leave_type=sick_leave,
                    year=today.year,
                    defaults={'allocated_days': Decimal('8.0'), 'used_days': Decimal('1.0'), 'pending_days': Decimal('0.0')}
                )

            # 16. Sample Pending Leave for Staff 1
            LeaveRequest.objects.get_or_create(
                business=biz,
                employee=staff_employees[0],
                leave_type=casual_leave,
                start_date=today + timedelta(days=7),
                end_date=today + timedelta(days=8),
                defaults={'reason': 'Attending family function', 'status': LeaveRequestStatus.PENDING}
            )

            self.stdout.write(self.style.SUCCESS("Successfully seeded comprehensive platform development data!"))
