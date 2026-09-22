"""
Root URL Configuration for OwnManage Backend.
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    # Versioned API
    path('api/v1/', include('config.api_v1_urls')),
]
