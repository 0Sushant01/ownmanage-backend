from django.urls import path
from apps.meetings.views import (
    MeetingListCreateView, MeetingDetailView, MeetingCancelView,
    MeetingRespondView, EligibleParticipantsView, MeetingConflictCheckView
)

urlpatterns = [
    path('', MeetingListCreateView.as_view(), name='meeting-list-create'),
    path('eligible-participants/', EligibleParticipantsView.as_view(), name='meeting-eligible-participants'),
    path('check-conflicts/', MeetingConflictCheckView.as_view(), name='meeting-check-conflicts'),
    path('<uuid:pk>/', MeetingDetailView.as_view(), name='meeting-detail'),
    path('<uuid:pk>/cancel/', MeetingCancelView.as_view(), name='meeting-cancel'),
    path('<uuid:pk>/respond/', MeetingRespondView.as_view(), name='meeting-respond'),
]
