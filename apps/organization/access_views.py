import os
import mimetypes
from pathlib import Path
from django.conf import settings
from django.http import FileResponse
from django.utils import timezone
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessRole, Designation, EmployeeDocument,
    Holiday, ManagerAccessControl, Permission, Employee
)
from apps.organization.serializers import (
    DesignationSerializer, EmployeeDocumentSerializer, HolidaySerializer
)
from apps.organization.services.permission_service import PermissionService
from apps.core.services.audit_service import AuditService


class PermissionListView(views.APIView):
    """
    Returns canonical permissions grouped by functional module.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        perms = Permission.objects.all().order_by('module', 'key')
        modules = {}
        for p in perms:
            if p.module not in modules:
                modules[p.module] = []
            modules[p.module].append({
                'id': str(p.id),
                'key': p.key,
                'name': p.name,
                'description': p.description,
                'default_scope': p.default_scope
            })
        return Response({'modules': modules})


class ManagerAccessControlView(views.APIView):
    """
    Enterprise Admin configures and inspects permissions for a specific Center Manager.
    """
    permission_classes = [IsAuthenticated]

    def get_manager_user(self, request, pk):
        ctx = get_user_context(request)
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Enterprise Admin can view or configure Manager Access Control.')

        biz = ctx['business']
        manager_user = User.objects.filter(id=pk).first()
        manager_emp = None
        if not manager_user:
            manager_emp = Employee.objects.filter(id=pk).select_related('user', 'branch').first()
            if manager_emp and manager_emp.user:
                manager_user = manager_emp.user
        else:
            manager_emp = Employee.objects.filter(user=manager_user, business=biz).select_related('branch').first()

        if not manager_user:
            raise NotFound('Manager not found.')

        # Ensure manager belongs to this business
        if not ctx['is_superadmin']:
            is_member = manager_user.business_memberships.filter(
                business=biz,
                role=BusinessRole.MANAGER,
                is_active=True
            ).exists()
            if not is_member:
                # Also allow if employee belongs to this business and has manager role/designation
                is_mgr = manager_user.business_memberships.filter(
                    business=biz,
                    is_active=True
                ).exists() and Employee.objects.filter(
                    user=manager_user,
                    business=biz,
                    designation__icontains='Manager'
                ).exists()
                if not is_mgr:
                    raise PermissionDenied('User is not an active manager in this business.')

        return biz, manager_user, manager_emp

    def get(self, request, pk):
        biz, manager_user, manager_emp = self.get_manager_user(request, pk)
        matrix = PermissionService.get_manager_access_matrix(business=biz, manager_user=manager_user)

        modules = {}
        granted_count = 0
        for item in matrix:
            mod_key = item.get('module', 'general')
            if mod_key not in modules:
                modules[mod_key] = {
                    'module_display': mod_key.replace('_', ' ').title(),
                    'permissions': []
                }
            if item.get('is_granted'):
                granted_count += 1
            modules[mod_key]['permissions'].append(item)

        branch_name = 'All Centers'
        if manager_emp and manager_emp.branch:
            branch_name = manager_emp.branch.name

        return Response({
            'manager_id': str(manager_user.id),
            'manager_name': manager_user.get_full_name(),
            'manager_email': manager_user.email,
            'permissions': matrix,
            'manager': {
                'id': str(manager_emp.id) if manager_emp else str(manager_user.id),
                'name': manager_user.get_full_name(),
                'email': manager_user.email,
                'branch_name': branch_name,
            },
            'granted_count': granted_count,
            'total_permissions': len(matrix),
            'modules': modules
        })

    def put(self, request, pk):
        biz, manager_user, manager_emp = self.get_manager_user(request, pk)
        permissions_data = request.data.get('permissions', [])
        if not isinstance(permissions_data, list):
            raise ValidationError({'detail': 'Expected a list of permissions to configure.'})

        updated = []
        for item in permissions_data:
            key = item.get('key') or item.get('permission_key')
            is_granted = item.get('is_granted')
            scope = item.get('scope', 'CENTER')
            if key and is_granted is not None:
                ctl = PermissionService.set_manager_permission(
                    business=biz,
                    manager_user=manager_user,
                    permission_key=key,
                    is_granted=bool(is_granted),
                    scope=scope,
                    granted_by=request.user
                )
                updated.append(key)

        AuditService.log(
            user_or_request=request,
            action='UPDATE_MANAGER_PERMISSIONS',
            entity_type='ManagerAccessControl',
            entity_id=str(manager_user.id),
            new_data={'updated_permissions': updated},
            business=biz,
            reason=f'Enterprise Admin updated access controls for manager {manager_user.email}'
        )

        matrix = PermissionService.get_manager_access_matrix(business=biz, manager_user=manager_user)
        modules = {}
        granted_count = 0
        for item in matrix:
            mod_key = item.get('module', 'general')
            if mod_key not in modules:
                modules[mod_key] = {
                    'module_display': mod_key.replace('_', ' ').title(),
                    'permissions': []
                }
            if item.get('is_granted'):
                granted_count += 1
            modules[mod_key]['permissions'].append(item)

        branch_name = 'All Centers'
        if manager_emp and manager_emp.branch:
            branch_name = manager_emp.branch.name

        return Response({
            'detail': f'Updated {len(updated)} permissions for {manager_user.get_full_name()}.',
            'permissions': matrix,
            'manager': {
                'id': str(manager_emp.id) if manager_emp else str(manager_user.id),
                'name': manager_user.get_full_name(),
                'email': manager_user.email,
                'branch_name': branch_name,
            },
            'granted_count': granted_count,
            'total_permissions': len(matrix),
            'modules': modules
        })


class DesignationListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            return Response([])
        qs = Designation.objects.filter(business=biz, is_active=True).order_by('name')
        return Response(DesignationSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise PermissionDenied('No active business context.')

        if not PermissionService.has_permission(request.user, 'employees.manage_designation', business=biz):
            raise PermissionDenied('You do not have permission to manage designations.')

        serializer = DesignationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        desig = serializer.save(business=biz)
        return Response(DesignationSerializer(desig).data, status=status.HTTP_201_CREATED)


class DesignationDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        desig = Designation.objects.filter(id=pk, business=biz).first()
        if not desig:
            raise NotFound('Designation not found.')

        if not PermissionService.has_permission(request.user, 'employees.manage_designation', business=biz):
            raise PermissionDenied('You do not have permission to manage designations.')

        serializer = DesignationSerializer(desig, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        desig = serializer.save()
        return Response(DesignationSerializer(desig).data)

    def delete(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        desig = Designation.objects.filter(id=pk, business=biz).first()
        if not desig:
            raise NotFound('Designation not found.')

        if not PermissionService.has_permission(request.user, 'employees.manage_designation', business=biz):
            raise PermissionDenied('You do not have permission to manage designations.')

        desig.is_active = False
        desig.save(update_fields=['is_active'])
        return Response({'detail': 'Designation deactivated successfully.'}, status=status.HTTP_200_OK)


class EmployeeDocumentListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_employee(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).first()
        if not emp:
            raise NotFound('Employee not found.')

        if not PermissionService.has_permission(request.user, 'documents.view', business=emp.business, target_employee=emp):
            raise PermissionDenied('Access denied to employee documents.')

        return emp

    def get(self, request, pk):
        emp = self.get_employee(request, pk)
        docs = EmployeeDocument.objects.filter(employee=emp, is_deleted=False).order_by('-created_at')
        return Response(EmployeeDocumentSerializer(docs, many=True).data)

    def post(self, request, pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'documents.upload', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to upload documents for this employee.')

        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        uploaded_file = request.FILES.get('file')

        if 'document_type' in data and 'category' not in data:
            data['category'] = data['document_type']

        doc_dir = Path(getattr(settings, 'DOCUMENTS_STORAGE_DIR', '/home/s/projects/ownmanage/document'))
        doc_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = None
        if uploaded_file:
            orig_name = uploaded_file.name
            safe_filename = orig_name
            target_path = doc_dir / safe_filename
            if target_path.exists():
                stem, ext = os.path.splitext(orig_name)
                safe_filename = f"{stem}_{emp.employee_id or str(emp.id)[:8]}{ext}"
                target_path = doc_dir / safe_filename
                if target_path.exists():
                    safe_filename = f"{stem}_{int(timezone.now().timestamp())}{ext}"
                    target_path = doc_dir / safe_filename

            with open(target_path, 'wb+') as dest:
                for chunk in uploaded_file.chunks():
                    dest.write(chunk)

            data['file_name'] = safe_filename
            data['file_size_bytes'] = uploaded_file.size
            if not data.get('title'):
                data['title'] = orig_name
        elif data.get('existing_file_name'):
            safe_filename = os.path.basename(data['existing_file_name'])
            target_path = doc_dir / safe_filename
            if target_path.exists():
                data['file_name'] = safe_filename
                data['file_size_bytes'] = target_path.stat().st_size
                if not data.get('title'):
                    data['title'] = safe_filename

        if not safe_filename:
            safe_filename = data.get('file_name') or 'document.pdf'
            data['file_name'] = safe_filename

        if not data.get('title'):
            data['title'] = data.get('file_name') or data.get('category') or 'Employee Document'

        if not data.get('file_url'):
            data['file_url'] = f"/api/v1/documents/file/{safe_filename}"

        serializer = EmployeeDocumentSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        doc = serializer.save(
            business=emp.business,
            employee=emp,
            uploaded_by=request.user
        )

        # Direct download endpoint for this document
        doc.file_url = f"/api/v1/documents/{doc.id}/download/"
        doc.save(update_fields=['file_url'])

        from apps.organization.models import EmployeeActivityLog
        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='DOCUMENT_UPLOADED',
            description=f"Document uploaded: {doc.title} ({doc.get_category_display()})",
            new_value={'title': doc.title, 'category': doc.category, 'file_name': doc.file_name},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='UPLOAD_DOCUMENT',
            entity_type='EmployeeDocument',
            entity_id=str(doc.id),
            new_data={'title': doc.title, 'category': doc.category, 'employee_id': str(emp.id)},
            business=emp.business,
            reason=f'Uploaded document {doc.title} for {emp.full_name}'
        )
        return Response(EmployeeDocumentSerializer(doc).data, status=status.HTTP_201_CREATED)


class EmployeeDocumentDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        doc = EmployeeDocument.objects.filter(id=pk, is_deleted=False).select_related('employee', 'business').first()
        if not doc:
            raise NotFound('Document not found.')

        if not PermissionService.has_permission(request.user, 'documents.delete', business=doc.business, target_employee=doc.employee):
            raise PermissionDenied('You do not have permission to delete employee documents.')

        doc.is_deleted = True
        doc.deleted_at = timezone.now()
        doc.deleted_by = request.user
        doc.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])

        from apps.organization.models import EmployeeActivityLog
        EmployeeActivityLog.objects.create(
            business=doc.business,
            employee=doc.employee,
            activity_type='DOCUMENT_DELETED',
            description=f"Document deleted: {doc.title}",
            old_value={'title': doc.title, 'category': doc.category},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='DELETE_DOCUMENT',
            entity_type='EmployeeDocument',
            entity_id=str(doc.id),
            business=doc.business,
            reason=f'Soft deleted document {doc.title} for employee {doc.employee.full_name}'
        )
        return Response({'detail': 'Document deleted successfully.'}, status=status.HTTP_200_OK)


class EmployeeDocumentVerifyView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        doc = EmployeeDocument.objects.filter(id=pk, is_deleted=False).select_related('employee', 'business').first()
        if not doc:
            raise NotFound('Document not found.')

        if not PermissionService.has_permission(request.user, 'documents.verify', business=doc.business, target_employee=doc.employee):
            raise PermissionDenied('You do not have permission to verify documents.')

        new_status = request.data.get('status', 'VERIFIED').upper()
        if new_status not in ['VERIFIED', 'REJECTED']:
            raise ValidationError({'detail': "Status must be either 'VERIFIED' or 'REJECTED'."})

        doc.verification_status = new_status
        doc.verified_by = request.user
        doc.verified_at = timezone.now()
        doc.remarks = request.data.get('remarks', doc.remarks)
        doc.save(update_fields=['verification_status', 'verified_by', 'verified_at', 'remarks', 'updated_at'])

        return Response(EmployeeDocumentSerializer(doc).data)


class EmployeeDocumentDownloadView(views.APIView):
    """
    Downloads or streams the physical document file stored in /home/s/projects/ownmanage/document.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        doc = EmployeeDocument.objects.filter(id=pk, is_deleted=False).select_related('employee', 'business').first()
        if not doc:
            raise NotFound('Document not found.')

        if not PermissionService.has_permission(request.user, 'documents.view', business=doc.business, target_employee=doc.employee):
            raise PermissionDenied('You do not have permission to access this document.')

        doc_dir = Path(getattr(settings, 'DOCUMENTS_STORAGE_DIR', '/home/s/projects/ownmanage/document'))
        file_path = None
        candidates = [
            doc_dir / doc.file_name,
            doc_dir / str(doc.employee.id) / doc.file_name,
            doc_dir / f"{doc.employee.employee_id}_{doc.file_name}",
        ]
        for c in candidates:
            if c.exists():
                file_path = c
                break

        if not file_path or not file_path.exists():
            raise NotFound('Physical document file not found in document directory.')

        content_type, _ = mimetypes.guess_type(str(file_path))
        content_type = content_type or 'application/octet-stream'

        is_inline = request.query_params.get('inline', '0') in ['1', 'true', 'True']
        disposition = 'inline' if is_inline else 'attachment'

        response = FileResponse(open(file_path, 'rb'), content_type=content_type)
        response['Content-Disposition'] = f'{disposition}; filename="{doc.file_name}"'
        return response


class AvailableDocumentFilesView(views.APIView):
    """
    Lists physical files in /home/s/projects/ownmanage/document for assignment or inspection.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        doc_dir = Path(getattr(settings, 'DOCUMENTS_STORAGE_DIR', '/home/s/projects/ownmanage/document'))
        if not doc_dir.exists():
            return Response([])
        files = []
        for f in sorted(doc_dir.iterdir(), key=lambda x: x.name):
            if f.is_file():
                files.append({
                    'name': f.name,
                    'size_bytes': f.stat().st_size,
                    'modified_at': timezone.datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
        return Response(files)


class HolidayListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            return Response([])

        qs = Holiday.objects.filter(business=biz)
        centre_id = request.query_params.get('centre_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            # Applies if applies_to_all_centres is True OR specific centre attached
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                from django.db.models import Q
                qs = qs.filter(Q(applies_to_all_centres=True) | Q(centres__id=branch_obj.id)).distinct()

        year = request.query_params.get('year')
        if year:
            qs = qs.filter(holiday_date__year=int(year))

        return Response(HolidaySerializer(qs.prefetch_related('centres').order_by('holiday_date'), many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise PermissionDenied('No active business context.')

        if not PermissionService.has_permission(request.user, 'holidays.create', business=biz):
            raise PermissionDenied('You do not have permission to add holidays.')

        serializer = HolidaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        holiday = serializer.save(business=biz)
        return Response(HolidaySerializer(holiday).data, status=status.HTTP_201_CREATED)


class HolidayDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        holiday = Holiday.objects.filter(id=pk, business=biz).first()
        if not holiday:
            raise NotFound('Holiday not found.')

        if not PermissionService.has_permission(request.user, 'holidays.delete', business=biz):
            raise PermissionDenied('You do not have permission to delete holidays.')

        holiday.delete()
        return Response({'detail': 'Holiday deleted successfully.'}, status=status.HTTP_200_OK)
