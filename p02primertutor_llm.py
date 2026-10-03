# ============================================================
# PRACTICA 2 - ENTRENADOR PERSONAL INTELIGENTE CON LLM (v3)
# ============================================================
#
# Objetivo: Implementación de un LLM.
#
#   1. Configuración del sistema distinta: agente entrenador personal
#      que se adapta al perfil del usuario (crear_mensaje_sistema).
#   2. Interfaz gráfica: panel lateral, chat con respuesta en tiempo
#      real, sugerencias rápidas y diseño de un solo color de acento.
#   3. Resumen del historial: ventana "Resumen del progreso".
#
# Modelo: llama3.2 (Ollama)
# Ejecución: streamlit run p02entrenador_llm_gui_v3.py
# ============================================================

import ollama
import streamlit as st

MODELO = "llama3.2:1b"   # si va lento: "llama3.2:1b"

NIVELES = ["Principiante", "Intermedio", "Avanzado"]
OBJETIVOS = ["Mejorar mi condición general", "Ganar masa muscular",
             "Perder grasa corporal", "Mejorar mi resistencia",
             "Ganar flexibilidad y movilidad"]
EQUIPOS = ["Sin equipo", "Mancuernas", "Barra y discos",
           "Ligas de resistencia", "Gimnasio completo"]

# (texto, icono Material, mensaje que se envía)
ACCIONES = [
    ("Rutina de hoy", ":material/fitness_center:",
     "Dame la rutina de hoy, paso a paso, con series, repeticiones y descansos."),
    ("Calentamiento", ":material/local_fire_department:",
     "Dame un calentamiento de 5 minutos para empezar a entrenar."),
    ("Estiramientos", ":material/self_improvement:",
     "Dame una rutina corta de estiramientos para después de entrenar."),
    ("Técnica correcta", ":material/lightbulb:",
     "Explícame la técnica correcta de un ejercicio de mi rutina y los errores comunes."),
]

PETICION_INICIO = ("Preséntate brevemente y proponme un plan para mi primera "
                   "semana, adaptado a mi perfil.")

AV_COACH, AV_USER = ":material/sports_gymnastics:", ":material/person:"


# ------------------------------------------------------------
# DISEÑO: un solo color de acento (naranja) sobre fondo oscuro
# ------------------------------------------------------------

ESTILO = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,1,0');
:root { --ac:#ff7a18; --bg:#121217; --panel:#17171d; --card:#22222b; --line:#2c2c36; }
html, body, [class*="css"] { font-family:'Poppins',sans-serif; }
[data-testid="stAppViewContainer"], [data-testid="stHeader"] { background:var(--bg); }
[data-testid="stSidebar"] { background:var(--panel); border-right:1px solid var(--line); }
.marca { display:flex; gap:12px; align-items:center; margin-bottom:20px; }
.logo { width:48px; height:48px; border-radius:14px; display:flex; align-items:center;
        justify-content:center; background:linear-gradient(135deg,#ff7a18,#ff3d6e); }
.ms { font-family:'Material Symbols Rounded'; font-size:28px; color:#fff; line-height:1; }
.marca b { font-size:1.2rem; } .marca span { display:block; font-size:.8rem; opacity:.65; }
.sec { font-size:.72rem; font-weight:600; letter-spacing:.08em; opacity:.6; margin:18px 0 8px; }
.tiles { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:10px 4px; text-align:center; }
.tile b { display:block; font-size:1.3rem; color:var(--ac); }
.tile span { font-size:.72rem; opacity:.7; }
.cab { display:flex; justify-content:space-between; align-items:center;
       border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:18px; }
.cab h2 { margin:0; font-size:1.7rem; } .cab p { margin:2px 0 0; font-size:.85rem; opacity:.65; }
.pill { background:var(--card); border-radius:999px; padding:6px 16px; font-size:.85rem;
        font-weight:600; display:flex; align-items:center; gap:8px; }
.dot { width:9px; height:9px; border-radius:50%; background:#22c55e; }
.dot.off { background:#ef4444; }
[data-testid="stChatMessage"] { background:var(--card); border:1px solid var(--line);
        border-radius:16px; padding:12px 16px; }
.stButton > button, .stFormSubmitButton > button { border-radius:14px; font-weight:500;
        border:1px solid var(--line); background:transparent; justify-content:flex-start; }
.stButton > button:hover { border-color:var(--ac); color:var(--ac); }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
        background:var(--ac); border:none; color:#fff; justify-content:center; }
.stButton span[data-testid="stIconMaterial"],
.stFormSubmitButton span[data-testid="stIconMaterial"] { color:var(--ac); }
button[kind="primary"] span[data-testid="stIconMaterial"] { color:#fff !important; }
[data-testid="stSidebar"] .stButton > button { border-radius:999px; }
</style>
"""

MARCA = """
<div class="marca"><div class="logo"><span class="ms">fitness_center</span></div>
<div><b>CoachBot</b><span>Entrenador personal</span></div></div>
"""


# ------------------------------------------------------------
# 1. CONFIGURACIÓN DEL SISTEMA (PUNTO 1)
# ------------------------------------------------------------

def crear_mensaje_sistema(p):
    equipo = ", ".join(p["equipo"])
    limit = p["limitaciones"].strip() or "Ninguna indicada"
    return f"""
Eres un entrenador personal profesional, motivador y cercano.

Perfil de tu cliente:
- Nombre: {p['nombre']}  - Edad: {p['edad']} años
- Nivel: {p['nivel']}  - Objetivo: {p['objetivo']}
- Días por semana: {p['dias']}  - Minutos por sesión: {p['minutos']}
- Equipo: {equipo}
- Lesiones o limitaciones: {limit}

Reglas:
1. Responde siempre en español, con tono motivador y claro.
2. Adapta cada rutina al perfil: nivel, tiempo, equipo y limitaciones.
3. Presenta rutinas con ejercicio, series, repeticiones y descanso.
4. Explica la técnica correcta y cómo evitar lesiones.
5. Aplica progresión gradual; nunca aumentos bruscos de carga.
6. Si el cliente reporta dolor, mareo o molestias fuertes, indícale
   detenerse y consultar a un médico o fisioterapeuta.
7. No recomiendes dietas extremas ni suplementos; para alimentación,
   sugiere acudir con un nutriólogo.
8. Si reporta una sesión terminada, felicítalo y ajusta el plan
   según el esfuerzo indicado.
"""


# ------------------------------------------------------------
# 2. FUNCIONES AUXILIARES
# ------------------------------------------------------------

def ollama_activo():
    try:
        ollama.list()
        return True
    except Exception:
        return False


def resumen_local(sesiones):
    n = len(sesiones)
    return {"sesiones": n,
            "minutos": sum(s["minutos"] for s in sesiones),
            "esfuerzo": round(sum(s["esfuerzo"] for s in sesiones) / n, 1) if n else None,
            "ultimas": sesiones[-5:]}


def resumen_con_llm(mensajes, sesiones):
    """Llamada aparte: no se agrega al historial del chat."""
    registro = "\n".join(f"- {s['ejercicio']}: {s['minutos']} min, esfuerzo {s['esfuerzo']}/10"
                         for s in sesiones) or "Sin sesiones registradas."
    conversacion = "\n".join(
        f"{'Cliente' if m['role'] == 'user' else 'Entrenador'}: {m['content']}"
        for m in mensajes[2:])
    peticion = [
        {"role": "system", "content": "Resumes el progreso de clientes de gimnasio, en español."},
        {"role": "user", "content": (
            "Escribe máximo 4 viñetas: logros, patrón que notas y una recomendación "
            f"para la próxima semana.\n\nREGISTRO:\n{registro}\n\nCONVERSACIÓN:\n{conversacion}")},
    ]
    return ollama.chat(model=MODELO, messages=peticion)["message"]["content"]


def enviar(texto):
    """Envía al LLM y muestra la respuesta en tiempo real."""
    oculto = len(st.session_state.mensajes) == 1     # petición de inicio
    st.session_state.mensajes.append({"role": "user", "content": texto})
    if not oculto:
        with st.chat_message("user", avatar=AV_USER):
            st.markdown(texto)
    with st.chat_message("assistant", avatar=AV_COACH):
        caja = st.empty()
        caja.markdown("*Tu entrenador está pensando...*")
        try:
            completo = ""
            for trozo in ollama.chat(model=MODELO, messages=st.session_state.mensajes,
                                     stream=True):
                completo += trozo["message"]["content"]
                caja.markdown(completo + "▌")
            caja.markdown(completo)
            st.session_state.mensajes.append({"role": "assistant", "content": completo})
        except Exception as error:
            caja.empty()
            st.session_state.mensajes.pop()
            st.session_state.error = str(error)
    st.rerun()


# ------------------------------------------------------------
# 3. VENTANAS EMERGENTES
# ------------------------------------------------------------

@st.dialog("Registrar sesión")
def dialogo_registro():
    ejercicio = st.text_input("¿Qué entrenaste?", placeholder="Ej.: pierna y core")
    minutos = st.number_input("Duración (min)", 5, 240, 45)
    esfuerzo = st.slider("Esfuerzo percibido (1-10)", 1, 10, 6)
    if st.button("Guardar sesión", type="primary", icon=":material/check:",
                 use_container_width=True):
        if not ejercicio.strip():
            st.warning("Describe brevemente qué entrenaste.")
            return
        st.session_state.sesiones.append({"ejercicio": ejercicio.strip(),
                                          "minutos": int(minutos), "esfuerzo": int(esfuerzo)})
        st.session_state.aviso = "Sesión guardada. ¡Sigue así!"
        st.session_state.pendiente = (
            f"Terminé mi sesión: {ejercicio.strip()}, {int(minutos)} minutos, "
            f"esfuerzo {int(esfuerzo)}/10. ¿Cómo ajustas mi plan?")
        st.rerun()


@st.dialog("Resumen del progreso", width="large")     # PUNTO 3
def dialogo_resumen():
    d = resumen_local(st.session_state.sesiones)
    c1, c2, c3 = st.columns(3)
    c1.metric("Sesiones", d["sesiones"])
    c2.metric("Minutos", d["minutos"])
    c3.metric("Esfuerzo promedio", d["esfuerzo"] if d["esfuerzo"] else "—")

    meta = st.session_state.perfil["dias"]
    st.progress(min(d["sesiones"] / meta, 1.0),
                text=f"Meta semanal: {d['sesiones']}/{meta} sesiones")

    st.markdown("**Últimas sesiones**")
    for s in reversed(d["ultimas"]):
        st.write(f"- {s['ejercicio']} — {s['minutos']} min (esfuerzo {s['esfuerzo']}/10)")
    if not d["ultimas"]:
        st.info("Aún no registras sesiones.")

    if st.button("Generar análisis con el LLM", icon=":material/psychology:"):
        try:
            with st.spinner("Analizando tu progreso..."):
                st.session_state.resumen = resumen_con_llm(
                    st.session_state.mensajes, st.session_state.sesiones)
        except Exception as error:
            st.error("No se pudo generar el análisis. Verifica que Ollama esté activo.")
            st.caption(str(error))
    if st.session_state.resumen:
        st.markdown(st.session_state.resumen)


# ------------------------------------------------------------
# 4. PÁGINA Y ESTADO
# ------------------------------------------------------------

st.set_page_config(page_title="Entrenador Personal con LLM", page_icon="🏋️", layout="wide")
st.markdown(ESTILO, unsafe_allow_html=True)

INICIALES = {"mensajes": [], "sesiones": [], "perfil": None, "iniciada": False,
             "pendiente": None, "resumen": None, "error": None, "aviso": None}
for k, v in INICIALES.items():
    if k not in st.session_state:
        st.session_state[k] = v

if st.session_state.aviso:
    st.toast(st.session_state.aviso, icon=":material/check_circle:")
    st.session_state.aviso = None

iniciada = st.session_state.iniciada


# ------------------------------------------------------------
# 5. BARRA LATERAL
# ------------------------------------------------------------

with st.sidebar:
    st.markdown(MARCA, unsafe_allow_html=True)
    st.markdown('<div class="sec">TU SESIÓN</div>', unsafe_allow_html=True)
    d = resumen_local(st.session_state.sesiones)
    st.markdown(
        f'<div class="tiles"><div class="tile"><b>{d["sesiones"]}</b><span>Sesiones</span></div>'
        f'<div class="tile"><b>{d["minutos"]}</b><span>Minutos</span></div>'
        f'<div class="tile"><b>{d["esfuerzo"] or "—"}</b><span>Esfuerzo</span></div></div>',
        unsafe_allow_html=True)

    if st.button("Resumen del progreso", type="primary", icon=":material/description:",
                 use_container_width=True, disabled=not iniciada):
        dialogo_resumen()
    if st.button("Registrar sesión", icon=":material/edit_note:",
                 use_container_width=True, disabled=not iniciada):
        dialogo_registro()
    if st.button("Nuevo plan", icon=":material/refresh:", use_container_width=True):
        for k, v in INICIALES.items():
            st.session_state[k] = [] if isinstance(v, list) else v
        st.rerun()

    st.markdown('<div class="sec">PRUEBA PREGUNTANDO</div>', unsafe_allow_html=True)
    for i, (texto, icono, frase) in enumerate(ACCIONES):
        if st.button(texto, icon=icono, key=f"q{i}", use_container_width=True,
                     disabled=not iniciada):
            st.session_state.pendiente = frase
            st.rerun()


# ------------------------------------------------------------
# 6. ZONA PRINCIPAL
# ------------------------------------------------------------

perfil = st.session_state.perfil
sub = (f"{perfil['nivel']} · {perfil['objetivo']} · {perfil['dias']} días/sem · "
       f"{perfil['minutos']} min" if perfil
       else f"Modelo {MODELO} ejecutado localmente con Ollama")
activo = ollama_activo()
pill = (f'<span class="pill"><i class="dot{"" if activo else " off"}"></i>'
        f'{"Conectado" if activo else "Sin conexión"}</span>')
st.markdown(f'<div class="cab"><div><h2>Entrenador Personal</h2><p>{sub}</p></div>{pill}</div>',
            unsafe_allow_html=True)

# ---- Pantalla inicial: Plan personalizado ----
if not iniciada:
    with st.container(border=True):
        st.markdown("### :material/assignment: Plan personalizado")
        st.caption("Cuéntame sobre ti y tu entrenador armará un plan a tu medida.")
        with st.form("perfil_form"):
            c1, c2 = st.columns(2)
            nombre = c1.text_input("Nombre")
            edad = c2.number_input("Edad", 14, 90, 25)
            nivel = c1.selectbox("Nivel de experiencia", NIVELES)
            objetivo = c2.selectbox("Objetivo principal", OBJETIVOS)
            dias = c1.slider("Días de entrenamiento por semana", 1, 7, 3)
            minutos = c2.slider("Minutos por sesión", 15, 120, 45, step=5)
            equipo = st.multiselect("Equipo disponible", EQUIPOS, default=["Sin equipo"])
            limitaciones = st.text_area("Lesiones o limitaciones (opcional)",
                                        placeholder="Ej.: molestia en la rodilla derecha")
            crear = st.form_submit_button("Crear mi plan", type="primary",
                                          icon=":material/play_arrow:",
                                          use_container_width=True)
        if crear:
            if not nombre.strip():
                st.warning("Escribe tu nombre para personalizar el plan.")
            elif not equipo:
                st.warning("Elige al menos una opción de equipo.")
            else:
                p = {"nombre": nombre.strip(), "edad": int(edad), "nivel": nivel,
                     "objetivo": objetivo, "dias": dias, "minutos": minutos,
                     "equipo": equipo, "limitaciones": limitaciones}
                st.session_state.perfil = p
                st.session_state.mensajes = [
                    {"role": "system", "content": crear_mensaje_sistema(p)}]
                st.session_state.iniciada = True
                st.session_state.pendiente = PETICION_INICIO
                st.rerun()
    st.stop()

# ---- Errores amigables ----
if st.session_state.error:
    st.error("No se pudo conectar con el entrenador (LLM).")
    st.write("Verifica que Ollama esté ejecutándose y el modelo descargado:")
    st.code(f"ollama serve &\nollama pull {MODELO}", language="bash")
    st.caption(st.session_state.error)
    st.session_state.error = None

# ---- Conversación (se omiten 'system' y la petición oculta de inicio) ----
for i, m in enumerate(st.session_state.mensajes):
    if i < 2:
        continue
    es_user = m["role"] == "user"
    with st.chat_message("user" if es_user else "assistant",
                         avatar=AV_USER if es_user else AV_COACH):
        st.markdown(m["content"])

if st.session_state.pendiente:
    texto = st.session_state.pendiente
    st.session_state.pendiente = None
    enviar(texto)

if len(st.session_state.mensajes) == 1:
    if st.button("Reintentar", icon=":material/refresh:"):
        st.session_state.pendiente = PETICION_INICIO
        st.rerun()

pregunta = st.chat_input("Pregúntale a tu entrenador lo que quieras...")
if pregunta:
    if pregunta.strip():
        st.session_state.pendiente = pregunta.strip()
        st.rerun()
    else:
        st.warning("Escribe una pregunta antes de enviar.")