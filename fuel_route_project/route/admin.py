from django.contrib import admin
from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('name', 'lat', 'lon', 'avg_price')
    search_fields = ('name', 'city', 'state')
    list_filter = ('state',)
