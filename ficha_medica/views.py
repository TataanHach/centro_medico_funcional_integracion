from django.contrib.auth.decorators import user_passes_test, login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.utils.timezone import localtime, now
from django.utils import timezone
from datetime import datetime, timedelta, date
from django.contrib.auth.models import Group, User
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import logging
import pytz
from django.utils.timezone import make_aware
from django.utils.timezone import localtime
from datetime import datetime
from django.utils import timezone
from django.db.models import Q
from .models import Disponibilidad, Reserva

from ficha_medica.utils import role_required
from ficha_medica.forms import (
    FichaMedicaForm, DisponibilidadForm, ReservaForm,
    PacienteForm, MedicoForm, RecepcionistaForm, ActivarCuentaForm
)
from .models import (
    FichaMedica, Paciente, Reserva, Disponibilidad,
    Medico, Especialidad, Recepcionista, Notificacion
)

# Configuración de logging
logger = logging.getLogger(__name__)

# --- FUNCIONES AUXILIARES ---

def es_recepcionista(user):
    return user.groups.filter(name='Recepcionista').exists()

def admin_or_superuser_required(view_func):
    return user_passes_test(lambda u: u.is_active and (u.is_staff or u.is_superuser))(view_func)

# --- VISTAS GENERALES (Login, Home, Activación) ---

def home(request):
    """
    Página de inicio que maneja el inicio de sesión y redirección según roles.
    """
    if request.user.is_authenticated:
        # Obtenemos los nombres de los grupos a los que pertenece el usuario
        grupos = request.user.groups.values_list('name', flat=True)

        if 'Recepcionista' in grupos:
            return redirect('recepcionista_dashboard')
        elif 'Medico' in grupos:
            return redirect('medico_dashboard')
        elif 'Administrador' in grupos or request.user.is_superuser:
            return redirect('admin_dashboard')
        elif 'Paciente' in grupos:  # <--- ESTO ES LO QUE FALTABA
            return redirect('dashboard_paciente')
        
        # Respaldo: Si no tiene grupo pero tiene perfil de paciente
        elif hasattr(request.user, 'paciente'):
            return redirect('dashboard_paciente')

    # Lógica de Login (POST)
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)

        if user:
            login(request, user)
            # Al hacer login exitoso, llamamos a esta misma función (home) 
            # para que se ejecuten las redirecciones de arriba (el bloque if request.user.is_authenticated)
            return redirect('home') 
        else:
            messages.error(request, "Credenciales inválidas.")

    return render(request, 'core/home.html')

def activar_cuenta(request):
    if request.user.is_authenticated:
        return redirect('dashboard_paciente')

    if request.method == 'POST':
        form = ActivarCuentaForm(request.POST)
        if form.is_valid():
            rut_input = form.cleaned_data['rut'].strip().replace('.', '')
            email_input = form.cleaned_data['email'].strip().lower()
            password = form.cleaned_data['password_1']

            try:
                user = User.objects.get(username=rut_input)
                
                # Validaciones
                if user.has_usable_password():
                    messages.warning(request, "Usuario ya activo. Inicie sesión.")
                    return redirect('home')

                if user.email.strip().lower() != email_input:
                    messages.error(request, "El correo no coincide.")
                    return render(request, 'core/activar_cuenta.html', {'form': form})

                # Guardar clave
                user.set_password(password)
                user.save()
                
                # --- CAMBIO: LOGIN FORZADO ---
                # Esto evita problemas si authenticate falla
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                
                messages.success(request, f"¡Bienvenido {user.first_name}!")
                return redirect('dashboard_paciente')

            except User.DoesNotExist:
                messages.error(request, "RUT no encontrado.")
    else:
        form = ActivarCuentaForm()

    return render(request, 'core/activar_cuenta.html', {'form': form})
# --- VISTAS DE PACIENTE ---

@login_required
def dashboard_paciente(request):
    # Verificamos si el usuario tiene un perfil de paciente asociado
    # Usamos 'user' (el nombre del campo en el modelo), no 'usuario'
    if not hasattr(request.user, 'paciente'):
        messages.error(request, "No tienes un perfil de paciente asignado.")
        return redirect('home')
    
    paciente = request.user.paciente
    
    # Buscamos reservas futuras
    reservas = Reserva.objects.filter(
        paciente=paciente,
        fecha_reserva__fecha_disponible__gte=timezone.now()
    ).order_by('fecha_reserva__fecha_disponible')

    return render(request, 'pacientes/dashboard_paciente.html', {
        'reservas': reservas,
        'usuario': request.user
    })

@login_required
def mis_fichas(request):
    if not hasattr(request.user, 'paciente'): return redirect('home')
    fichas = FichaMedica.objects.filter(paciente=request.user.paciente).order_by('-fecha_creacion')
    return render(request, 'pacientes/mis_fichas.html', {'fichas': fichas})

@login_required
def mis_recetas(request):
    if not hasattr(request.user, 'paciente'): return redirect('home')
    recetas = FichaMedica.objects.filter(paciente=request.user.paciente).exclude(tratamiento="").order_by('-fecha_creacion')
    return render(request, 'pacientes/mis_recetas.html', {'recetas': recetas})

@login_required
def cancelar_reserva_paciente(request, reserva_id):
    # 1. Buscar la reserva
    reserva = get_object_or_404(Reserva, id=reserva_id)
    
    # 2. Verificar seguridad: Que la reserva pertenezca al usuario logueado
    if reserva.paciente.user != request.user:
        messages.error(request, "No tienes permiso para cancelar esta reserva.")
        return redirect('dashboard_paciente')
    
    # --- AQUÍ ESTÁ LA MAGIA PARA LIBERAR LA HORA ---
    disponibilidad = reserva.fecha_reserva  # Obtenemos el objeto Disponibilidad asociado
    disponibilidad.ocupada = False          # La marcamos como NO ocupada (Libre)
    disponibilidad.save()                   # Guardamos el cambio en la base de datos
    # -----------------------------------------------

    # 3. Eliminar la reserva
    reserva.delete()
    
    messages.success(request, "Reserva cancelada exitosamente. La hora ha quedado liberada.")
    return redirect('dashboard_paciente')
# --- VISTAS DE RECEPCIONISTA ---

@login_required
@role_required('Recepcionista')
def recepcionista_dashboard(request):
    return render(request, 'core/recepcionista.html')

@login_required
@user_passes_test(es_recepcionista)
def crear_paciente(request):
    if request.method == 'POST':
        form = PacienteForm(request.POST)
        if form.is_valid():
            try:
                rut = form.cleaned_data['rut']
                nombre = form.cleaned_data['nombre']
                email = form.cleaned_data['email']
                
                partes = nombre.strip().split()
                nom = partes[0]
                ape = partes[1] if len(partes) > 1 else ""

                if User.objects.filter(username=rut).exists():
                    messages.error(request, "Ya existe un usuario con este RUT.")
                    return render(request, 'pacientes/crear_paciente.html', {'form': form})

                # Crear usuario SIN contraseña usable
                user = User(username=rut, email=email)
                user.first_name = nom
                user.last_name = ape
                user.set_unusable_password() # <--- CLAVE: No puede loguearse hasta activarla
                user.save()
                
                grupo, _ = Group.objects.get_or_create(name='Paciente')
                user.groups.add(grupo)

                paciente = form.save(commit=False)
                paciente.user = user
                paciente.save()

                messages.success(request, "Paciente registrado. Indíquele que use 'Crear Contraseña' en el inicio.")
                return redirect('listar_pacientes')

            except Exception as e:
                messages.error(request, f"Error: {e}")
    else:
        form = PacienteForm()
    return render(request, 'pacientes/crear_paciente.html', {'form': form})

@login_required
@role_required('Recepcionista')
def listar_pacientes(request):
    rut_query = request.GET.get('rut', '')
    pacientes = Paciente.objects.filter(rut__icontains=rut_query).order_by('nombre') if rut_query else Paciente.objects.all().order_by('nombre')
    paginator = Paginator(pacientes, 5)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, 'pacientes/listar_pacientes.html', {'pacientes': page_obj, 'rut_query': rut_query})

@login_required
@role_required('Recepcionista')
def modificar_paciente(request, paciente_id):
    paciente = get_object_or_404(Paciente, id=paciente_id)
    if request.method == 'POST':
        paciente.nombre = request.POST.get('nombre')
        paciente.email = request.POST.get('email')
        paciente.telefono = request.POST.get('telefono')
        paciente.direccion = request.POST.get('direccion')
        paciente.save()
        messages.success(request, "Datos actualizados.")
        return redirect('listar_pacientes')
    return render(request, 'pacientes/modificar_paciente.html', {'paciente': paciente})

@login_required
@role_required('Recepcionista')
def eliminar_paciente(request, paciente_id):
    paciente = get_object_or_404(Paciente, id=paciente_id)
    if request.method == 'POST':
        paciente.delete()
        messages.success(request, "Paciente eliminado.")
    return redirect('listar_pacientes')

# --- VISTAS DE RESERVAS ---

@login_required
def crear_reserva(request):
    if request.method == 'POST':
        form = ReservaForm(request.POST)
        
        # Obtenemos el valor del input oculto 'rut_paciente'
        # Si no validaron, esto vendrá vacío ""
        rut_confirmado = request.POST.get('rut_paciente')

        # --- VALIDACIÓN DE SEGURIDAD ---
        # Si es recepcionista (no tiene perfil paciente)
        if not hasattr(request.user, 'paciente'):
            if not rut_confirmado:
                messages.error(request, "Error: Debe validar el RUT antes de crear la reserva.")
                return render(request, 'reservas/crear_reserva.html', {'form': form})

        if form.is_valid():
            reserva = form.save(commit=False)
            
            # Asignación
            if hasattr(request.user, 'paciente'):
                reserva.paciente = request.user.paciente
            else:
                # Recepcionista: Usamos el RUT que vino en el campo oculto
                try:
                    paciente_obj = Paciente.objects.get(rut=rut_confirmado)
                    reserva.paciente = paciente_obj
                except Paciente.DoesNotExist:
                    messages.error(request, "Error crítico: El paciente validado no existe.")
                    return render(request, 'reservas/crear_reserva.html', {'form': form})

            # Guardar
            reserva.save()
            
            # Marcar ocupada la disponibilidad
            disp = reserva.fecha_reserva
            disp.ocupada = True
            disp.save()
            
            messages.success(request, "Reserva creada exitosamente.")
            
            if hasattr(request.user, 'paciente'):
                return redirect('dashboard_paciente')
            return redirect('listar_reservas')
    else:
        form = ReservaForm()
        
    return render(request, 'reservas/crear_reserva.html', {'form': form})
@login_required
@role_required('Recepcionista')
def listar_reservas(request):
    fecha_inicio = request.GET.get('fecha_inicio')
    fecha_fin = request.GET.get('fecha_fin')
    reservas = Reserva.objects.all().order_by('-fecha_reserva')
    if fecha_inicio and fecha_fin:
        try:
            reservas = reservas.filter(fecha_reserva__fecha_disponible__range=[fecha_inicio, fecha_fin])
        except ValueError: pass

    paginator = Paginator(reservas, 5)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'reservas/listar_reservas.html', {
        'reservas': page_obj, 'fecha_inicio': fecha_inicio, 'fecha_fin': fecha_fin,
        'es_medico': request.user.groups.filter(name='Medico').exists()
    })

@login_required
@role_required('Recepcionista')
def modificar_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    especialidades = Especialidad.objects.all()
    medicos = Medico.objects.filter(especialidad=reserva.especialidad)
    disponibilidades = Disponibilidad.objects.filter(medico=reserva.medico, ocupada=False)

    if request.method == 'POST':
        nueva_disp_id = request.POST.get('fecha_reserva')
        nueva_disp = Disponibilidad.objects.get(id=nueva_disp_id)
        
        if reserva.fecha_reserva != nueva_disp:
            reserva.fecha_reserva.ocupada = False
            reserva.fecha_reserva.save()
            nueva_disp.ocupada = True
            nueva_disp.save()
            
        reserva.fecha_reserva = nueva_disp
        reserva.medico_id = request.POST.get('medico')
        reserva.motivo = request.POST.get('motivo')
        reserva.save()
        messages.success(request, "Reserva modificada.")
        return redirect('listar_reservas')

    return render(request, 'reservas/modificar_reserva.html', {
        'reserva': reserva, 'especialidades': especialidades, 
        'medicos': medicos, 'disponibilidades': disponibilidades
    })

@login_required
@role_required('Recepcionista')
def eliminar_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if request.method == 'POST':
        reserva.fecha_reserva.ocupada = False
        reserva.fecha_reserva.save()
        reserva.delete()
        return JsonResponse({"success": True})
    return JsonResponse({"error": "Error"}, status=405)

def obtener_reservas_activas(request):
    hora_actual = localtime(now())
    reservas = Reserva.objects.filter(fecha_reserva__fecha_disponible__gte=hora_actual)
    data = [{"id": r.id, "paciente": r.paciente.nombre, "hora": r.fecha_reserva.fecha_disponible.strftime('%H:%M')} for r in reservas]
    return JsonResponse(data, safe=False)

# --- VISTAS DE MÉDICO ---

@login_required
@role_required('Medico')
def medico_dashboard(request):
    medico = request.user.medico
    hoy = localtime(now()).date()
    reservas_hoy = Reserva.objects.filter(medico=medico, fecha_reserva__fecha_disponible__date=hoy)
    notif = Notificacion.objects.filter(usuario=request.user, leido=False)
    return render(request, 'core/medico.html', {'reservas_hoy': reservas_hoy, 'notificaciones': notif})

@login_required
@role_required('Medico')
def listar_fichas(request):
    fichas = FichaMedica.objects.all()
    rut = request.GET.get('rut')
    if rut: fichas = fichas.filter(paciente__rut__icontains=rut)
    paginator = Paginator(fichas, 10)
    return render(request, 'fichas_medicas/gestionar_fichas.html', {'fichas': paginator.get_page(request.GET.get('page'))})

@login_required
@role_required('Medico')
def crear_ficha_medica(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if request.method == 'POST':
        form = FichaMedicaForm(request.POST)
        if form.is_valid():
            ficha = form.save(commit=False)
            ficha.paciente = reserva.paciente
            ficha.medico = request.user.medico
            ficha.reserva = reserva
            ficha.save()
            messages.success(request, "Ficha creada.")
            return redirect('medico_dashboard')
    else:
        form = FichaMedicaForm()
    
    ctx = {
        'form': form, 'reserva': reserva, 'paciente': reserva.paciente,
        'medico_nombre': request.user.get_full_name(),
        'rut_medico': request.user.username,
        'edad': "No registrada"
    }
    return render(request, 'fichas_medicas/crear_ficha_medica.html', ctx)

@login_required
@role_required('Medico')
def modificar_ficha(request, ficha_id):
    ficha = get_object_or_404(FichaMedica, id=ficha_id)
    if request.method == 'POST':
        form = FichaMedicaForm(request.POST, instance=ficha)
        if form.is_valid():
            form.save()
            messages.success(request, "Ficha modificada.")
            return redirect('listar_fichas_medicas')
    else:
        form = FichaMedicaForm(instance=ficha)
    return render(request, 'fichas_medicas/modificar_ficha.html', {'form': form, 'ficha': ficha})

@login_required
@role_required('Medico')
def eliminar_ficha(request, ficha_id):
    ficha = get_object_or_404(FichaMedica, id=ficha_id)
    if request.method == 'POST':
        ficha.delete()
        messages.success(request, "Ficha eliminada.")
        return redirect('listar_fichas_medicas')
    return render(request, 'fichas_medicas/listar_fichas.html', {'ficha': ficha})

@login_required
@role_required('Medico')
def filtrar_fichas_medicas(request):
    fichas = FichaMedica.objects.all()
    rut = request.GET.get('rut')
    if rut: fichas = fichas.filter(paciente__rut__icontains=rut)
    paginator = Paginator(fichas, 5)
    return render(request, 'fichas_medicas/filtrar_fichas.html', {'fichas': paginator.get_page(request.GET.get('page')), 'rut_query': rut})

# --- LA FUNCIÓN QUE FALTABA (AGREGADA AQUÍ) ---
@login_required
@role_required('Medico')
def filtrar_fichas_por_paciente(request, paciente_rut):
    fichas = FichaMedica.objects.filter(paciente__rut=paciente_rut)
    return render(request, 'fichas_medicas/filtrar_fichas.html', {
        'fichas': fichas,
        'paciente_rut': paciente_rut
    })

@login_required
@role_required('Medico')
def gestionar_disponibilidades(request):
    medico = request.user.medico
    disponibilidades = disponibilidades = Disponibilidad.objects.filter(
    medico=medico
).order_by('-fecha_disponible')
    if request.method == 'POST':
        fecha = request.POST.get("fecha")
        hora = request.POST.get("hora")
        if fecha and hora:
            dt_naive = datetime.strptime(
                f"{fecha} {hora}", "%Y-%m-%d %H:%M"
            )
            dt_aware = timezone.make_aware(
                dt_naive,
                timezone.get_current_timezone()
            )
            Disponibilidad.objects.create(
                medico=medico,
                fecha_disponible=dt_aware,
                ocupada=False
            )
        return redirect('gestionar_disponibilidades')
    return render(request, 'fichas_medicas/gestionar_disponibilidades.html', {
        'disponibilidades': disponibilidades
    })

@login_required
@role_required('Medico')
def eliminar_disponibilidad(request, disponibilidad_id):
    disp = get_object_or_404(Disponibilidad, id=disponibilidad_id)
    if disp.medico == request.user.medico:
        disp.delete()
    return redirect('gestionar_disponibilidades')

def modificar_disponibilidad(request):
    if request.method == "POST":
        disp = Disponibilidad.objects.get(id=request.POST.get('disponibilidad_id'))

        dt_naive = datetime.strptime(
            f"{request.POST.get('fecha')} {request.POST.get('hora')}",
            "%Y-%m-%d %H:%M"
        )

        disp.fecha_disponible = timezone.make_aware(
            dt_naive,
            timezone.get_current_timezone()
        )

        disp.save()
        return redirect('gestionar_disponibilidades')

# --- VISTAS ADMIN ---

@login_required
@admin_or_superuser_required
def admin_dashboard(request):
    if not request.user.is_superuser and not request.user.groups.filter(name='Administrador').exists():
        return HttpResponseForbidden("Acceso denegado")
    ctx = {
        'total_medicos': Medico.objects.count(),
        'total_recepcionistas': Recepcionista.objects.count(),
        'total_pacientes': Paciente.objects.count(),
        'total_reservas': Reserva.objects.count(),
    }
    return render(request, 'core/admin_dashboard.html', ctx)

@login_required
@admin_or_superuser_required
def crear_medico(request):
    if request.method == 'POST':
        form = MedicoForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('listar_medicos')
    else:
        form = MedicoForm()
    return render(request, 'core/crear_medico.html', {'form': form})

@login_required
@admin_or_superuser_required
def listar_medicos(request):
    return render(request, 'core/listar_medicos.html', {'medicos': Medico.objects.all()})

@login_required
@admin_or_superuser_required
def modificar_medico(request, medico_id):
    medico = get_object_or_404(Medico, id=medico_id)
    if request.method == 'POST':
        form = MedicoForm(request.POST, instance=medico)
        if form.is_valid():
            m = form.save(commit=False)
            m.user.first_name = form.cleaned_data['first_name']
            m.user.last_name = form.cleaned_data['last_name']
            m.user.username = form.cleaned_data['username']
            m.user.save()
            m.save()
            return redirect('listar_medicos')
    else:
        form = MedicoForm(instance=medico)
        form.fields['first_name'].initial = medico.user.first_name
        form.fields['last_name'].initial = medico.user.last_name
        form.fields['username'].initial = medico.user.username
    return render(request, 'core/modificar_medico.html', {'form': form, 'medico': medico})

@login_required
@admin_or_superuser_required
def eliminar_medico(request, medico_id):
    m = get_object_or_404(Medico, id=medico_id)
    m.user.delete()
    m.delete()
    return redirect('listar_medicos')

@login_required
@admin_or_superuser_required
def crear_recepcionista(request):
    if request.method == 'POST':
        form = RecepcionistaForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('listar_recepcionistas')
    else:
        form = RecepcionistaForm()
    return render(request, 'core/crear_recepcionista.html', {'form': form})

@login_required
@admin_or_superuser_required
def listar_recepcionistas(request):
    return render(request, 'core/listar_recepcionistas.html', {'recepcionistas': Recepcionista.objects.all()})

@login_required
@admin_or_superuser_required
def modificar_recepcionista(request, recepcionista_id):
    rec = get_object_or_404(Recepcionista, id=recepcionista_id)
    if request.method == 'POST':
        rec.user.first_name = request.POST.get('first_name')
        rec.user.last_name = request.POST.get('last_name')
        rec.user.username = request.POST.get('username')
        rec.telefono = request.POST.get('telefono')
        rec.direccion = request.POST.get('direccion')
        rec.user.save()
        rec.save()
        return redirect('listar_recepcionistas')
    return render(request, 'core/modificar_recepcionista.html', {'recepcionista': rec})

@login_required
@admin_or_superuser_required
def eliminar_recepcionista(request, recepcionista_id):
    r = get_object_or_404(Recepcionista, id=recepcionista_id)
    r.user.delete()
    r.delete()
    return redirect('listar_recepcionistas')

# --- APIS Y OTROS ---

def generar_ficha_pdf(request, ficha_id):
    ficha = FichaMedica.objects.get(id=ficha_id)
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="ficha_{ficha_id}.pdf"'
    p = canvas.Canvas(response, pagesize=A4)
    p.drawString(100, 800, f"Ficha Médica: {ficha.paciente.nombre}")
    p.drawString(100, 780, f"Diagnóstico: {ficha.diagnostico}")
    p.drawString(100, 760, f"Tratamiento: {ficha.tratamiento}")
    p.showPage()
    p.save()
    return response

def api_medicos(request):
    eid = request.GET.get('especialidad_id')
    medicos = Medico.objects.filter(especialidad_id=eid)
    data = [{'id': m.id, 'nombre': f"{m.user.first_name} {m.user.last_name}"} for m in medicos]
    return JsonResponse(data, safe=False)

def api_disponibilidades(request):
    medico_id = request.GET.get("medico_id")
    reserva_id = request.GET.get("reserva_id")

    if not medico_id:
        return JsonResponse([], safe=False)

    medico = get_object_or_404(Medico, id=medico_id)
    disponibilidades = Disponibilidad.objects.filter(
        medico=medico,
        ocupada=False,
        fecha_disponible__gte=timezone.now()
    )

    if reserva_id and reserva_id.isdigit():
        reserva = get_object_or_404(Reserva, pk=reserva_id)
        disponibilidades = list(disponibilidades)
        disponibilidades.insert(0, reserva.fecha_reserva)

    disponibilidades = sorted(
        set(disponibilidades),
        key=lambda d: d.fecha_disponible
    )

    data = [
        {
            "id": d.id,
            "fecha_hora": timezone.localtime(d.fecha_disponible).strftime(
                "%d/%m/%Y %H:%M"
            )
        }
        for d in disponibilidades
    ]

    return JsonResponse(data, safe=False)

def api_validar_rut(request):
    rut = request.GET.get('rut')
    try:
        p = Paciente.objects.get(rut=rut)
        
        # Calcular la edad
        edad = "Sin fecha de nacimiento"
        if p.fecha_nacimiento:
            hoy = date.today()
            nacimiento = p.fecha_nacimiento
            # Calculo matemático de edad ajustando si ya pasó el cumpleaños este año o no
            calculo_edad = hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
            edad = f"{calculo_edad} años"

        # Devolvemos nombre y edad en el JSON
        return JsonResponse({
            'nombre': p.nombre,
            'edad': edad
        })
    except Paciente.DoesNotExist:
        return JsonResponse({'error': 'No encontrado'}, status=404)

@login_required
def marcar_notificacion_leida(request, notificacion_id):
    if request.method == 'POST':
        Notificacion.objects.filter(id=notificacion_id, usuario=request.user).update(leido=True)
        return JsonResponse({"success": True})
    return JsonResponse({"success": False}, status=405)

@login_required
def obtener_notificaciones(request):
    notifs = Notificacion.objects.filter(leido=False, usuario=request.user)
    data = [{"id": n.id, "mensaje": n.mensaje} for n in notifs]
    return JsonResponse(data, safe=False)