#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 LogiSmart Python Suite  -  Centro de control inteligente (versión modular)
==============================================================================
Archivo ÚNICO del proyecto: TODO el código (MongoDB incluido) vive aquí; solo van
aparte el .env, requirements.txt y los .json de datos. Capas por secciones:

  Sección 0  -> Persistencia en MongoDB: conexión, CRUD de 5 colecciones, agregación
  Sección 1  -> Diseño PEAS del sistema
  Sección 2  -> Motor de reglas lógicas: A y E ORIGINALES + tablas de verdad
  Sección 2B -> Ampliación: 2 reglas nuevas (horario y somnolencia), decisión con
                explicación paso a paso, detección de conflictos/redundancias y
                registro de cada decisión en MongoDB (colección 'accesos')
  Sección 3  -> Clasificador de incidentes por reglas + correo + salida JSON
  Sección 3B -> Clasificador HÍBRIDO: LLM (Ollama) con JSON validado con pydantic,
                reintento, respaldo por reglas, fusión (seguridad primero) y
                experimento con 36 correos etiquetados (exactitud, matriz, latencia)
  Sección 4  -> Clase EvaluadorRiesgosIA (matriz de riesgos éticos)
  Sección 4B -> Asistente explicativo (RAG: primero consulta MongoDB, después el LLM,
                con citas) + datos de demostración
  Sección 5  -> Interfaz gráfica (Streamlit): panel, control de acceso, simulador,
                bandeja de incidentes, asistente (chat), CRUD de las 5 colecciones
                y configuración
  Pruebas    -> unittest para las secciones 0 (validaciones), 2, 2B, 3, 3B y 4

Uso:
  python logiuncodigo.py            -> ejecuta la demostración completa
  python logiuncodigo.py --tests    -> ejecuta solo las pruebas unitarias
  streamlit run logiuncodigo.py     -> abre la INTERFAZ GRÁFICA en el navegador
  python logiuncodigo.py --mongo    -> prueba la conexión y el CRUD en MongoDB
  python logiuncodigo.py --evaluar  -> experimento de clasificación (reglas vs LLM vs híbrido)
                                       opciones: --sin-llm (solo reglas)  --guardar (a MongoDB)
  python logiuncodigo.py --preguntar "¿Por qué CAM-102 fue enviado a inspección?"
  python logiuncodigo.py --demo-datos      -> carga datos de demostración en MongoDB
  python logiuncodigo.py --limpiar-demo    -> borra solo los datos de demostración

El motor de reglas, el clasificador por reglas y la matriz de riesgos usan solo
la biblioteca estándar. MongoDB:  pip install "pymongo[srv]" python-dotenv
El clasificador híbrido usa:      pip install ollama pydantic
==============================================================================
"""

# -----------------------------------------------------------------------------
# IMPORTACIONES
# -----------------------------------------------------------------------------
import itertools          # Para generar todas las combinaciones de la tabla de verdad
import json               # Para serializar la salida estricta en JSON
import logging            # Para mensajes de diagnóstico SIN contaminar la salida JSON
import os                 # Para leer credenciales SMTP desde variables de entorno
import re                 # Expresiones regulares para extraer datos del correo
import smtplib            # Cliente SMTP para el envío real del correo
import statistics         # Media y mediana de las latencias del experimento
import sys                # Para leer argumentos de línea de comandos
import time               # Medición de latencias (perf_counter)
import unittest           # Marco de pruebas unitarias de la biblioteca estándar
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

# Los mensajes de log van a stderr; el JSON se entrega solo por el valor de retorno.
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("logismart")


# =============================================================================
# SECCIÓN 0: PERSISTENCIA EN MONGODB (Atlas o local)
# =============================================================================
# Contiene: configuración (.env), conexión con manejo de errores, CRUD de las 5
# colecciones (camiones, accesos, incidentes, riesgos_eticos, evaluaciones_llm),
# la agregación de incidentes por categoría y semana y los indicadores del panel.
#
# Las credenciales NUNCA se escriben en el código: se leen del archivo .env
#   mongo_user, mongo_password, mongo_closter (o mongo_cluster), mongo_db
#   mongo_prefijo (opcional: se antepone al nombre de cada colección; útil porque
#                  la base del curso es compartida entre varios alumnos)
#   o bien mongo_uri con la cadena de conexión completa.
#
# Esta capa no sabe nada de la GUI ni del LLM: solo persiste y consulta datos.

try:
    from bson import ObjectId
    from bson.errors import InvalidId
    from dotenv import load_dotenv
    from pymongo import DESCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError, PyMongoError
    MONGO_DISPONIBLE = True
except ImportError:                     # sin pymongo el resto del programa sigue funcionando
    MONGO_DISPONIBLE = False
    ObjectId = MongoClient = None
    DESCENDING = -1

    class PyMongoError(Exception):
        pass

    class DuplicateKeyError(PyMongoError):
        pass

    class InvalidId(Exception):
        pass

    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()


def _env(*nombres: str, defecto: str = "") -> str:
    """Lee la primera variable de entorno definida (acepta minúsculas o MAYÚSCULAS)."""
    for nombre in nombres:
        valor = os.getenv(nombre) or os.getenv(nombre.upper())
        if valor:
            return valor.strip()
    return defecto


def construir_uri(usuario: str, clave: str, cluster: str) -> str:
    """Arma la URI de Atlas codificando caracteres especiales de usuario y contraseña."""
    return (f"mongodb+srv://{quote_plus(usuario)}:{quote_plus(clave)}@{cluster}"
            "/?retryWrites=true&w=majority&appName=logismart")


def _uri_desde_entorno() -> str:
    uri = _env("mongo_uri")
    if uri:
        return uri
    usuario, clave = _env("mongo_user"), _env("mongo_password")
    cluster = _env("mongo_closter", "mongo_cluster")
    return construir_uri(usuario, clave, cluster) if usuario and clave and cluster else ""


MONGO_URI = _uri_desde_entorno()
MONGO_DB = _env("mongo_db", defecto="logismart")
PREFIJO_COLECCIONES = _env("mongo_prefijo")

# Puntaje (1-25) a partir del cual un riesgo se considera crítico.
UMBRAL_RIESGO_CRITICO = 17


def nombre_col(base: str) -> str:
    """Nombre real de la colección en la base (con el prefijo opcional)."""
    return f"{PREFIJO_COLECCIONES}{base}"


# =============================================================================
# CONEXIÓN
# =============================================================================

class ErrorConexion(Exception):
    """Error de conexión o de consulta a MongoDB, con mensaje apto para el usuario."""


_cliente = None


def obtener_db():
    """Devuelve la base de datos; conecta la primera vez y reutiliza la conexión."""
    global _cliente
    if not MONGO_DISPONIBLE:
        raise ErrorConexion('Faltan las bibliotecas de MongoDB. Instala: pip install "pymongo[srv]" python-dotenv')
    if _cliente is None:
        if not MONGO_URI:
            raise ErrorConexion(
                "Faltan las credenciales de MongoDB. Define mongo_user, mongo_password, "
                "mongo_closter y mongo_db en el archivo .env (ver .env.example)."
            )
        try:
            cliente = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
            cliente.admin.command("ping")          # comprueba que realmente responde
            _cliente = cliente
        except PyMongoError as error:
            raise ErrorConexion(
                "No se pudo conectar a MongoDB. Revisa tu internet, la URI del .env "
                "y que tu IP esté permitida en Atlas (Network Access)."
            ) from error
    return _cliente[MONGO_DB]


def probar_conexion():
    """Devuelve (True, mensaje) si hay conexión, o (False, motivo) si no."""
    try:
        obtener_db()
        return True, f"Conectado a la base '{MONGO_DB}'."
    except ErrorConexion as error:
        return False, str(error)


# =============================================================================
# REPOSITORIOS (CRUD, agregación e indicadores)
# =============================================================================

ESTADOS_INCIDENTE = ("nuevo", "en_atencion", "cerrado")


# ------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------

def ahora():
    return datetime.now(timezone.utc)


def _oid(id_):
    try:
        return ObjectId(id_)
    except (InvalidId, TypeError):
        raise ValueError("Identificador de registro no válido.")


def _limpiar(doc):
    if doc is not None:
        doc["_id"] = str(doc["_id"])
    return doc


def _rango(campo, desde, hasta):
    """Filtro de fechas para MongoDB. 'desde' y 'hasta' son datetime o None."""
    r = {}
    if desde:
        r["$gte"] = desde
    if hasta:
        r["$lte"] = hasta
    return {campo: r} if r else {}


def _seguro(funcion):
    """Convierte errores de PyMongo en mensajes amigables."""
    @wraps(funcion)
    def envoltura(*args, **kwargs):
        try:
            return funcion(*args, **kwargs)
        except DuplicateKeyError as error:
            raise ValueError("Ya existe un registro con ese valor único (por ejemplo, la placa).") from error
        except PyMongoError as error:
            raise ErrorConexion(
                "Error al consultar MongoDB. Verifica tu conexión e inténtalo de nuevo."
            ) from error
    return envoltura


# ------------------------------------------------------------
# Repositorio base (CRUD completo)
# ------------------------------------------------------------

class Repositorio:
    coleccion = ""

    @property
    def col(self):
        return obtener_db()[nombre_col(self.coleccion)]

    def _validar(self, datos, parcial=False):
        """Las subclases validan y normalizan. 'parcial' = actualización."""
        return datos

    def _preparar(self, datos):
        return self._validar(dict(datos))

    @_seguro
    def crear(self, datos):
        resultado = self.col.insert_one(self._preparar(datos))
        return str(resultado.inserted_id)

    @_seguro
    def obtener(self, id_):
        return _limpiar(self.col.find_one({"_id": _oid(id_)}))

    @_seguro
    def listar(self, filtro=None, limite=200, orden=None):
        cursor = self.col.find(filtro or {}).sort(orden or [("_id", DESCENDING)]).limit(limite)
        return [_limpiar(d) for d in cursor]

    @_seguro
    def actualizar(self, id_, cambios):
        cambios = self._validar(dict(cambios), parcial=True)
        r = self.col.update_one({"_id": _oid(id_)}, {"$set": cambios})
        return r.matched_count == 1

    @_seguro
    def eliminar(self, id_):
        return self.col.delete_one({"_id": _oid(id_)}).deleted_count == 1


# ------------------------------------------------------------
# camiones
# ------------------------------------------------------------

class Camiones(Repositorio):
    """Campos: placa, camion_id, empresa, autorizacion, certificacion_conductor."""
    coleccion = "camiones"

    def _validar(self, d, parcial=False):
        for campo in ("placa", "camion_id"):
            if not parcial or campo in d:
                valor = str(d.get(campo, "")).strip().upper()
                if not valor:
                    raise ValueError(f"El campo '{campo}' es obligatorio.")
                d[campo] = valor
        for campo in ("autorizacion", "certificacion_conductor"):
            if campo in d:
                d[campo] = bool(d[campo])
            elif not parcial:
                d[campo] = False
        if not parcial:
            d.setdefault("empresa", "")
        return d

    @_seguro
    def buscar_por_placa(self, placa):
        return _limpiar(self.col.find_one({"placa": str(placa).strip().upper()}))

    @_seguro
    def buscar_por_camion_id(self, camion_id):
        return _limpiar(self.col.find_one({"camion_id": str(camion_id).strip().upper()}))


# ------------------------------------------------------------
# accesos (bitácora)
# ------------------------------------------------------------

class Accesos(Repositorio):
    """Campos: P, Q, R, S, A, E, timestamp, operador, explicacion (lista de pasos)."""
    coleccion = "accesos"

    def _validar(self, d, parcial=False):
        for variable in ("P", "Q", "R", "S", "A", "E"):
            if variable in d:
                d[variable] = bool(d[variable])
            elif not parcial:
                raise ValueError(f"Falta la variable {variable}.")
        if not parcial:
            d.setdefault("operador", "sistema")
            d.setdefault("explicacion", [])
            d.setdefault("timestamp", ahora())
        return d

    @_seguro
    def historial_de(self, camion_id=None, placa=None, limite=50):
        filtro = {}
        if camion_id:
            filtro["camion_id"] = str(camion_id).strip().upper()
        if placa:
            filtro["placa"] = str(placa).strip().upper()
        cursor = self.col.find(filtro).sort("timestamp", DESCENDING).limit(limite)
        return [_limpiar(d) for d in cursor]


# ------------------------------------------------------------
# incidentes
# ------------------------------------------------------------

class Incidentes(Repositorio):
    """Campos: correo_original, clasificacion, datos_extraidos, estado, historial."""
    coleccion = "incidentes"

    def _validar(self, d, parcial=False):
        if not parcial:
            if not str(d.get("correo_original", "")).strip():
                raise ValueError("El correo no puede estar vacío.")
            d.setdefault("clasificacion", {})
            d.setdefault("datos_extraidos", {})
            d.setdefault("estado", "nuevo")
            d.setdefault("requiere_revision_humana", False)
            d.setdefault("creado", ahora())
            d.setdefault("historial", [{"fecha": ahora(), "evento": "Incidente creado"}])
        if "estado" in d and d["estado"] not in ESTADOS_INCIDENTE:
            raise ValueError(f"Estado no válido. Opciones: {', '.join(ESTADOS_INCIDENTE)}.")
        return d

    @_seguro
    def cambiar_estado(self, id_, nuevo_estado, operador="sistema"):
        if nuevo_estado not in ESTADOS_INCIDENTE:
            raise ValueError(f"Estado no válido. Opciones: {', '.join(ESTADOS_INCIDENTE)}.")
        r = self.col.update_one(
            {"_id": _oid(id_)},
            {"$set": {"estado": nuevo_estado},
             "$push": {"historial": {"fecha": ahora(), "operador": operador,
                                     "evento": f"Estado cambiado a '{nuevo_estado}'"}}},
        )
        return r.matched_count == 1

    @_seguro
    def editar_clasificacion(self, id_, clasificacion, operador="sistema"):
        """Permite al operador corregir el resultado de la clasificación (queda en el historial)."""
        r = self.col.update_one(
            {"_id": _oid(id_)},
            {"$set": {"clasificacion": clasificacion, "requiere_revision_humana": False},
             "$push": {"historial": {"fecha": ahora(), "operador": operador,
                                     "evento": "Clasificación editada manualmente"}}},
        )
        return r.matched_count == 1

    @_seguro
    def por_categoria_y_semana(self, desde=None, hasta=None):
        """AGREGACIÓN: cantidad de incidentes por categoría y semana ISO."""
        tuberia = []
        filtro = _rango("creado", desde, hasta)
        if filtro:
            tuberia.append({"$match": filtro})
        tuberia += [
            {"$group": {
                "_id": {
                    "categoria": "$clasificacion.categoria",
                    "semana": {"$dateToString": {"format": "%G-W%V", "date": "$creado"}},
                },
                "total": {"$sum": 1},
            }},
            {"$sort": {"_id.semana": 1, "_id.categoria": 1}},
            {"$project": {"_id": 0, "semana": "$_id.semana",
                          "categoria": "$_id.categoria", "total": 1}},
        ]
        return list(self.col.aggregate(tuberia))


# ------------------------------------------------------------
# riesgos_eticos
# ------------------------------------------------------------

def nivel_riesgo(puntaje):
    if puntaje <= 4:
        return "Bajo"
    if puntaje <= 9:
        return "Medio"
    if puntaje <= 16:
        return "Alto"
    return "Crítico"


class Riesgos(Repositorio):
    """Matriz de riesgos: puntaje = probabilidad x impacto, antes y después de mitigar."""
    coleccion = "riesgos_eticos"

    CAMPOS_NUM = ("probabilidad", "impacto", "probabilidad_residual", "impacto_residual")

    def _validar(self, d, parcial=False):
        if not parcial:
            for campo in ("modulo", "descripcion", "categoria", "mitigacion"):
                if not str(d.get(campo, "")).strip():
                    raise ValueError(f"El campo '{campo}' es obligatorio.")
        for campo in self.CAMPOS_NUM:
            if campo in d:
                try:
                    valor = int(d[campo])
                except (TypeError, ValueError):
                    raise ValueError(f"'{campo}' debe ser un número entero entre 1 y 5.")
                if not 1 <= valor <= 5:
                    raise ValueError(f"'{campo}' debe estar entre 1 y 5.")
                d[campo] = valor
        return d

    @staticmethod
    def _con_puntajes(d):
        if "probabilidad" not in d or "impacto" not in d:
            raise ValueError("Indica probabilidad e impacto (1 a 5).")
        d.setdefault("probabilidad_residual", d["probabilidad"])
        d.setdefault("impacto_residual", d["impacto"])
        d["puntaje_inicial"] = d["probabilidad"] * d["impacto"]
        d["puntaje_residual"] = d["probabilidad_residual"] * d["impacto_residual"]
        d["nivel_inicial"] = nivel_riesgo(d["puntaje_inicial"])
        d["nivel_residual"] = nivel_riesgo(d["puntaje_residual"])
        return d

    def _preparar(self, datos):
        d = self._con_puntajes(self._validar(dict(datos)))
        d.setdefault("historico", [])
        d.setdefault("creado", ahora())
        return d

    @_seguro
    def actualizar(self, id_, cambios):
        """Actualiza, recalcula puntajes y guarda una foto del estado anterior en 'historico'."""
        actual = self.col.find_one({"_id": _oid(id_)})
        if not actual:
            return False
        cambios = self._validar(dict(cambios), parcial=True)
        nuevo = {k: v for k, v in actual.items() if k not in ("_id", "historico")}
        nuevo.update(cambios)
        nuevo = self._con_puntajes(nuevo)
        foto = {"fecha": ahora()}
        foto.update({k: actual.get(k) for k in self.CAMPOS_NUM + ("puntaje_inicial", "puntaje_residual")})
        self.col.update_one({"_id": actual["_id"]}, {"$set": nuevo, "$push": {"historico": foto}})
        return True


# ------------------------------------------------------------
# evaluaciones_llm
# ------------------------------------------------------------

class EvaluacionesLLM(Repositorio):
    """Campos: prompt, respuesta, modelo, latencia_ms, coincidio_reglas, timestamp."""
    coleccion = "evaluaciones_llm"

    def _validar(self, d, parcial=False):
        if not parcial:
            for campo in ("prompt", "modelo"):
                if not str(d.get(campo, "")).strip():
                    raise ValueError(f"El campo '{campo}' es obligatorio.")
            d.setdefault("respuesta", "")
            d.setdefault("latencia_ms", 0)
            d.setdefault("coincidio_reglas", None)
            d.setdefault("timestamp", ahora())
        return d


# ------------------------------------------------------------
# Instancias listas para usar + índices + indicadores del panel
# ------------------------------------------------------------

repo_camiones = Camiones()
repo_accesos = Accesos()
repo_incidentes = Incidentes()
repo_riesgos = Riesgos()
repo_evaluaciones = EvaluacionesLLM()


@_seguro
def crear_indices():
    """Crea los índices (es seguro ejecutarlo varias veces)."""
    db = obtener_db()
    db[nombre_col("camiones")].create_index("placa", unique=True)
    db[nombre_col("camiones")].create_index("camion_id")
    db[nombre_col("accesos")].create_index([("timestamp", DESCENDING)])
    db[nombre_col("accesos")].create_index("camion_id")
    db[nombre_col("incidentes")].create_index("estado")
    db[nombre_col("incidentes")].create_index("creado")
    db[nombre_col("riesgos_eticos")].create_index("modulo")
    db[nombre_col("evaluaciones_llm")].create_index([("timestamp", DESCENDING)])


@_seguro
def indicadores_panel(desde=None, hasta=None, umbral_critico=UMBRAL_RIESGO_CRITICO):
    """Indicadores del panel de control, con filtro opcional por fechas."""
    db = obtener_db()
    atendidos = db[nombre_col("accesos")].distinct("camion_id", _rango("timestamp", desde, hasta))
    filtro_abiertos = {"estado": {"$ne": "cerrado"}}
    filtro_abiertos.update(_rango("creado", desde, hasta))
    return {
        "camiones_atendidos": len(atendidos),
        "incidentes_abiertos": db[nombre_col("incidentes")].count_documents(filtro_abiertos),
        "riesgos_criticos": db[nombre_col("riesgos_eticos")].count_documents(
            {"puntaje_inicial": {"$gte": umbral_critico}}),
    }


# =============================================================================
# PRUEBA DE HUMO  ->  python logiuncodigo.py --mongo
# =============================================================================

def _paso(texto):
    print(f"  ✔ {texto}")


def probar_mongo():
    print("1) Conexión")
    ok, mensaje = probar_conexion()
    print(f"  {'✔' if ok else '✘'} {mensaje}")
    if not ok:
        return

    ids = {}
    try:
        crear_indices()
        _paso("Índices creados")

        print("2) CREATE")
        ids["camion"] = repo_camiones.crear({
            "placa": "tst-001", "camion_id": "cam-test", "empresa": "Prueba SA",
            "autorizacion": True, "certificacion_conductor": True})
        _paso("camiones")
        ids["acceso"] = repo_accesos.crear({
            "camion_id": "CAM-TEST", "placa": "TST-001", "P": 1, "Q": 0, "R": 0, "S": 1,
            "A": True, "E": False, "operador": "prueba",
            "explicacion": ["P=V, S=V y Q=F -> A=V"]})
        _paso("accesos")
        ids["incidente"] = repo_incidentes.crear({
            "correo_original": "Se descompuso el camion CAM-TEST en la entrada",
            "clasificacion": {"categoria": "falla_mecanica", "prioridad": "alta"}})
        _paso("incidentes")
        ids["riesgo"] = repo_riesgos.crear({
            "modulo": "clasificador", "descripcion": "Alucinaciones del LLM",
            "categoria": "fiabilidad", "probabilidad": 4, "impacto": 4,
            "mitigacion": "Validar con pydantic y respaldo por reglas",
            "probabilidad_residual": 2, "impacto_residual": 3})
        _paso("riesgos_eticos")
        ids["eval"] = repo_evaluaciones.crear({
            "prompt": "clasifica este correo", "respuesta": "{}", "modelo": "llama3.2:1b",
            "latencia_ms": 850, "coincidio_reglas": True})
        _paso("evaluaciones_llm")

        print("3) READ")
        camion = repo_camiones.buscar_por_placa("TST-001")
        _paso(f"Camión encontrado por placa: {camion['camion_id']}")
        riesgo = repo_riesgos.obtener(ids["riesgo"])
        _paso(f"Riesgo: {riesgo['puntaje_inicial']} ({riesgo['nivel_inicial']}) "
             f"-> residual {riesgo['puntaje_residual']} ({riesgo['nivel_residual']})")

        print("4) UPDATE")
        repo_incidentes.cambiar_estado(ids["incidente"], "en_atencion", operador="prueba")
        _paso("Estado del incidente -> en_atencion (queda en el historial)")
        repo_riesgos.actualizar(ids["riesgo"], {"probabilidad_residual": 1})
        _paso("Riesgo actualizado (se guardó una foto en 'historico')")

        print("5) AGREGACIÓN y PANEL")
        print("  ", repo_incidentes.por_categoria_y_semana())
        print("  ", indicadores_panel())

        print("6) Validaciones (deben fallar con mensaje claro)")
        for texto, accion in [
            ("placa vacía", lambda: repo_camiones.crear({"placa": "", "camion_id": "X"})),
            ("estado inválido", lambda: repo_incidentes.cambiar_estado(ids["incidente"], "roto")),
            ("probabilidad 9", lambda: repo_riesgos.actualizar(ids["riesgo"], {"probabilidad": 9})),
        ]:
            try:
                accion()
                print(f"  ✘ {texto}: NO falló")
            except ValueError as error:
                _paso(f"{texto}: «{error}»")

    except (ErrorConexion, ValueError) as error:
        print(f"  ✘ Error: {error}")

    finally:
        print("7) DELETE (limpieza)")
        colecciones = {"camion": repo_camiones, "acceso": repo_accesos,
                       "incidente": repo_incidentes, "riesgo": repo_riesgos,
                       "eval": repo_evaluaciones}
        for nombre, id_ in ids.items():
            try:
                colecciones[nombre].eliminar(id_)
            except (ErrorConexion, ValueError):
                pass
        _paso("Registros de prueba eliminados")


# =============================================================================
# SECCIÓN 1: DISEÑO PEAS (Performance, Environment, Actuators, Sensors)
# =============================================================================
# PEAS describe a un agente racional definiendo:
#   P = Medida de desempeño : ¿cómo sabemos que el agente lo hace bien?
#   E = Entorno             : ¿dónde opera y cómo es (propiedades)?
#   A = Actuadores          : ¿con qué actúa sobre el entorno?
#   S = Sensores            : ¿con qué percibe el entorno?
# Se modela como diccionario para poder imprimirlo, exportarlo o probarlo.

PEAS_LOGISMART: Dict[str, object] = {
    "agente": "Agente de control inteligente de acceso y seguridad logística LogiSmart",
    "P_desempeno": [
        "Tiempo promedio de atención por camión (minutos) -> minimizar",
        "% de camiones autorizados que ingresan sin fricción -> maximizar",
        "% de camiones no autorizados / con sobrepeso detenidos -> maximizar (meta 100%)",
        "Falsos positivos de inspección especial -> minimizar",
        "Falsos negativos (carga peligrosa que no fue inspeccionada) -> minimizar (meta 0)",
        "Incidentes de seguridad y accidentes en patio -> minimizar",
        "Tiempo de clasificación y respuesta a incidentes de soporte -> minimizar",
    ],
    "E_entorno": {
        "descripcion": "Patio de maniobras, casetas de acceso, básculas y andenes del centro logístico",
        "propiedades": {
            "observabilidad": "Parcialmente observable (sensores con ruido, mala visión nocturna)",
            "determinismo": "Estocástico (llegadas, clima y fallas impredecibles)",
            "episodico": "Secuencial (una decisión afecta el tráfico posterior)",
            "dinamico": "Dinámico (el entorno cambia mientras el agente decide)",
            "discreto": "Mixto (estados discretos de acceso; peso y posición continuos)",
            "agentes": "Multiagente (conductores, personal, otros sistemas)",
        },
        "actores_externos": ["Conductores", "Operadores de caseta", "Personal de seguridad", "Autoridades"],
    },
    "A_actuadores": [
        "Barrera vehicular (abrir / cerrar)",
        "Semáforo y pantallas de instrucciones al conductor",
        "Alarma sonora y luminosa",
        "Sistema de asignación de andén / carril de inspección especial",
        "Notificaciones (correo, SMS, panel) a supervisores y soporte",
        "Registro en base de datos (bitácora de accesos e incidentes)",
    ],
    "S_sensores": [
        "Cámaras con lector de placas (LPR) y cámara de somnolencia del conductor",
        "Báscula de piso (peso del vehículo)",
        "Lector RFID / QR de autorización previa",
        "Escáner de documentos (certificación del conductor)",
        "Sensores de detección de materiales peligrosos / lectura de placas de riesgo (NOM)",
        "Buzón de correo de soporte (entrada de texto de incidentes)",
    ],
}


def imprimir_peas(peas: Dict[str, object] = PEAS_LOGISMART) -> None:
    """Imprime el marco PEAS de forma legible en consola."""
    print("=" * 78)
    print("SECCIÓN 1 - MARCO PEAS")
    print("=" * 78)
    print(f"Agente: {peas['agente']}\n")
    print("P (Medidas de desempeño):")
    for item in peas["P_desempeno"]:
        print(f"   - {item}")
    entorno = peas["E_entorno"]
    print(f"\nE (Entorno): {entorno['descripcion']}")
    for nombre, valor in entorno["propiedades"].items():
        print(f"   - {nombre}: {valor}")
    print("\nA (Actuadores):")
    for item in peas["A_actuadores"]:
        print(f"   - {item}")
    print("\nS (Sensores):")
    for item in peas["S_sensores"]:
        print(f"   - {item}")
    print()


# =============================================================================
# SECCIÓN 2: MOTOR DE REGLAS LÓGICAS (LÓGICA PROPOSICIONAL)
# =============================================================================
# Premisas (variables booleanas):
#   P: Vehículo con autorización previa
#   Q: El peso excede el límite
#   R: Carga con materiales peligrosos
#   S: Conductor con certificación vigente
#
# Fórmulas:
#   A (Acceso Estándar)      = P ∧ S ∧ ¬Q
#   E (Inspección Especial)  = P ∧ (R ∨ Q)
#
# En Python:  ∧ -> and,   ∨ -> or,   ¬ -> not

def evaluar_camion(P: bool, Q: bool, R: bool, S: bool) -> Dict[str, bool]:
    """
    Evalúa las reglas de acceso e inspección para un camión.

    Parámetros
    ----------
    P : bool  -> ¿Tiene autorización previa?
    Q : bool  -> ¿El peso excede el límite?
    R : bool  -> ¿Lleva materiales peligrosos?
    S : bool  -> ¿El conductor tiene certificación vigente?

    Retorna
    -------
    dict con:
        "acceso_estandar"       (A): True si se permite el acceso estándar
        "inspeccion_especial"   (E): True si debe activarse inspección especial
    """
    # Validación defensiva: las premisas deben ser booleanas estrictas.
    # (Evita que un 1, "si" o None pase silenciosamente y dé resultados ambiguos.)
    for nombre, valor in (("P", P), ("Q", Q), ("R", R), ("S", S)):
        if not isinstance(valor, bool):
            raise TypeError(f"La premisa {nombre} debe ser bool, se recibió {type(valor).__name__}")

    # A = P ∧ S ∧ ¬Q  -> autorizado, conductor certificado y SIN exceso de peso
    acceso_estandar = P and S and (not Q)

    # E = P ∧ (R ∨ Q) -> autorizado y (carga peligrosa O exceso de peso)
    inspeccion_especial = P and (R or Q)

    return {"acceso_estandar": acceso_estandar, "inspeccion_especial": inspeccion_especial}


def generar_tabla_verdad() -> List[Dict[str, bool]]:
    """
    Genera la tabla de verdad completa (2^4 = 16 filas) con columnas intermedias.

    Las columnas intermedias (¬Q, P∧S, R∨Q) permiten justificar paso a paso
    cómo se llega a A y a E.
    Orden de filas: convención clásica (de V,V,V,V hasta F,F,F,F).
    """
    filas: List[Dict[str, bool]] = []
    # itertools.product([True, False], repeat=4) produce las 16 combinaciones de (P,Q,R,S)
    for P, Q, R, S in itertools.product([True, False], repeat=4):
        resultado = evaluar_camion(P, Q, R, S)
        filas.append({
            "P": P, "Q": Q, "R": R, "S": S,
            "no_Q": not Q,             # ¬Q
            "P_y_S": P and S,          # P ∧ S
            "R_o_Q": R or Q,           # R ∨ Q
            "A": resultado["acceso_estandar"],
            "E": resultado["inspeccion_especial"],
        })
    return filas


def imprimir_tablas_verdad() -> None:
    """Imprime las tablas de verdad de A y de E (columna intermedia incluida)."""
    v = lambda b: "V" if b else "F"   # Traduce True/False a V/F para leer mejor
    tabla = generar_tabla_verdad()

    print("=" * 78)
    print("SECCIÓN 2 - TABLAS DE VERDAD")
    print("=" * 78)

    # --- Tabla A --------------------------------------------------------------
    print("\nTabla 1: A = P ∧ S ∧ ¬Q")
    print(f"{'P':^3}{'Q':^3}{'S':^3} | {'¬Q':^4}{'P∧S':^6}{'A':^4}")
    print("-" * 28)
    vistos = set()   # A no depende de R: mostramos solo las 8 combinaciones (P,Q,S) únicas
    for f in tabla:
        clave = (f["P"], f["Q"], f["S"])
        if clave in vistos:
            continue
        vistos.add(clave)
        print(f"{v(f['P']):^3}{v(f['Q']):^3}{v(f['S']):^3} | "
              f"{v(f['no_Q']):^4}{v(f['P_y_S']):^6}{v(f['A']):^4}")

    # --- Tabla E --------------------------------------------------------------
    print("\nTabla 2: E = P ∧ (R ∨ Q)")
    print(f"{'P':^3}{'Q':^3}{'R':^3} | {'R∨Q':^5}{'E':^4}")
    print("-" * 24)
    vistos = set()   # E no depende de S: 8 combinaciones (P,Q,R) únicas
    for f in tabla:
        clave = (f["P"], f["Q"], f["R"])
        if clave in vistos:
            continue
        vistos.add(clave)
        print(f"{v(f['P']):^3}{v(f['Q']):^3}{v(f['R']):^3} | "
              f"{v(f['R_o_Q']):^5}{v(f['E']):^4}")

    # --- Tabla completa conjunta ---------------------------------------------
    print("\nTabla 3: tabla completa (16 combinaciones) con A y E")
    print(f"{'P':^3}{'Q':^3}{'R':^3}{'S':^3} | {'¬Q':^4}{'P∧S':^6}{'R∨Q':^6} | {'A':^3}{'E':^3}")
    print("-" * 42)
    for f in tabla:
        print(f"{v(f['P']):^3}{v(f['Q']):^3}{v(f['R']):^3}{v(f['S']):^3} | "
              f"{v(f['no_Q']):^4}{v(f['P_y_S']):^6}{v(f['R_o_Q']):^6} | "
              f"{v(f['A']):^3}{v(f['E']):^3}")

    # --- Conclusiones para justificar ----------------------------------------
    n_A = sum(f["A"] for f in tabla)
    n_E = sum(f["E"] for f in tabla)
    n_ambas = sum(f["A"] and f["E"] for f in tabla)
    print(f"\nA es verdadera en {n_A}/16 combinaciones; E en {n_E}/16.")
    print(f"Ambas verdaderas simultáneamente en {n_ambas}/16 casos "
          "(P∧S∧¬Q∧R: acceso estándar Y revisión por carga peligrosa).")
    print("Observaciones: sin P (autorización previa) tanto A como E son siempre F;")
    print("si Q es V, A es siempre F (el exceso de peso bloquea el acceso estándar) y, con P, E es V.\n")


# =============================================================================
# SECCIÓN 2B: AMPLIACIÓN DEL MOTOR DE REGLAS (2 REGLAS NUEVAS + EXPLICACIÓN)
# =============================================================================
# Se CONSERVAN intactas A = P ∧ S ∧ ¬Q y E = P ∧ (R ∨ Q) (Sección 2).
# Se agregan dos premisas y dos reglas nuevas, justificadas con los sensores
# del diseño PEAS (Sección 1):
#
#   H: la entrada ocurre DENTRO del horario permitido para materiales peligrosos
#   D: la cámara detecta SOMNOLENCIA en el conductor
#
#   Regla nueva 3 (Horario restringido):  RH = P ∧ R ∧ ¬H  -> REPROGRAMAR la entrada
#       Justificación: la carga peligrosa solo debe maniobrar en la ventana
#       horaria en que hay personal de seguridad y supervisión disponible.
#   Regla nueva 4 (Somnolencia):          B  = P ∧ D        -> BLOQUEO preventivo
#       Justificación: un conductor con fatiga es un riesgo en el patio aunque
#       el resto de las premisas esté en orden (usa la cámara del PEAS).
#
# Cuando varias reglas se activan a la vez gana la de MAYOR PRIORIDAD
# (ante la duda, seguridad primero):  B > RH > E > A.  Si ninguna se activa,
# el resultado por defecto es DENEGADO.

HORARIO_PELIGROSOS = (dtime(6, 0), dtime(20, 0))   # ventana permitida (configurable)


def en_horario_permitido(hora: Optional[dtime] = None) -> bool:
    """True si 'hora' (por defecto, la actual) cae dentro de la ventana de peligrosos."""
    hora = hora or datetime.now().time()
    inicio, fin = HORARIO_PELIGROSOS
    return inicio <= hora <= fin


@dataclass(frozen=True)
class Regla:
    """Una regla del motor: condición lógica, acción y prioridad (1 = la más importante)."""
    id: str
    nombre: str
    formula: str
    condicion: Callable[[Dict[str, bool]], bool]
    accion: str
    prioridad: int
    premisas: Tuple[str, ...]
    justificacion: str


REGLAS: List[Regla] = [
    Regla("B", "Bloqueo por somnolencia", "P ∧ D",
          lambda v: v["P"] and v["D"], "BLOQUEADO_SOMNOLENCIA", 1, ("P", "D"),
          "Un conductor con fatiga es un riesgo en el patio aunque todo lo demás esté en orden."),
    Regla("RH", "Horario restringido de materiales peligrosos", "P ∧ R ∧ ¬H",
          lambda v: v["P"] and v["R"] and not v["H"], "REPROGRAMAR", 2, ("P", "R", "H"),
          "La carga peligrosa solo maniobra en la ventana con supervisión de seguridad."),
    Regla("E", "Inspección especial", "P ∧ (R ∨ Q)",
          lambda v: v["P"] and (v["R"] or v["Q"]), "INSPECCION_ESPECIAL", 3, ("P", "R", "Q"),
          "Regla original: carga peligrosa o exceso de peso exigen inspección."),
    Regla("A", "Acceso estándar", "P ∧ S ∧ ¬Q",
          lambda v: v["P"] and v["S"] and not v["Q"], "ACCESO_ESTANDAR", 4, ("P", "S", "Q"),
          "Regla original: autorizado, conductor certificado y sin exceso de peso."),
]

ACCION_POR_DEFECTO = "DENEGADO"

SEMAFORO: Dict[str, str] = {
    "ACCESO_ESTANDAR": "verde",
    "INSPECCION_ESPECIAL": "amarillo",
    "REPROGRAMAR": "amarillo",
    "BLOQUEADO_SOMNOLENCIA": "rojo",
    "DENEGADO": "rojo",
}

PREMISAS: Dict[str, str] = {
    "P": "autorización previa", "Q": "peso excede el límite",
    "R": "carga con materiales peligrosos", "S": "conductor con certificación vigente",
    "H": "dentro del horario permitido", "D": "somnolencia detectada",
}


def _vf(valor: bool) -> str:
    return "V" if valor else "F"


def _decidir(valores: Dict[str, bool], reglas: List[Regla]) -> Optional[Regla]:
    """Devuelve la regla de mayor prioridad entre las activadas (o None)."""
    activadas = sorted((r for r in reglas if r.condicion(valores)), key=lambda r: r.prioridad)
    return activadas[0] if activadas else None


def evaluar_decision(P: bool, Q: bool, R: bool, S: bool, H: bool = True, D: bool = False,
                     reglas: Optional[List[Regla]] = None) -> Dict[str, object]:
    """
    Decisión completa de acceso con EXPLICACIÓN PASO A PASO.

    Conserva A y E originales (se calculan con evaluar_camion) y añade las reglas
    nuevas. Por defecto H=True (en horario) y D=False (sin somnolencia), de modo que
    con solo (P, Q, R, S) el resultado coincide con el comportamiento original.
    """
    valores = {"P": P, "Q": Q, "R": R, "S": S, "H": H, "D": D}
    for nombre, valor in valores.items():
        if not isinstance(valor, bool):
            raise TypeError(f"La premisa {nombre} debe ser bool, se recibió {type(valor).__name__}")

    reglas = REGLAS if reglas is None else reglas
    base = evaluar_camion(P, Q, R, S)                      # A y E originales
    activadas = sorted((r for r in reglas if r.condicion(valores)), key=lambda r: r.prioridad)
    ganadora = activadas[0] if activadas else None
    accion = ganadora.accion if ganadora else ACCION_POR_DEFECTO

    # ---- Explicación paso a paso ----
    pasos: List[str] = ["Premisas: " + ", ".join(f"{k}={_vf(v)} ({PREMISAS[k]})" for k, v in valores.items())]
    for r in sorted(reglas, key=lambda r: r.prioridad):
        usadas = ", ".join(f"{p}={_vf(valores[p])}" for p in r.premisas)
        pasos.append(f"Regla {r.id} ({r.nombre}) [{r.formula}] con {usadas} -> {_vf(r.condicion(valores))}")
    if len(activadas) > 1:
        pasos.append(f"Conflicto: se activaron {len(activadas)} reglas ({', '.join(r.id for r in activadas)}); "
                     f"prevalece {ganadora.id} por prioridad (seguridad primero).")
    if ganadora:
        causa = {p: valores[p] for p in ganadora.premisas}
        pasos.append("Causa de la decisión: " + ", ".join(f"{p}={_vf(v)}" for p, v in causa.items()))
    elif not P:
        causa = {"P": P}
        pasos.append("Ninguna regla se activó: sin autorización previa (P=F) nada puede permitir el paso.")
    else:
        causa = {"P": P, "S": S}
        pasos.append("Ninguna regla se activó: conductor sin certificación vigente (S=F) y sin carga "
                     "peligrosa ni exceso de peso.")
    pasos.append(f"Resultado final: {accion} (semáforo {SEMAFORO.get(accion, 'rojo')})")

    return {
        **valores,
        "A": base["acceso_estandar"], "E": base["inspeccion_especial"],
        "resultado": accion, "semaforo": SEMAFORO.get(accion, "rojo"),
        "reglas_activadas": [r.id for r in activadas],
        "regla_ganadora": ganadora.id if ganadora else None,
        "premisas_causa": causa, "explicacion": pasos,
    }


# ----------------------- Tablas de verdad de las reglas nuevas -----------------------
def generar_tabla_reglas_nuevas() -> Dict[str, List[Dict[str, bool]]]:
    """Tablas de verdad de RH (8 filas) y B (4 filas), con columnas intermedias."""
    rh = [{"P": P, "R": R, "H": H, "no_H": not H, "P_y_R": P and R, "RH": P and R and (not H)}
          for P, R, H in itertools.product([True, False], repeat=3)]
    b = [{"P": P, "D": D, "B": P and D} for P, D in itertools.product([True, False], repeat=2)]
    return {"RH": rh, "B": b}


def generar_tabla_decision() -> List[Dict[str, object]]:
    """Tabla completa de decisiones con las 6 premisas (2^6 = 64 filas)."""
    return [evaluar_decision(*c) for c in itertools.product([True, False], repeat=6)]


def imprimir_tablas_reglas_nuevas() -> None:
    v = lambda b: "V" if b else "F"
    t = generar_tabla_reglas_nuevas()
    print("=" * 78)
    print("SECCIÓN 2B - TABLAS DE VERDAD DE LAS REGLAS NUEVAS")
    print("=" * 78)
    print("\nTabla 4: RH = P ∧ R ∧ ¬H  (carga peligrosa fuera de horario -> REPROGRAMAR)")
    print(f"{'P':^3}{'R':^3}{'H':^3} | {'¬H':^4}{'P∧R':^6}{'RH':^4}")
    print("-" * 28)
    for f in t["RH"]:
        print(f"{v(f['P']):^3}{v(f['R']):^3}{v(f['H']):^3} | "
              f"{v(f['no_H']):^4}{v(f['P_y_R']):^6}{v(f['RH']):^4}")
    print("\nTabla 5: B = P ∧ D  (somnolencia detectada -> BLOQUEO preventivo)")
    print(f"{'P':^3}{'D':^3} | {'B':^4}")
    print("-" * 14)
    for f in t["B"]:
        print(f"{v(f['P']):^3}{v(f['D']):^3} | {v(f['B']):^4}")
    n_rh = sum(f["RH"] for f in t["RH"])
    n_b = sum(f["B"] for f in t["B"])
    print(f"\nRH es verdadera en {n_rh}/8 combinaciones (solo P=V, R=V, H=F); "
          f"B en {n_b}/4 (solo P=V y D=V).\n")


# ----------------------- Reto opcional: contradicciones y redundancias -----------------------
def analizar_reglas(reglas: Optional[List[Regla]] = None) -> Dict[str, object]:
    """
    Recorre las 64 combinaciones de premisas y detecta:
      - CONFLICTOS: dos reglas que se activan a la vez con acciones distintas.
        Si tienen la misma prioridad quedan 'sin resolver' (contradicción real).
      - REDUNDANCIAS: reglas que, al quitarlas, no cambian ninguna decisión.
    """
    reglas = REGLAS if reglas is None else reglas
    combos = [dict(zip("PQRSHD", c)) for c in itertools.product([True, False], repeat=6)]

    conflictos: Dict[str, int] = {}
    sin_resolver = 0
    for valores in combos:
        activas = [r for r in reglas if r.condicion(valores)]
        for r1, r2 in itertools.combinations(activas, 2):
            if r1.accion != r2.accion:
                clave = " vs ".join(sorted((r1.id, r2.id)))
                conflictos[clave] = conflictos.get(clave, 0) + 1
                if r1.prioridad == r2.prioridad:
                    sin_resolver += 1

    redundantes = []
    for r in reglas:
        resto = [x for x in reglas if x is not r]
        iguales = all(
            (getattr(_decidir(v, reglas), "accion", ACCION_POR_DEFECTO)
             == getattr(_decidir(v, resto), "accion", ACCION_POR_DEFECTO)) for v in combos)
        if iguales:
            redundantes.append(r.id)

    return {"combinaciones": len(combos), "conflictos": conflictos,
            "sin_resolver": sin_resolver, "redundantes": redundantes}


def imprimir_analisis_reglas() -> None:
    a = analizar_reglas()
    print("=" * 78)
    print("SECCIÓN 2B - ANÁLISIS DE CONTRADICCIONES Y REDUNDANCIAS (reto opcional)")
    print("=" * 78)
    print(f"Combinaciones evaluadas: {a['combinaciones']}")
    for par, n in a["conflictos"].items():
        print(f"  - Reglas {par}: se activan a la vez en {n} combinaciones (se resuelve por prioridad)")
    print(f"Conflictos SIN resolver (misma prioridad, acciones distintas): {a['sin_resolver']}")
    print("Reglas redundantes: " + (", ".join(a["redundantes"]) or "ninguna") + "\n")


# ----------------------- Persistencia de cada decisión (MongoDB) -----------------------
def registrar_decision(decision: Dict[str, object], placa: Optional[str] = None,
                       camion_id: Optional[str] = None, operador: str = "operador",
                       extra: Optional[Dict[str, object]] = None) -> str:
    """Guarda la decisión CON su explicación paso a paso en la colección 'accesos'."""
    campos = ("P", "Q", "R", "S", "H", "D", "A", "E", "resultado", "semaforo",
              "reglas_activadas", "regla_ganadora", "explicacion")
    doc = {k: decision[k] for k in campos}
    doc.update({"placa": placa.strip().upper() if placa else None,
                "camion_id": camion_id.strip().upper() if camion_id else None,
                "operador": operador})
    doc.update(extra or {})
    return repo_accesos.crear(doc)


def premisas_por_placa(placa: str) -> Optional[Dict[str, object]]:
    """Busca el camión en MongoDB y devuelve sus premisas P (autorización) y S (certificación)."""
    camion = repo_camiones.buscar_por_placa(placa)
    if not camion:
        return None
    return {"P": bool(camion["autorizacion"]), "S": bool(camion["certificacion_conductor"]),
            "camion": camion}


# =============================================================================
# SECCIÓN 3: CLASIFICADOR DE INCIDENTES Y EXTRACCIÓN -> JSON ESTRICTO
# =============================================================================
# Flujo:
#   1) Se recibe el correo del incidente (remitente, asunto, cuerpo).
#   2) Se CLASIFICA (categoría y prioridad) con reglas por palabras clave.
#   3) Se EXTRAE información estructurada con expresiones regulares.
#   4) Se ENVÍA el correo de soporte (SMTP real o simulación).
#   5) Se DEVUELVE únicamente un JSON válido (string), nada más.
#
# Nota: el clasificador es basado en reglas para que funcione sin internet.
# Podría sustituirse por una llamada a un LLM manteniendo el mismo contrato JSON.

# Categorías con sus palabras clave (en minúsculas y sin acentos, ver _normalizar)
CATEGORIAS: Dict[str, List[str]] = {
    "materiales_peligrosos": ["peligroso", "derrame", "fuga", "quimico", "inflamable", "toxico", "corrosivo"],
    "sobrepeso": ["sobrepeso", "excede", "bascula", "exceso de peso", "sobrecarga"],
    "acceso_no_autorizado": ["sin autorizacion", "no autorizado", "acceso denegado", "barrera", "intruso"],
    "falla_hardware": ["camara", "sensor", "lector", "rfid", "no enciende", "apagado", "danado", "falla electrica"],
    "falla_software": ["sistema", "error", "pantalla", "caido", "no carga", "lento", "software", "aplicacion"],
    "somnolencia_conductor": ["somnolencia", "dormido", "cansancio", "fatiga", "sueno"],
}

# Palabras que elevan la prioridad sin importar la categoría
PALABRAS_URGENTES = ["urgente", "emergencia", "accidente", "incendio", "herido", "critico", "inmediato"]

# Prioridad base por categoría (se puede escalar con palabras urgentes)
PRIORIDAD_BASE: Dict[str, str] = {
    "materiales_peligrosos": "critica",
    "somnolencia_conductor": "alta",
    "acceso_no_autorizado": "alta",
    "sobrepeso": "media",
    "falla_hardware": "media",
    "falla_software": "baja",
    "otro": "baja",
}
ORDEN_PRIORIDAD = ["baja", "media", "alta", "critica"]


def _normalizar(texto: str) -> str:
    """Pasa a minúsculas y quita acentos/ñ para comparar palabras clave sin sorpresas."""
    tabla = str.maketrans("áéíóúüñ", "aeiouun")
    return texto.lower().translate(tabla)


def clasificar_incidente(asunto: str, cuerpo: str) -> Dict[str, object]:
    """
    Clasifica el incidente por conteo de palabras clave.
    Devuelve categoría, prioridad y las palabras que dispararon la decisión.
    """
    texto = _normalizar(f"{asunto} {cuerpo}")

    # Puntuación por categoría = número de palabras clave encontradas
    puntajes: Dict[str, List[str]] = {
        cat: [kw for kw in kws if kw in texto] for cat, kws in CATEGORIAS.items()
    }
    # Elegimos la categoría con más coincidencias; en empate gana la primera del diccionario
    # (materiales peligrosos primero: ante la duda, se privilegia la seguridad).
    mejor_cat = max(puntajes, key=lambda c: len(puntajes[c]))
    if not puntajes[mejor_cat]:
        mejor_cat = "otro"
        coincidencias: List[str] = []
    else:
        coincidencias = puntajes[mejor_cat]

    # Prioridad: base por categoría y escalamiento de un nivel si hay palabras urgentes
    prioridad = PRIORIDAD_BASE[mejor_cat]
    urgentes = [p for p in PALABRAS_URGENTES if p in texto]
    if urgentes:
        idx = min(ORDEN_PRIORIDAD.index(prioridad) + 1, len(ORDEN_PRIORIDAD) - 1)
        prioridad = ORDEN_PRIORIDAD[idx]

    return {
        "categoria": mejor_cat,
        "prioridad": prioridad,
        "palabras_clave": coincidencias + urgentes,
    }


def extraer_datos(asunto: str, cuerpo: str) -> Dict[str, Optional[object]]:
    """
    Extrae entidades del texto con expresiones regulares.
    Cada campo que no se encuentre queda en None (null en JSON), nunca se inventa.
    """
    texto = f"{asunto}\n{cuerpo}"

    # Placa tipo mexicano: ABC-123-D (3 caracteres - 2/3 dígitos - 1/2 caracteres)
    m_placa = re.search(r"\b[A-Z0-9]{2,3}-\d{2,3}-[A-Z0-9]{1,2}\b", texto.upper())

    # ID interno de camión: CAM-102, CAM-7, etc.
    m_camion = re.search(r"\bCAM-\d+\b", texto.upper())

    # Peso reportado: "48.5 toneladas", "48,5 t", "32000 kg"
    m_peso = re.search(r"(\d+(?:[.,]\d+)?)\s*(toneladas|tonelada|ton|t|kg)\b", texto.lower())
    peso_kg: Optional[float] = None
    if m_peso:
        valor = float(m_peso.group(1).replace(",", "."))
        peso_kg = valor if m_peso.group(2) == "kg" else valor * 1000  # todo a kilogramos

    # Ubicación: "andén 3", "puerta B", "muelle 2", "caseta norte"
    m_ubic = re.search(r"\b(and[eé]n|puerta|muelle|caseta|dock)\s+([A-Za-z0-9]+)", texto, re.IGNORECASE)

    return {
        "placa": m_placa.group(0) if m_placa else None,
        "camion_id": m_camion.group(0) if m_camion else None,
        "peso_reportado_kg": peso_kg,
        "ubicacion": f"{m_ubic.group(1)} {m_ubic.group(2)}".lower() if m_ubic else None,
    }


def enviar_correo_soporte(remitente: str, destinatario: str, asunto: str, cuerpo: str,
                          simulacion: bool = True) -> Dict[str, object]:
    """
    Envía un correo de soporte por SMTP.

    - simulacion=True  (por defecto): NO se conecta a ningún servidor; solo construye
      el mensaje. Ideal para clase y pruebas unitarias.
    - simulacion=False: usa variables de entorno SMTP_HOST, SMTP_PORT, SMTP_USER,
      SMTP_PASSWORD (nunca escribas contraseñas en el código).

    Devuelve un dict con el estado del envío (no lanza excepción por fallo de red;
    el fallo se reporta en el JSON final para no romper el flujo del sistema).
    """
    # Construcción del mensaje MIME estándar
    msg = EmailMessage()
    msg["From"] = remitente
    msg["To"] = destinatario
    msg["Subject"] = asunto
    msg.set_content(cuerpo)

    if simulacion:
        log.info("Envío SIMULADO de correo a %s (asunto: %s)", destinatario, asunto)
        return {"enviado": True, "modo": "simulacion", "error": None}

    try:
        host = os.environ["SMTP_HOST"]
        puerto = int(os.environ.get("SMTP_PORT", "587"))
        usuario = os.environ["SMTP_USER"]
        clave = os.environ["SMTP_PASSWORD"]
        with smtplib.SMTP(host, puerto, timeout=15) as servidor:
            servidor.starttls()               # Cifra la conexión antes de autenticarse
            servidor.login(usuario, clave)
            servidor.send_message(msg)
        log.info("Correo enviado a %s", destinatario)
        return {"enviado": True, "modo": "smtp", "error": None}
    except (KeyError, OSError, smtplib.SMTPException) as exc:
        # KeyError: falta variable de entorno; OSError/SMTPException: fallo de red o servidor
        log.error("No se pudo enviar el correo: %s", exc)
        return {"enviado": False, "modo": "smtp", "error": f"{type(exc).__name__}: {exc}"}


def procesar_incidente(remitente: str, asunto: str, cuerpo: str,
                       destinatario_soporte: str = "soporte@logismart.example",
                       simulacion: bool = True) -> str:
    """
    FUNCIÓN PRINCIPAL DE LA SECCIÓN 3.

    Clasifica + extrae + envía correo de soporte y devuelve ESTRICTAMENTE un JSON
    (cadena de texto válida, sin texto adicional antes o después).
    """
    clasificacion = clasificar_incidente(asunto, cuerpo)
    datos = extraer_datos(asunto, cuerpo)

    # Asunto normalizado para el equipo de soporte, con prioridad visible
    asunto_soporte = f"[{clasificacion['prioridad'].upper()}] {clasificacion['categoria']} - {asunto}"
    cuerpo_soporte = (
        f"Incidente reportado por: {remitente}\n"
        f"Categoría: {clasificacion['categoria']}\n"
        f"Prioridad: {clasificacion['prioridad']}\n"
        f"Datos extraídos: {json.dumps(datos, ensure_ascii=False)}\n\n"
        f"Mensaje original:\n{cuerpo}"
    )
    envio = enviar_correo_soporte(remitente, destinatario_soporte, asunto_soporte,
                                  cuerpo_soporte, simulacion=simulacion)

    resultado = {
        "fecha_procesamiento": datetime.now().isoformat(timespec="seconds"),
        "remitente": remitente,
        "asunto_original": asunto,
        "clasificacion": clasificacion,
        "datos_extraidos": datos,
        "correo_soporte": {"destinatario": destinatario_soporte, **envio},
    }

    # json.dumps garantiza sintaxis válida (comillas dobles, null, true/false).
    salida = json.dumps(resultado, ensure_ascii=False, indent=2)
    # Autoverificación: si esto falla, jamás entregamos un JSON inválido.
    json.loads(salida)
    return salida


# =============================================================================
# SECCIÓN 3B: CLASIFICADOR HÍBRIDO (REGLAS + LLM LOCAL CON OLLAMA)
# =============================================================================
# Flujo para cada correo:
#   1) Las REGLAS (Sección 3) clasifican siempre: son rápidas y funcionan sin internet.
#   2) El LLM recibe el correo y debe devolver un JSON con el esquema EXACTO
#      (categoria, prioridad, entidades, resumen).
#   3) La respuesta se VALIDA con pydantic. Si es inválida se REINTENTA una vez
#      (diciéndole al modelo qué falló). Si sigue mal, o si Ollama no responde,
#      se usa el clasificador por REGLAS como plan de respaldo.
#   4) FUSIÓN: si LLM y reglas discrepan, prevalece la PRIORIDAD MÁS ALTA (ante la
#      duda, seguridad) y se marca requiere_revision_humana = True.
#   5) Las entidades que invente el LLM y no aparezcan en el correo se descartan
#      (mitigación del riesgo ético de alucinaciones).

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
    PYDANTIC_OK = True
except ImportError:
    PYDANTIC_OK = False

try:
    import ollama
except ImportError:
    ollama = None

OLLAMA_MODEL = _env("ollama_model", defecto="llama3.2:1b")   # editable en la GUI (Configuración)
OLLAMA_TIMEOUT = 120          # segundos máximos de espera por respuesta
MAX_INTENTOS_LLM = 2          # 1 intento + 1 reintento con corrección
CATEGORIAS_VALIDAS: List[str] = list(CATEGORIAS) + ["otro"]
RUTA_CORREOS = Path(__file__).with_name("correos_etiquetados.json")
RUTA_RESULTADOS = Path(__file__).with_name("resultados_evaluacion.json")

# ----------------------- Esquema JSON validado con pydantic -----------------------
if PYDANTIC_OK:
    class EntidadesLLM(BaseModel):
        """Entidades extraídas por el LLM (mismos campos que extraer_datos)."""
        model_config = ConfigDict(extra="ignore")
        placa: Optional[str] = None
        camion_id: Optional[str] = None
        peso_reportado_kg: Optional[float] = None
        ubicacion: Optional[str] = None

        @field_validator("placa", "camion_id", "ubicacion", mode="before")
        @classmethod
        def _texto_o_nulo(cls, v):
            if v is None or str(v).strip().lower() in ("", "null", "none", "n/a"):
                return None
            return str(v).strip()

        @field_validator("peso_reportado_kg", mode="before")
        @classmethod
        def _peso_numerico(cls, v):
            # Solo se acepta un número; texto como "48 toneladas" no se interpreta.
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    class ClasificacionLLM(BaseModel):
        """Esquema EXACTO que debe devolver el LLM (extra='forbid': no se permiten campos de más)."""
        model_config = ConfigDict(extra="forbid")
        categoria: str
        prioridad: str
        entidades: EntidadesLLM
        resumen: str = Field(min_length=3, max_length=400)

        @field_validator("categoria", "prioridad", mode="before")
        @classmethod
        def _normalizar_texto(cls, v):
            return _normalizar(str(v).strip())

        @field_validator("categoria")
        @classmethod
        def _categoria_valida(cls, v):
            if v not in CATEGORIAS_VALIDAS:
                raise ValueError(f"categoria '{v}' no es válida")
            return v

        @field_validator("prioridad")
        @classmethod
        def _prioridad_valida(cls, v):
            if v not in ORDEN_PRIORIDAD:
                raise ValueError(f"prioridad '{v}' no es válida")
            return v

    ESQUEMA_JSON: object = ClasificacionLLM.model_json_schema()
else:
    ESQUEMA_JSON = "json"

SISTEMA_CLASIFICADOR = (
    "Eres un clasificador de correos de incidentes para el control de acceso de camiones de LogiSmart. "
    "Los correos pueden tener faltas de ortografía, abreviaturas o lenguaje informal: interpreta la intención. "
    "Responde ÚNICAMENTE con un objeto JSON válido, sin markdown ni texto adicional."
)


def construir_prompt_llm(correo: str) -> str:
    categorias = " | ".join(CATEGORIAS_VALIDAS)
    return (
        "Clasifica el siguiente correo.\n\n"
        "Esquema EXACTO de la respuesta (no agregues ni quites campos):\n"
        "{\n"
        f'  "categoria": "{categorias}",\n'
        '  "prioridad": "baja | media | alta | critica",\n'
        '  "entidades": {"placa": null, "camion_id": null, "peso_reportado_kg": null, "ubicacion": null},\n'
        '  "resumen": "una frase de máximo 25 palabras"\n'
        "}\n\n"
        "Criterios:\n"
        '- categoria "otro": consultas, agradecimientos o mensajes sin incidente operativo.\n'
        "- Si el correo NIEGA el problema (por ejemplo \"no hay fuga\"), la categoria es \"otro\".\n"
        "- prioridad critica: riesgo para personas, derrames, incendios o accidentes; alta: riesgo de "
        "seguridad u operación detenida; media: afecta la operación sin riesgo inmediato; baja: "
        "molestias o consultas.\n"
        "- entidades: copia SOLO datos que aparezcan literalmente en el correo; si no aparecen usa null. "
        "NO inventes datos. peso_reportado_kg va en kilogramos (número).\n\n"
        f"Correo:\n---\n{correo}\n---\n"
    )


# ----------------------- Cliente de Ollama -----------------------
_CLIENTE_OLLAMA = None


def _chat_ollama(modelo: str, mensajes: List[Dict[str, str]], esquema: object) -> str:
    """Llama a Ollama y devuelve el texto de la respuesta. Lanza excepción si no responde."""
    global _CLIENTE_OLLAMA
    if ollama is None:
        raise RuntimeError("Falta instalar la biblioteca ollama (pip install ollama).")
    if _CLIENTE_OLLAMA is None:
        _CLIENTE_OLLAMA = ollama.Client(timeout=OLLAMA_TIMEOUT)
    opciones = {"temperature": 0}                      # respuestas lo más repetibles posible
    try:
        r = _CLIENTE_OLLAMA.chat(model=modelo, messages=mensajes, format=esquema, options=opciones)
    except ConnectionError:
        raise                                          # Ollama apagado: no tiene caso probar otro formato
    except Exception:
        # Algunas versiones no aceptan un esquema en 'format'; se reintenta en modo JSON simple.
        r = _CLIENTE_OLLAMA.chat(model=modelo, messages=mensajes, format="json", options=opciones)
    return r["message"]["content"]


def validar_respuesta_llm(texto: str) -> Tuple[bool, Optional[Dict[str, object]], str]:
    """Valida la respuesta del LLM con pydantic. Devuelve (válida, dict_o_None, motivo_del_error)."""
    if not PYDANTIC_OK:
        return False, None, "pydantic no está instalado (pip install pydantic)"
    crudo = (texto or "").strip()
    coincidencia = re.search(r"\{.*\}", crudo, re.DOTALL)      # tolera ```json ... ``` alrededor
    if not coincidencia:
        return False, None, "la respuesta no contiene un objeto JSON"
    try:
        datos = json.loads(coincidencia.group(0))
    except json.JSONDecodeError as exc:
        return False, None, f"JSON mal formado ({exc.msg})"
    try:
        return True, ClasificacionLLM.model_validate(datos).model_dump(), ""
    except ValidationError as exc:
        error = exc.errors()[0]
        campo = ".".join(str(x) for x in error["loc"]) or "raíz"
        return False, None, f"campo '{campo}': {error['msg']}"


def llamar_llm(correo: str, modelo: Optional[str] = None, max_intentos: int = MAX_INTENTOS_LLM,
               chat_fn: Optional[Callable] = None) -> Dict[str, object]:
    """
    Pide al LLM la clasificación del correo, valida el JSON y reintenta si es inválido.
    'chat_fn' permite sustituir a Ollama (por ejemplo, en las pruebas unitarias).
    Devuelve: ok, resultado (dict validado o None), respuesta_cruda, modelo, latencia_ms,
    intentos, error.
    """
    modelo = modelo or OLLAMA_MODEL
    chat_fn = chat_fn or _chat_ollama
    mensajes = [{"role": "system", "content": SISTEMA_CLASIFICADOR},
                {"role": "user", "content": construir_prompt_llm(correo)}]
    inicio = time.perf_counter()
    cruda, error, intentos = "", "", 0

    for intento in range(1, max_intentos + 1):
        intentos = intento
        try:
            cruda = chat_fn(modelo, mensajes, ESQUEMA_JSON)
        except Exception as exc:                       # Ollama apagado, tiempo agotado, modelo ausente...
            error = f"{type(exc).__name__}: {exc}"
            break                                      # reintentar no ayuda: se pasa al plan de respaldo
        valido, resultado, motivo = validar_respuesta_llm(cruda)
        if valido:
            return {"ok": True, "resultado": resultado, "respuesta_cruda": cruda, "modelo": modelo,
                    "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1),
                    "intentos": intento, "error": ""}
        error = motivo
        mensajes += [{"role": "assistant", "content": cruda},
                     {"role": "user", "content": f"Tu respuesta no cumple el esquema ({motivo}). "
                                                 "Responde SOLO con el JSON corregido."}]

    return {"ok": False, "resultado": None, "respuesta_cruda": cruda, "modelo": modelo,
            "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1),
            "intentos": intentos, "error": error}


# ----------------------- Reglas con el mismo formato de salida -----------------------
def clasificar_por_reglas(correo: str, asunto: str = "") -> Dict[str, object]:
    """Clasificador por reglas (Sección 3) con el mismo formato que el LLM: plan de respaldo."""
    clas = clasificar_incidente(asunto, correo)
    entidades = extraer_datos(asunto, correo)
    resumen = f"Clasificado por reglas como {clas['categoria']} (prioridad {clas['prioridad']})"
    if clas["palabras_clave"]:
        resumen += "; palabras clave: " + ", ".join(clas["palabras_clave"])
    return {"categoria": clas["categoria"], "prioridad": clas["prioridad"],
            "entidades": entidades, "resumen": resumen + "."}


def combinar_entidades(entidades_reglas: Dict[str, object], entidades_llm: Optional[Dict[str, object]],
                       correo: str) -> Dict[str, object]:
    """Las expresiones regulares mandan; el LLM solo completa datos que aparezcan literalmente en el correo."""
    resultado = dict(entidades_reglas)
    texto = _normalizar(correo)
    for campo in ("placa", "camion_id", "ubicacion"):
        valor = (entidades_llm or {}).get(campo)
        if resultado.get(campo) is None and valor and _normalizar(str(valor)) in texto:
            resultado[campo] = str(valor)
    return resultado


def fusionar_clasificaciones(reglas: Dict[str, object], llm: Optional[Dict[str, object]],
                             correo: str = "") -> Dict[str, object]:
    """
    Fusión de reglas y LLM.
      - Sin respuesta válida del LLM -> se usa el clasificador por reglas.
      - Coinciden en categoría y prioridad -> consenso.
      - Discrepan -> prevalece la prioridad MÁS ALTA (seguridad primero), la categoría sigue a la
        fuente que dio esa prioridad (en empate, la de las reglas) y se pide revisión humana.
    """
    if llm is None:
        return {"categoria": reglas["categoria"], "prioridad": reglas["prioridad"],
                "entidades": reglas["entidades"], "resumen": reglas["resumen"],
                "fuente": "reglas", "requiere_revision_humana": False, "coincidencia": None,
                "motivo": "El LLM no respondió o su JSON fue inválido: se usó el clasificador por reglas."}

    entidades = combinar_entidades(reglas["entidades"], llm.get("entidades"), correo)
    categoria_igual = reglas["categoria"] == llm["categoria"]
    prioridad_igual = reglas["prioridad"] == llm["prioridad"]
    if categoria_igual and prioridad_igual:
        return {"categoria": llm["categoria"], "prioridad": llm["prioridad"], "entidades": entidades,
                "resumen": llm["resumen"], "fuente": "consenso", "requiere_revision_humana": False,
                "coincidencia": True, "motivo": "Reglas y LLM coinciden."}

    i_reglas = ORDEN_PRIORIDAD.index(reglas["prioridad"])
    i_llm = ORDEN_PRIORIDAD.index(llm["prioridad"])
    gana_llm = i_llm > i_reglas
    return {
        "categoria": llm["categoria"] if gana_llm else reglas["categoria"],
        "prioridad": ORDEN_PRIORIDAD[max(i_reglas, i_llm)],
        "entidades": entidades, "resumen": llm["resumen"],
        "fuente": "hibrido_llm" if gana_llm else "hibrido_reglas",
        "requiere_revision_humana": True, "coincidencia": False,
        "motivo": (f"Discrepancia: reglas={reglas['categoria']}/{reglas['prioridad']}, "
                   f"LLM={llm['categoria']}/{llm['prioridad']}. Prevalece la prioridad más alta "
                   "(seguridad primero); se requiere revisión humana."),
    }


def registrar_evaluacion_llm(correo: str, info_llm: Dict[str, object], coincidio: Optional[bool]) -> Optional[str]:
    """Guarda en 'evaluaciones_llm': prompt, respuesta, modelo, latencia y si coincidió con las reglas."""
    try:
        return repo_evaluaciones.crear({
            "prompt": correo, "respuesta": info_llm["respuesta_cruda"], "modelo": info_llm["modelo"],
            "latencia_ms": info_llm["latencia_ms"], "coincidio_reglas": coincidio,
            "valido": info_llm["ok"], "intentos": info_llm["intentos"]})
    except (ErrorConexion, ValueError) as exc:
        log.warning("No se pudo guardar la evaluación del LLM: %s", exc)
        return None


def clasificar_hibrido(correo: str, modelo: Optional[str] = None, usar_llm: bool = True,
                       guardar_evaluacion: bool = False, chat_fn: Optional[Callable] = None) -> Dict[str, object]:
    """FUNCIÓN PRINCIPAL DE LA SECCIÓN 3B: reglas + LLM + fusión, con latencias y detalle completo."""
    inicio = time.perf_counter()
    reglas = clasificar_por_reglas(correo)
    lat_reglas = round((time.perf_counter() - inicio) * 1000, 3)

    info_llm: Dict[str, object] = {"ok": False, "resultado": None, "respuesta_cruda": "",
                                   "modelo": modelo or OLLAMA_MODEL, "latencia_ms": 0.0,
                                   "intentos": 0, "error": "LLM desactivado"}
    if usar_llm:
        info_llm = llamar_llm(correo, modelo, chat_fn=chat_fn)

    resultado = fusionar_clasificaciones(reglas, info_llm["resultado"], correo)
    if guardar_evaluacion and usar_llm:
        registrar_evaluacion_llm(correo, info_llm, resultado["coincidencia"])

    resultado["detalle"] = {
        "reglas": reglas, "llm": info_llm["resultado"], "llm_valido": info_llm["ok"],
        "llm_intentos": info_llm["intentos"], "llm_error": info_llm["error"],
        "latencia_ms": {"reglas": lat_reglas, "llm": info_llm["latencia_ms"],
                        "total": round((time.perf_counter() - inicio) * 1000, 1)},
    }
    return resultado


def guardar_incidente(correo: str, resultado: Dict[str, object], operador: str = "operador") -> str:
    """Guarda el correo clasificado en la colección 'incidentes' (estado inicial: nuevo)."""
    return repo_incidentes.crear({
        "correo_original": correo,
        "clasificacion": {"categoria": resultado["categoria"], "prioridad": resultado["prioridad"],
                          "resumen": resultado["resumen"], "fuente": resultado["fuente"]},
        "datos_extraidos": resultado["entidades"],
        "requiere_revision_humana": resultado["requiere_revision_humana"],
        "historial": [{"fecha": ahora(), "operador": operador,
                       "evento": f"Incidente creado (clasificación: {resultado['fuente']})"}],
    })


# ----------------------- Experimento: reglas vs LLM vs híbrido -----------------------
ABREVIATURAS = {"materiales_peligrosos": "mat_pel", "sobrepeso": "sobrep", "acceso_no_autorizado": "acc_no",
                "falla_hardware": "f_hw", "falla_software": "f_sw", "somnolencia_conductor": "somnol",
                "otro": "otro", "sin_respuesta": "s/resp"}


def cargar_correos_etiquetados(ruta: Optional[object] = None) -> List[Dict[str, object]]:
    """Lee los correos etiquetados a mano (correos_etiquetados.json) y valida sus etiquetas."""
    ruta = Path(ruta) if ruta else RUTA_CORREOS
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró {ruta.name}; debe estar junto a logiuncodigo.py.")
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    for fila in datos:
        if fila["categoria"] not in CATEGORIAS_VALIDAS or fila["prioridad"] not in ORDEN_PRIORIDAD:
            raise ValueError(f"Etiqueta no válida en el correo {fila.get('id')}")
    return datos


def calcular_exactitud(reales: List[str], predichos: List[str]) -> float:
    return sum(r == p for r, p in zip(reales, predichos)) / len(reales) if reales else 0.0


def calcular_matriz_confusion(reales: List[str], predichos: List[str]) -> Dict[str, Dict[str, int]]:
    """Filas = etiqueta real, columnas = lo que predijo el clasificador."""
    columnas = CATEGORIAS_VALIDAS + ["sin_respuesta"]
    matriz = {r: {c: 0 for c in columnas} for r in CATEGORIAS_VALIDAS}
    for real, pred in zip(reales, predichos):
        matriz[real][pred if pred in columnas else "sin_respuesta"] += 1
    return matriz


def _estadisticas_latencia(valores: List[float]) -> Dict[str, float]:
    if not valores:
        return {"media": 0.0, "mediana": 0.0, "p95": 0.0, "max": 0.0}
    ordenados = sorted(valores)
    p95 = ordenados[max(0, -(-95 * len(ordenados) // 100) - 1)]
    return {"media": round(statistics.mean(valores), 1), "mediana": round(statistics.median(valores), 1),
            "p95": round(p95, 1), "max": round(max(valores), 1)}


def evaluar_clasificadores(correos: Optional[List[Dict[str, object]]] = None, modelo: Optional[str] = None,
                           usar_llm: bool = True, guardar: bool = False,
                           progreso: Optional[Callable] = None,
                           chat_fn: Optional[Callable] = None) -> Dict[str, object]:
    """
    Corre reglas, LLM e híbrido sobre los correos etiquetados y calcula exactitud (categoría y
    prioridad), matriz de confusión y latencia. El LLM se llama UNA vez por correo; el híbrido
    reutiliza esa respuesta.
    """
    correos = correos if correos is not None else cargar_correos_etiquetados()
    modelo = modelo or OLLAMA_MODEL
    filas = []
    for i, c in enumerate(correos, 1):
        r = clasificar_hibrido(c["correo"], modelo, usar_llm, guardar, chat_fn)
        d = r["detalle"]
        llm = d["llm"]
        filas.append({
            "id": c.get("id", i), "informal": bool(c.get("informal")),
            "real_categoria": c["categoria"], "real_prioridad": c["prioridad"],
            "reglas_categoria": d["reglas"]["categoria"], "reglas_prioridad": d["reglas"]["prioridad"],
            "llm_categoria": llm["categoria"] if llm else "sin_respuesta",
            "llm_prioridad": llm["prioridad"] if llm else "sin_respuesta",
            "hibrido_categoria": r["categoria"], "hibrido_prioridad": r["prioridad"],
            "requiere_revision": r["requiere_revision_humana"], "llm_valido": d["llm_valido"],
            "lat_reglas": d["latencia_ms"]["reglas"], "lat_llm": d["latencia_ms"]["llm"],
            "lat_hibrido": round(d["latencia_ms"]["reglas"] + d["latencia_ms"]["llm"], 1),
        })
        if progreso:
            progreso(i, len(correos), c)

    nombres = ["reglas", "llm", "hibrido"] if usar_llm else ["reglas"]
    reales_cat = [f["real_categoria"] for f in filas]
    reales_pri = [f["real_prioridad"] for f in filas]
    resumen: Dict[str, object] = {}
    for n in nombres:
        pc = [f[f"{n}_categoria"] for f in filas]
        pp = [f[f"{n}_prioridad"] for f in filas]
        por_tipo = {}
        for etiqueta, es_informal in (("formal", False), ("informal", True)):
            sub = [f for f in filas if f["informal"] == es_informal]
            por_tipo[etiqueta] = round(calcular_exactitud([f["real_categoria"] for f in sub],
                                                          [f[f"{n}_categoria"] for f in sub]), 3)
        resumen[n] = {
            "exactitud_categoria": round(calcular_exactitud(reales_cat, pc), 3),
            "exactitud_prioridad": round(calcular_exactitud(reales_pri, pp), 3),
            "exactitud_por_redaccion": por_tipo,
            "matriz_confusion": calcular_matriz_confusion(reales_cat, pc),
            "latencia_ms": _estadisticas_latencia([f[f"lat_{n}"] for f in filas]),
        }
    return {"modelo": modelo, "n_correos": len(filas), "usar_llm": usar_llm, "clasificadores": resumen,
            "llm_invalidos": sum(not f["llm_valido"] for f in filas) if usar_llm else 0,
            "requieren_revision": sum(f["requiere_revision"] for f in filas), "detalle": filas}


def imprimir_resultados_evaluacion(res: Dict[str, object]) -> None:
    print("=" * 78)
    print(f"EXPERIMENTO DE CLASIFICACIÓN  |  {res['n_correos']} correos  |  modelo: {res['modelo']}")
    print("=" * 78)
    print(f"\n{'Clasificador':<12}{'Exact.cat':>10}{'Exact.prio':>11}{'Formal':>9}{'Informal':>10}"
          f"{'Lat.media':>11}{'Lat.p95':>9}")
    print("-" * 72)
    for nombre, m in res["clasificadores"].items():
        tipo = m["exactitud_por_redaccion"]
        print(f"{nombre:<12}{m['exactitud_categoria']:>10.1%}{m['exactitud_prioridad']:>11.1%}"
              f"{tipo['formal']:>9.0%}{tipo['informal']:>10.0%}"
              f"{m['latencia_ms']['media']:>9.1f}ms{m['latencia_ms']['p95']:>7.1f}ms")
    if res["usar_llm"]:
        print(f"\nRespuestas del LLM inválidas (cayeron a reglas): {res['llm_invalidos']}")
        print(f"Correos marcados para revisión humana:           {res['requieren_revision']}")
    for nombre, m in res["clasificadores"].items():
        print(f"\nMatriz de confusión - {nombre} (filas = real, columnas = predicho)")
        columnas = CATEGORIAS_VALIDAS + ["sin_respuesta"]
        print(f"{'':<9}" + "".join(f"{ABREVIATURAS[c]:>8}" for c in columnas))
        for real, fila in m["matriz_confusion"].items():
            print(f"{ABREVIATURAS[real]:<9}" + "".join(f"{fila[c]:>8}" for c in columnas))
    print()


def guardar_resultados_evaluacion(res: Dict[str, object], ruta: Optional[object] = None) -> Path:
    ruta = Path(ruta) if ruta else RUTA_RESULTADOS
    ruta.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return ruta


def ejecutar_experimento(usar_llm: bool = True, guardar: bool = False) -> None:
    """Corre el experimento mostrando el avance y guarda resultados_evaluacion.json."""
    def avance(i, n, c):
        print(f"\r  Clasificando correo {i}/{n}...", end="", flush=True)
    print("Iniciando experimento" + ("" if usar_llm else " (solo reglas)") + "...")
    res = evaluar_clasificadores(usar_llm=usar_llm, guardar=guardar, progreso=avance)
    print("\r" + " " * 40 + "\r", end="")
    imprimir_resultados_evaluacion(res)
    print(f"Resultados guardados en: {guardar_resultados_evaluacion(res).name}")


# =============================================================================
# SECCIÓN 4: EVALUACIÓN ÉTICA Y MATRIZ DE RIESGOS
# =============================================================================
# Matriz de riesgos: puntaje = probabilidad (1-5) x impacto (1-5)  -> rango 1..25
#   1-4  bajo | 5-9 medio | 10-16 alto | 17-25 crítico

@dataclass
class RiesgoEtico:
    """Un riesgo ético asociado a un módulo del sistema."""
    descripcion: str            # Ej. "Sesgo en visión nocturna"
    categoria: str              # Ej. "sesgo", "privacidad", "transparencia", "seguridad"
    probabilidad: int           # 1 (muy improbable) a 5 (casi seguro)
    impacto: int                # 1 (insignificante) a 5 (catastrófico)
    mitigacion: str = ""        # Acción propuesta para reducir el riesgo

    @property
    def puntaje(self) -> int:
        """Puntaje de la matriz = probabilidad x impacto."""
        return self.probabilidad * self.impacto

    @property
    def nivel(self) -> str:
        """Clasificación cualitativa según el puntaje."""
        p = self.puntaje
        if p >= 17:
            return "crítico"
        if p >= 10:
            return "alto"
        if p >= 5:
            return "medio"
        return "bajo"


@dataclass
class ModuloIA:
    """Módulo del sistema que se evalúa (con su lista de riesgos)."""
    nombre: str
    descripcion: str = ""
    riesgos: List[RiesgoEtico] = field(default_factory=list)


class EvaluadorRiesgosIA:
    """
    Registra módulos de un sistema de IA, sus riesgos éticos y genera reportes.

    Ejemplo:
        ev = EvaluadorRiesgosIA("LogiSmart")
        ev.registrar_modulo("Cámara de Detección de Somnolencia", "Visión por computadora")
        ev.registrar_riesgo("Cámara de Detección de Somnolencia",
                            "Sesgo en visión nocturna", "sesgo", 4, 4, "Entrenar con datos nocturnos diversos")
        print(ev.reporte_texto())
    """
    CATEGORIAS_VALIDAS = {"sesgo", "privacidad", "transparencia", "seguridad", "responsabilidad", "otro"}

    def __init__(self, nombre_sistema: str) -> None:
        self.nombre_sistema = nombre_sistema
        self._modulos: Dict[str, ModuloIA] = {}   # nombre -> módulo (evita duplicados)

    # ----------------------------- Registro ---------------------------------
    def registrar_modulo(self, nombre: str, descripcion: str = "") -> ModuloIA:
        """Da de alta un módulo. Lanza ValueError si ya existe o el nombre está vacío."""
        if not nombre or not nombre.strip():
            raise ValueError("El nombre del módulo no puede estar vacío")
        if nombre in self._modulos:
            raise ValueError(f"El módulo '{nombre}' ya está registrado")
        modulo = ModuloIA(nombre=nombre.strip(), descripcion=descripcion)
        self._modulos[nombre] = modulo
        return modulo

    def registrar_riesgo(self, modulo: str, descripcion: str, categoria: str,
                         probabilidad: int, impacto: int, mitigacion: str = "") -> RiesgoEtico:
        """Asocia un riesgo ético a un módulo ya registrado (validando escalas 1-5)."""
        if modulo not in self._modulos:
            raise KeyError(f"El módulo '{modulo}' no existe; regístralo primero")
        if categoria not in self.CATEGORIAS_VALIDAS:
            raise ValueError(f"Categoría inválida '{categoria}'. Usa una de {sorted(self.CATEGORIAS_VALIDAS)}")
        for nombre, valor in (("probabilidad", probabilidad), ("impacto", impacto)):
            if not isinstance(valor, int) or isinstance(valor, bool) or not 1 <= valor <= 5:
                raise ValueError(f"{nombre} debe ser un entero entre 1 y 5")
        riesgo = RiesgoEtico(descripcion, categoria, probabilidad, impacto, mitigacion)
        self._modulos[modulo].riesgos.append(riesgo)
        return riesgo

    # ----------------------------- Consultas --------------------------------
    def todos_los_riesgos(self) -> List[tuple]:
        """Lista plana de (nombre_modulo, riesgo), ordenada de mayor a menor puntaje."""
        plano = [(m.nombre, r) for m in self._modulos.values() for r in m.riesgos]
        return sorted(plano, key=lambda par: par[1].puntaje, reverse=True)

    def resumen(self) -> Dict[str, object]:
        """Estadísticas agregadas del sistema evaluado."""
        riesgos = self.todos_los_riesgos()
        por_nivel = {"crítico": 0, "alto": 0, "medio": 0, "bajo": 0}
        por_categoria: Dict[str, int] = {}
        for _, r in riesgos:
            por_nivel[r.nivel] += 1
            por_categoria[r.categoria] = por_categoria.get(r.categoria, 0) + 1
        puntajes = [r.puntaje for _, r in riesgos]
        return {
            "sistema": self.nombre_sistema,
            "total_modulos": len(self._modulos),
            "total_riesgos": len(riesgos),
            "riesgos_por_nivel": por_nivel,
            "riesgos_por_categoria": por_categoria,
            "puntaje_promedio": round(sum(puntajes) / len(puntajes), 2) if puntajes else 0,
            "modulos_sin_evaluar": [m.nombre for m in self._modulos.values() if not m.riesgos],
        }

    # ----------------------------- Reportes ---------------------------------
    def reporte_texto(self) -> str:
        """Reporte resumido en texto plano, listo para imprimir o guardar."""
        r = self.resumen()
        L = []
        L.append("=" * 78)
        L.append(f"REPORTE DE RIESGOS ÉTICOS DE IA - {r['sistema']}")
        L.append(f"Generado: {datetime.now():%Y-%m-%d %H:%M}")
        L.append("=" * 78)
        L.append(f"Módulos: {r['total_modulos']} | Riesgos: {r['total_riesgos']} | "
                 f"Puntaje promedio: {r['puntaje_promedio']}")
        L.append("Por nivel: " + ", ".join(f"{k}={v}" for k, v in r["riesgos_por_nivel"].items()))
        L.append("Por categoría: " + (", ".join(f"{k}={v}" for k, v in r["riesgos_por_categoria"].items()) or "-"))
        if r["modulos_sin_evaluar"]:
            L.append("ATENCIÓN - Módulos sin riesgos evaluados: " + ", ".join(r["modulos_sin_evaluar"]))
        L.append("\nMATRIZ (ordenada de mayor a menor riesgo):")
        L.append(f"{'Módulo':<32}{'Riesgo':<34}{'P':>2}{'I':>3}{'Pts':>5}  Nivel")
        L.append("-" * 78)
        for modulo, riesgo in self.todos_los_riesgos():
            L.append(f"{modulo[:31]:<32}{riesgo.descripcion[:33]:<34}"
                     f"{riesgo.probabilidad:>2}{riesgo.impacto:>3}{riesgo.puntaje:>5}  {riesgo.nivel}")
        L.append("\nMITIGACIONES PROPUESTAS:")
        for modulo, riesgo in self.todos_los_riesgos():
            if riesgo.mitigacion:
                L.append(f"  - [{riesgo.nivel.upper()}] {modulo}: {riesgo.mitigacion}")
        return "\n".join(L)

    def exportar_json(self, ruta: Optional[str] = None) -> str:
        """Exporta el reporte completo en JSON. Si se da 'ruta', también lo guarda en archivo."""
        datos = {
            "resumen": self.resumen(),
            "modulos": [
                {"nombre": m.nombre, "descripcion": m.descripcion,
                 "riesgos": [{**asdict(r), "puntaje": r.puntaje, "nivel": r.nivel} for r in m.riesgos]}
                for m in self._modulos.values()
            ],
        }
        texto = json.dumps(datos, ensure_ascii=False, indent=2)
        if ruta:
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(texto)
        return texto

    def exportar_texto(self, ruta: str) -> None:
        """Guarda el reporte de texto en un archivo .txt (UTF-8)."""
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(self.reporte_texto())


# =============================================================================
# SECCIÓN 4B: ASISTENTE EXPLICATIVO (RAG SENCILLO) + DATOS DE DEMOSTRACIÓN
# =============================================================================
# Patrón RAG: PRIMERO se consulta MongoDB y DESPUÉS se entrega ese contexto al LLM.
#   1) extraer_referencias: detecta CAM-xxx, placas y temas (riesgos, incidentes, accesos...).
#   2) recuperar_contexto: consulta camiones, accesos, incidentes, riesgos y evaluaciones.
#   3) El LLM recibe SOLO esos registros numerados [1], [2]... y debe citarlos.
#   4) Se verifica que cite registros reales. Si no lo hace, o si Ollama no responde, se muestra
#      un resumen directo de los registros: nunca se presenta texto inventado.
#   5) Si no hay registros, responde «No tengo información» SIN llamar al LLM.

MAX_REGISTROS_CONTEXTO = 12
MENSAJE_SIN_INFO = ("No tengo información sobre eso en la base de datos. Prueba preguntando por un "
                    "camión (por ejemplo CAM-102), una placa, o por incidentes, accesos o riesgos.")
RESPUESTA_LLM_SIN_INFO = "No tengo información suficiente para responder."
SISTEMA_RAG = ("Eres el asistente explicativo de LogiSmart. Explicas por qué el sistema tomó una decisión "
               "usando únicamente los registros de la base de datos que se te entregan.")

_TEMAS = {   # palabra clave (sin acentos) -> colección relacionada
    "riesgo": "riesgos_eticos", "etic": "riesgos_eticos", "mitigacion": "riesgos_eticos",
    "incidente": "incidentes", "correo": "incidentes",
    "acceso": "accesos", "bitacora": "accesos", "inspeccion": "accesos", "semaforo": "accesos",
    "evaluacion": "evaluaciones_llm", "latencia": "evaluaciones_llm", "llm": "evaluaciones_llm",
    "camion": "camiones", "placa": "camiones", "empresa": "camiones",
}
_ALIAS_CATEGORIAS = {
    "materiales_peligrosos": ("peligros", "derrame", "fuga", "quimic"),
    "sobrepeso": ("sobrepeso", "exceso de peso", "sobrecarga"),
    "acceso_no_autorizado": ("no autorizado", "intruso", "sin autorizacion"),
    "falla_hardware": ("hardware", "camara", "lector", "rfid", "sensor"),
    "falla_software": ("software",),
    "somnolencia_conductor": ("somnolencia", "dormido", "fatiga", "cansancio"),
}


def extraer_referencias(pregunta: str) -> Dict[str, List[str]]:
    """Detecta en la pregunta IDs de camión, placas, colecciones relacionadas y categorías."""
    mayus, norm = pregunta.upper(), _normalizar(pregunta)
    return {
        "camiones": list(dict.fromkeys(re.findall(r"\bCAM-\d+\b", mayus))),
        "placas": list(dict.fromkeys(re.findall(r"\b[A-Z0-9]{2,3}-\d{2,3}-[A-Z0-9]{1,2}\b", mayus))),
        "colecciones": list(dict.fromkeys(c for kw, c in _TEMAS.items() if kw in norm)),
        "categorias": [c for c, alias in _ALIAS_CATEGORIAS.items() if any(a in norm for a in alias)],
    }


def compactar_registro(coleccion: str, d: Dict[str, object]) -> Dict[str, object]:
    """Deja solo los campos útiles de un documento (el modelo de 1B tiene poco contexto)."""
    if coleccion == "camiones":
        datos = {k: d.get(k) for k in ("placa", "camion_id", "empresa", "autorizacion", "certificacion_conductor")}
    elif coleccion == "accesos":
        clave = ("Causa", "Conflicto", "Ninguna", "Resultado final")
        datos = {"fecha": str(d.get("timestamp", ""))[:16], "camion_id": d.get("camion_id"),
                 "placa": d.get("placa"), "premisas": {k: d.get(k) for k in "PQRSHD" if k in d},
                 "A": d.get("A"), "E": d.get("E"), "resultado": d.get("resultado"),
                 "regla_ganadora": d.get("regla_ganadora"),
                 "explicacion": [p for p in d.get("explicacion", []) if p.startswith(clave)]}
    elif coleccion == "incidentes":
        cl = d.get("clasificacion", {})
        datos = {"fecha": str(d.get("creado", ""))[:16], "categoria": cl.get("categoria"),
                 "prioridad": cl.get("prioridad"), "estado": d.get("estado"), "resumen": cl.get("resumen"),
                 "correo": str(d.get("correo_original", ""))[:160], "entidades": d.get("datos_extraidos"),
                 "requiere_revision_humana": d.get("requiere_revision_humana")}
    elif coleccion == "riesgos_eticos":
        datos = {k: d.get(k) for k in ("modulo", "descripcion", "categoria", "puntaje_inicial", "nivel_inicial",
                                       "puntaje_residual", "nivel_residual", "mitigacion")}
    else:
        datos = {"fecha": str(d.get("timestamp", ""))[:16], "modelo": d.get("modelo"),
                 "latencia_ms": d.get("latencia_ms"), "coincidio_reglas": d.get("coincidio_reglas"),
                 "prompt": str(d.get("prompt", ""))[:100]}
    return {"coleccion": coleccion, "id": d["_id"], "datos": datos}


def recuperar_contexto(pregunta: str, limite: int = MAX_REGISTROS_CONTEXTO) -> List[Dict[str, object]]:
    """Consulta MongoDB según lo que mencione la pregunta. Sin pistas, ni siquiera consulta la base."""
    ref = extraer_referencias(pregunta)
    if not any(ref.values()):
        return []
    registros: List[Dict[str, object]] = []

    def agregar(coleccion, docs):
        registros.extend(compactar_registro(coleccion, d) for d in docs if d)

    for cid in ref["camiones"]:
        agregar("camiones", [repo_camiones.buscar_por_camion_id(cid)])
        agregar("accesos", repo_accesos.historial_de(camion_id=cid, limite=3))
        agregar("incidentes", repo_incidentes.listar(
            {"$or": [{"datos_extraidos.camion_id": cid},
                     {"correo_original": {"$regex": re.escape(cid), "$options": "i"}}]}, limite=3))
    for placa in ref["placas"]:
        agregar("camiones", [repo_camiones.buscar_por_placa(placa)])
        agregar("accesos", repo_accesos.historial_de(placa=placa, limite=3))
    con_id = bool(ref["camiones"] or ref["placas"])
    cols = ref["colecciones"]
    if "incidentes" in cols or ref["categorias"]:
        filtro = {"clasificacion.categoria": {"$in": ref["categorias"]}} if ref["categorias"] else {}
        agregar("incidentes", repo_incidentes.listar(filtro, limite=5))
    if "accesos" in cols and not con_id:
        agregar("accesos", repo_accesos.listar(limite=5, orden=[("timestamp", DESCENDING)]))
    if "camiones" in cols and not con_id:
        agregar("camiones", repo_camiones.listar(limite=5))
    if "riesgos_eticos" in cols:
        agregar("riesgos_eticos", repo_riesgos.listar(limite=6, orden=[("puntaje_inicial", DESCENDING)]))
    if "evaluaciones_llm" in cols:
        agregar("evaluaciones_llm", repo_evaluaciones.listar(limite=5))

    vistos, unicos = set(), []
    for r in registros:
        if (r["coleccion"], r["id"]) not in vistos:
            vistos.add((r["coleccion"], r["id"]))
            unicos.append(r)
    return unicos[:limite]


def construir_prompt_rag(pregunta: str, registros: List[Dict[str, object]],
                         historial: Optional[List[Dict[str, str]]] = None) -> str:
    bloque = "\n".join(f"[{i}] ({r['coleccion']}) {json.dumps(r['datos'], ensure_ascii=False, default=str)}"
                       for i, r in enumerate(registros, 1))
    previo = ""
    if historial:
        previo = ("Conversación previa (solo ayuda a entender la pregunta; NO es una fuente):\n"
                  + "\n".join(f"- {h['rol']}: {h['texto'][:200]}" for h in historial[-4:]) + "\n\n")
    return (f"{previo}Pregunta del operador: {pregunta}\n\nRegistros recuperados de MongoDB:\n{bloque}\n\n"
            "Instrucciones:\n"
            "- Responde SOLO con la información de los registros anteriores.\n"
            "- Cita el registro de origen de cada dato con su número entre corchetes, por ejemplo [1].\n"
            f"- Si los registros no contienen la respuesta, responde exactamente: «{RESPUESTA_LLM_SIN_INFO}»\n"
            "- No inventes datos. Responde en español, en máximo 6 oraciones.")


def citas_validas(texto: str, n_registros: int) -> bool:
    """True si el texto cita al menos un registro y todos los citados existen."""
    citas = {int(x) for x in re.findall(r"\[(\d+)\]", texto)}
    return bool(citas) and all(1 <= c <= n_registros for c in citas)


def respuesta_sin_llm(registros: List[Dict[str, object]]) -> str:
    """Resumen directo de los registros (plan de respaldo, sin modelo): nada inventado."""
    lineas = []
    for i, r in enumerate(registros, 1):
        d, c = r["datos"], r["coleccion"]
        if c == "accesos":
            txt = (f"El {d.get('fecha')} el camión {d.get('camion_id') or d.get('placa')} obtuvo "
                   f"{d.get('resultado')} (regla ganadora: {d.get('regla_ganadora') or 'ninguna'}). "
                   + " ".join(d.get("explicacion") or []))
        elif c == "camiones":
            txt = (f"Camión {d.get('camion_id')} (placa {d.get('placa')}, {d.get('empresa') or 'sin empresa'}): "
                   f"autorización={'sí' if d.get('autorizacion') else 'no'}, "
                   f"certificación del conductor={'sí' if d.get('certificacion_conductor') else 'no'}.")
        elif c == "incidentes":
            txt = (f"Incidente de {d.get('categoria')}, prioridad {d.get('prioridad')} "
                   f"(estado {d.get('estado')}): {d.get('resumen')}")
        elif c == "riesgos_eticos":
            txt = (f"Riesgo en {d.get('modulo')}: {d.get('descripcion')}. Puntaje {d.get('puntaje_inicial')} "
                   f"({d.get('nivel_inicial')}) -> residual {d.get('puntaje_residual')} "
                   f"({d.get('nivel_residual')}). Mitigación: {d.get('mitigacion')}")
        else:
            txt = json.dumps(d, ensure_ascii=False, default=str)
        lineas.append(f"[{i}] {txt}")
    return "\n".join(lineas)


def _chat_ollama_texto(modelo: str, mensajes: List[Dict[str, str]]) -> str:
    """Respuesta en texto libre (el asistente no usa formato JSON)."""
    global _CLIENTE_OLLAMA
    if ollama is None:
        raise RuntimeError("Falta instalar la biblioteca ollama (pip install ollama).")
    if _CLIENTE_OLLAMA is None:
        _CLIENTE_OLLAMA = ollama.Client(timeout=OLLAMA_TIMEOUT)
    return _CLIENTE_OLLAMA.chat(model=modelo, messages=mensajes, options={"temperature": 0.1})["message"]["content"]


def responder_pregunta(pregunta: str, historial: Optional[List[Dict[str, str]]] = None,
                       modelo: Optional[str] = None, chat_fn: Optional[Callable] = None,
                       recuperador: Optional[Callable] = None, guardar: bool = False) -> Dict[str, object]:
    """
    FUNCIÓN PRINCIPAL DEL ASISTENTE. Devuelve respuesta, fuentes (registro de origen), modo
    ('llm' | 'llm_sin_info' | 'sin_llm' | 'sin_datos'), latencia_ms y error.
    """
    inicio = time.perf_counter()
    modelo = modelo or OLLAMA_MODEL
    registros = (recuperador or recuperar_contexto)(pregunta)
    fuentes = [{"n": i, "fuente": f"{r['coleccion']}/{r['id']}", "coleccion": r["coleccion"], "datos": r["datos"]}
               for i, r in enumerate(registros, 1)]

    def resultado(texto: str, modo: str, error: str = "") -> Dict[str, object]:
        r = {"respuesta": texto, "fuentes": fuentes, "modo": modo, "error": error, "modelo": modelo,
             "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1)}
        if guardar and modo != "sin_datos":
            _registrar_consulta_asistente(pregunta, r)
        return r

    if not registros:
        return resultado(MENSAJE_SIN_INFO, "sin_datos")
    mensajes = [{"role": "system", "content": SISTEMA_RAG},
                {"role": "user", "content": construir_prompt_rag(pregunta, registros, historial)}]
    try:
        texto = (chat_fn or _chat_ollama_texto)(modelo, mensajes).strip()
    except Exception as exc:
        return resultado("(El modelo no está disponible: se muestra un resumen directo de los registros)\n\n"
                         + respuesta_sin_llm(registros), "sin_llm", f"{type(exc).__name__}: {exc}")
    if "no tengo informacion" in _normalizar(texto)[:80]:
        return resultado(texto, "llm_sin_info")
    if not citas_validas(texto, len(registros)):
        return resultado("(El modelo no citó registros válidos: se muestra un resumen directo de los registros)\n\n"
                         + respuesta_sin_llm(registros), "sin_llm", "respuesta sin citas válidas")
    return resultado(texto, "llm")


def _registrar_consulta_asistente(pregunta: str, r: Dict[str, object]) -> Optional[str]:
    try:
        return repo_evaluaciones.crear({
            "prompt": pregunta, "respuesta": r["respuesta"], "modelo": r["modelo"],
            "latencia_ms": r["latencia_ms"], "coincidio_reglas": None, "tipo": "asistente",
            "modo": r["modo"], "fuentes": [f["fuente"] for f in r["fuentes"]]})
    except (ErrorConexion, ValueError) as exc:
        log.warning("No se pudo guardar la consulta del asistente: %s", exc)
        return None


def formatear_respuesta(r: Dict[str, object]) -> str:
    texto = r["respuesta"]
    if r["fuentes"]:
        texto += "\n\nFuentes consultadas:\n" + "\n".join(f"  [{f['n']}] {f['fuente']}" for f in r["fuentes"])
    return texto


def preguntar_en_consola(pregunta: str) -> None:
    if not pregunta.strip():
        print('Escribe una pregunta, por ejemplo: --preguntar "¿Por qué CAM-102 fue enviado a inspección?"')
        return
    try:
        r = responder_pregunta(pregunta.strip())
    except ErrorConexion as exc:
        print(f"✘ {exc}")
        return
    print(f"\nPregunta: {pregunta}\nModo: {r['modo']}  ·  {r['latencia_ms']} ms\n\n{formatear_respuesta(r)}\n")


# ----------------------- Datos de demostración -----------------------
_DEMO_CAMIONES = [   # (camion_id, placa, empresa, P, Q, R, S, H, D)
    ("CAM-101", "ABC-101-A", "Transportes del Norte", 1, 0, 1, 1, 1, 0),
    ("CAM-102", "ABC-102-B", "Logística Centro", 1, 1, 0, 1, 1, 0),
    ("CAM-103", "ABC-103-C", "Fletes Rápidos", 1, 0, 0, 1, 1, 1),
    ("CAM-104", "ABC-104-D", "Transportes del Norte", 1, 0, 0, 1, 1, 0),
    ("CAM-105", "ABC-105-E", "Carga Express", 0, 0, 0, 1, 1, 0),
    ("CAM-106", "ABC-106-F", "Logística Centro", 1, 0, 1, 1, 0, 0),
]
_DEMO_INCIDENTES = [   # (correo, días atrás, estado)
    ("Urgente: fuga de químico inflamable en el camión CAM-101, andén 3. Evacuar la zona.", 0, "en_atencion"),
    ("La báscula marca 52 toneladas para el CAM-102 y excede el límite permitido de 48 toneladas.", 1, "nuevo"),
    ("el chofer del CAM-103 se esta quedando dormido en la fila, cabecea mucho", 2, "cerrado"),
    ("Reporto carga con material peligroso sin hoja de seguridad. El camión CAM-106 espera en la puerta B.", 3, "nuevo"),
    ("La cámara del andén 4 no enciende desde esta mañana.", 8, "cerrado"),
    ("El sistema está caído y no carga la lista de citas; no podemos registrar entradas.", 9, "en_atencion"),
    ("se metio un tipo a la caseta sur y no traia gafete, seguridad ya va para alla", 15, "nuevo"),
]
_DEMO_RIESGOS = [   # (módulo, descripción, categoría, prob, imp, mitigación, prob_res, imp_res)
    ("Clasificador híbrido", "Alucinaciones del LLM: categorías, entidades o respuestas inventadas", "fiabilidad",
     4, 4, "Validación con pydantic, descarte de entidades ausentes del correo, RAG con citas y 'no tengo información'",
     2, 3),
    ("Clasificador híbrido", "Sesgo en correos con ortografía informal: menor exactitud de las reglas", "sesgo",
     4, 4, "El LLM interpreta la intención, revisión humana cuando hay discrepancia y medición formal vs informal",
     3, 3),
    ("Control de acceso", "Privacidad de datos del conductor (placa, certificación, somnolencia)", "privacidad",
     4, 5, "Guardar solo datos necesarios, credenciales en .env, operador identificado en la bitácora, retención limitada",
     2, 4),
    ("Sistema completo", "Dependencia excesiva de la automatización por parte del operador", "responsabilidad",
     3, 4, "requiere_revision_humana, el operador puede editar cada resultado y la decisión final es suya",
     2, 3),
]


@_seguro
def sembrar_datos_demo() -> Dict[str, int]:
    """Carga datos de demostración marcados con demo=True (se pueden borrar con limpiar_datos_demo)."""
    if repo_camiones.buscar_por_camion_id("CAM-102"):
        print("Los datos de demostración ya existen (usa --limpiar-demo para borrarlos).")
        return {}
    crear_indices()
    for i, (cid, placa, empresa, p, q, r, s, h, d) in enumerate(_DEMO_CAMIONES):
        repo_camiones.crear({"placa": placa, "camion_id": cid, "empresa": empresa, "autorizacion": bool(p),
                             "certificacion_conductor": bool(s), "demo": True})
        dec = evaluar_decision(bool(p), bool(q), bool(r), bool(s), bool(h), bool(d))
        registrar_decision(dec, placa, cid, "demo", {"demo": True, "timestamp": ahora() - timedelta(hours=5 * i)})
    for correo, dias, estado in _DEMO_INCIDENTES:
        c = clasificar_hibrido(correo, usar_llm=False)
        fecha = ahora() - timedelta(days=dias)
        repo_incidentes.crear({
            "correo_original": correo, "estado": estado, "creado": fecha, "demo": True,
            "clasificacion": {"categoria": c["categoria"], "prioridad": c["prioridad"],
                              "resumen": c["resumen"], "fuente": c["fuente"]},
            "datos_extraidos": c["entidades"],
            "historial": [{"fecha": fecha, "operador": "demo", "evento": "Incidente creado (datos de demostración)"}]})
    for modulo, desc, cat, p, i, mit, pr, ir in _DEMO_RIESGOS:
        repo_riesgos.crear({"modulo": modulo, "descripcion": desc, "categoria": cat, "probabilidad": p,
                            "impacto": i, "mitigacion": mit, "probabilidad_residual": pr,
                            "impacto_residual": ir, "demo": True})
    resumen = {"camiones": len(_DEMO_CAMIONES), "accesos": len(_DEMO_CAMIONES),
               "incidentes": len(_DEMO_INCIDENTES), "riesgos": len(_DEMO_RIESGOS)}
    print("✔ Datos de demostración cargados: " + ", ".join(f"{v} {k}" for k, v in resumen.items()))
    return resumen


@_seguro
def limpiar_datos_demo() -> Dict[str, int]:
    """Borra únicamente los documentos marcados con demo=True."""
    borrados = {r.coleccion: r.col.delete_many({"demo": True}).deleted_count
                for r in (repo_camiones, repo_accesos, repo_incidentes, repo_riesgos, repo_evaluaciones)}
    print("✔ Datos de demostración borrados: " + ", ".join(f"{v} de {k}" for k, v in borrados.items()))
    return borrados


# =============================================================================
# SECCIÓN 5: INTERFAZ GRÁFICA (STREAMLIT)
# =============================================================================
# Se abre con:  streamlit run logiuncodigo.py
# Cada página llama a las secciones anteriores (reglas, clasificador híbrido, MongoDB);
# aquí no hay lógica de negocio, solo presentación y validación de entradas.

try:
    import streamlit as st
    STREAMLIT_OK = True
except ImportError:
    st = None
    STREAMLIT_OK = False

ICONOS_PAGINA = {
    "Panel de control": ":material/dashboard:",
    "Control de acceso": ":material/traffic:",
    "Simulador lógico": ":material/toggle_on:",
    "Bandeja de incidentes": ":material/inbox:",
    "Asistente": ":material/smart_toy:",
    "Administrar datos": ":material/database:",
    "Configuración": ":material/settings:",
}

ESTILO_GUI = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,1,0');
:root { --ac:#ff7a18; --bg:#121217; --panel:#17171d; --card:#22222b; --line:#2c2c36; }
html, body, [class*="css"] { font-family:'Poppins',sans-serif; }
[data-testid="stAppViewContainer"], [data-testid="stHeader"] { background:var(--bg); }
[data-testid="stSidebar"] { background:var(--panel); border-right:1px solid var(--line); }
.marca { display:flex; gap:12px; align-items:center; margin-bottom:18px; }
.logo { width:48px; height:48px; border-radius:14px; display:flex; align-items:center;
        justify-content:center; background:linear-gradient(135deg,#ff7a18,#ff3d6e); }
.ms { font-family:'Material Symbols Rounded'; font-size:28px; color:#fff; line-height:1; }
.marca b { font-size:1.2rem; } .marca span { display:block; font-size:.8rem; opacity:.65; }
.sec { font-size:.72rem; font-weight:600; letter-spacing:.08em; opacity:.6; margin:16px 0 8px; }
.cab { display:flex; justify-content:space-between; align-items:center;
       border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:18px; }
.cab h2 { margin:0; font-size:1.7rem; } .cab p { margin:2px 0 0; font-size:.85rem; opacity:.65; }
.pill { background:var(--card); border-radius:999px; padding:6px 14px; font-size:.8rem;
        font-weight:600; display:inline-flex; align-items:center; gap:8px; margin-left:8px; }
.dot { width:9px; height:9px; border-radius:50%; background:#22c55e; } .dot.off { background:#ef4444; }
[data-testid="stMetric"], [data-testid="stExpander"] { background:var(--card);
        border:1px solid var(--line); border-radius:14px; padding:10px 14px; }
.stButton > button, .stFormSubmitButton > button { border-radius:14px; font-weight:500;
        border:1px solid var(--line); background:transparent; }
.stButton > button:hover { border-color:var(--ac); color:var(--ac); }
.stButton > button[kind="primary"] { background:var(--ac); border:none; color:#fff; }
.stButton span[data-testid="stIconMaterial"] { color:var(--ac); }
button[kind="primary"] span[data-testid="stIconMaterial"] { color:#fff !important; }
.semaforo { display:flex; flex-direction:column; gap:10px; background:#0b0b0f; padding:14px;
            border-radius:18px; width:76px; margin:auto; }
.luz { width:48px; height:48px; border-radius:50%; opacity:.16; }
.luz.rojo { background:#ef4444; color:#ef4444; } .luz.amarillo { background:#facc15; color:#facc15; }
.luz.verde { background:#22c55e; color:#22c55e; } .luz.on { opacity:1; box-shadow:0 0 24px currentColor; }
</style>
"""


def _semaforo_html(color: str) -> str:
    luces = "".join(f'<span class="luz {c}{" on" if c == color else ""}"></span>'
                    for c in ("rojo", "amarillo", "verde"))
    return f'<div class="semaforo">{luces}</div>'


def _inicio_dia(fecha) -> Optional[datetime]:
    return datetime.combine(fecha, dtime.min, tzinfo=timezone.utc) if fecha else None


def _fin_dia(fecha) -> Optional[datetime]:
    return datetime.combine(fecha, dtime.max, tzinfo=timezone.utc) if fecha else None


def _estado_conexiones() -> Dict[str, bool]:
    """Comprueba MongoDB y Ollama como máximo cada 30 segundos."""
    previo = st.session_state.get("_conex")
    if previo and time.time() - previo["ts"] < 30:
        return previo
    mongo_ok, _ = probar_conexion()
    try:
        ollama.list()
        ollama_ok = True
    except Exception:
        ollama_ok = False
    st.session_state["_conex"] = {"mongo": mongo_ok, "ollama": ollama_ok, "ts": time.time()}
    return st.session_state["_conex"]


def _segura(pagina: Callable) -> Callable:
    """Muestra errores de conexión y validación como mensajes amables, sin romper la página."""
    @wraps(pagina)
    def envoltura():
        try:
            pagina()
        except ErrorConexion as exc:
            st.error(str(exc))
            st.caption("Revisa tu .env y que tu IP esté permitida en Atlas (Network Access).")
        except ValueError as exc:
            st.warning(str(exc))
    return envoltura


# ----------------------- Páginas -----------------------
@_segura
def pagina_panel() -> None:
    c1, c2 = st.columns(2)
    desde, hasta = c1.date_input("Desde", value=None), c2.date_input("Hasta", value=None)
    ini, fin = _inicio_dia(desde), _fin_dia(hasta)
    ind = indicadores_panel(ini, fin, st.session_state.cfg["umbral_critico"])
    m1, m2, m3 = st.columns(3)
    m1.metric("Camiones atendidos", ind["camiones_atendidos"])
    m2.metric("Incidentes abiertos", ind["incidentes_abiertos"])
    m3.metric("Riesgos críticos", ind["riesgos_criticos"])

    st.markdown('<div class="sec">INCIDENTES POR CATEGORÍA Y SEMANA</div>', unsafe_allow_html=True)
    filas = repo_incidentes.por_categoria_y_semana(ini, fin)
    if not filas:
        st.info("Aún no hay incidentes en el periodo seleccionado.")
        return
    import pandas as pd
    df = pd.DataFrame(filas).fillna({"categoria": "sin_categoria"})
    st.bar_chart(df.pivot_table(index="semana", columns="categoria", values="total",
                                aggfunc="sum", fill_value=0))
    st.dataframe(df, use_container_width=True, hide_index=True)


def _buscar_placa() -> None:
    placa = st.session_state.get("acc_placa", "").strip()
    try:
        datos = premisas_por_placa(placa) if placa else None
    except ErrorConexion as exc:
        st.session_state.acc_msg = ("error", str(exc))
        return
    if not placa:
        st.session_state.acc_msg = ("warning", "Escribe una placa para buscar.")
    elif not datos:
        st.session_state.acc_msg = ("warning", f"No hay ningún camión con la placa {placa.upper()}.")
    else:
        st.session_state.update(acc_P=datos["P"], acc_S=datos["S"], acc_camion=datos["camion"]["camion_id"])
        st.session_state.acc_msg = ("success", f"{datos['camion']['camion_id']} · "
                                    f"{datos['camion'].get('empresa', '')}: P y S cargadas desde MongoDB.")


@_segura
def pagina_acceso() -> None:
    for clave, valor in {"acc_placa": "", "acc_camion": "", "acc_P": False, "acc_Q": False, "acc_R": False,
                         "acc_S": False, "acc_H": en_horario_permitido(), "acc_D": False}.items():
        st.session_state.setdefault(clave, valor)

    izq, centro, der = st.columns([1.2, 1.6, 0.6])
    with izq:
        st.text_input("Placa del camión", key="acc_placa", placeholder="ABC-123-D")
        st.button("Buscar en MongoDB", icon=":material/search:", on_click=_buscar_placa, use_container_width=True)
        tipo, texto = st.session_state.pop("acc_msg", (None, None))
        if tipo:
            getattr(st, tipo)(texto)
        st.markdown('<div class="sec">PREMISAS</div>', unsafe_allow_html=True)
        for clave, etiqueta in (("P", "P · autorización previa"), ("Q", "Q · peso excede el límite"),
                                ("R", "R · carga con materiales peligrosos"),
                                ("S", "S · conductor con certificación vigente"),
                                ("H", "H · dentro del horario permitido"), ("D", "D · somnolencia detectada")):
            st.toggle(etiqueta, key=f"acc_{clave}")

    dec = evaluar_decision(*(st.session_state[f"acc_{k}"] for k in "PQRSHD"))
    with der:
        st.markdown(_semaforo_html(dec["semaforo"]), unsafe_allow_html=True)
    with centro:
        st.markdown(f"### {dec['resultado'].replace('_', ' ').title()}")
        st.caption(f"A = {_vf(dec['A'])}   ·   E = {_vf(dec['E'])}   ·   regla ganadora: {dec['regla_ganadora'] or 'ninguna'}")
        st.markdown("**Explicación paso a paso**")
        for i, paso in enumerate(dec["explicacion"], 1):
            st.write(f"{i}. {paso}")
        if st.button("Guardar en bitácora", type="primary", icon=":material/save:", use_container_width=True):
            registrar_decision(dec, st.session_state.acc_placa or None, st.session_state.acc_camion or None,
                               st.session_state.cfg["operador"])
            st.toast("Decisión guardada en la bitácora de accesos.", icon=":material/check_circle:")


@_segura
def pagina_simulador() -> None:
    st.caption("Activa los interruptores y observa cómo cambian A y E, y la decisión final, en vivo.")
    cols = st.columns(6)
    v = {k: cols[i].toggle(k, value=(k == "H"), key=f"sim_{k}") for i, k in enumerate("PQRSHD")}
    dec = evaluar_decision(*(v[k] for k in "PQRSHD"))
    a, b, c = st.columns(3)
    a.metric("A = P ∧ S ∧ ¬Q", _vf(dec["A"]))
    b.metric("E = P ∧ (R ∨ Q)", _vf(dec["E"]))
    c.metric("Decisión", dec["resultado"].replace("_", " ").title())
    st.markdown(_semaforo_html(dec["semaforo"]).replace('margin:auto', ''), unsafe_allow_html=True)
    with st.expander("Tabla completa de decisiones (64 combinaciones)"):
        import pandas as pd
        filas = [{**{k: _vf(f[k]) for k in "PQRSHD"}, "A": _vf(f["A"]), "E": _vf(f["E"]),
                  "resultado": f["resultado"]} for f in generar_tabla_decision()]
        st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)


@_segura
def pagina_incidentes() -> None:
    cfg = st.session_state.cfg
    nuevo, bandeja = st.tabs(["Nuevo correo", "Bandeja"])
    with nuevo:
        correo = st.text_area("Pega el correo del incidente", height=170, key="inc_correo",
                              placeholder="Ej.: el camion ABC-123-D trae un tambo escurriendo algo toxico...")
        if st.button("Clasificar", type="primary", icon=":material/psychology:"):
            if len(correo.strip()) < 10:
                st.warning("Escribe un correo de al menos 10 caracteres.")
            else:
                with st.spinner("Clasificando con reglas y LLM..."):
                    st.session_state.clasif = clasificar_hibrido(correo.strip(), cfg["modelo"], cfg["usar_llm"], True)
                    st.session_state.clasif_correo = correo.strip()
        r = st.session_state.get("clasif")
        if r:
            if r["requiere_revision_humana"]:
                st.warning("Reglas y LLM no coinciden: requiere revisión humana.")
            st.caption(f"Fuente: {r['fuente']} · {r['motivo']}")
            e1, e2 = st.columns(2)
            cat = e1.selectbox("Categoría", CATEGORIAS_VALIDAS, index=CATEGORIAS_VALIDAS.index(r["categoria"]))
            pri = e2.selectbox("Prioridad", ORDEN_PRIORIDAD, index=ORDEN_PRIORIDAD.index(r["prioridad"]))
            resumen = st.text_input("Resumen", value=r["resumen"])
            st.json(r["entidades"], expanded=False)
            if st.button("Guardar incidente", icon=":material/save:"):
                final = {**r, "categoria": cat, "prioridad": pri, "resumen": resumen}
                guardar_incidente(st.session_state.clasif_correo, final, cfg["operador"])
                envio = enviar_correo_soporte("sistema@logismart.example", "soporte@logismart.example",
                                              f"[{pri.upper()}] {cat}", resumen, simulacion=cfg["simulacion_correo"])
                st.session_state.pop("clasif")
                st.toast("Incidente guardado" + (" (correo simulado)" if envio["modo"] == "simulacion" else ""),
                         icon=":material/check_circle:")
                st.rerun()
    with bandeja:
        filtro = st.selectbox("Estado", ["todos"] + list(ESTADOS_INCIDENTE))
        docs = repo_incidentes.listar({} if filtro == "todos" else {"estado": filtro}, limite=100)
        if not docs:
            st.info("No hay incidentes con ese estado.")
        for d in docs:
            cl = d.get("clasificacion", {})
            etiqueta = f"{cl.get('categoria', '—')} · {cl.get('prioridad', '—')} · {d['estado']}"
            with st.expander(etiqueta + (" · REVISAR" if d.get("requiere_revision_humana") else "")):
                st.text(d["correo_original"])
                st.caption(cl.get("resumen", ""))
                c1, c2, c3 = st.columns(3)
                estado = c1.selectbox("Estado", ESTADOS_INCIDENTE, index=ESTADOS_INCIDENTE.index(d["estado"]),
                                      key=f"est_{d['_id']}")
                cat = c2.selectbox("Categoría", CATEGORIAS_VALIDAS, key=f"cat_{d['_id']}",
                                   index=CATEGORIAS_VALIDAS.index(cl.get("categoria", "otro")))
                pri = c3.selectbox("Prioridad", ORDEN_PRIORIDAD, key=f"pri_{d['_id']}",
                                   index=ORDEN_PRIORIDAD.index(cl.get("prioridad", "baja")))
                b1, b2, b3 = st.columns(3)
                if b1.button("Cambiar estado", key=f"be_{d['_id']}", icon=":material/sync:"):
                    repo_incidentes.cambiar_estado(d["_id"], estado, cfg["operador"])
                    st.rerun()
                if b2.button("Guardar edición", key=f"bc_{d['_id']}", icon=":material/edit:"):
                    repo_incidentes.editar_clasificacion(d["_id"], {**cl, "categoria": cat, "prioridad": pri},
                                                         cfg["operador"])
                    st.rerun()
                if b3.button("Eliminar", key=f"bd_{d['_id']}", icon=":material/delete:"):
                    repo_incidentes.eliminar(d["_id"])
                    st.rerun()
                st.markdown("**Historial**")
                for h in d.get("historial", []):
                    st.caption(f"{str(h['fecha'])[:16]} · {h.get('operador', 'sistema')} · {h['evento']}")


def pagina_config() -> None:
    cfg = st.session_state.cfg
    cfg["modelo"] = st.text_input("Modelo de Ollama", value=cfg["modelo"])
    cfg["usar_llm"] = st.toggle("Usar el LLM en la clasificación (si se apaga, solo reglas)", value=cfg["usar_llm"])
    cfg["simulacion_correo"] = st.toggle("Modo simulación de correo (no envía correos reales)",
                                         value=cfg["simulacion_correo"])
    cfg["operador"] = st.text_input("Nombre del operador", value=cfg["operador"]).strip() or "operador"
    cfg["umbral_critico"] = st.slider("Umbral de riesgo crítico (puntaje 1-25)", 5, 25, cfg["umbral_critico"])
    h1, h2 = st.columns(2)
    cfg["h_ini"] = h1.time_input("Horario permitido para peligrosos: inicio", value=cfg["h_ini"])
    cfg["h_fin"] = h2.time_input("Horario permitido para peligrosos: fin", value=cfg["h_fin"])
    st.success("Los cambios se aplican de inmediato en esta sesión.")


PREGUNTAS_SUGERIDAS = ["¿Por qué CAM-102 fue enviado a inspección?", "¿Qué decisión se tomó con CAM-103?",
                       "¿Qué incidentes hay registrados?", "¿Cuáles son los riesgos más altos?"]


@_segura
def pagina_asistente() -> None:
    cfg = st.session_state.cfg
    chat = st.session_state.setdefault("chat", [])
    st.caption("Responde SOLO con datos de MongoDB y cita el registro de origen. "
               "Si no hay datos, dice «no tengo información».")
    cols = st.columns(len(PREGUNTAS_SUGERIDAS))
    for col, sugerida in zip(cols, PREGUNTAS_SUGERIDAS):
        if col.button(sugerida, key=f"sug_{sugerida}", use_container_width=True):
            st.session_state.chat_pendiente = sugerida

    for m in chat:
        with st.chat_message(m["rol"], avatar=":material/smart_toy:" if m["rol"] == "assistant" else ":material/person:"):
            st.markdown(m["texto"])
            if m.get("fuentes"):
                with st.expander(f"Fuentes consultadas ({len(m['fuentes'])})"):
                    for f in m["fuentes"]:
                        st.caption(f"[{f['n']}] {f['fuente']}")
            if m.get("modo") in ("sin_llm", "sin_datos", "llm_sin_info"):
                st.caption({"sin_llm": "Resumen directo de los registros (sin LLM)",
                            "sin_datos": "Sin registros relacionados en MongoDB",
                            "llm_sin_info": "El modelo indicó que no tiene información suficiente"}[m["modo"]])

    entrada = st.chat_input("Pregunta sobre camiones, accesos, incidentes o riesgos...")
    pregunta = (st.session_state.pop("chat_pendiente", None) or entrada or "").strip()
    if entrada is not None and len(pregunta) < 3:
        st.warning("Escribe una pregunta de al menos 3 caracteres.")
    elif pregunta:
        historial = [{"rol": m["rol"], "texto": m["texto"]} for m in chat]
        with st.spinner("Consultando MongoDB y al modelo..."):
            r = responder_pregunta(pregunta, historial, cfg["modelo"], guardar=True)
        chat += [{"rol": "user", "texto": pregunta},
                 {"rol": "assistant", "texto": r["respuesta"], "fuentes": r["fuentes"], "modo": r["modo"]}]
        st.rerun()
    if chat and st.button("Limpiar conversación", icon=":material/delete:"):
        chat.clear()
        st.rerun()


# ----------------------- CRUD de las 5 colecciones -----------------------
# Cada campo: (nombre, tipo, extra).  tipo: texto | area | bool | int (extra=(mín, máx)) | select (extra=opciones)
CRUD_ESQUEMAS: Dict[str, Dict[str, object]] = {
    "camiones": {"titulo": "Camiones", "repo": repo_camiones, "campos": [
        ("placa", "texto", None), ("camion_id", "texto", None), ("empresa", "texto", None),
        ("autorizacion", "bool", None), ("certificacion_conductor", "bool", None)]},
    "accesos": {"titulo": "Accesos (bitácora)", "repo": repo_accesos, "campos": [
        ("placa", "texto", None), ("camion_id", "texto", None), ("operador", "texto", None),
        ("P", "bool", None), ("Q", "bool", None), ("R", "bool", None), ("S", "bool", None),
        ("A", "bool", None), ("E", "bool", None)]},
    "incidentes": {"titulo": "Incidentes", "repo": repo_incidentes, "campos": [
        ("correo_original", "area", None), ("estado", "select", list(ESTADOS_INCIDENTE)),
        ("requiere_revision_humana", "bool", None)]},
    "riesgos_eticos": {"titulo": "Riesgos éticos", "repo": repo_riesgos, "campos": [
        ("modulo", "texto", None), ("descripcion", "area", None), ("categoria", "texto", None),
        ("mitigacion", "area", None), ("probabilidad", "int", (1, 5)), ("impacto", "int", (1, 5)),
        ("probabilidad_residual", "int", (1, 5)), ("impacto_residual", "int", (1, 5))]},
    "evaluaciones_llm": {"titulo": "Evaluaciones del LLM", "repo": repo_evaluaciones, "campos": [
        ("prompt", "area", None), ("respuesta", "area", None), ("modelo", "texto", None),
        ("latencia_ms", "int", (0, 600000)), ("coincidio_reglas", "bool", None)]},
}


def _valor_inicial(tipo: str, extra: object) -> object:
    """Valor por defecto de un campo al crear un registro."""
    return {"texto": "", "area": "", "bool": False}.get(tipo, extra[0] if tipo in ("select", "int") else "")


def _widget_campo(tipo: str, nombre: str, valor: object, extra: object, clave: str) -> object:
    if tipo == "bool":
        return st.toggle(nombre, value=bool(valor), key=clave)
    if tipo == "int":
        minimo, maximo = extra
        return st.number_input(nombre, min_value=minimo, max_value=maximo, step=1, key=clave,
                               value=max(minimo, min(maximo, int(valor if valor is not None else minimo))))
    if tipo == "select":
        return st.selectbox(nombre, extra, index=extra.index(valor) if valor in extra else 0, key=clave)
    if tipo == "area":
        return st.text_area(nombre, value=str(valor or ""), key=clave)
    return st.text_input(nombre, value=str(valor or ""), key=clave)


@_segura
def pagina_datos() -> None:
    import pandas as pd
    nombre = st.selectbox("Colección", list(CRUD_ESQUEMAS), format_func=lambda c: CRUD_ESQUEMAS[c]["titulo"])
    esquema = CRUD_ESQUEMAS[nombre]
    repo, campos = esquema["repo"], esquema["campos"]
    docs = repo.listar(limite=100)
    t_ver, t_crear, t_editar = st.tabs(["Consultar", "Crear", "Editar o eliminar"])

    with t_ver:
        if docs:
            st.dataframe(pd.DataFrame([{"id": d["_id"], **{c: d.get(c) for c, _, _ in campos}} for d in docs]),
                         use_container_width=True, hide_index=True)
            st.caption(f"{len(docs)} registro(s) mostrados (máximo 100).")
        else:
            st.info("Esta colección aún no tiene registros.")

    with t_crear:
        with st.form(f"crear_{nombre}", clear_on_submit=True):
            valores = {c: _widget_campo(t, c, _valor_inicial(t, e), e, f"nuevo_{nombre}_{c}") for c, t, e in campos}
            enviar = st.form_submit_button("Crear registro", type="primary", icon=":material/add:")
        if enviar:
            repo.crear(valores)
            st.toast("Registro creado.", icon=":material/check_circle:")
            st.rerun()

    with t_editar:
        if not docs:
            st.info("No hay registros para editar.")
            return
        etiquetas = {d["_id"]: " · ".join(str(d.get(c)) for c, _, _ in campos[:2]) + f"  (…{d['_id'][-5:]})"
                     for d in docs}
        elegido = st.selectbox("Registro", list(etiquetas), format_func=etiquetas.get, key=f"sel_{nombre}")
        actual = next(d for d in docs if d["_id"] == elegido)
        with st.form(f"editar_{nombre}_{elegido}"):
            nuevos = {c: _widget_campo(t, c, actual.get(c), e, f"ed_{nombre}_{elegido}_{c}") for c, t, e in campos}
            guardar = st.form_submit_button("Guardar cambios", type="primary", icon=":material/save:")
        if guardar:
            repo.actualizar(elegido, nuevos)
            st.toast("Registro actualizado.", icon=":material/check_circle:")
            st.rerun()
        confirmar = st.checkbox("Confirmo que quiero eliminar este registro", key=f"conf_{nombre}_{elegido}")
        if st.button("Eliminar registro", icon=":material/delete:", disabled=not confirmar):
            repo.eliminar(elegido)
            st.toast("Registro eliminado.", icon=":material/check_circle:")
            st.rerun()


PAGINAS: Dict[str, Callable] = {
    "Panel de control": pagina_panel, "Control de acceso": pagina_acceso,
    "Simulador lógico": pagina_simulador, "Bandeja de incidentes": pagina_incidentes,
    "Asistente": pagina_asistente, "Administrar datos": pagina_datos,
    "Configuración": pagina_config,
}


def app_streamlit() -> None:
    """Punto de entrada de la interfaz gráfica."""
    global HORARIO_PELIGROSOS
    st.set_page_config(page_title="LogiSmart", page_icon="🚚", layout="wide")
    st.markdown(ESTILO_GUI, unsafe_allow_html=True)
    st.session_state.setdefault("cfg", {
        "modelo": OLLAMA_MODEL, "usar_llm": True, "simulacion_correo": True, "operador": "operador",
        "umbral_critico": UMBRAL_RIESGO_CRITICO, "h_ini": HORARIO_PELIGROSOS[0], "h_fin": HORARIO_PELIGROSOS[1]})
    st.session_state.setdefault("pagina", "Panel de control")
    cfg = st.session_state.cfg
    HORARIO_PELIGROSOS = (cfg["h_ini"], cfg["h_fin"])

    with st.sidebar:
        st.markdown('<div class="marca"><div class="logo"><span class="ms">local_shipping</span></div>'
                    '<div><b>LogiSmart</b><span>Centro de control</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec">MENÚ</div>', unsafe_allow_html=True)
        for nombre, icono in ICONOS_PAGINA.items():
            activa = st.session_state.pagina == nombre
            if st.button(nombre, icon=icono, key=f"nav_{nombre}", use_container_width=True,
                         type="primary" if activa else "secondary"):
                st.session_state.pagina = nombre
                st.rerun()

    conex = _estado_conexiones()
    pills = "".join(f'<span class="pill"><i class="dot{"" if ok else " off"}"></i>{nombre}</span>'
                    for nombre, ok in (("MongoDB", conex["mongo"]), ("Ollama", conex["ollama"])))
    st.markdown(f'<div class="cab"><div><h2>{st.session_state.pagina}</h2>'
                f'<p>Control de acceso inteligente de camiones</p></div><div>{pills}</div></div>',
                unsafe_allow_html=True)
    PAGINAS[st.session_state.pagina]()


def _en_streamlit() -> bool:
    """True si el archivo se está ejecutando con 'streamlit run'."""
    if not STREAMLIT_OK:
        return False
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


# =============================================================================
# PRUEBAS UNITARIAS
# =============================================================================
class TestSeccion2Logica(unittest.TestCase):
    """Verifica el motor de reglas contra una tabla de verdad escrita A MANO
    (independiente de la implementación) y contra propiedades lógicas."""

    # Tabla esperada (P,Q,R,S) -> (A,E). Orden V→F igual que la tabla impresa.
    ESPERADO = {
        (1, 1, 1, 1): (0, 1), (1, 1, 1, 0): (0, 1), (1, 1, 0, 1): (0, 1), (1, 1, 0, 0): (0, 1),
        (1, 0, 1, 1): (1, 1), (1, 0, 1, 0): (0, 1), (1, 0, 0, 1): (1, 0), (1, 0, 0, 0): (0, 0),
        (0, 1, 1, 1): (0, 0), (0, 1, 1, 0): (0, 0), (0, 1, 0, 1): (0, 0), (0, 1, 0, 0): (0, 0),
        (0, 0, 1, 1): (0, 0), (0, 0, 1, 0): (0, 0), (0, 0, 0, 1): (0, 0), (0, 0, 0, 0): (0, 0),
    }

    def test_tabla_completa(self):
        for (p, q, r, s), (a, e) in self.ESPERADO.items():
            res = evaluar_camion(bool(p), bool(q), bool(r), bool(s))
            self.assertEqual(res["acceso_estandar"], bool(a), f"A mal en {(p, q, r, s)}")
            self.assertEqual(res["inspeccion_especial"], bool(e), f"E mal en {(p, q, r, s)}")

    def test_sin_autorizacion_nunca_pasa(self):
        for q, r, s in itertools.product([True, False], repeat=3):
            res = evaluar_camion(False, q, r, s)
            self.assertFalse(res["acceso_estandar"])
            self.assertFalse(res["inspeccion_especial"])

    def test_sobrepeso_bloquea_acceso_estandar(self):
        for r, s in itertools.product([True, False], repeat=2):
            self.assertFalse(evaluar_camion(True, True, r, s)["acceso_estandar"])

    def test_caso_ambas_reglas_verdaderas(self):
        self.assertEqual(evaluar_camion(True, False, True, True),
                         {"acceso_estandar": True, "inspeccion_especial": True})

    def test_tipo_invalido(self):
        with self.assertRaises(TypeError):
            evaluar_camion(1, False, False, True)   # 1 no es bool estricto

    def test_tabla_verdad_tiene_16_filas(self):
        self.assertEqual(len(generar_tabla_verdad()), 16)


class TestSeccion3Incidentes(unittest.TestCase):
    """Verifica clasificación, extracción y que la salida sea JSON estricto."""

    CORREO = dict(
        remitente="operador@planta.example",
        asunto="URGENTE: derrame en andén 3",
        cuerpo="El camión CAM-102 con placas ABC-123-D presenta fuga de químico inflamable. "
               "La báscula marcó 48.5 toneladas.",
    )

    def test_salida_es_json_valido_y_solo_json(self):
        salida = procesar_incidente(**self.CORREO, simulacion=True)
        datos = json.loads(salida)          # Si hubiera texto extra, esto fallaría
        self.assertIsInstance(datos, dict)
        self.assertTrue(salida.lstrip().startswith("{") and salida.rstrip().endswith("}"))

    def test_clasificacion_y_extraccion(self):
        datos = json.loads(procesar_incidente(**self.CORREO, simulacion=True))
        self.assertEqual(datos["clasificacion"]["categoria"], "materiales_peligrosos")
        self.assertEqual(datos["clasificacion"]["prioridad"], "critica")  # ya era crítica, no excede
        self.assertEqual(datos["datos_extraidos"]["placa"], "ABC-123-D")
        self.assertEqual(datos["datos_extraidos"]["camion_id"], "CAM-102")
        self.assertEqual(datos["datos_extraidos"]["peso_reportado_kg"], 48500.0)
        self.assertEqual(datos["datos_extraidos"]["ubicacion"], "andén 3")
        self.assertTrue(datos["correo_soporte"]["enviado"])

    def test_campos_faltantes_son_null(self):
        datos = json.loads(procesar_incidente("a@b.c", "Consulta", "Hola, tengo una duda general.",
                                              simulacion=True))
        self.assertEqual(datos["clasificacion"]["categoria"], "otro")
        self.assertIsNone(datos["datos_extraidos"]["placa"])
        self.assertIsNone(datos["datos_extraidos"]["peso_reportado_kg"])

    def test_urgencia_escala_prioridad(self):
        normal = clasificar_incidente("Pantalla lenta", "El sistema carga lento")
        urgente = clasificar_incidente("Pantalla lenta urgente", "El sistema carga lento")
        self.assertEqual(normal["prioridad"], "baja")
        self.assertEqual(urgente["prioridad"], "media")

    def test_fallo_smtp_no_rompe_json(self):
        # Sin variables de entorno SMTP, el envío real falla pero el JSON sigue siendo válido
        for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
            os.environ.pop(var, None)
        datos = json.loads(procesar_incidente(**self.CORREO, simulacion=False))
        self.assertFalse(datos["correo_soporte"]["enviado"])
        self.assertIsNotNone(datos["correo_soporte"]["error"])


class TestSeccion4Riesgos(unittest.TestCase):
    """Verifica el registro, validaciones, cálculos y exportación de la matriz."""

    def setUp(self):
        self.ev = EvaluadorRiesgosIA("LogiSmart")
        self.ev.registrar_modulo("Cámara de Detección de Somnolencia", "Visión por computadora")
        self.ev.registrar_modulo("Lector de Placas")

    def test_puntaje_y_nivel(self):
        r = self.ev.registrar_riesgo("Cámara de Detección de Somnolencia",
                                     "Sesgo en visión nocturna", "sesgo", 4, 5)
        self.assertEqual(r.puntaje, 20)
        self.assertEqual(r.nivel, "crítico")

    def test_niveles_limite(self):
        casos = {(1, 4): "bajo", (1, 5): "medio", (2, 5): "alto", (4, 4): "alto", (5, 4): "crítico"}
        for (p, i), esperado in casos.items():
            self.assertEqual(RiesgoEtico("x", "otro", p, i).nivel, esperado, f"{(p, i)}")

    def test_validaciones(self):
        with self.assertRaises(KeyError):
            self.ev.registrar_riesgo("No existe", "x", "sesgo", 1, 1)
        with self.assertRaises(ValueError):
            self.ev.registrar_riesgo("Lector de Placas", "x", "sesgo", 6, 1)
        with self.assertRaises(ValueError):
            self.ev.registrar_riesgo("Lector de Placas", "x", "inventada", 1, 1)
        with self.assertRaises(ValueError):
            self.ev.registrar_modulo("Lector de Placas")     # duplicado
        with self.assertRaises(ValueError):
            self.ev.registrar_modulo("   ")                  # vacío

    def test_resumen_y_orden(self):
        self.ev.registrar_riesgo("Lector de Placas", "Fuga de datos de placas", "privacidad", 2, 3)
        self.ev.registrar_riesgo("Cámara de Detección de Somnolencia",
                                 "Vigilancia excesiva del conductor", "privacidad", 4, 4)
        res = self.ev.resumen()
        self.assertEqual(res["total_riesgos"], 2)
        self.assertEqual(res["riesgos_por_categoria"], {"privacidad": 2})
        self.assertEqual(self.ev.todos_los_riesgos()[0][1].puntaje, 16)   # el mayor primero

    def test_modulo_sin_riesgos_se_reporta(self):
        self.ev.registrar_riesgo("Lector de Placas", "x", "otro", 1, 1)
        self.assertIn("Cámara de Detección de Somnolencia", self.ev.resumen()["modulos_sin_evaluar"])

    def test_exportar_json_valido(self):
        self.ev.registrar_riesgo("Lector de Placas", "Error de lectura", "seguridad", 3, 3, "Revisión manual")
        datos = json.loads(self.ev.exportar_json())
        self.assertEqual(datos["resumen"]["total_riesgos"], 1)
        self.assertEqual(datos["modulos"][1]["riesgos"][0]["nivel"], "medio")

    def test_reporte_texto_contiene_datos(self):
        self.ev.registrar_riesgo("Lector de Placas", "Error de lectura", "seguridad", 3, 3, "Revisión manual")
        texto = self.ev.reporte_texto()
        self.assertIn("Error de lectura", texto)
        self.assertIn("Revisión manual", texto)


class TestSeccion3BHibrido(unittest.TestCase):
    """Pruebas del clasificador híbrido SIN necesitar Ollama (se simula con chat_fn)."""

    JSON_OK = ('{"categoria": "somnolencia_conductor", "prioridad": "critica", '
               '"entidades": {"placa": null, "camion_id": "CAM-33", "peso_reportado_kg": null, '
               '"ubicacion": null}, "resumen": "Conductor con somnolencia."}')

    def test_validacion_acepta_json_correcto_y_markdown(self):
        for texto in (self.JSON_OK, "```json\n" + self.JSON_OK + "\n```"):
            valido, datos, _ = validar_respuesta_llm(texto)
            self.assertTrue(valido)
            self.assertEqual(datos["categoria"], "somnolencia_conductor")

    def test_validacion_rechaza_respuestas_malas(self):
        malas = [
            "no es json",
            '{"categoria": "inventada", "prioridad": "alta", "entidades": {}, "resumen": "xxx"}',
            '{"categoria": "otro", "prioridad": "urgentisima", "entidades": {}, "resumen": "xxx"}',
            '{"categoria": "otro", "prioridad": "baja", "entidades": {}}',                    # falta resumen
            '{"categoria": "otro", "prioridad": "baja", "entidades": {}, "resumen": "xxx", "extra": 1}',
        ]
        for texto in malas:
            self.assertFalse(validar_respuesta_llm(texto)[0], texto)

    def test_prioridad_con_acento_se_normaliza(self):
        texto = self.JSON_OK.replace('"critica"', '"Crítica"')
        self.assertEqual(validar_respuesta_llm(texto)[1]["prioridad"], "critica")

    def test_reintento_exitoso(self):
        respuestas = iter(["esto no es json", self.JSON_OK])
        info = llamar_llm("correo", chat_fn=lambda m, msgs, e: next(respuestas))
        self.assertTrue(info["ok"])
        self.assertEqual(info["intentos"], 2)

    def test_json_invalido_cae_a_reglas(self):
        r = clasificar_hibrido("Fuga de químico inflamable en el CAM-101",
                               chat_fn=lambda m, msgs, e: "basura")
        self.assertEqual(r["fuente"], "reglas")
        self.assertEqual(r["categoria"], "materiales_peligrosos")
        self.assertFalse(r["detalle"]["llm_valido"])
        self.assertEqual(r["detalle"]["llm_intentos"], MAX_INTENTOS_LLM)

    def test_ollama_apagado_cae_a_reglas(self):
        def caido(modelo, mensajes, esquema):
            raise ConnectionError("Ollama no responde")
        r = clasificar_hibrido("El sistema está caído", chat_fn=caido)
        self.assertEqual(r["fuente"], "reglas")
        self.assertEqual(r["detalle"]["llm_intentos"], 1)
        self.assertIn("ConnectionError", r["detalle"]["llm_error"])

    def test_fusion_seguridad_primero(self):
        reglas = {"categoria": "falla_software", "prioridad": "baja", "entidades": {}, "resumen": "r"}
        llm = {"categoria": "materiales_peligrosos", "prioridad": "critica", "entidades": {}, "resumen": "l"}
        f = fusionar_clasificaciones(reglas, llm, "texto")
        self.assertEqual((f["categoria"], f["prioridad"]), ("materiales_peligrosos", "critica"))
        self.assertTrue(f["requiere_revision_humana"])
        self.assertEqual(f["fuente"], "hibrido_llm")

    def test_fusion_gana_la_prioridad_de_las_reglas(self):
        reglas = {"categoria": "acceso_no_autorizado", "prioridad": "alta", "entidades": {}, "resumen": "r"}
        llm = {"categoria": "otro", "prioridad": "baja", "entidades": {}, "resumen": "l"}
        f = fusionar_clasificaciones(reglas, llm, "texto")
        self.assertEqual((f["categoria"], f["prioridad"]), ("acceso_no_autorizado", "alta"))
        self.assertTrue(f["requiere_revision_humana"])

    def test_fusion_empate_de_prioridad_conserva_reglas_y_pide_revision(self):
        reglas = {"categoria": "falla_hardware", "prioridad": "media", "entidades": {}, "resumen": "r"}
        llm = {"categoria": "sobrepeso", "prioridad": "media", "entidades": {}, "resumen": "l"}
        f = fusionar_clasificaciones(reglas, llm, "texto")
        self.assertEqual(f["categoria"], "falla_hardware")
        self.assertTrue(f["requiere_revision_humana"])

    def test_fusion_consenso_no_pide_revision(self):
        base = {"categoria": "sobrepeso", "prioridad": "media", "entidades": {}, "resumen": "x"}
        f = fusionar_clasificaciones(base, dict(base), "texto")
        self.assertEqual(f["fuente"], "consenso")
        self.assertFalse(f["requiere_revision_humana"])

    def test_entidades_inventadas_se_descartan(self):
        ent_reglas = {"placa": None, "camion_id": None, "peso_reportado_kg": None, "ubicacion": None}
        ent_llm = {"placa": "ZZZ-999-Z", "camion_id": "CAM-5", "ubicacion": None}
        r = combinar_entidades(ent_reglas, ent_llm, "El camión CAM-5 está en la entrada")
        self.assertIsNone(r["placa"])                      # inventada: no aparece en el correo
        self.assertEqual(r["camion_id"], "CAM-5")          # sí aparece

    def test_correos_etiquetados_cumplen_el_minimo(self):
        datos = cargar_correos_etiquetados()
        self.assertGreaterEqual(len(datos), 30)
        self.assertGreaterEqual(sum(1 for d in datos if d["informal"]), 10)
        self.assertEqual(len({d["categoria"] for d in datos}), len(CATEGORIAS_VALIDAS))

    def test_experimento_solo_reglas(self):
        res = evaluar_clasificadores(usar_llm=False)
        m = res["clasificadores"]["reglas"]
        self.assertEqual(list(res["clasificadores"]), ["reglas"])
        self.assertTrue(0 <= m["exactitud_categoria"] <= 1)
        self.assertEqual(sum(sum(f.values()) for f in m["matriz_confusion"].values()), res["n_correos"])

    def test_experimento_con_llm_simulado(self):
        falso = '{"categoria": "otro", "prioridad": "baja", "entidades": {}, "resumen": "nada"}'
        res = evaluar_clasificadores(chat_fn=lambda m, msgs, e: falso)
        self.assertEqual(set(res["clasificadores"]), {"reglas", "llm", "hibrido"})
        self.assertEqual(res["llm_invalidos"], 0)
        for m in res["clasificadores"].values():
            self.assertIn("p95", m["latencia_ms"])
        # El híbrido nunca baja la prioridad frente a las reglas (seguridad primero).
        for f in res["detalle"]:
            self.assertGreaterEqual(ORDEN_PRIORIDAD.index(f["hibrido_prioridad"]),
                                    ORDEN_PRIORIDAD.index(f["reglas_prioridad"]))


class TestSeccion5Interfaz(unittest.TestCase):
    """Pruebas de los ayudantes de la interfaz (no requieren abrir Streamlit)."""

    def test_semaforo_enciende_solo_el_color_pedido(self):
        html = _semaforo_html("amarillo")
        self.assertEqual(html.count(" on"), 1)
        self.assertIn("luz amarillo on", html)

    def test_rango_de_fechas(self):
        from datetime import date
        self.assertIsNone(_inicio_dia(None))
        self.assertLess(_inicio_dia(date(2026, 10, 3)), _fin_dia(date(2026, 10, 3)))

    def test_todas_las_paginas_tienen_icono_y_funcion(self):
        self.assertEqual(set(PAGINAS), set(ICONOS_PAGINA))


class TestSeccion4BAsistente(unittest.TestCase):
    """Pruebas del asistente RAG SIN MongoDB ni Ollama (se simulan con recuperador y chat_fn)."""

    REGISTROS = [{"coleccion": "accesos", "id": "abc123", "datos": {
        "fecha": "2026-10-03 10:00", "camion_id": "CAM-102", "placa": "ABC-102-B", "resultado": "INSPECCION_ESPECIAL",
        "regla_ganadora": "E", "explicacion": ["Causa de la decisión: P=V, R=F, Q=V"]}}]

    def recuperador(self, pregunta):
        return self.REGISTROS

    def test_extrae_referencias(self):
        ref = extraer_referencias("¿Por qué cam-102 y ABC-123-D fueron a inspección? ¿hay riesgos?")
        self.assertEqual(ref["camiones"], ["CAM-102"])
        self.assertEqual(ref["placas"], ["ABC-123-D"])
        self.assertIn("accesos", ref["colecciones"])
        self.assertIn("riesgos_eticos", ref["colecciones"])

    def test_sin_pistas_no_consulta_la_base(self):
        self.assertEqual(recuperar_contexto("hola, ¿cómo estás?"), [])   # si consultara, fallaría sin Mongo

    def test_sin_datos_responde_no_tengo_informacion_sin_llamar_al_llm(self):
        def prohibido(modelo, mensajes):
            raise AssertionError("no debe llamar al LLM cuando no hay datos")
        r = responder_pregunta("¿Qué pasó con CAM-999?", recuperador=lambda p: [], chat_fn=prohibido)
        self.assertEqual(r["modo"], "sin_datos")
        self.assertEqual(r["respuesta"], MENSAJE_SIN_INFO)
        self.assertEqual(r["fuentes"], [])

    def test_respuesta_con_citas_validas(self):
        r = responder_pregunta("¿Por qué CAM-102 fue a inspección?", recuperador=self.recuperador,
                               chat_fn=lambda m, msgs: "Fue enviado a inspección por la regla E [1].")
        self.assertEqual(r["modo"], "llm")
        self.assertEqual(r["fuentes"][0]["fuente"], "accesos/abc123")
        self.assertIn("accesos/abc123", formatear_respuesta(r))

    def test_respuesta_sin_citas_se_reemplaza_por_resumen_directo(self):
        r = responder_pregunta("pregunta CAM-102", recuperador=self.recuperador,
                               chat_fn=lambda m, msgs: "Porque el camión era sospechoso.")
        self.assertEqual(r["modo"], "sin_llm")
        self.assertIn("INSPECCION_ESPECIAL", r["respuesta"])
        self.assertNotIn("sospechoso", r["respuesta"])

    def test_cita_a_un_registro_inexistente_se_rechaza(self):
        r = responder_pregunta("pregunta CAM-102", recuperador=self.recuperador,
                               chat_fn=lambda m, msgs: "Lo dice el registro [7].")
        self.assertEqual(r["modo"], "sin_llm")

    def test_ollama_apagado_muestra_los_registros(self):
        def caido(modelo, mensajes):
            raise ConnectionError("sin Ollama")
        r = responder_pregunta("pregunta CAM-102", recuperador=self.recuperador, chat_fn=caido)
        self.assertEqual(r["modo"], "sin_llm")
        self.assertIn("ConnectionError", r["error"])
        self.assertIn("CAM-102", r["respuesta"])

    def test_el_modelo_puede_decir_que_no_sabe(self):
        r = responder_pregunta("pregunta CAM-102", recuperador=self.recuperador,
                               chat_fn=lambda m, msgs: RESPUESTA_LLM_SIN_INFO)
        self.assertEqual(r["modo"], "llm_sin_info")

    def test_prompt_numera_registros_e_incluye_historial(self):
        prompt = construir_prompt_rag("¿por qué?", self.REGISTROS, [{"rol": "user", "texto": "hola CAM-102"}])
        self.assertIn("[1] (accesos)", prompt)
        self.assertIn("Cita el registro de origen", prompt)
        self.assertIn("hola CAM-102", prompt)

    def test_compactar_accesos_conserva_solo_lo_util(self):
        doc = {"_id": "x1", "P": True, "Q": False, "A": True, "E": False, "resultado": "ACCESO_ESTANDAR",
               "explicacion": ["Premisas: ...", "Regla A ...", "Causa de la decisión: P=V", "Resultado final: X"],
               "operador": "secreto"}
        datos = compactar_registro("accesos", doc)["datos"]
        self.assertEqual(len(datos["explicacion"]), 2)
        self.assertNotIn("operador", datos)

    def test_datos_demo_cuentan_la_historia_esperada(self):
        esperado = ["INSPECCION_ESPECIAL", "INSPECCION_ESPECIAL", "BLOQUEADO_SOMNOLENCIA",
                    "ACCESO_ESTANDAR", "DENEGADO", "REPROGRAMAR"]
        obtenido = [evaluar_decision(*[bool(x) for x in fila[3:]])["resultado"] for fila in _DEMO_CAMIONES]
        self.assertEqual(obtenido, esperado)
        self.assertEqual(len(_DEMO_RIESGOS), 4)


class TestSeccion5Crud(unittest.TestCase):
    """Los formularios del CRUD deben producir datos que las validaciones de MongoDB aceptan."""

    def test_cinco_colecciones(self):
        self.assertEqual(set(CRUD_ESQUEMAS), {"camiones", "accesos", "incidentes", "riesgos_eticos",
                                              "evaluaciones_llm"})

    def test_formularios_coinciden_con_las_validaciones(self):
        for nombre, esquema in CRUD_ESQUEMAS.items():
            valores = {c: "x" if t in ("texto", "area") else _valor_inicial(t, e)
                       for c, t, e in esquema["campos"]}
            self.assertTrue(esquema["repo"]._preparar(valores), nombre)   # no toca MongoDB


class TestSeccion0Mongo(unittest.TestCase):
    """Valida las reglas de la capa de datos SIN necesitar conexión a MongoDB."""

    def test_camion_placa_obligatoria(self):
        with self.assertRaises(ValueError):
            repo_camiones._validar({"placa": "", "camion_id": "X"})

    def test_camion_normaliza_datos(self):
        d = repo_camiones._validar({"placa": " abc-123-d ", "camion_id": "cam-1"})
        self.assertEqual((d["placa"], d["camion_id"]), ("ABC-123-D", "CAM-1"))
        self.assertFalse(d["autorizacion"])

    def test_incidente_validaciones(self):
        with self.assertRaises(ValueError):
            repo_incidentes._validar({"correo_original": "   "})
        with self.assertRaises(ValueError):
            repo_incidentes._validar({"correo_original": "hola", "estado": "roto"})
        d = repo_incidentes._validar({"correo_original": "hola"})
        self.assertEqual(d["estado"], "nuevo")

    def test_acceso_requiere_todas_las_premisas(self):
        with self.assertRaises(ValueError):
            repo_accesos._validar({"P": 1, "Q": 0, "R": 0})

    def test_riesgo_puntaje_residual_y_niveles(self):
        d = repo_riesgos._preparar({"modulo": "m", "descripcion": "d", "categoria": "c",
                                    "mitigacion": "x", "probabilidad": 4, "impacto": 4,
                                    "probabilidad_residual": 2, "impacto_residual": 3})
        self.assertEqual((d["puntaje_inicial"], d["nivel_inicial"]), (16, "Alto"))
        self.assertEqual((d["puntaje_residual"], d["nivel_residual"]), (6, "Medio"))

    def test_riesgo_fuera_de_rango(self):
        with self.assertRaises(ValueError):
            repo_riesgos._validar({"probabilidad": 9}, parcial=True)

    def test_uri_codifica_caracteres_especiales(self):
        uri = construir_uri("usuario", "p@ss/word", "cluster.example.net")
        self.assertIn("p%40ss%2Fword", uri)
        self.assertTrue(uri.startswith("mongodb+srv://usuario:"))


class TestSeccion2BAmpliada(unittest.TestCase):
    """Verifica las reglas nuevas, la prioridad, la explicación y el análisis."""

    def test_conserva_A_y_E_originales(self):
        for P, Q, R, S in itertools.product([True, False], repeat=4):
            base = evaluar_camion(P, Q, R, S)
            d = evaluar_decision(P, Q, R, S)
            self.assertEqual(d["A"], base["acceso_estandar"])
            self.assertEqual(d["E"], base["inspeccion_especial"])

    def test_tabla_regla_horario_escrita_a_mano(self):
        # (P, R, H) -> RH = P ∧ R ∧ ¬H : solo (1,1,0) es verdadera
        esperado = {(1, 1, 1): 0, (1, 1, 0): 1, (1, 0, 1): 0, (1, 0, 0): 0,
                    (0, 1, 1): 0, (0, 1, 0): 0, (0, 0, 1): 0, (0, 0, 0): 0}
        for f in generar_tabla_reglas_nuevas()["RH"]:
            self.assertEqual(f["RH"], bool(esperado[(int(f["P"]), int(f["R"]), int(f["H"]))]))

    def test_tabla_regla_somnolencia_escrita_a_mano(self):
        esperado = {(1, 1): 1, (1, 0): 0, (0, 1): 0, (0, 0): 0}
        for f in generar_tabla_reglas_nuevas()["B"]:
            self.assertEqual(f["B"], bool(esperado[(int(f["P"]), int(f["D"]))]))

    def test_decisiones_clave(self):
        # (P, Q, R, S, H, D) -> resultado esperado
        casos = [
            ((1, 0, 0, 1, 1, 0), "ACCESO_ESTANDAR"),
            ((1, 0, 1, 1, 1, 0), "INSPECCION_ESPECIAL"),   # A y E verdaderas: gana la inspección
            ((1, 1, 0, 1, 1, 0), "INSPECCION_ESPECIAL"),   # exceso de peso
            ((1, 0, 1, 1, 0, 0), "REPROGRAMAR"),           # peligrosa fuera de horario
            ((1, 0, 0, 1, 1, 1), "BLOQUEADO_SOMNOLENCIA"),
            ((0, 0, 0, 1, 1, 0), "DENEGADO"),              # sin autorización
            ((1, 0, 0, 0, 1, 0), "DENEGADO"),              # sin certificación
        ]
        for premisas, esperado in casos:
            res = evaluar_decision(*[bool(x) for x in premisas])
            self.assertEqual(res["resultado"], esperado, f"{premisas}")

    def test_seguridad_primero_en_conflicto(self):
        res = evaluar_decision(True, False, False, True, True, True)
        self.assertEqual(res["regla_ganadora"], "B")
        self.assertIn("A", res["reglas_activadas"])         # A también se activó, pero pierde

    def test_explicacion_paso_a_paso(self):
        res = evaluar_decision(True, False, True, True, False, False)
        self.assertGreaterEqual(len(res["explicacion"]), 6)
        self.assertTrue(any("RH" in paso for paso in res["explicacion"]))
        self.assertTrue(res["explicacion"][-1].startswith("Resultado final"))

    def test_tipo_invalido_en_premisas_nuevas(self):
        with self.assertRaises(TypeError):
            evaluar_decision(True, False, False, True, H=1)

    def test_tabla_decision_tiene_64_filas(self):
        self.assertEqual(len(generar_tabla_decision()), 64)

    def test_analisis_sin_contradicciones_ni_redundancias(self):
        a = analizar_reglas()
        self.assertEqual(a["sin_resolver"], 0)
        self.assertEqual(a["redundantes"], [])
        self.assertIn("A vs E", a["conflictos"])

    def test_detecta_regla_redundante(self):
        copia_de_A = Regla("A2", "copia de A", "P ∧ S ∧ ¬Q", REGLAS[-1].condicion,
                           "ACCESO_ESTANDAR", 4, ("P", "S", "Q"), "duplicada a propósito")
        self.assertIn("A2", analizar_reglas(REGLAS + [copia_de_A])["redundantes"])

    def test_horario(self):
        self.assertTrue(en_horario_permitido(dtime(12, 0)))
        self.assertFalse(en_horario_permitido(dtime(23, 30)))


# =============================================================================
# DEMOSTRACIÓN COMPLETA
# =============================================================================
def demo() -> None:
    """Ejecuta las secciones con datos de ejemplo."""
    # ---- Sección 1 ----
    imprimir_peas()

    # ---- Sección 2 ----
    imprimir_tablas_verdad()
    print("Ejemplos del motor de reglas:")
    casos = [
        ("Autorizado, conductor certificado, peso OK, sin peligrosos", (True, False, False, True)),
        ("Autorizado, peso excedido", (True, True, False, True)),
        ("Autorizado, peso OK, carga peligrosa", (True, False, True, True)),
        ("Sin autorización previa", (False, False, False, True)),
    ]
    for descripcion, (P, Q, R, S) in casos:
        res = evaluar_camion(P, Q, R, S)
        print(f"  {descripcion}\n     -> P={P} Q={Q} R={R} S={S} => "
              f"Acceso estándar={res['acceso_estandar']}, Inspección especial={res['inspeccion_especial']}")

    # ---- Sección 2B ----
    imprimir_tablas_reglas_nuevas()
    print("Decisión con explicación paso a paso (carga peligrosa fuera de horario):")
    dec = evaluar_decision(True, False, True, True, H=False, D=False)
    print(f"  Resultado: {dec['resultado']} (semáforo {dec['semaforo']})")
    for paso in dec["explicacion"]:
        print(f"   - {paso}")
    print()
    imprimir_analisis_reglas()

    # ---- Sección 3 ----
    print("\n" + "=" * 78)
    print("SECCIÓN 3 - CLASIFICADOR DE INCIDENTES (salida JSON estricta)")
    print("=" * 78)
    salida_json = procesar_incidente(
        remitente="operador.caseta@logismart.example",
        asunto="URGENTE: derrame en andén 3",
        cuerpo=("El camión CAM-102 con placas ABC-123-D presenta una fuga de químico inflamable. "
                "La báscula marcó 48.5 toneladas. Se requiere apoyo inmediato."),
        simulacion=True,     # Cambia a False y define SMTP_* para enviar de verdad
    )
    print(salida_json)

    # ---- Sección 3B ----
    print("\n" + "=" * 78)
    print("SECCIÓN 3B - CLASIFICADOR HÍBRIDO (reglas + LLM con respaldo)")
    print("=" * 78)
    ejemplo = "el compa de la pipa viene bien desvelado, bosteza y se le cierran los ojos"
    hib = clasificar_hibrido(ejemplo)
    print(f"  Correo:   {ejemplo}")
    print(f"  Resultado: {hib['categoria']} / {hib['prioridad']}  (fuente: {hib['fuente']}, "
          f"revisión humana: {hib['requiere_revision_humana']})")
    print(f"  Motivo:   {hib['motivo']}")
    print("  (Para el experimento completo con 36 correos: python logiuncodigo.py --evaluar)\n")

    # ---- Sección 4 ----
    print("\n" + "=" * 78)
    print("SECCIÓN 4 - EVALUACIÓN ÉTICA")
    print("=" * 78)
    ev = EvaluadorRiesgosIA("LogiSmart Python Suite")
    cam = "Cámara de Detección de Somnolencia"
    lpr = "Lector de Placas (LPR)"
    clas = "Clasificador de Incidentes"
    ev.registrar_modulo(cam, "Visión por computadora que monitorea al conductor")
    ev.registrar_modulo(lpr, "Reconoce placas para validar autorización previa")
    ev.registrar_modulo(clas, "Clasifica correos de soporte y extrae datos")
    ev.registrar_riesgo(cam, "Sesgo en visión nocturna (falsos positivos por tono de piel / poca luz)",
                        "sesgo", 4, 4, "Entrenar y auditar con datos nocturnos y diversos; umbral ajustable")
    ev.registrar_riesgo(cam, "Violación de privacidad (video continuo del conductor)",
                        "privacidad", 4, 5, "Procesar en el borde, no almacenar video, aviso y consentimiento")
    ev.registrar_riesgo(lpr, "Lectura errónea que niega acceso injustificadamente",
                        "responsabilidad", 3, 3, "Revisión humana y canal de apelación")
    ev.registrar_riesgo(lpr, "Conservación indebida de datos de placas",
                        "privacidad", 2, 4, "Política de retención y cifrado")
    ev.registrar_riesgo(clas, "Priorizar mal un incidente crítico (falso negativo)",
                        "seguridad", 2, 5, "Palabras críticas siempre escalan; revisión humana de 'otro'")
    print(ev.reporte_texto())
    ev.exportar_texto("reporte_riesgos.txt")
    ev.exportar_json("reporte_riesgos.json")
    print("\n(Archivos generados: reporte_riesgos.txt y reporte_riesgos.json)")


if __name__ == "__main__":
    if _en_streamlit():
        app_streamlit()
    elif "--mongo" in sys.argv:
        probar_mongo()
    elif "--preguntar" in sys.argv:
        preguntar_en_consola(" ".join(sys.argv[sys.argv.index("--preguntar") + 1:]))
    elif "--demo-datos" in sys.argv:
        sembrar_datos_demo()
    elif "--limpiar-demo" in sys.argv:
        limpiar_datos_demo()
    elif "--evaluar" in sys.argv:
        ejecutar_experimento(usar_llm="--sin-llm" not in sys.argv, guardar="--guardar" in sys.argv)
    elif "--tests" in sys.argv:
        # Quita nuestro flag para que unittest no lo interprete como argumento suyo
        unittest.main(argv=[sys.argv[0], "-v"])
    else:
        demo()
        print("\n" + "=" * 78)
        print("PRUEBAS UNITARIAS")
        print("=" * 78)
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        unittest.TextTestRunner(verbosity=2).run(suite)