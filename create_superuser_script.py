import os
import django

# Configurar Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nombre_de_tu_proyecto.settings") # CAMBIA ESTO por el nombre de tu carpeta de settings
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'admin1234')

if not User.objects.filter(username=username).exists():
    print(f"Creando superusuario: {username}")
    User.objects.create_superuser(username, email, password)
else:
    print("El superusuario ya existe.")