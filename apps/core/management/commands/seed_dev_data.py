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
    SubscriptionAction, SubscriptionPayment, PaymentStatus, SubscriptionStatus
)
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceStatus, AttendanceEventType,
    AttendanceEventSource, WorkSchedule, WorkScheduleDay, EmployeeScheduleAssignment
)
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus, LeavePolicy, LeaveBalance
from apps.payroll.models import SalaryStructure, SalaryComponent, Payroll, PayrollStatus
from apps.core.models import Announcement, Notification, NotificationType


class Command(BaseCommand):
    help = 'Seeds safe development data for testing platform with all user roles, Broker, Plans, Flexible Centre Capacity, and Enterprise hierarchy.'

    def handle(self, *args, **options):
        self.stdout.write("Seeding development data with password '123456'...")
        dev_password = os.getenv('DEV_SEED_PASSWORD', '123456')

        with transaction.atomic():
            # 1. SuperAdmins (Platform Governance)
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

            ops_admin, _ = User.objects.get_or_create(
                email='ops.admin@ownmanage.in',
                defaults={
                    'first_name': 'Kavita',
                    'last_name': 'Rao',
                    'is_staff': True,
                    'is_superuser': True,
                    'is_active': True,
                }
            )
            ops_admin.set_password(dev_password)
            ops_admin.is_staff = True
            ops_admin.save()
            self.stdout.write(f"SuperAdmins ready: {superadmin.email}, {ops_admin.email}")

            # 2. Broker Partners
            broker_user1, _ = User.objects.get_or_create(
                email='broker@ownmanage.in',
                defaults={'first_name': 'Prime', 'last_name': 'Brokers', 'is_active': True}
            )
            broker_user1.set_password(dev_password)
            broker_user1.save()

            broker1, _ = Broker.objects.get_or_create(
                user=broker_user1,
                defaults={
                    'name': 'Prime Enterprise Advisors',
                    'referral_code': 'BROKER2026',
                    'commission_rate': Decimal('12.50'),
                    'is_active': True,
                }
            )

            broker_user2, _ = User.objects.get_or_create(
                email='apex.broker@ownmanage.in',
                defaults={'first_name': 'Neha', 'last_name': 'Singhal', 'is_active': True}
            )
            broker_user2.set_password(dev_password)
            broker_user2.save()

            broker2, _ = Broker.objects.get_or_create(
                user=broker_user2,
                defaults={
                    'name': 'Apex Strategic Partners',
                    'referral_code': 'APEX2026',
                    'commission_rate': Decimal('10.00'),
                    'is_active': True,
                }
            )
            self.stdout.write(f"Brokers ready: {broker1.name} (BROKER2026), {broker2.name} (APEX2026)")

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
            standard_plan, _ = Plan.objects.get_or_create(
                name='Standard',
                defaults={
                    'monthly_charge': Decimal('8999.00'),
                    'max_centres': 4,
                    'total_employee_capacity': 150,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': True},
                    'is_active': True,
                }
            )
            pro_plan, _ = Plan.objects.get_or_create(
                name='Professional',
                defaults={
                    'monthly_charge': Decimal('14999.00'),
                    'max_centres': 8,
                    'total_employee_capacity': 400,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': True, 'advanced_payroll': True},
                    'is_active': True,
                }
            )
            ent_plan, _ = Plan.objects.get_or_create(
                name='Enterprise',
                defaults={
                    'monthly_charge': Decimal('29999.00'),
                    'max_centres': 20,
                    'total_employee_capacity': 1200,
                    'features': {'attendance': True, 'leave': True, 'salary': True, 'reports': True, 'advanced_payroll': True, 'custom_workflows': True},
                    'is_active': True,
                }
            )
            self.stdout.write("Commercial plans ready: Starter, Standard, Professional, Enterprise")

            # 4. Enterprise 1: Acme Enterprises (Referred by Prime Brokers)
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
                    'employee_id_next_number': 11,
                }
            )
            Referral.objects.get_or_create(
                business=biz,
                defaults={'broker': broker1, 'referral_code_used': 'BROKER2026'}
            )

            today = date.today()
            sub, _ = Subscription.objects.get_or_create(
                business=biz,
                defaults={
                    'plan': pro_plan,
                    'status': SubscriptionStatus.ACTIVE_PAID,
                    'start_date': today - timedelta(days=180),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )
            sub.status = SubscriptionStatus.ACTIVE_PAID
            sub.save()
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

            # Acme Centres
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

            # Acme Departments
            dept_eng, _ = Department.objects.get_or_create(business=biz, code='ENG', defaults={'name': 'Engineering'})
            dept_prod, _ = Department.objects.get_or_create(business=biz, code='PROD', defaults={'name': 'Product & Design'})
            dept_ops, _ = Department.objects.get_or_create(business=biz, code='OPS', defaults={'name': 'Operations'})

            # Acme Business Admins
            admin_user, _ = User.objects.get_or_create(
                email='admin@acme.com',
                defaults={'first_name': 'Aarav', 'last_name': 'Mehta'}
            )
            admin_user.set_password(dev_password)
            admin_user.save()
            BusinessMembership.objects.get_or_create(business=biz, user=admin_user, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            hr_admin_user, _ = User.objects.get_or_create(
                email='hr.admin@acme.com',
                defaults={'first_name': 'Deepak', 'last_name': 'Joshi'}
            )
            hr_admin_user.set_password(dev_password)
            hr_admin_user.save()
            BusinessMembership.objects.get_or_create(business=biz, user=hr_admin_user, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            # Acme Managers
            mgr1_user, _ = User.objects.get_or_create(email='manager1@acme.com', defaults={'first_name': 'Vikram', 'last_name': 'Singh'})
            mgr1_user.set_password(dev_password)
            mgr1_user.save()
            BusinessMembership.objects.get_or_create(business=biz, user=mgr1_user, defaults={'role': BusinessRole.MANAGER})
            mgr1_emp, _ = Employee.objects.get_or_create(
                business=biz, user=mgr1_user,
                defaults={'first_name': 'Vikram', 'last_name': 'Singh', 'email': 'manager1@acme.com', 'designation': 'Engineering Manager', 'branch': centres[0], 'department': dept_eng, 'joining_date': date(2023, 1, 15), 'employee_id': 'ACM001'}
            )

            mgr2_user, _ = User.objects.get_or_create(email='manager2@acme.com', defaults={'first_name': 'Priya', 'last_name': 'Sharma'})
            mgr2_user.set_password(dev_password)
            mgr2_user.save()
            BusinessMembership.objects.get_or_create(business=biz, user=mgr2_user, defaults={'role': BusinessRole.MANAGER})
            mgr2_emp, _ = Employee.objects.get_or_create(
                business=biz, user=mgr2_user,
                defaults={'first_name': 'Priya', 'last_name': 'Sharma', 'email': 'manager2@acme.com', 'designation': 'Product Lead', 'branch': centres[1], 'department': dept_prod, 'joining_date': date(2023, 2, 1), 'employee_id': 'ACM002'}
            )

            mgr3_user, _ = User.objects.get_or_create(email='manager3@acme.com', defaults={'first_name': 'Neha', 'last_name': 'Kapoor'})
            mgr3_user.set_password(dev_password)
            mgr3_user.save()
            BusinessMembership.objects.get_or_create(business=biz, user=mgr3_user, defaults={'role': BusinessRole.MANAGER})
            mgr3_emp, _ = Employee.objects.get_or_create(
                business=biz, user=mgr3_user,
                defaults={'first_name': 'Neha', 'last_name': 'Kapoor', 'email': 'manager3@acme.com', 'designation': 'Operations Manager', 'branch': centres[2], 'department': dept_ops, 'joining_date': date(2023, 2, 15), 'employee_id': 'ACM008'}
            )

            # Acme Staff
            staff_data = [
                ('staff1@acme.com', 'Rahul', 'Verma', 'Frontend Engineer', centres[0], dept_eng, mgr1_emp, 'ACM003'),
                ('staff2@acme.com', 'Ananya', 'Iyer', 'Backend Engineer', centres[0], dept_eng, mgr1_emp, 'ACM004'),
                ('staff3@acme.com', 'Karan', 'Patel', 'QA Engineer', centres[0], dept_eng, mgr1_emp, 'ACM005'),
                ('staff4@acme.com', 'Sneha', 'Reddy', 'UI/UX Designer', centres[1], dept_prod, mgr2_emp, 'ACM006'),
                ('staff5@acme.com', 'Rohan', 'Gupta', 'Product Analyst', centres[1], dept_prod, mgr2_emp, 'ACM007'),
                ('staff6@acme.com', 'Pooja', 'Nair', 'Operations Associate', centres[2], dept_ops, mgr3_emp, 'ACM009'),
                ('staff7@acme.com', 'Arjun', 'Das', 'Support Specialist', centres[2], dept_ops, mgr3_emp, 'ACM010'),
            ]
            staff_employees = []
            for email, fname, lname, desig, centre, dept, manager, emp_code in staff_data:
                u, _ = User.objects.get_or_create(email=email, defaults={'first_name': fname, 'last_name': lname})
                u.set_password(dev_password)
                u.save()
                BusinessMembership.objects.get_or_create(business=biz, user=u, defaults={'role': BusinessRole.STAFF})
                emp, _ = Employee.objects.get_or_create(
                    business=biz, user=u,
                    defaults={'first_name': fname, 'last_name': lname, 'email': email, 'designation': desig, 'branch': centre, 'department': dept, 'manager': manager, 'joining_date': date(2023, 3, 1), 'employee_id': emp_code}
                )
                staff_employees.append(emp)

            # Acme Leaves & Policies
            casual_leave, _ = LeaveType.objects.get_or_create(business=biz, code='CL', defaults={'name': 'Casual Leave', 'is_paid': True})
            sick_leave, _ = LeaveType.objects.get_or_create(business=biz, code='SL', defaults={'name': 'Sick Leave', 'is_paid': True})
            LeavePolicy.objects.get_or_create(business=biz, leave_type=casual_leave, defaults={'annual_days': 12, 'carry_forward_days': 3, 'effective_from': date(2026, 1, 1)})
            LeavePolicy.objects.get_or_create(business=biz, leave_type=sick_leave, defaults={'annual_days': 8, 'carry_forward_days': 0, 'effective_from': date(2026, 1, 1)})

            for emp in staff_employees:
                LeaveBalance.objects.get_or_create(employee=emp, leave_type=casual_leave, year=today.year, defaults={'allocated_days': Decimal('12.0'), 'used_days': Decimal('2.0'), 'pending_days': Decimal('0.0')})
                LeaveBalance.objects.get_or_create(employee=emp, leave_type=sick_leave, year=today.year, defaults={'allocated_days': Decimal('8.0'), 'used_days': Decimal('1.0'), 'pending_days': Decimal('0.0')})

            LeaveRequest.objects.get_or_create(
                business=biz, employee=staff_employees[0], leave_type=casual_leave,
                start_date=today + timedelta(days=7), end_date=today + timedelta(days=8),
                defaults={'reason': 'Attending family function', 'status': LeaveRequestStatus.PENDING}
            )

            # Broker 1 Commissions from Acme
            Commission.objects.get_or_create(
                broker=broker1, business=biz,
                period_start=today.replace(day=1) - timedelta(days=30), period_end=today.replace(day=1) - timedelta(days=1),
                defaults={'subscription': sub, 'plan': pro_plan, 'commission_amount': Decimal('1624.88'), 'status': 'PAID', 'payment_reference': 'TXN_COMM_90881', 'paid_at': timezone.now() - timedelta(days=5), 'notes': 'Paid monthly commission via direct bank transfer'}
            )
            Commission.objects.get_or_create(
                broker=broker1, business=biz,
                period_start=today.replace(day=1), period_end=(today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                defaults={'subscription': sub, 'plan': pro_plan, 'commission_amount': Decimal('1624.88'), 'status': 'PENDING', 'notes': 'Current period commission pending month-end closing'}
            )

            # 5. Enterprise 2: Globex Logistics (Referred by Apex Partners)
            globex_biz, _ = Business.objects.get_or_create(
                name='Globex Logistics',
                defaults={
                    'legal_name': 'Globex Logistics & Supply Chain Pvt Ltd',
                    'email': 'contact@globex.com',
                    'phone': '+91 9988776655',
                    'city': 'Mumbai',
                    'state': 'Maharashtra',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'GLO',
                    'employee_id_next_number': 4,
                }
            )
            Referral.objects.get_or_create(business=globex_biz, defaults={'broker': broker2, 'referral_code_used': 'APEX2026'})

            globex_sub, _ = Subscription.objects.get_or_create(
                business=globex_biz,
                defaults={
                    'plan': starter_plan,
                    'status': SubscriptionStatus.ACTIVE_PAID,
                    'start_date': today - timedelta(days=120),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )
            globex_sub.status = SubscriptionStatus.ACTIVE_PAID
            globex_sub.save()

            # Globex Centres (2 centres, sum = 50 capacity)
            globex_c1, _ = Branch.objects.get_or_create(business=globex_biz, code='GLO-01', defaults={'name': 'Central Terminal Hub', 'city': 'Mumbai'})
            globex_c2, _ = Branch.objects.get_or_create(business=globex_biz, code='GLO-02', defaults={'name': 'Airport Cargo Centre', 'city': 'Mumbai'})
            CentreCapacityAllocation.objects.get_or_create(subscription=globex_sub, centre=globex_c1, defaults={'allocated_capacity': 30})
            CentreCapacityAllocation.objects.get_or_create(subscription=globex_sub, centre=globex_c2, defaults={'allocated_capacity': 20})

            # Globex Departments
            dept_fleet, _ = Department.objects.get_or_create(business=globex_biz, code='FLEET', defaults={'name': 'Fleet Operations'})
            dept_dispatch, _ = Department.objects.get_or_create(business=globex_biz, code='DISPATCH', defaults={'name': 'Dispatch & Routing'})

            # Globex Business Admin
            globex_admin, _ = User.objects.get_or_create(
                email='admin@globex.com',
                defaults={'first_name': 'Sanjay', 'last_name': 'Singhania'}
            )
            globex_admin.set_password(dev_password)
            globex_admin.save()
            BusinessMembership.objects.get_or_create(business=globex_biz, user=globex_admin, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            # Globex Manager
            globex_mgr, _ = User.objects.get_or_create(
                email='manager@globex.com',
                defaults={'first_name': 'Rajesh', 'last_name': 'Khanna'}
            )
            globex_mgr.set_password(dev_password)
            globex_mgr.save()
            BusinessMembership.objects.get_or_create(business=globex_biz, user=globex_mgr, defaults={'role': BusinessRole.MANAGER})
            globex_mgr_emp, _ = Employee.objects.get_or_create(
                business=globex_biz, user=globex_mgr,
                defaults={'first_name': 'Rajesh', 'last_name': 'Khanna', 'email': 'manager@globex.com', 'designation': 'Fleet Operations Manager', 'branch': globex_c1, 'department': dept_fleet, 'joining_date': date(2023, 4, 1), 'employee_id': 'GLO001'}
            )

            # Globex Staff
            globex_staff1, _ = User.objects.get_or_create(email='staff@globex.com', defaults={'first_name': 'Amit', 'last_name': 'Kumar'})
            globex_staff1.set_password(dev_password)
            globex_staff1.save()
            BusinessMembership.objects.get_or_create(business=globex_biz, user=globex_staff1, defaults={'role': BusinessRole.STAFF})
            Employee.objects.get_or_create(
                business=globex_biz, user=globex_staff1,
                defaults={'first_name': 'Amit', 'last_name': 'Kumar', 'email': 'staff@globex.com', 'designation': 'Senior Dispatcher', 'branch': globex_c1, 'department': dept_dispatch, 'manager': globex_mgr_emp, 'joining_date': date(2023, 4, 10), 'employee_id': 'GLO002'}
            )

            globex_driver1, _ = User.objects.get_or_create(email='driver1@globex.com', defaults={'first_name': 'Sunil', 'last_name': 'Verma'})
            globex_driver1.set_password(dev_password)
            globex_driver1.save()
            BusinessMembership.objects.get_or_create(business=globex_biz, user=globex_driver1, defaults={'role': BusinessRole.STAFF})
            Employee.objects.get_or_create(
                business=globex_biz, user=globex_driver1,
                defaults={'first_name': 'Sunil', 'last_name': 'Verma', 'email': 'driver1@globex.com', 'designation': 'Heavy Cargo Pilot', 'branch': globex_c2, 'department': dept_fleet, 'manager': globex_mgr_emp, 'joining_date': date(2023, 5, 1), 'employee_id': 'GLO003'}
            )

            # Broker 2 Commissions from Globex
            Commission.objects.get_or_create(
                broker=broker2, business=globex_biz,
                period_start=today.replace(day=1) - timedelta(days=30), period_end=today.replace(day=1) - timedelta(days=1),
                defaults={'subscription': globex_sub, 'plan': starter_plan, 'commission_amount': Decimal('499.90'), 'status': 'PAID', 'payment_reference': 'TXN_APEX_8821', 'paid_at': timezone.now() - timedelta(days=4), 'notes': 'Monthly commission payout'}
            )
            Commission.objects.get_or_create(
                broker=broker2, business=globex_biz,
                period_start=today.replace(day=1), period_end=(today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                defaults={'subscription': globex_sub, 'plan': starter_plan, 'commission_amount': Decimal('499.90'), 'status': 'PENDING', 'notes': 'Accruing monthly referral commission'}
            )

            # Helper for past month dates
            def get_month_dates(curr_date, offset):
                y = curr_date.year
                m = curr_date.month - offset
                while m <= 0:
                    m += 12
                    y -= 1
                b_start = date(y, m, 1)
                b_end = (b_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                return b_start, b_end

            # 6. Seed Acme Historical Payments (past 6 months)
            for offset in range(5, -1, -1):
                m_start, m_end = get_month_dates(today, offset)
                SubscriptionPayment.objects.get_or_create(
                    business=biz,
                    subscription=sub,
                    plan=pro_plan,
                    billing_date=m_start,
                    defaults={
                        'amount': pro_plan.monthly_charge,
                        'due_date': m_start + timedelta(days=5),
                        'status': PaymentStatus.PAID,
                        'paid_at': timezone.now() - timedelta(days=(offset * 30) + 2),
                        'payment_reference': f'TXN_ACM_{m_start.strftime("%Y%m")}',
                        'invoice_number': f'INV-ACM-{m_start.strftime("%Y%m")}',
                        'notes': f'Subscription billing for {m_start.strftime("%B %Y")}'
                    }
                )

            # 7. Seed Globex Historical Payments (past 4 months)
            for offset in range(3, -1, -1):
                m_start, m_end = get_month_dates(today, offset)
                SubscriptionPayment.objects.get_or_create(
                    business=globex_biz,
                    subscription=globex_sub,
                    plan=starter_plan,
                    billing_date=m_start,
                    defaults={
                        'amount': starter_plan.monthly_charge,
                        'due_date': m_start + timedelta(days=5),
                        'status': PaymentStatus.PAID,
                        'paid_at': timezone.now() - timedelta(days=(offset * 30) + 1),
                        'payment_reference': f'TXN_GLO_{m_start.strftime("%Y%m")}',
                        'invoice_number': f'INV-GLO-{m_start.strftime("%Y%m")}',
                        'notes': f'Subscription billing for {m_start.strftime("%B %Y")}'
                    }
                )

            # 8. Enterprise 3: Zenith Retail Ltd (Standard Plan, PAYMENT_DUE)
            zenith_biz, _ = Business.objects.get_or_create(
                name='Zenith Retail Ltd',
                defaults={
                    'legal_name': 'Zenith Retail Solutions India Pvt Ltd',
                    'email': 'contact@zenithretail.com',
                    'phone': '+91 9776655443',
                    'city': 'Bengaluru',
                    'state': 'Karnataka',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'ZEN',
                    'employee_id_next_number': 3,
                }
            )
            Referral.objects.get_or_create(business=zenith_biz, defaults={'broker': broker1, 'referral_code_used': 'BROKER2026'})
            zenith_sub, _ = Subscription.objects.get_or_create(
                business=zenith_biz,
                defaults={
                    'plan': standard_plan,
                    'status': SubscriptionStatus.PAYMENT_DUE,
                    'start_date': today - timedelta(days=70),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )
            zenith_sub.status = SubscriptionStatus.PAYMENT_DUE
            zenith_sub.save()

            zen_c1, _ = Branch.objects.get_or_create(business=zenith_biz, code='ZEN-01', defaults={'name': 'Indiranagar Flagship Store', 'city': 'Bengaluru'})
            zen_c2, _ = Branch.objects.get_or_create(business=zenith_biz, code='ZEN-02', defaults={'name': 'Whitefield Retail Depot', 'city': 'Bengaluru'})
            CentreCapacityAllocation.objects.get_or_create(subscription=zenith_sub, centre=zen_c1, defaults={'allocated_capacity': 80})
            CentreCapacityAllocation.objects.get_or_create(subscription=zenith_sub, centre=zen_c2, defaults={'allocated_capacity': 70})

            zen_admin, _ = User.objects.get_or_create(email='admin@zenithretail.com', defaults={'first_name': 'Tarun', 'last_name': 'Bansal'})
            zen_admin.set_password(dev_password)
            zen_admin.save()
            BusinessMembership.objects.get_or_create(business=zenith_biz, user=zen_admin, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            zen_mgr, _ = User.objects.get_or_create(email='manager@zenithretail.com', defaults={'first_name': 'Ritu', 'last_name': 'Sen'})
            zen_mgr.set_password(dev_password)
            zen_mgr.save()
            BusinessMembership.objects.get_or_create(business=zenith_biz, user=zen_mgr, defaults={'role': BusinessRole.MANAGER})
            Employee.objects.get_or_create(
                business=zenith_biz, user=zen_mgr,
                defaults={'first_name': 'Ritu', 'last_name': 'Sen', 'email': 'manager@zenithretail.com', 'designation': 'Store General Manager', 'branch': zen_c1, 'joining_date': date(2023, 6, 1), 'employee_id': 'ZEN001'}
            )

            # Zenith Payments (2 months paid, current month pending)
            for offset in [2, 1]:
                m_start, _ = get_month_dates(today, offset)
                SubscriptionPayment.objects.get_or_create(
                    business=zenith_biz,
                    subscription=zenith_sub,
                    plan=standard_plan,
                    billing_date=m_start,
                    defaults={
                        'amount': standard_plan.monthly_charge,
                        'due_date': m_start + timedelta(days=5),
                        'status': PaymentStatus.PAID,
                        'paid_at': timezone.now() - timedelta(days=(offset * 30) + 1),
                        'payment_reference': f'TXN_ZEN_{m_start.strftime("%Y%m")}',
                        'invoice_number': f'INV-ZEN-{m_start.strftime("%Y%m")}',
                    }
                )
            # Current month pending payment
            curr_start, _ = get_month_dates(today, 0)
            SubscriptionPayment.objects.get_or_create(
                business=zenith_biz,
                subscription=zenith_sub,
                plan=standard_plan,
                billing_date=curr_start,
                defaults={
                    'amount': standard_plan.monthly_charge,
                    'due_date': today + timedelta(days=3),
                    'status': PaymentStatus.PENDING,
                    'invoice_number': f'INV-ZEN-{curr_start.strftime("%Y%m")}',
                    'notes': 'Invoice generated, waiting for netbanking clearance'
                }
            )

            # 9. Enterprise 4: Horizon Healthcare (Enterprise Plan, OVERDUE)
            horizon_biz, _ = Business.objects.get_or_create(
                name='Horizon Healthcare',
                defaults={
                    'legal_name': 'Horizon Medical & Diagnostic Centers Pvt Ltd',
                    'email': 'billing@horizonhealth.com',
                    'phone': '+91 9665544332',
                    'city': 'Bengaluru',
                    'state': 'Karnataka',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'HOR',
                    'employee_id_next_number': 3,
                }
            )
            Referral.objects.get_or_create(business=horizon_biz, defaults={'broker': broker2, 'referral_code_used': 'APEX2026'})
            horizon_sub, _ = Subscription.objects.get_or_create(
                business=horizon_biz,
                defaults={
                    'plan': ent_plan,
                    'status': SubscriptionStatus.OVERDUE,
                    'start_date': today - timedelta(days=90),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )
            horizon_sub.status = SubscriptionStatus.OVERDUE
            horizon_sub.save()

            hor_c1, _ = Branch.objects.get_or_create(business=horizon_biz, code='HOR-01', defaults={'name': 'Horizon Main Hospital', 'city': 'Bengaluru'})
            hor_c2, _ = Branch.objects.get_or_create(business=horizon_biz, code='HOR-02', defaults={'name': 'Horizon Koramangala Clinic', 'city': 'Bengaluru'})
            CentreCapacityAllocation.objects.get_or_create(subscription=horizon_sub, centre=hor_c1, defaults={'allocated_capacity': 600})
            CentreCapacityAllocation.objects.get_or_create(subscription=horizon_sub, centre=hor_c2, defaults={'allocated_capacity': 400})

            hor_admin, _ = User.objects.get_or_create(email='admin@horizonhealth.com', defaults={'first_name': 'Dr. Alok', 'last_name': 'Mishra'})
            hor_admin.set_password(dev_password)
            hor_admin.save()
            BusinessMembership.objects.get_or_create(business=horizon_biz, user=hor_admin, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            # Horizon Payments (2 months ago paid, 1 month ago overdue, this month overdue)
            m2_start, _ = get_month_dates(today, 2)
            SubscriptionPayment.objects.get_or_create(
                business=horizon_biz, subscription=horizon_sub, plan=ent_plan, billing_date=m2_start,
                defaults={
                    'amount': ent_plan.monthly_charge, 'due_date': m2_start + timedelta(days=5),
                    'status': PaymentStatus.PAID, 'paid_at': timezone.now() - timedelta(days=62),
                    'payment_reference': f'TXN_HOR_{m2_start.strftime("%Y%m")}',
                    'invoice_number': f'INV-HOR-{m2_start.strftime("%Y%m")}',
                }
            )
            m1_start, _ = get_month_dates(today, 1)
            SubscriptionPayment.objects.get_or_create(
                business=horizon_biz, subscription=horizon_sub, plan=ent_plan, billing_date=m1_start,
                defaults={
                    'amount': ent_plan.monthly_charge, 'due_date': m1_start + timedelta(days=5),
                    'status': PaymentStatus.OVERDUE,
                    'invoice_number': f'INV-HOR-{m1_start.strftime("%Y%m")}',
                    'notes': 'Payment overdue by 25+ days'
                }
            )
            SubscriptionPayment.objects.get_or_create(
                business=horizon_biz, subscription=horizon_sub, plan=ent_plan, billing_date=curr_start,
                defaults={
                    'amount': ent_plan.monthly_charge, 'due_date': curr_start + timedelta(days=5),
                    'status': PaymentStatus.OVERDUE,
                    'invoice_number': f'INV-HOR-{curr_start.strftime("%Y%m")}',
                    'notes': 'Current billing cycle overdue'
                }
            )

            # 10. Enterprise 5: Spark Studios (Starter Plan, TRIAL)
            spark_biz, _ = Business.objects.get_or_create(
                name='Spark Creative Studios',
                defaults={
                    'legal_name': 'Spark Design & Interactive Media LLP',
                    'email': 'hello@sparkstudios.com',
                    'phone': '+91 9554433221',
                    'city': 'Bengaluru',
                    'state': 'Karnataka',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'SPK',
                    'employee_id_next_number': 2,
                }
            )
            spark_sub, _ = Subscription.objects.get_or_create(
                business=spark_biz,
                defaults={
                    'plan': starter_plan,
                    'status': SubscriptionStatus.TRIAL,
                    'start_date': today - timedelta(days=7),
                    'current_period_start': today - timedelta(days=7),
                    'current_period_end': today + timedelta(days=7),
                }
            )
            spark_sub.status = SubscriptionStatus.TRIAL
            spark_sub.current_period_end = today + timedelta(days=7)
            spark_sub.save()

            spark_c1, _ = Branch.objects.get_or_create(business=spark_biz, code='SPK-01', defaults={'name': 'Indiranagar Innovation Hub', 'city': 'Bengaluru'})
            CentreCapacityAllocation.objects.get_or_create(subscription=spark_sub, centre=spark_c1, defaults={'allocated_capacity': 25})

            spark_admin, _ = User.objects.get_or_create(email='admin@sparkstudios.com', defaults={'first_name': 'Maya', 'last_name': 'Sen'})
            spark_admin.set_password(dev_password)
            spark_admin.save()
            BusinessMembership.objects.get_or_create(business=spark_biz, user=spark_admin, defaults={'role': BusinessRole.BUSINESS_ADMIN})

            # 11. Enterprise 6: Orion Infotech (Professional Plan, SUSPENDED, inactive)
            orion_biz, _ = Business.objects.get_or_create(
                name='Orion Infotech',
                defaults={
                    'legal_name': 'Orion Infotech Services Pvt Ltd',
                    'email': 'contact@orioninfo.com',
                    'phone': '+91 9443322110',
                    'city': 'Hyderabad',
                    'state': 'Telangana',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'is_active': False,
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'ORI',
                    'employee_id_next_number': 2,
                }
            )
            orion_biz.is_active = False
            orion_biz.save()

            orion_sub, _ = Subscription.objects.get_or_create(
                business=orion_biz,
                defaults={
                    'plan': pro_plan,
                    'status': SubscriptionStatus.SUSPENDED,
                    'start_date': today - timedelta(days=150),
                    'current_period_start': today.replace(day=1),
                    'current_period_end': (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
                }
            )
            orion_sub.status = SubscriptionStatus.SUSPENDED
            orion_sub.save()

            # 12. Enterprise 7: Quantum Dynamics (Starter Plan, EXPIRED)
            quantum_biz, _ = Business.objects.get_or_create(
                name='Quantum Dynamics',
                defaults={
                    'legal_name': 'Quantum Dynamics Research Lab LLP',
                    'email': 'contact@quantumdynamics.com',
                    'phone': '+91 9332211009',
                    'city': 'Pune',
                    'state': 'Maharashtra',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'employee_id_enabled': True,
                    'employee_id_prefix': 'QNT',
                    'employee_id_next_number': 2,
                }
            )
            quantum_sub, _ = Subscription.objects.get_or_create(
                business=quantum_biz,
                defaults={
                    'plan': starter_plan,
                    'status': SubscriptionStatus.EXPIRED,
                    'start_date': today - timedelta(days=160),
                    'current_period_start': today.replace(day=1) - timedelta(days=60),
                    'current_period_end': today.replace(day=1) - timedelta(days=30),
                }
            )
            quantum_sub.status = SubscriptionStatus.EXPIRED
            quantum_sub.save()

            # 13. Authoritatively enforce password '123456' across ALL users
            all_users = User.objects.all()
            for user in all_users:
                user.set_password(dev_password)
                user.is_active = True
                user.save(update_fields=['password', 'is_active'])

            self.stdout.write(self.style.SUCCESS(
                f"Successfully seeded comprehensive multi-enterprise SaaS data ({Business.objects.count()} businesses, {SubscriptionPayment.objects.count()} payments, {all_users.count()} users)! All passwords '{dev_password}'."
            ))
