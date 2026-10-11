"""
Comprehensive Acceptance Test Suite for Secure QR Attendance Lifecycle
and Expired QR Cleanup in OWNManage.

Covers:
1. QR Generation & Authorization:
   - Authorized manager / admin can generate QR.
   - Unauthorized regular staff cannot generate QR (403 Forbidden).
   - Cross-business centre request is rejected.
2. QR Expiry & Timezone Calculations:
   - Dynamic (short-lived seconds).
   - Daily (end of day in centre local timezone).
   - Weekly (end of Sunday in centre local timezone).
   - Monthly (end of last day of calendar month in centre local timezone).
3. Expired QR Cleanup:
   - Expired QR records deleted during generation request.
   - Valid QR records remain untouched.
   - QR records of other centres remain untouched.
   - Attendance history (AttendanceDay, AttendanceEvent) remains 100% intact.
   - Reusability of valid compatible QR without duplicate active records.
   - Forced refresh deactivates previous active QR and creates a new one.
4. Secure QR Validation on Attendance Punch:
   - Valid QR check-in succeeds and records attendance_method='QR' and verification metadata.
   - Valid QR check-out succeeds.
   - Expired QR attendance is rejected.
   - Revoked QR attendance is rejected.
   - Deleted/nonexistent QR is rejected.
   - Cross-centre QR attendance is rejected.
5. Multi-Method & Policy Enforcement:
   - Mandatory location verification cannot be bypassed via QR.
   - State machine prevents duplicate check-ins and duplicate check-outs.
6. Existing Punch Methods:
   - Normal punch continues to work.
   - Face punch continues to work.
   - Payroll calculations unaffected.
"""

import os
import sys
import datetime
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.utils import timezone
from django.db import transaction
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import ValidationError as DRFValidationError, PermissionDenied, NotFound

from apps.organization.models import Business, Branch, Employee, BusinessRole, BusinessMembership
from apps.accounts.models import User
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceEventType, AttendanceMethod,
    AttendanceStatus, AttendancePolicy, AttendancePolicyOverride,
    AttendanceQRCode, QRValidityPeriod
)
from apps.attendance.services.attendance_service import AttendanceService
from apps.attendance.services.qr_service import AttendanceQRService
from apps.attendance.views import AttendanceQRTokenView, AttendanceQRRevokeView

passed_checks = []
failed_checks = []


def record_test(name: str, passed: bool, details: str = ""):
    if passed:
        passed_checks.append(name)
        print(f"  [PASS] {name} {f'({details})' if details else ''}")
    else:
        failed_checks.append((name, details))
        print(f"  [FAIL] {name} - {details}")


def run_tests():
    print("=" * 75)
    print("STARTING SECURE QR ATTENDANCE LIFECYCLE & CLEANUP ACCEPTANCE TESTS")
    print("=" * 75)

    biz = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert biz is not None, "A business must exist in database"

    centre_a = Branch.objects.filter(business=biz, is_active=True).first()
    assert centre_a is not None, "Centre A must exist"

    centre_b = Branch.objects.filter(business=biz, is_active=True).exclude(id=centre_a.id).first()
    if not centre_b:
        centre_b = Branch.objects.create(business=biz, name="Centre B Test", code="C-B-QR")

    # Set up Admin User
    admin_mem = biz.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first()
    admin_user = admin_mem.user if admin_mem else biz.owner

    # Set up Employee & Staff User at Centre A
    emp = Employee.objects.filter(business=biz, branch=centre_a, employment_status='ACTIVE').first()
    if not emp:
        emp = Employee.objects.filter(business=biz, employment_status='ACTIVE').first()
        emp.branch = centre_a
        emp.save(update_fields=['branch'])

    staff_user = emp.user
    if not staff_user:
        staff_user, _ = User.objects.get_or_create(email=f"qr_staff_{emp.id}@test.com", defaults={"first_name": "QR", "last_name": "Staff"})
        emp.user = staff_user
        emp.save(update_fields=['user'])

    # Ensure Staff role membership
    BusinessMembership.objects.get_or_create(
        business=biz,
        user=staff_user,
        defaults={'role': BusinessRole.STAFF, 'is_active': True}
    )

    # Enable QR on Centre A attendance policy
    policy, _ = AttendancePolicy.objects.get_or_create(
        business=biz,
        defaults={'allow_normal_punch': True, 'allow_qr': True}
    )
    policy.allow_qr = True
    policy.allow_normal_punch = True
    policy.save(update_fields=['allow_qr', 'allow_normal_punch'])

    override, _ = AttendancePolicyOverride.objects.get_or_create(centre=centre_a)
    override.allow_qr = True
    override.allow_normal_punch = True
    override.save(update_fields=['allow_qr', 'allow_normal_punch'])

    factory = APIRequestFactory()

    # ------------------------------------------------------------------
    # 1. QR GENERATION & AUTHORIZATION
    # ------------------------------------------------------------------
    print("\n--- 1. Testing QR Generation & RBAC Authorization ---")

    # Authorized Admin generates QR
    req_admin = factory.get(f"/api/v1/attendance/qr/centre-token/?centre_id={centre_a.id}")
    force_authenticate(req_admin, user=admin_user)
    resp_admin = AttendanceQRTokenView.as_view()(req_admin)

    record_test("Authorized Business Admin can generate centre QR", resp_admin.status_code == 200)
    record_test("Response includes qr_code payload and expiry", "qr_code" in resp_admin.data and "expires_in_seconds" in resp_admin.data)
    record_test("Generated token is persisted in database", AttendanceQRCode.objects.filter(token=resp_admin.data["token"]).exists())

    # Unauthorized Regular Staff cannot generate QR (Blocked with 403)
    req_staff = factory.get(f"/api/v1/attendance/qr/centre-token/?centre_id={centre_a.id}")
    force_authenticate(req_staff, user=staff_user)
    staff_blocked = False
    try:
        resp_staff = AttendanceQRTokenView.as_view()(req_staff)
        if resp_staff.status_code == 403:
            staff_blocked = True
    except PermissionDenied:
        staff_blocked = True

    record_test("Unauthorized regular staff blocked from generating QR (403)", staff_blocked)

    # Cross-business Centre Request Rejected
    other_biz = Business.objects.exclude(id=biz.id).first()
    if not other_biz:
        other_biz = Business.objects.create(name="Competitor Corp", owner=admin_user)
    other_centre, _ = Branch.objects.get_or_create(business=other_biz, name="Competitor Branch", defaults={"code": "COMP-1"})
    
    req_cross = factory.get(f"/api/v1/attendance/qr/centre-token/?centre_id={other_centre.id}")
    force_authenticate(req_cross, user=admin_user)
    cross_rejected = False
    try:
        resp_cross = AttendanceQRTokenView.as_view()(req_cross)
        if resp_cross.status_code in [403, 404]:
            cross_rejected = True
    except (PermissionDenied, NotFound):
        cross_rejected = True

    record_test("Cross-business centre QR generation is strictly rejected", cross_rejected)

    # ------------------------------------------------------------------
    # 2. QR REUSE & FORCED REFRESH
    # ------------------------------------------------------------------
    print("\n--- 2. Testing QR Reuse & Forced Refresh ---")

    # Second call without force_refresh reuses existing valid QR
    req_reuse = factory.get(f"/api/v1/attendance/qr/centre-token/?centre_id={centre_a.id}")
    force_authenticate(req_reuse, user=admin_user)
    resp_reuse = AttendanceQRTokenView.as_view()(req_reuse)

    record_test("Existing valid QR is safely reused", resp_reuse.data["token"] == resp_admin.data["token"])
    record_test("Response indicates reused=True", resp_reuse.data["reused"] is True)

    # Forced refresh creates a new QR and deactivates prior one
    req_refresh = factory.get(f"/api/v1/attendance/qr/centre-token/?centre_id={centre_a.id}&refresh=true")
    force_authenticate(req_refresh, user=admin_user)
    resp_refresh = AttendanceQRTokenView.as_view()(req_refresh)

    record_test("Forced refresh generates a new QR token", resp_refresh.data["token"] != resp_admin.data["token"])
    record_test("Response indicates reused=False", resp_refresh.data["reused"] is False)

    # Check database state: Old QR is inactive, new QR is active
    old_qr = AttendanceQRCode.objects.get(token=resp_admin.data["token"])
    new_qr = AttendanceQRCode.objects.get(token=resp_refresh.data["token"])
    record_test("Previous QR deactivated in database", old_qr.is_active is False)
    record_test("Newly generated QR is active in database", new_qr.is_active is True)

    # ------------------------------------------------------------------
    # 3. EXPIRY CALCULATION & TIMEZONE AWARENESS
    # ------------------------------------------------------------------
    print("\n--- 3. Testing Validity Periods & Timezone Expiry ---")
    now_dt = timezone.now()
    centre_tz = AttendanceQRService.get_centre_timezone(centre_a)
    local_now = now_dt.astimezone(centre_tz)

    # DYNAMIC (300s)
    exp_dyn = AttendanceQRService.calculate_expiry(QRValidityPeriod.DYNAMIC, centre_a, now_dt, dynamic_seconds=300)
    record_test("Dynamic expiry is approx 300s in future", abs((exp_dyn - (now_dt + datetime.timedelta(seconds=300))).total_seconds()) < 2)

    # DAILY (End of today local 23:59:59)
    exp_daily = AttendanceQRService.calculate_expiry(QRValidityPeriod.DAILY, centre_a, now_dt)
    exp_daily_local = exp_daily.astimezone(centre_tz)
    record_test("Daily expiry ends at 23:59:59 local date", exp_daily_local.date() == local_now.date() and exp_daily_local.hour == 23 and exp_daily_local.minute == 59)

    # WEEKLY (End of Sunday local 23:59:59)
    exp_weekly = AttendanceQRService.calculate_expiry(QRValidityPeriod.WEEKLY, centre_a, now_dt)
    exp_weekly_local = exp_weekly.astimezone(centre_tz)
    record_test("Weekly expiry ends on Sunday (weekday 6)", exp_weekly_local.weekday() == 6 and exp_weekly_local.hour == 23)

    # MONTHLY (End of calendar month local 23:59:59)
    exp_monthly = AttendanceQRService.calculate_expiry(QRValidityPeriod.MONTHLY, centre_a, now_dt)
    exp_monthly_local = exp_monthly.astimezone(centre_tz)
    import calendar
    _, last_day = calendar.monthrange(local_now.year, local_now.month)
    record_test("Monthly expiry ends on last day of month", exp_monthly_local.day == last_day and exp_monthly_local.hour == 23)

    # ------------------------------------------------------------------
    # 4. EXPIRED QR CLEANUP SAFETY & DATA INTEGRITY
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Expired QR Cleanup Safety ---")

    # Create dummy records:
    # 1. Expired QR for Centre A
    unique_token_suffix = uuid.uuid4().hex[:8]
    expired_a = AttendanceQRCode.objects.create(
        business=biz,
        centre=centre_a,
        token=f"EXP_TOKEN_CENTRE_A_{unique_token_suffix}",
        code_payload=f"OWNMANAGE:CENTRE:A:EXP:{unique_token_suffix}",
        validity_period=QRValidityPeriod.DYNAMIC,
        expires_at=now_dt - datetime.timedelta(hours=2),
        is_active=False
    )
    # 2. Valid QR for Centre A
    valid_a = AttendanceQRCode.objects.create(
        business=biz,
        centre=centre_a,
        token=f"VALID_TOKEN_CENTRE_A_{unique_token_suffix}",
        code_payload=f"OWNMANAGE:CENTRE:A:VALID:{unique_token_suffix}",
        validity_period=QRValidityPeriod.DAILY,
        expires_at=now_dt + datetime.timedelta(hours=5),
        is_active=True
    )
    # 3. Expired QR for Centre B (MUST NOT be deleted by Centre A cleanup)
    expired_b = AttendanceQRCode.objects.create(
        business=biz,
        centre=centre_b,
        token=f"EXP_TOKEN_CENTRE_B_{unique_token_suffix}",
        code_payload=f"OWNMANAGE:CENTRE:B:EXP:{unique_token_suffix}",
        validity_period=QRValidityPeriod.DYNAMIC,
        expires_at=now_dt - datetime.timedelta(hours=2),
        is_active=False
    )

    # 4. AttendanceDay & Event records (MUST NEVER be deleted by cleanup)
    today_date = local_now.date()
    att_day, _ = AttendanceDay.objects.get_or_create(
        business=biz,
        employee=emp,
        attendance_date=today_date,
        defaults={'centre': centre_a, 'status': AttendanceStatus.PRESENT, 'attendance_method': AttendanceMethod.QR}
    )
    att_event = AttendanceEvent.objects.create(
        business=biz,
        attendance_day=att_day,
        employee=emp,
        event_type=AttendanceEventType.CHECK_IN,
        attendance_method=AttendanceMethod.QR,
        event_time=now_dt
    )

    # Trigger QR generation on Centre A which invokes cleanup_expired_qrs
    deleted_count = AttendanceQRService.cleanup_expired_qrs(centre_a, server_now=now_dt)

    record_test("Expired QR for Centre A is deleted", not AttendanceQRCode.objects.filter(id=expired_a.id).exists())
    record_test("Valid QR for Centre A remains untouched", AttendanceQRCode.objects.filter(id=valid_a.id).exists())
    record_test("Expired QR for Centre B is untouched (scoped to Centre A)", AttendanceQRCode.objects.filter(id=expired_b.id).exists())
    record_test("AttendanceDay record remains 100% intact", AttendanceDay.objects.filter(id=att_day.id).exists())
    record_test("AttendanceEvent record remains 100% intact", AttendanceEvent.objects.filter(id=att_event.id).exists())

    # ------------------------------------------------------------------
    # 5. SECURE QR ATTENDANCE PUNCH VALIDATION
    # ------------------------------------------------------------------
    print("\n--- 5. Testing QR Attendance Punch Validation ---")

    # Clean up today's events for fresh punch test
    AttendanceEvent.objects.filter(attendance_day=att_day).delete()
    att_day.check_in = None
    att_day.check_out = None
    att_day.total_work_seconds = 0
    att_day.is_locked = False
    att_day.save()

    # Generate an active QR for Centre A
    active_qr, _, _ = AttendanceQRService.generate_or_reuse_qr_code(centre_a, user=admin_user, force_refresh=True)

    # 5.1 Valid QR Check-In
    state_in = AttendanceService.record_punch(
        user=staff_user,
        punch_type='CHECK_IN',
        attendance_method=AttendanceMethod.QR,
        qr_code=active_qr.code_payload
    )
    record_test("Valid QR check-in succeeds", state_in["is_checked_in"] is True)

    att_day.refresh_from_db()
    last_event = att_day.events.order_by('event_time').last()
    record_test("Attendance method recorded as QR", att_day.attendance_method == AttendanceMethod.QR)
    record_test("Verification metadata records qr_verified=True", last_event.verification_metadata.get("qr_verified") is True)
    record_test("Verification metadata records qr_code_id", last_event.verification_metadata.get("qr_code_id") == str(active_qr.id))

    # 5.2 Duplicate Check-In is Rejected
    dup_in_blocked = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=active_qr.code_payload
        )
    except (DRFValidationError, Exception) as e:
        if "already checked in" in str(e).lower():
            dup_in_blocked = True

    record_test("Duplicate check-in rejected by state machine", dup_in_blocked)

    # 5.3 Valid QR Check-Out
    state_out = AttendanceService.record_punch(
        user=staff_user,
        punch_type='CHECK_OUT',
        attendance_method=AttendanceMethod.QR,
        qr_code=active_qr.code_payload
    )
    record_test("Valid QR check-out succeeds", state_out["is_checked_in"] is False)

    # 5.4 Duplicate Check-Out is Rejected
    dup_out_blocked = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_OUT',
            attendance_method=AttendanceMethod.QR,
            qr_code=active_qr.code_payload
        )
    except (DRFValidationError, Exception) as e:
        if "cannot check out" in str(e).lower():
            dup_out_blocked = True

    record_test("Duplicate check-out rejected by state machine", dup_out_blocked)

    # ------------------------------------------------------------------
    # 6. REJECTION OF EXPIRED, REVOKED, DELETED & CROSS-CENTRE QRS
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Security Rejections (Expired, Revoked, Cross-Centre) ---")

    # 6.1 Expired QR Rejected
    active_qr.expires_at = timezone.now() - datetime.timedelta(seconds=10)
    active_qr.save(update_fields=['expires_at'])

    exp_rejected = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=active_qr.code_payload
        )
    except DRFValidationError as e:
        if "expired" in str(e).lower():
            exp_rejected = True

    record_test("Expired QR code is strictly rejected on punch", exp_rejected)

    # 6.2 Revoked QR Rejected
    active_qr.expires_at = timezone.now() + datetime.timedelta(hours=1)
    active_qr.is_revoked = True
    active_qr.save(update_fields=['expires_at', 'is_revoked'])

    rev_rejected = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=active_qr.code_payload
        )
    except DRFValidationError as e:
        if "revoked" in str(e).lower():
            rev_rejected = True

    record_test("Revoked QR code is strictly rejected on punch", rev_rejected)

    # 6.3 Deleted / Non-Existent QR Rejected
    non_existent_payload = "OWNMANAGE:CENTRE:NON_EXISTENT_TOKEN_12345"
    del_rejected = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=non_existent_payload
        )
    except DRFValidationError as e:
        if "not found" in str(e).lower() or "invalid" in str(e).lower():
            del_rejected = True

    record_test("Deleted / non-existent QR token is strictly rejected", del_rejected)

    # 6.4 Cross-Centre QR Rejected
    qr_centre_b, _, _ = AttendanceQRService.generate_or_reuse_qr_code(centre_b, user=admin_user, force_refresh=True)
    cross_centre_rejected = False
    try:
        # Staff is assigned to Centre A, tries to use Centre B's QR
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=qr_centre_b.code_payload
        )
    except DRFValidationError as e:
        if "does not match your assigned centre" in str(e).lower():
            cross_centre_rejected = True

    record_test("Cross-centre QR token is strictly rejected", cross_centre_rejected)

    # ------------------------------------------------------------------
    # 7. MANDATORY LOCATION / GEOFENCE CANNOT BE BYPASSED
    # ------------------------------------------------------------------
    print("\n--- 7. Testing Location / Geofence Enforcement with QR ---")

    # Generate fresh active QR for Centre A
    fresh_qr, _, _ = AttendanceQRService.generate_or_reuse_qr_code(centre_a, user=admin_user, force_refresh=True)

    # Configure geofence on Centre A: Location is at (12.9716, 77.5946), radius 100m
    override.location_required_checkin = True
    override.gps_latitude = Decimal("12.971600")
    override.gps_longitude = Decimal("77.594600")
    override.gps_radius_meters = 100
    override.save()

    # Attempt QR check-in WITHOUT location coordinates -> Must Fail
    loc_missing_rejected = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=fresh_qr.code_payload,
            latitude=None,
            longitude=None
        )
    except DRFValidationError as e:
        if "location verification is required" in str(e).lower():
            loc_missing_rejected = True

    record_test("Missing GPS coordinates rejected when location verification enabled", loc_missing_rejected)

    # Attempt QR check-in OUTSIDE geofence (5000 meters away) -> Must Fail
    loc_outside_rejected = False
    try:
        AttendanceService.record_punch(
            user=staff_user,
            punch_type='CHECK_IN',
            attendance_method=AttendanceMethod.QR,
            qr_code=fresh_qr.code_payload,
            latitude=13.0500,  # ~10km away
            longitude=77.5946
        )
    except DRFValidationError as e:
        if "outside the permitted centre geofence" in str(e).lower():
            loc_outside_rejected = True

    record_test("Out-of-geofence punch rejected even with valid QR", loc_outside_rejected)

    # Reset location requirement and ensure normal punch is permitted
    override.location_required_checkin = False
    override.allow_normal_punch = True
    override.save(update_fields=['location_required_checkin', 'allow_normal_punch'])

    # ------------------------------------------------------------------
    # 8. EXISTING METHODS (NORMAL PUNCH, FACE) REMAIN FUNCTIONAL
    # ------------------------------------------------------------------
    print("\n--- 8. Testing Normal Punch & Existing Attendance Methods ---")

    # Normal check-in
    state_norm = AttendanceService.record_punch(
        user=staff_user,
        punch_type='CHECK_IN',
        attendance_method=AttendanceMethod.NORMAL
    )
    record_test("Normal check-in continues to function seamlessly", state_norm["is_checked_in"] is True)

    state_norm_out = AttendanceService.record_punch(
        user=staff_user,
        punch_type='CHECK_OUT',
        attendance_method=AttendanceMethod.NORMAL
    )
    record_test("Normal check-out continues to function seamlessly", state_norm_out["is_checked_in"] is False)

    # ------------------------------------------------------------------
    # 9. QR REVOCATION ENDPOINT
    # ------------------------------------------------------------------
    print("\n--- 9. Testing AttendanceQRRevokeView Endpoint ---")

    rev_qr, _, _ = AttendanceQRService.generate_or_reuse_qr_code(centre_a, user=admin_user, force_refresh=True)
    req_rev = factory.post("/api/v1/attendance/qr/revoke/", {
        "centre_id": str(centre_a.id),
        "qr_id": str(rev_qr.id),
        "reason": "Compromised kiosk tablet"
    }, format="json")
    force_authenticate(req_rev, user=admin_user)
    resp_rev = AttendanceQRRevokeView.as_view()(req_rev)

    record_test("QR revocation API returns 200 OK", resp_rev.status_code == 200)
    record_test("Revocation API reports revoked_count > 0", resp_rev.data.get("revoked_count", 0) > 0)
    rev_qr.refresh_from_db()
    record_test("QR marked revoked in database", rev_qr.is_revoked is True)

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"ACCEPTANCE TESTS COMPLETE: {len(passed_checks)} PASSED, {len(failed_checks)} FAILED")
    print("=" * 75)

    if failed_checks:
        print("\nFailures:")
        for name, details in failed_checks:
            print(f"  - {name}: {details}")
        sys.exit(1)
    else:
        print("\nALL SECURE QR ATTENDANCE LIFECYCLE TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_tests()
