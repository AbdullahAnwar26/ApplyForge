from django.contrib import admin
from .models import Profile, Skill, Resume, Company, Contact, JobOpportunity, Application, ApplicationEvent

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'max_acceptable_experience_years', 'created_at')

@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'profile')
    list_filter = ('category', 'profile')

@admin.register(Resume)
class ResumeAdmin(admin.ModelAdmin):
    list_display = ('name', 'profile', 'is_active', 'is_default')
    list_filter = ('is_active', 'is_default')

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'website', 'created_at')
    search_fields = ('name', 'normalized_name')

@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'company')
    search_fields = ('name', 'email', 'normalized_email', 'phone')

@admin.register(JobOpportunity)
class JobOpportunityAdmin(admin.ModelAdmin):
    list_display = ('title', 'company', 'status', 'source', 'posted_date')
    list_filter = ('status', 'source')
    search_fields = ('title', 'company__name')

@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('job', 'profile', 'status', 'channel', 'created_at')
    list_filter = ('status', 'channel')

@admin.register(ApplicationEvent)
class ApplicationEventAdmin(admin.ModelAdmin):
    list_display = ('application', 'status', 'timestamp')
