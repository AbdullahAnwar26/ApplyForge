from django.db import models

class Company(models.Model):
    name = models.CharField(max_length=255)
    website = models.URLField(max_length=500, blank=True, null=True)
    normalized_name = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.name:
            self.normalized_name = self.name.lower().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Contact(models.Model):
    name = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(blank=True, null=True, db_index=True)
    phone = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    whatsapp = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    
    normalized_email = models.EmailField(blank=True, null=True, db_index=True)
    normalized_phone = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    normalized_whatsapp = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    
    company = models.ForeignKey(Company, related_name='contacts', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.email:
            self.normalized_email = self.email.lower().strip()
        if self.phone:
            # Strip non-numeric for normalization
            self.normalized_phone = ''.join(filter(str.isdigit, self.phone))
        if self.whatsapp:
            self.normalized_whatsapp = ''.join(filter(str.isdigit, self.whatsapp))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.email or self.phone or self.whatsapp or "Unknown Contact"
